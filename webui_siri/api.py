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
  <link rel="icon" type="image/png" href="/img/webui-siri-logo.png"/>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      height: 100vh;
      overflow: hidden;
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
    img { width: 280px; height: 280px; object-fit: contain; }
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
    .links {
      display: flex;
      gap: 1rem;
      align-items: center;
      flex-wrap: wrap;
      justify-content: center;
    }
    .link-btn {
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      background: #1a1a1a;
      border: 1px solid #2a2a2a;
      border-radius: 8px;
      padding: 0.5rem 1rem;
      font-size: 0.85rem;
      color: #e8e8e8;
      text-decoration: none;
      transition: border-color 0.15s, background 0.15s;
    }
    .link-btn:hover { background: #222; border-color: #444; }
    .link-btn.shortcut { color: #5ac8fa; border-color: #1e3a4a; }
    .link-btn.shortcut:hover { background: #0d2030; border-color: #5ac8fa; }
  </style>
</head>
<body>
  <img src="/img/webui-siri-logo.png" alt="webui-siri-shortcut logo"/>
  <h1>webui-siri-shortcut</h1>
  <p>Siri Shortcut bridge for Open&nbsp;WebUI — say <em>"Hey Siri, Siri Plus"</em> to start a conversation.</p>
  <span class="badge">GET /api/health &nbsp;·&nbsp; POST /api/chat</span>
  <div class="links">
    <a class="link-btn" href="https://github.com/acaranta/webui-siri-shortcuts" target="_blank" rel="noopener">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
        <path d="M12 0C5.37 0 0 5.37 0 12c0 5.3 3.438 9.8 8.205 11.387.6.113.82-.258.82-.577
          0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61-.546-1.387-1.333-1.756
          -1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.84 1.237 1.84 1.237 1.07
          1.834 2.807 1.304 3.492.997.108-.775.418-1.305.762-1.605-2.665-.3-5.466-1.332
          -5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005
          -.322 3.3 1.23A11.51 11.51 0 0 1 12 5.803c1.02.005 2.047.138 3.006.404 2.29-1.552
          3.297-1.23 3.297-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0
          4.61-2.807 5.625-5.479 5.92.43.372.823 1.102.823 2.222 0 1.606-.015 2.896-.015
          3.286 0 .322.216.694.825.576C20.565 21.796 24 17.3 24 12c0-6.63-5.37-12-12-12z"/>
      </svg>
      GitHub
    </a>
    <a class="link-btn shortcut" href="https://www.icloud.com/shortcuts/980fff4cb9fb479fadc07bef96d6943e" target="_blank" rel="noopener">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M12 2a7 7 0 0 1 7 7c0 5-7 13-7 13S5 14 5 9a7 7 0 0 1 7-7z"/>
        <circle cx="12" cy="9" r="2.5"/>
      </svg>
      🇬🇧 Get Siri Shortcut
    </a>
    <a class="link-btn shortcut" href="https://www.icloud.com/shortcuts/e641e22518e445d7b01877bd65dde2de" target="_blank" rel="noopener">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M12 2a7 7 0 0 1 7 7c0 5-7 13-7 13S5 14 5 9a7 7 0 0 1 7-7z"/>
        <circle cx="12" cy="9" r="2.5"/>
      </svg>
      🇫🇷 Get Siri Shortcut
    </a>
  </div>
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
