# webui-siri-shortcut

A stateless FastAPI service, packaged as a Docker container, that bridges Apple Siri Shortcuts to an [Open WebUI](https://github.com/open-webui/open-webui) instance. Say "Hey Siri, Siri Plus" to start a voice-driven LLM conversation — your words are transcribed by the Shortcuts app, sent to the service, and the LLM response is spoken back to you.

Modeled on [webui-grambot](https://git.minixer.com/Albus-Insec/webui-grambot) — same Open WebUI API integration, without the Telegram dependency.

---

## Table of Contents

- [How it works](#how-it-works)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [OPEN_WEBUI_FOLDER — chat organisation](#open_webui_folder--chat-organisation)
- [API Reference](#api-reference)
- [Siri Shortcut Setup](#siri-shortcut-setup)
- [Reverse Proxy and HTTPS](#reverse-proxy-and-https)
- [Development](#development)
- [CI / CD](#ci--cd)
- [Architecture Notes](#architecture-notes)

---

## How it works

1. You invoke the Siri Shortcut ("Hey Siri, Siri Plus").
2. The shortcut says "Yes?" and records your spoken question via dictation.
3. The shortcut calls `POST /api/chat` — the service creates a new Open WebUI chat and sends the first message.
4. The LLM response is spoken back to you.
5. The shortcut asks if you want to continue — your next dictation becomes the follow-up message.
6. Say "no", "nope", "none", "stop", or any phrase containing "no" to end the session.

All conversation history is stored in Open WebUI and visible in its browser interface.

---

## Prerequisites

- Docker and Docker Compose on your home server
- A running [Open WebUI](https://github.com/open-webui/open-webui) instance reachable from the server
- An Open WebUI API token (Settings → Account → API Keys)
- The service must be reachable over **HTTPS** from your iPhone or Mac (iOS/macOS Shortcuts blocks plain HTTP)
- iOS 16+ or macOS Ventura+ for the Siri Shortcut

---

## Quick Start

### 1. Copy and fill in the config

```bash
cp docker-compose.yml.example docker-compose.yml
```

Edit `docker-compose.yml` and fill in at minimum:

| Field | What to set |
|---|---|
| `OPEN_WEBUI_URL` | Base URL of your Open WebUI instance |
| `OPEN_WEBUI_TOKEN` | API token from Open WebUI → Settings → Account → API Keys |
| `OPEN_WEBUI_MODEL` | Model ID to use (e.g. `llama3.2`, `gpt-4o`) |
| `API_KEY` | A random secret — see step 2 |

### 2. Generate an API key

```bash
openssl rand -hex 32
```

Paste the output into `API_KEY` in `docker-compose.yml`.

### 3. Start the service

```bash
docker compose up -d
```

### 4. Verify it is running

```bash
curl http://localhost:8080/api/health
# {"status":"ok"}
```

### 5. Set up the Siri Shortcut

```bash
python shortcut/generate_shortcut.py \
  --url https://YOUR_SERVER \
  --api-key YOUR_API_KEY \
  --serve
```

The script generates `siri-plus.shortcut`, starts a local HTTP server, and prints a
`shortcuts://` URL. Open that URL in **Safari** on your device to import — this works
on all macOS/iOS versions including Sequoia and iOS 18+ (direct file import is blocked
on those versions).

See [shortcut/SETUP.md](shortcut/SETUP.md) for the manual build guide and troubleshooting tips.

---

## Configuration

All configuration is through environment variables. Set them in `docker-compose.yml`.

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPEN_WEBUI_URL` | yes | — | Base URL of your Open WebUI instance (e.g. `http://open-webui:3000`) |
| `OPEN_WEBUI_TOKEN` | yes | — | Open WebUI API token |
| `OPEN_WEBUI_MODEL` | yes | — | Default model ID (e.g. `llama3.2`, `gpt-4o`) |
| `API_KEY` | yes | — | Shared secret sent by the Siri Shortcut in the `X-API-Key` header |
| `API_PORT` | no | `8080` | Port the service listens on inside the container |
| `OPEN_WEBUI_FOLDER` | no | — | Name of the Open WebUI folder to file Siri chats under — see below |

---

## OPEN_WEBUI_FOLDER — chat organisation

When `OPEN_WEBUI_FOLDER` is set, every chat created by this service is automatically moved into a named folder in Open WebUI (e.g. `"Siri"`). This keeps your Siri conversations separated from chats you start manually in the browser.

**Behaviour:**

- At the time the first chat is created after startup, the service looks up the folder by name via the Open WebUI API.
- If the folder does not exist it is created automatically.
- The resolved folder ID is cached for the lifetime of the process. No repeated lookups are made.
- Restarting the container clears the cache; the lookup runs again on the next chat creation.

**Example `docker-compose.yml` snippet:**

```yaml
environment:
  OPEN_WEBUI_FOLDER: "Siri"
```

Leave the variable unset (or remove the line) to disable folder filing — new chats will land in the default location.

---

## API Reference

All chat endpoints require the header `X-API-Key: <your key>`. The health endpoint has no authentication.

Interactive API docs are available at `http://localhost:8080/api/docs` while the service is running.

---

### POST /api/chat

Start a new conversation. Returns a `chat_id` that must be passed to subsequent follow-up requests.

**Request body:**

```json
{
  "message": "What is the capital of France?",
  "model": "llama3.2"
}
```

`model` is optional and defaults to `OPEN_WEBUI_MODEL`.

**Response:**

```json
{
  "chat_id": "abc123-...",
  "response": "The capital of France is Paris.",
  "title": "Capital of France"
}
```

**Example:**

```bash
curl -s -X POST https://YOUR_SERVER/api/chat \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message": "What is the capital of France?"}' | python -m json.tool
```

---

### POST /api/chat/{chat_id}/message

Send a follow-up message to an existing chat. Conversation history stored in Open WebUI is included automatically as context.

**Request body:**

```json
{
  "message": "And what language do they speak there?"
}
```

**Response:**

```json
{
  "chat_id": "abc123-...",
  "response": "The official language of France is French."
}
```

---

### GET /api/health

Health check. No authentication required.

**Response:**

```json
{"status": "ok"}
```

---

## Siri Shortcut Setup

### Option A — Generate automatically (recommended)

Run the provided script on macOS (stdlib only, no extra dependencies):

```bash
python shortcut/generate_shortcut.py \
  --url https://YOUR_SERVER \
  --api-key YOUR_API_KEY \
  --serve
```

The `--serve` flag starts a local HTTP server and prints a `shortcuts://import-shortcut?url=...`
link. Open it in **Safari** on your iPhone or Mac to import. This works on all versions including
macOS Sequoia and iOS 18+, where direct file import is blocked for unsigned shortcuts.

Without `--serve` (macOS Ventura/Sonoma, iOS 16–17 only), double-click `siri-plus.shortcut` to import.

### Option B — Build manually

Follow the step-by-step guide in [shortcut/SETUP.md](shortcut/SETUP.md). The guide covers each Shortcuts action, variable naming, the follow-up loop, and the stop-phrase detection logic.

### Security note

The API key is stored in plain text inside the shortcut file. Do not share the exported `.shortcut` file with others. If the key is compromised, generate a new one (`openssl rand -hex 32`), update `API_KEY` in `docker-compose.yml`, and rebuild the shortcut.

---

## Reverse Proxy and HTTPS

iOS and macOS Shortcuts enforce HTTPS for all outbound URL requests. The container itself serves plain HTTP — you must place it behind a reverse proxy that handles TLS termination.

**nginx example:**

```nginx
server {
    listen 443 ssl;
    server_name siri.example.com;

    ssl_certificate     /etc/letsencrypt/live/siri.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/siri.example.com/privkey.pem;

    location / {
        proxy_pass http://localhost:8080;
        proxy_set_header Host $host;
        proxy_read_timeout 120s;
    }
}
```

Set `proxy_read_timeout` to at least `90s`. LLM inference can take 5–30 seconds and the Shortcuts app has a hard URL request timeout of approximately 60 seconds; large models on slow hardware can approach this limit.

Self-signed certificates will fail on iOS unless you install a trust profile on the device.

---

## Development

```bash
# Install dependencies (requires uv)
uv sync

# Set required environment variables
export OPEN_WEBUI_URL=http://localhost:3000
export OPEN_WEBUI_TOKEN=sk-your-token
export OPEN_WEBUI_MODEL=llama3.2
export API_KEY=dev-key-123

# Run locally
uv run python -m webui_siri.main
```

Interactive API docs are then available at `http://localhost:8080/api/docs`.

**Stack:** Python 3.11+, FastAPI, uvicorn, httpx, pydantic-settings. Dependency management via [uv](https://github.com/astral-sh/uv).

---

## CI / CD

A Drone CI pipeline (`.drone.yml`) builds and publishes a multi-arch Docker image (`ai/webui-siri-shortcut:latest`) for `linux/amd64` on pushes to `master`, `main`, and `dev`. The pipeline uses a shared template (`docker-build-multiarch.yaml`).

---

## Architecture Notes

- **Stateless** — no database, no volumes. All conversation history lives in Open WebUI.
- The Siri Shortcut holds `chat_id` in a local variable across loop iterations; the service itself is session-unaware.
- Open WebUI's linked-list message history is fully maintained by the service: each message is written back so the conversation is readable in the browser interface.
- The folder ID resolved via `OPEN_WEBUI_FOLDER` is process-scoped. It is looked up once and cached in memory; a container restart resets the cache.
