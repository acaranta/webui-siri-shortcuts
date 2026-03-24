from __future__ import annotations

import pathlib
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security.api_key import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from webui_siri.config import AppConfig
from webui_siri.logging_setup import get_logger
from webui_siri.openwebui import ChatAccessError, OpenWebUIClient

# Resolve the repo root's img/ directory relative to this file
_REPO_ROOT = pathlib.Path(__file__).parent.parent
_IMG_DIR = _REPO_ROOT / "img"

_LANDING_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>webui-siri-shortcut</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      background: #0f0f0f;
      color: #e8e8e8;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      gap: 1.5rem;
      padding: 2rem;
      text-align: center;
    }
    img { width: 140px; height: 140px; object-fit: contain; }
    h1 { font-size: 1.6rem; font-weight: 600; letter-spacing: -0.02em; }
    p { color: #888; font-size: 0.95rem; max-width: 34ch; line-height: 1.5; }
    .badge {
      display: inline-block;
      background: #1a1a1a;
      border: 1px solid #2a2a2a;
      border-radius: 6px;
      padding: 0.35rem 0.75rem;
      font-size: 0.8rem;
      color: #5ac8fa;
      font-family: ui-monospace, monospace;
    }
  </style>
</head>
<body>
  <img src="/img/webui-siri-logo.png" alt="webui-siri-shortcut logo"/>
  <h1>webui-siri-shortcut</h1>
  <p>Siri Shortcut bridge for Open&nbsp;WebUI — say <em>"Hey Siri, Siri Plus"</em> to start a conversation.</p>
  <span class="badge">GET /api/health &nbsp;·&nbsp; POST /api/chat</span>
</body>
</html>
"""

_log = get_logger("webui_siri.api")

_API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


class NewChatRequest(BaseModel):
    message: str = Field(..., description="User's question")
    model: Optional[str] = Field(None, description="Optional model override")


class NewChatResponse(BaseModel):
    chat_id: str
    response: str
    title: str


class FollowUpRequest(BaseModel):
    message: str = Field(..., description="Follow-up question in the same chat")


class FollowUpResponse(BaseModel):
    chat_id: str
    response: str


class HealthResponse(BaseModel):
    status: str = Field("ok")


def create_app(config: AppConfig, openwebui: OpenWebUIClient) -> FastAPI:
    """Factory that wires up the FastAPI application."""
    app = FastAPI(
        title="webui-siri-shortcut API",
        version="1.0.0",
        description=(
            "REST API that bridges Apple Siri Shortcuts to Open WebUI. "
            "All routes except /api/health require an X-API-Key header."
        ),
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    # Serve the logo (and any other assets placed in img/)
    if _IMG_DIR.is_dir():
        app.mount("/img", StaticFiles(directory=str(_IMG_DIR)), name="img")

    async def require_api_key(key: str = Security(_API_KEY_HEADER)) -> str:
        if key != config.api_key:
            raise HTTPException(status_code=403, detail="Invalid or missing API key")
        return key

    @app.get("/", include_in_schema=False)
    async def root() -> HTMLResponse:
        return HTMLResponse(content=_LANDING_HTML)

    @app.get("/api", include_in_schema=False)
    async def api_root() -> RedirectResponse:
        return RedirectResponse(url="/api/docs")

    @app.get(
        "/api/health",
        tags=["ops"],
        summary="Health check",
        response_model=HealthResponse,
    )
    async def health() -> HealthResponse:
        """Returns service health. No authentication required."""
        return HealthResponse(status="ok")

    @app.post(
        "/api/chat",
        tags=["chat"],
        summary="Start a new chat",
        response_model=NewChatResponse,
    )
    async def new_chat(
        req: NewChatRequest,
        _: str = Depends(require_api_key),
    ) -> NewChatResponse:
        """Create a new Open WebUI chat, send the first message, and return the response.

        On success returns the chat_id (needed for follow-up questions), the
        assistant response text, and the auto-generated chat title.
        """
        model = req.model or config.open_webui_model
        try:
            chat_data = await openwebui.create_chat(model=model)
        except Exception as exc:
            _log.error("failed to create chat: %s", exc)
            raise HTTPException(status_code=502, detail=f"Failed to create chat in OpenWebUI: {exc}")

        chat_id = chat_data.get("id")
        if not chat_id:
            raise HTTPException(status_code=502, detail="OpenWebUI did not return a chat ID")

        try:
            result = await openwebui.send_message(chat_id=chat_id, model=model, content=req.message)
        except ChatAccessError as exc:
            raise HTTPException(status_code=401, detail=f"OpenWebUI token error: {exc}")
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=502, detail=f"OpenWebUI error: {exc}")
        except Exception as exc:
            _log.error("send_message failed: %s", exc)
            raise HTTPException(status_code=502, detail=f"OpenWebUI error: {exc}")

        response_text = openwebui._extract_content_from_completion(result["completion"])
        if not response_text:
            response_text = "I didn't get a response. Please try again."

        # Fetch the title that was generated and persisted by send_message
        try:
            chat = await openwebui.get_chat(chat_id)
            chat_obj = chat.get("chat") if isinstance(chat.get("chat"), dict) else chat
            title = (chat_obj or {}).get("title") or "New Chat"
        except Exception:
            title = "New Chat"

        return NewChatResponse(chat_id=chat_id, response=response_text, title=title)

    @app.post(
        "/api/chat/{chat_id}/message",
        tags=["chat"],
        summary="Continue an existing chat",
        response_model=FollowUpResponse,
    )
    async def follow_up(
        chat_id: str,
        req: FollowUpRequest,
        _: str = Depends(require_api_key),
    ) -> FollowUpResponse:
        """Send a follow-up message to an existing Open WebUI chat.

        The chat_id is obtained from the POST /api/chat response.
        Conversation history is stored in Open WebUI and automatically
        included as context for the next response.
        """
        # Fetch the existing chat to get the model in use
        try:
            chat = await openwebui.get_chat(chat_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise HTTPException(status_code=404, detail="Chat not found")
            raise HTTPException(status_code=502, detail=f"OpenWebUI error: {exc}")
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"OpenWebUI error: {exc}")

        chat_obj = chat.get("chat") if isinstance(chat.get("chat"), dict) else chat
        model = (chat_obj or {}).get("model") or config.open_webui_model

        try:
            result = await openwebui.send_message(chat_id=chat_id, model=model, content=req.message)
        except ChatAccessError as exc:
            raise HTTPException(status_code=401, detail=f"OpenWebUI token error: {exc}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise HTTPException(status_code=404, detail="Chat not found")
            raise HTTPException(status_code=502, detail=f"OpenWebUI error: {exc}")
        except Exception as exc:
            _log.error("send_message failed: %s", exc)
            raise HTTPException(status_code=502, detail=f"OpenWebUI error: {exc}")

        response_text = openwebui._extract_content_from_completion(result["completion"])
        if not response_text:
            response_text = "I didn't get a response. Please try again."

        return FollowUpResponse(chat_id=chat_id, response=response_text)

    return app
