# webui-siri-shortcut

A stateless Docker service that bridges Apple Siri Shortcuts to an [Open WebUI](https://github.com/open-webui/open-webui) instance. Invoke it with "Hey Siri, Siri Plus" for a voice-driven LLM conversation.

Modeled on [webui-grambot](https://git.minixer.com/Albus-Insec/webui-grambot) — same OpenWebUI API integration, without the Telegram dependency.

---

## How it works

1. Siri Shortcut is invoked ("Hey Siri, Siri Plus")
2. Shortcut speaks "Yes?" and records your question
3. Shortcut calls `POST /api/chat` → service creates a new Open WebUI chat and sends the message
4. LLM response is spoken back to you
5. Shortcut asks if you want to continue — voice input is used as the next prompt
6. Say "no", "nope", "none", "stop" (or any phrase containing "no") to end the conversation

All conversation history is stored in Open WebUI (visible in the web UI).

---

## Quick Start

### 1. Copy and fill in the config

```bash
cp docker-compose.yml.example docker-compose.yml
# Edit docker-compose.yml with your values
```

### 2. Generate an API key

```bash
openssl rand -hex 32
```

### 3. Start the service

```bash
docker compose up -d
```

### 4. Verify it's running

```bash
curl http://localhost:8080/api/health
# {"status":"ok"}
```

### 5. Set up the Siri Shortcut

See [shortcut/SETUP.md](shortcut/SETUP.md) for the manual setup guide, or generate the shortcut automatically:

```bash
python shortcut/generate_shortcut.py \
  --url https://YOUR_SERVER \
  --api-key YOUR_API_KEY
```

---

## Configuration

All configuration is via environment variables:

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPEN_WEBUI_URL` | yes | — | Base URL of your Open WebUI instance |
| `OPEN_WEBUI_TOKEN` | yes | — | Open WebUI API token (Settings → Account → API Keys) |
| `OPEN_WEBUI_MODEL` | yes | — | Default LLM model ID (e.g. `llama3.2`, `gpt-4o`) |
| `API_KEY` | yes | — | Shared secret for the Siri Shortcut (`X-API-Key` header) |
| `API_PORT` | no | `8080` | Port the service listens on |
| `OPEN_WEBUI_FOLDER` | no | — | Folder name for new chats in Open WebUI |

---

## API Reference

All chat endpoints require the `X-API-Key` header.

### `POST /api/chat`

Start a new conversation.

**Request:**
```json
{
  "message": "What is the capital of France?",
  "model": "llama3.2"
}
```
`model` is optional — defaults to `OPEN_WEBUI_MODEL`.

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

### `POST /api/chat/{chat_id}/message`

Send a follow-up message to an existing chat.

**Request:**
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

### `GET /api/health`

Health check — no authentication required.

**Response:**
```json
{"status": "ok"}
```

---

## Development

```bash
# Install dependencies
uv sync

# Run locally (set env vars first)
export OPEN_WEBUI_URL=http://localhost:3000
export OPEN_WEBUI_TOKEN=sk-your-token
export OPEN_WEBUI_MODEL=llama3.2
export API_KEY=dev-key-123

uv run python -m webui_siri.main
```

API docs available at `http://localhost:8080/api/docs` while the service is running.

---

## Siri Shortcut

See [shortcut/SETUP.md](shortcut/SETUP.md) for detailed setup instructions.

### Quick generate

```bash
python shortcut/generate_shortcut.py \
  --url https://YOUR_SERVER \
  --api-key YOUR_API_KEY \
  --output siri-plus.shortcut
```

Then double-click `siri-plus.shortcut` on macOS to import, or AirDrop to iOS.

---

## Architecture Notes

- **Stateless service** — no database. All conversation history lives in Open WebUI.
- The Siri Shortcut stores `chat_id` locally in a variable between loop iterations.
- OpenWebUI's linked-list chat history is fully managed: each message is written to the history so the conversation is visible in the Open WebUI browser interface.
- The service requires HTTPS on iOS/macOS — deploy behind a reverse proxy with TLS.

---

## Deployment with reverse proxy

Put the container behind nginx or Traefik with a valid TLS certificate. The iOS Shortcuts app blocks plain HTTP requests.

See the nginx example in [shortcut/SETUP.md](shortcut/SETUP.md).
