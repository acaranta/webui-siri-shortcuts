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

# Open WebUI runs the tool loop as a background task, so the reply is read back
# from the chat: how long to wait for it, and how often to poll the task list.
_TOOL_LOOP_TIMEOUT = 240.0
_TOOL_LOOP_POLL_INTERVAL = 1.0
# Polls tolerated after the task list empties but before the assistant message
# has been written to the chat.
_TOOL_LOOP_IDLE_POLLS = 3
# How long a model's tool/feature configuration is cached, in seconds.
_MODEL_OPTIONS_TTL = 300.0


class ChatAccessError(Exception):
    """Raised when a stored Open WebUI chat_id returns 401.

    This typically means the token was changed or the chat was created
    under a different account.
    """


@dataclass
class ModelOptions:
    """Tools and built-in features a workspace model is configured with."""

    tool_ids: list[str]
    features: dict[str, bool]


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
        # model id -> (fetched_at, options), see get_model_options()
        self._model_options: dict[str, tuple[float, ModelOptions]] = {}

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

    async def get_model_options(self, model_id: str) -> ModelOptions:
        """Resolve the tools and built-in features a workspace model is set up with.

        ``meta.toolIds`` lists the tools (workspace tools, MCP and OpenAPI tool
        servers) attached to the model, and ``meta.defaultFeatureIds`` the
        built-in features it should start with — gated by ``meta.capabilities``,
        which is what the workspace UI itself checks before offering a feature.

        Open WebUI's backend never reads either off the model: the browser puts
        them in the request body, so an API client has to do the same. Base
        (non-workspace) models have no such row and yield nothing. Results are
        cached so this costs one request per model, not one per message.
        """
        now = time.time()
        cached = self._model_options.get(model_id)
        if cached is not None and now - cached[0] < _MODEL_OPTIONS_TTL:
            return cached[1]
        options = ModelOptions(tool_ids=[], features={})
        try:
            response = await self._client.get(
                "/api/v1/models/model", params={"id": model_id}
            )
            if response.is_success:
                payload = self._safe_json(response)
                if isinstance(payload, dict):
                    # This endpoint returns the workspace row directly, so meta
                    # sits at the top level; /api/models nests it under "info".
                    meta = payload.get("meta")
                    if not isinstance(meta, dict):
                        info = payload.get("info")
                        meta = info.get("meta") if isinstance(info, dict) else None
                    if isinstance(meta, dict):
                        options = self._parse_model_meta(meta)
            else:
                _log.warning(
                    "could not read options for model %s (%s); "
                    "the model will run without its tools",
                    model_id, response.status_code,
                )
        except Exception as exc:
            # Never block a reply over this — send the message unadorned.
            _log.warning("failed to read options for model %s: %s", model_id, exc)
            return options
        _log.info(
            "model %s: tool_ids=%s features=%s",
            model_id, options.tool_ids, sorted(options.features),
        )
        self._model_options[model_id] = (now, options)
        return options

    @staticmethod
    def _parse_model_meta(meta: dict[str, Any]) -> ModelOptions:
        """Pull the tool ids and default-on features out of a model's meta."""
        raw_tools = meta.get("toolIds")
        tool_ids = (
            [t for t in raw_tools if isinstance(t, str)]
            if isinstance(raw_tools, list)
            else []
        )
        capabilities = meta.get("capabilities")
        capabilities = capabilities if isinstance(capabilities, dict) else {}
        raw_features = meta.get("defaultFeatureIds")
        features = {
            f: True
            for f in (raw_features if isinstance(raw_features, list) else [])
            if isinstance(f, str) and capabilities.get(f) is True
        }
        return ModelOptions(tool_ids=tool_ids, features=features)

    async def send_message(
        self,
        chat_id: str,
        model: str,
        content: str,
    ) -> dict[str, Any]:
        """Send a user message and return the assistant's reply.

        Open WebUI only executes tools server-side on the streaming path, and
        only for a request that names an existing chat and assistant message,
        so the completion is fired as a background task and the finished reply
        is read back from the chat once the task list drains.
        """
        user_msg_id = str(uuid.uuid4())
        assistant_msg_id = str(uuid.uuid4())
        now = int(time.time())

        # 1. Fetch existing history BEFORE adding the new user message.
        prior_messages = await self._fetch_history_messages(chat_id)

        # 2. Persist the user message plus an empty assistant message: the
        #    server-side tool loop writes its answer into the latter, so both
        #    have to exist before the completion is fired.
        user_message = await self._add_user_message_to_chat(
            chat_id=chat_id,
            user_msg_id=user_msg_id,
            content=content,
            timestamp=now,
            model=model,
            assistant_msg_id=assistant_msg_id,
        )

        # 3. Build the full messages array for the completions API.
        options = await self.get_model_options(model)
        messages = prior_messages + [{"role": "user", "content": content}]
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            # The native, multi-round tool loop lives on the streaming path
            # only; a non-streaming request hands back tool calls that nobody
            # executes, and the reply comes out empty.
            "stream": True,
            "chat_id": chat_id,
            "id": assistant_msg_id,
            # Open WebUI owns the message tree: it writes the assistant message
            # with parentId taken from user_message["id"], so without this the
            # reply is stored detached and the chat shows nothing but the last
            # answer.
            "user_message": user_message,
            # Any non-empty session_id switches on the built-in tools (web
            # search, memory, ...) and makes the request asynchronous.
            "session_id": f"siri-{uuid.uuid4()}",
            "background_tasks": {
                "title_generation": not prior_messages,
                "tags_generation": False,
                "follow_up_generation": False,
            },
        }
        # Tools and features are read from the request body, so a workspace
        # model's own configuration has to be forwarded explicitly or it is
        # ignored — see get_model_options().
        if options.tool_ids:
            payload["tool_ids"] = options.tool_ids
        if options.features:
            payload["features"] = options.features

        response = await self._client.post("/api/chat/completions", json=payload)
        if not response.is_success:
            _log.warning(
                "completions request failed (%s) — body: %s",
                response.status_code,
                response.text[:500],
            )
        response.raise_for_status()

        # 4. The response body only acknowledges the task ("task_ids"); the
        #    answer itself is written into the chat.
        message = await self._wait_for_assistant_message(chat_id, assistant_msg_id)
        assistant_content = message.get("content")
        if not isinstance(assistant_content, str):
            assistant_content = ""
        if not assistant_content and message.get("error"):
            _log.warning(
                "assistant message %s came back with an error: %s",
                assistant_msg_id, message.get("error"),
            )

        # 5. On the first turn Open WebUI generates the title itself; fall back
        #    to an explicit request when that background task produced nothing.
        if not prior_messages and assistant_content:
            title_messages = messages + [{"role": "assistant", "content": assistant_content}]
            await self._ensure_chat_title(chat_id, model, title_messages)

        return {"content": assistant_content, "message_id": assistant_msg_id}

    async def _wait_for_assistant_message(
        self,
        chat_id: str,
        assistant_msg_id: str,
    ) -> dict[str, Any]:
        """Poll until the server-side tool loop finishes, then read the reply."""
        deadline = time.monotonic() + _TOOL_LOOP_TIMEOUT
        idle_polls = 0
        message: dict[str, Any] = {}
        while time.monotonic() < deadline:
            await asyncio.sleep(_TOOL_LOOP_POLL_INTERVAL)
            if await self._chat_has_running_tasks(chat_id):
                idle_polls = 0
                continue
            message = await self._get_chat_message(chat_id, assistant_msg_id)
            if message.get("done") or message.get("content") or message.get("error"):
                return message
            # The task list can read empty for a moment before the message is
            # written to the chat, so allow a few polls before giving up.
            idle_polls += 1
            if idle_polls >= _TOOL_LOOP_IDLE_POLLS:
                return message
        _log.warning(
            "timed out after %.0fs waiting for message %s in chat %s",
            _TOOL_LOOP_TIMEOUT, assistant_msg_id, chat_id,
        )
        return message

    async def _chat_has_running_tasks(self, chat_id: str) -> bool:
        """True while Open WebUI still has background tasks running for this chat."""
        try:
            response = await self._client.get(f"/api/tasks/chat/{chat_id}")
            response.raise_for_status()
            return bool(self._safe_json_dict(response).get("task_ids"))
        except Exception as exc:
            # Treat an unreadable task list as "nothing running": the caller
            # then falls back to whatever the chat already holds.
            _log.warning("failed to poll tasks for chat %s: %s", chat_id, exc)
            return False

    async def _get_chat_message(self, chat_id: str, message_id: str) -> dict[str, Any]:
        """Return one message of a chat's history, or {} if it is not there yet."""
        try:
            data = await self.get_chat(chat_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                raise ChatAccessError(
                    f"chat {chat_id} is not accessible (401) — token may have changed"
                ) from exc
            _log.warning("failed to read chat %s: %s", chat_id, exc)
            return {}
        except Exception as exc:
            _log.warning("failed to read chat %s: %s", chat_id, exc)
            return {}
        chat_obj = data.get("chat") if isinstance(data.get("chat"), dict) else data
        if not isinstance(chat_obj, dict):
            return {}
        history = chat_obj.get("history")
        msgs = history.get("messages") if isinstance(history, dict) else None
        message = msgs.get(message_id) if isinstance(msgs, dict) else None
        return message if isinstance(message, dict) else {}

    async def _ensure_chat_title(
        self,
        chat_id: str,
        model: str,
        messages: list,
    ) -> None:
        """Generate a title only if Open WebUI's own title task did not set one."""
        try:
            data = await self.get_chat(chat_id)
            chat_obj = data.get("chat") if isinstance(data.get("chat"), dict) else data
            title = chat_obj.get("title") if isinstance(chat_obj, dict) else None
            if title and title != "New Chat":
                return
        except Exception as exc:
            _log.warning("failed to read title of chat %s: %s", chat_id, exc)
        await self._generate_and_persist_title(chat_id, model, messages)

    async def _add_user_message_to_chat(
        self,
        chat_id: str,
        user_msg_id: str,
        content: str,
        timestamp: int,
        model: Optional[str] = None,
        assistant_msg_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Insert the user message into the chat's history linked-list.

        When *assistant_msg_id* is given, an empty assistant message is added
        as its child: Open WebUI's server-side tool loop only runs for a
        request whose assistant message already exists, and it writes the
        finished answer into that message.

        Returns the user message as stored, so the completion request can hand
        Open WebUI the very same object.
        """
        user_message: dict[str, Any] = {
            "id": user_msg_id,
            "parentId": None,
            "childrenIds": [assistant_msg_id] if assistant_msg_id else [],
            "role": "user",
            "content": content,
            "timestamp": timestamp,
            "models": [model] if model else [],
        }
        try:
            resp = await self._client.get(f"/api/v1/chats/{chat_id}")
            if resp.status_code in (404, 405):
                resp = await self._client.get(f"/api/chats/{chat_id}")
            resp.raise_for_status()
            data = self._safe_json_dict(resp)

            chat_obj = data.get("chat") if isinstance(data.get("chat"), dict) else data
            if not isinstance(chat_obj, dict):
                _log.warning("unexpected chat structure; skipping user message upsert")
                return user_message

            if model:
                chat_obj["model"] = model
                chat_obj["models"] = [model]

            history = chat_obj.get("history")
            if not isinstance(history, dict):
                history = {"messages": {}, "currentId": None}

            msgs = history.get("messages")
            if not isinstance(msgs, dict):
                msgs = {}

            user_message["parentId"] = history.get("currentId")

            msgs[user_msg_id] = user_message
            history["currentId"] = user_msg_id

            if assistant_msg_id:
                msgs[assistant_msg_id] = {
                    "id": assistant_msg_id,
                    "parentId": user_msg_id,
                    "childrenIds": [],
                    "role": "assistant",
                    "content": "",
                    "model": model,
                    "modelName": model,
                    "modelIdx": 0,
                    "done": False,
                    "timestamp": timestamp + 1,
                }
                history["currentId"] = assistant_msg_id

            history["messages"] = msgs
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
        return user_message

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
