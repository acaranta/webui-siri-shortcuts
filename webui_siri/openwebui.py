from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import httpx

# Exceptions considered transient — safe to retry without side effects
_TRANSIENT_EXC = (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError)


class ChatAccessError(Exception):
    """Raised when a stored Open WebUI chat_id returns 401.

    This typically means the token was changed or the chat was created
    under a different account.
    """


@dataclass
class OpenWebUIConfig:
    base_url: str
    token: str
    folder: Optional[str]


_log = logging.getLogger(__name__)


class OpenWebUIClient:
    def __init__(self, config: OpenWebUIConfig, timeout: float = 60.0) -> None:
        self._config = config
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=self._config.base_url,
            headers={
                "Authorization": f"Bearer {self._config.token}",
                "Accept": "application/json",
            },
            timeout=timeout,
        )
        # resolved UUID for self._config.folder (looked up lazily by name)
        self._folder_id: Optional[str] = None

    async def close(self) -> None:
        await self._client.aclose()

    async def _with_retry(
        self,
        coro_factory,
        *,
        retries: int = 3,
        min_delay: float = 1.0,
        max_delay: float = 3.0,
    ) -> Any:
        """Execute coro_factory() with up to *retries* attempts on transient errors."""
        last_exc: Exception
        for attempt in range(retries):
            try:
                return await coro_factory()
            except _TRANSIENT_EXC as exc:
                last_exc = exc
                if attempt < retries - 1:
                    delay = random.uniform(min_delay, max_delay)
                    _log.warning(
                        "OpenWebUI request failed (attempt %d/%d, retrying in %.1fs): %s",
                        attempt + 1, retries, delay, exc,
                    )
                    await asyncio.sleep(delay)
        raise last_exc  # type: ignore[possibly-undefined]

    async def verify_access(self) -> None:
        model_paths = ("/api/models", "/api/v1/models", "/openai/models")
        response = None
        for path in model_paths:
            response = await self._client.get(path)
            if response.status_code not in (404, 405):
                break
        assert response is not None
        response.raise_for_status()
        self._raise_if_html(response)
        payload = self._safe_json(response)
        if payload is None:
            raise RuntimeError("Open WebUI returned empty response for models endpoint")

    def verify_access_sync(self) -> None:
        model_paths = ("/api/models", "/api/v1/models", "/openai/models")
        with httpx.Client(
            base_url=self._config.base_url,
            headers={
                "Authorization": f"Bearer {self._config.token}",
                "Accept": "application/json",
            },
            timeout=self._timeout,
        ) as client:
            response = None
            for path in model_paths:
                response = client.get(path)
                if response.status_code not in (404, 405):
                    break
            assert response is not None
            response.raise_for_status()
            self._raise_if_html(response)
            payload = self._safe_json(response)
            if payload is None:
                raise RuntimeError(
                    "Open WebUI returned empty response for models endpoint"
                )

    async def get_chat(self, chat_id: str) -> dict[str, Any]:
        response = await self._client.get(f"/api/v1/chats/{chat_id}")
        if response.status_code in (404, 405):
            response = await self._client.get(f"/api/chats/{chat_id}")
        response.raise_for_status()
        return self._safe_json_dict(response)

    async def create_chat(self, model: str, title: Optional[str] = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat": {"model": model, "models": [model]}}
        if title:
            payload["chat"]["title"] = title
        if self._config.folder:
            folder_id = await self._ensure_folder(self._config.folder)
            if folder_id:
                payload["folder_id"] = folder_id
        response = await self._client.post("/api/v1/chats/new", json=payload)
        if response.status_code in (404, 405):
            response = await self._client.post("/api/chats/new", json=payload)
        response.raise_for_status()
        return self._safe_json_dict(response)

    async def _ensure_folder(self, folder_name: str) -> Optional[str]:
        """Return the folder UUID for folder_name, creating it if it does not exist.

        The result is cached so the folder API is only called once per process.
        """
        if self._folder_id is not None:
            return self._folder_id
        try:
            response = await self._client.get("/api/v1/folders/")
            response.raise_for_status()
            folders = self._safe_json(response)
            if isinstance(folders, list):
                for f in folders:
                    if isinstance(f, dict) and f.get("name") == folder_name:
                        self._folder_id = f["id"]
                        _log.info("resolved folder %r -> %s", folder_name, self._folder_id)
                        return self._folder_id
        except Exception as exc:
            _log.warning("failed to list folders; folder assignment skipped: %s", exc)
            return None
        # Folder not found — create it
        try:
            response = await self._client.post(
                "/api/v1/folders/", json={"name": folder_name}
            )
            response.raise_for_status()
            created = self._safe_json_dict(response)
            self._folder_id = created.get("id")
            _log.info("created folder %r -> %s", folder_name, self._folder_id)
            return self._folder_id
        except Exception:
            _log.warning("failed to create folder %r; folder assignment skipped", folder_name, exc_info=True)
            return None

    async def send_message(
        self,
        chat_id: str,
        model: str,
        content: str,
    ) -> dict[str, Any]:
        """Send a user message and return the LLM completion."""
        user_msg_id = str(uuid.uuid4())
        assistant_msg_id = str(uuid.uuid4())
        now = int(time.time())

        # 1. Fetch existing history BEFORE adding the new user message.
        prior_messages = await self._fetch_history_messages(chat_id)

        # 2. Persist the user message in the chat history before calling completions.
        await self._add_user_message_to_chat(
            chat_id=chat_id,
            user_msg_id=user_msg_id,
            content=content,
            timestamp=now,
            model=model,
        )

        # 3. Build the full messages array for the completions API.
        messages = prior_messages + [{"role": "user", "content": content}]
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "chat_id": chat_id,
            "id": assistant_msg_id,
            "parent_id": user_msg_id,
        }

        response = await self._client.post("/api/chat/completions", json=payload)
        if not response.is_success:
            _log.warning(
                "completions request failed (%s) — body: %s",
                response.status_code,
                response.text[:500],
            )
        response.raise_for_status()
        completion = self._safe_json(response)
        if not isinstance(completion, dict):
            completion = {}

        # 4. Persist the assistant reply into the history linked-list.
        assistant_content = self._extract_content_from_completion(completion)
        if assistant_content:
            await self._add_assistant_message_to_chat(
                chat_id=chat_id,
                assistant_msg_id=assistant_msg_id,
                user_msg_id=user_msg_id,
                content=assistant_content,
                model=model,
                timestamp=int(time.time()),
            )

        # 5. On the first turn, generate and persist a descriptive title.
        if not prior_messages and assistant_content:
            title_messages = messages + [{"role": "assistant", "content": assistant_content}]
            await self._generate_and_persist_title(chat_id, model, title_messages)

        # 6. Signal Open WebUI that generation is finished.
        await self._finalize_completion(
            chat_id=chat_id,
            model=model,
            assistant_msg_id=assistant_msg_id,
            messages=messages,
        )

        return {"completion": completion, "message_id": assistant_msg_id}

    async def _add_user_message_to_chat(
        self,
        chat_id: str,
        user_msg_id: str,
        content: str,
        timestamp: int,
        model: Optional[str] = None,
    ) -> None:
        """Insert the user message into the chat's history linked-list."""
        try:
            resp = await self._client.get(f"/api/v1/chats/{chat_id}")
            if resp.status_code in (404, 405):
                resp = await self._client.get(f"/api/chats/{chat_id}")
            resp.raise_for_status()
            data = self._safe_json_dict(resp)

            chat_obj = data.get("chat") if isinstance(data.get("chat"), dict) else data
            if not isinstance(chat_obj, dict):
                _log.warning("unexpected chat structure; skipping user message upsert")
                return

            if model:
                chat_obj["model"] = model
                chat_obj["models"] = [model]

            history = chat_obj.get("history")
            if not isinstance(history, dict):
                history = {"messages": {}, "currentId": None}

            msgs = history.get("messages")
            if not isinstance(msgs, dict):
                msgs = {}

            parent_id: Optional[str] = history.get("currentId")

            msgs[user_msg_id] = {
                "id": user_msg_id,
                "parentId": parent_id,
                "role": "user",
                "content": content,
                "timestamp": timestamp,
            }
            history["messages"] = msgs
            history["currentId"] = user_msg_id
            chat_obj["history"] = history

            update_body = {"chat": chat_obj}
            upd = await self._client.post(f"/api/v1/chats/{chat_id}", json=update_body)
            if upd.status_code in (404, 405):
                upd = await self._client.post(f"/api/chats/{chat_id}", json=update_body)
            upd.raise_for_status()
            _log.debug("upserted user message %s into chat %s", user_msg_id, chat_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                raise ChatAccessError(
                    f"chat {chat_id} is not accessible (401) — token may have changed"
                ) from exc
            _log.warning(
                "failed to upsert user message into chat history; "
                "the response will still be returned but may not appear in the UI: %s", exc,
            )
        except ChatAccessError:
            raise
        except Exception as exc:
            _log.warning(
                "failed to upsert user message into chat history; "
                "the response will still be returned but may not appear in the UI: %s", exc,
            )

    async def _fetch_history_messages(self, chat_id: str) -> list[dict]:
        """Return the current conversation history as a chronological messages list."""
        try:
            resp = await self._client.get(f"/api/v1/chats/{chat_id}")
            if resp.status_code in (404, 405):
                resp = await self._client.get(f"/api/chats/{chat_id}")
            resp.raise_for_status()
            data = self._safe_json_dict(resp)
            chat_obj = data.get("chat") if isinstance(data.get("chat"), dict) else data
            if not isinstance(chat_obj, dict):
                return []
            history = chat_obj.get("history")
            if not isinstance(history, dict):
                return []
            return self._build_messages_from_history(history)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                raise ChatAccessError(
                    f"chat {chat_id} is not accessible (401) — token may have changed"
                ) from exc
            _log.warning("failed to fetch history messages for chat %s: %s", chat_id, exc)
            return []
        except ChatAccessError:
            raise
        except Exception as exc:
            _log.warning("failed to fetch history messages for chat %s: %s", chat_id, exc)
            return []

    def _build_messages_from_history(self, history: dict) -> list[dict]:
        """Convert Open WebUI's dict-keyed linked-list history into a flat chronological list."""
        msgs: dict = history.get("messages") or {}
        current_id: Optional[str] = history.get("currentId")

        chain: list[dict] = []
        visited: set[str] = set()
        node_id = current_id
        while node_id and node_id not in visited:
            visited.add(node_id)
            node = msgs.get(node_id)
            if not isinstance(node, dict):
                break
            chain.append(node)
            node_id = node.get("parentId")

        chain.reverse()

        result = []
        for node in chain:
            role = node.get("role")
            content = node.get("content")
            if role not in ("user", "assistant"):
                continue
            if isinstance(content, str) and content:
                result.append({"role": role, "content": content})
            elif isinstance(content, list):
                text_parts = [
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                text = " ".join(t for t in text_parts if t)
                if text:
                    result.append({"role": role, "content": text})
        return result

    async def _add_assistant_message_to_chat(
        self,
        chat_id: str,
        assistant_msg_id: str,
        user_msg_id: str,
        content: str,
        model: str,
        timestamp: int,
    ) -> None:
        """Insert the assistant reply into the chat's history linked-list."""
        try:
            resp = await self._client.get(f"/api/v1/chats/{chat_id}")
            if resp.status_code in (404, 405):
                resp = await self._client.get(f"/api/chats/{chat_id}")
            resp.raise_for_status()
            data = self._safe_json_dict(resp)

            chat_obj = data.get("chat") if isinstance(data.get("chat"), dict) else data
            if not isinstance(chat_obj, dict):
                _log.warning("unexpected chat structure; skipping assistant message upsert")
                return

            history = chat_obj.get("history")
            if not isinstance(history, dict):
                history = {"messages": {}, "currentId": None}

            msgs = history.get("messages")
            if not isinstance(msgs, dict):
                msgs = {}

            msgs[assistant_msg_id] = {
                "id": assistant_msg_id,
                "parentId": user_msg_id,
                "childrenIds": [],
                "role": "assistant",
                "content": content,
                "model": model,
                "timestamp": timestamp,
                "done": True,
            }
            if user_msg_id in msgs:
                children = msgs[user_msg_id].get("childrenIds")
                if not isinstance(children, list):
                    children = []
                if assistant_msg_id not in children:
                    children.append(assistant_msg_id)
                msgs[user_msg_id]["childrenIds"] = children

            history["messages"] = msgs
            history["currentId"] = assistant_msg_id
            chat_obj["history"] = history

            upd = await self._client.post(f"/api/v1/chats/{chat_id}", json={"chat": chat_obj})
            if upd.status_code in (404, 405):
                upd = await self._client.post(f"/api/chats/{chat_id}", json={"chat": chat_obj})
            upd.raise_for_status()
            _log.debug("upserted assistant message %s into chat %s", assistant_msg_id, chat_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                _log.debug("skipping assistant message upsert for inaccessible chat %s", chat_id)
                return
            _log.warning(
                "failed to upsert assistant message into chat history; "
                "next turn will lack this reply as context: %s", exc,
            )
        except Exception as exc:
            _log.warning(
                "failed to upsert assistant message into chat history; "
                "next turn will lack this reply as context: %s", exc,
            )

    @staticmethod
    def _extract_content_from_completion(completion: dict) -> str:
        """Extract the assistant's text content from a /api/chat/completions response."""
        if "choices" in completion and completion["choices"]:
            message = completion["choices"][0].get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content
        if "message" in completion and isinstance(completion["message"], dict):
            content = completion["message"].get("content")
            if isinstance(content, str):
                return content
        return ""

    async def _generate_and_persist_title(
        self,
        chat_id: str,
        model: str,
        messages: list,
    ) -> None:
        """Generate a descriptive title for the chat and persist it to Open WebUI."""
        try:
            title_payload = {"model": model, "messages": messages, "chat_id": chat_id}
            resp = await self._client.post(
                "/api/v1/tasks/title/completions", json=title_payload
            )
            if not resp.is_success:
                _log.warning(
                    "title generation returned %s; chat title will remain 'New Chat'",
                    resp.status_code,
                )
                return
            title_completion = self._safe_json(resp)
            if not isinstance(title_completion, dict):
                return
            title = self._extract_content_from_completion(title_completion)
            if not title:
                if isinstance(title_completion, str):
                    title = title_completion.strip()
            if title:
                try:
                    parsed = json.loads(title)
                    if isinstance(parsed, dict) and "title" in parsed:
                        title = str(parsed["title"]).strip()
                except (ValueError, TypeError):
                    pass
            if not title:
                _log.warning("title generation returned empty content")
                return

            chat_resp = await self._client.get(f"/api/v1/chats/{chat_id}")
            if chat_resp.status_code in (404, 405):
                chat_resp = await self._client.get(f"/api/chats/{chat_id}")
            chat_resp.raise_for_status()
            data = self._safe_json_dict(chat_resp)
            chat_obj = data.get("chat") if isinstance(data.get("chat"), dict) else data
            if not isinstance(chat_obj, dict):
                return
            chat_obj["title"] = title
            upd = await self._client.post(
                f"/api/v1/chats/{chat_id}", json={"chat": chat_obj}
            )
            if upd.status_code in (404, 405):
                upd = await self._client.post(
                    f"/api/chats/{chat_id}", json={"chat": chat_obj}
                )
            upd.raise_for_status()
            _log.info("set title for chat %s: %r", chat_id, title)
        except Exception as exc:
            _log.warning("failed to generate/persist chat title: %s", exc)

    async def _finalize_completion(
        self,
        chat_id: str,
        model: str,
        assistant_msg_id: str,
        messages: list,
    ) -> None:
        """POST to /api/chat/completed to clear the 'still generating' state in the UI."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "chat_id": chat_id,
            "id": assistant_msg_id,
            "session_id": "",
        }
        try:
            resp = await self._client.post("/api/chat/completed", json=payload)
            if not resp.is_success:
                _log.warning(
                    "POST /api/chat/completed returned %s; "
                    "chat may remain in 'ongoing' state in the UI",
                    resp.status_code,
                )
        except Exception:
            _log.warning("failed to call POST /api/chat/completed", exc_info=True)

    def _safe_json_dict(self, response: httpx.Response) -> dict[str, Any]:
        payload = self._safe_json(response)
        if isinstance(payload, dict):
            return payload
        return {}

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any | None:
        content = response.content or b""
        if not content.strip():
            return None
        OpenWebUIClient._raise_if_html(response)
        try:
            return response.json()
        except ValueError:
            return None

    @staticmethod
    def _raise_if_html(response: httpx.Response) -> None:
        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type:
            raise RuntimeError(
                "Open WebUI returned HTML instead of JSON. "
                "Check OPEN_WEBUI_URL and OPEN_WEBUI_TOKEN."
            )
