# Siri Shortcut Setup Guide

This guide explains how to create the "Siri Plus" shortcut that bridges Siri to your Open WebUI instance.

## Prerequisites

- The `webui-siri-shortcut` service is deployed and reachable over **HTTPS** from your device
- You have your server URL (e.g. `https://siri.example.com`) and API key ready
- iOS 16+ or macOS Ventura+

> **Important:** iOS blocks plain HTTP requests in Shortcuts. Your server must be behind a reverse proxy (nginx, Traefik, Caddy, etc.) with a valid TLS certificate. Self-signed certificates will fail unless you install a trust profile.

---

## Option A: Generate the shortcut automatically

Run the provided script on macOS (no extra dependencies required):

```bash
python shortcut/generate_shortcut.py \
  --url https://YOUR_SERVER \
  --api-key YOUR_API_KEY \
  --output siri-plus.shortcut
```

Then double-click `siri-plus.shortcut` to import it into the Shortcuts app.

If the import fails, convert it to binary plist first:
```bash
plutil -convert binary1 siri-plus.shortcut
```

---

## Option B: Create the shortcut manually

Open the **Shortcuts** app on iPhone, iPad, or Mac and create a new shortcut.

### Step 1 — Name and Siri activation

1. Tap the shortcut name at the top and rename it to **Siri Plus**
2. Tap the info (ⓘ) button → enable **"Use with Ask Siri"** (and "Add to Home Screen" if desired)

### Step 2 — Add these actions in order

---

#### Action 1: Speak Text
- Action: **Speak Text**
- Text: `Yes?`
- Wait until finished: **ON**

---

#### Action 2: Dictate Text
- Action: **Dictate Text**
- Language: **Default** (auto-detects)
- Stop listening: **After Pause**
- This captures the user's spoken question.
- Rename the output variable to **`Question`**

---

#### Action 3: Get Contents of URL (new chat)
- Action: **Get Contents of URL**
- URL: `https://YOUR_SERVER/api/chat`
- Method: **POST**
- Headers:
  - `X-API-Key` → `YOUR_API_KEY`
  - `Content-Type` → `application/json`
- Request Body: **JSON**
  - Add key `message` → value: select the variable **`Question`**
- Rename the output variable to **`ChatResponse`**

---

#### Action 4: Get Dictionary Value — response
- Action: **Get Dictionary Value**
- Get: **Value** for key `response`
- From: **`ChatResponse`**
- Rename the output variable to **`AssistantReply`**

---

#### Action 5: Get Dictionary Value — chat_id
- Action: **Get Dictionary Value**
- Get: **Value** for key `chat_id`
- From: **`ChatResponse`**
- Rename the output variable to **`ChatID`**

---

#### Action 6: Speak Text (first response)
- Action: **Speak Text**
- Text: select variable **`AssistantReply`**
- Wait until finished: **ON**

---

#### Action 7: Repeat
- Action: **Repeat**
- Count: `9999` (effectively infinite; the loop exits via the "Stop Shortcut" action below)

Inside the loop, add the following actions:

---

#### Action 7a: Dictate Text (follow-up)
- Action: **Dictate Text**
- Language: **Default**
- Stop listening: **After Pause**
- Rename output to **`FollowUp`**

---

#### Action 7b: If (exit condition)
- Action: **If**
- Input: **`FollowUp`**
- Condition: **contains**
- Value: `no`

> To also match "nope", "none", "stop", "exit", "done" — add additional **Otherwise If** blocks with the same condition for each word, all leading to the same Speak + Stop actions below.

---

#### Action 7c: Speak Text (goodbye) — inside the If block
- Action: **Speak Text**
- Text: `OK, see you`
- Wait until finished: **ON**

---

#### Action 7d: Stop Shortcut — inside the If block
- Action: **Stop Shortcut**

---

#### Action 7e: Otherwise
- (automatically added by the If block)

---

#### Action 7f: Get Contents of URL (follow-up)
- Action: **Get Contents of URL**
- URL: `https://YOUR_SERVER/api/chat/` + variable **`ChatID`** + `/message`
  - Tap the URL field and use **Insert Variable** to embed `ChatID` in the path
- Method: **POST**
- Headers:
  - `X-API-Key` → `YOUR_API_KEY`
  - `Content-Type` → `application/json`
- Request Body: **JSON**
  - Add key `message` → value: select variable **`FollowUp`**
- Rename output to **`FollowUpResponse`**

---

#### Action 7g: Get Dictionary Value — response (follow-up)
- Action: **Get Dictionary Value**
- Get: **Value** for key `response`
- From: **`FollowUpResponse`**
- Rename output to **`Reply`**

---

#### Action 7h: Speak Text (follow-up response)
- Action: **Speak Text**
- Text: select variable **`Reply`**
- Wait until finished: **ON**

---

#### Action 7i: End If

---

#### Action 8: End Repeat

---

## Tips & Known Limitations

### URL construction for follow-up
When building the follow-up URL with an embedded variable, tap the URL field and type:
```
https://YOUR_SERVER/api/chat/
```
Then tap **Insert Variable** → choose **ChatID**, then type `/message`.
The final URL should look like: `https://YOUR_SERVER/api/chat/[ChatID]/message`

### "No" detection
The shortcut checks if your follow-up dictation **contains** "no". This matches:
- "no", "No", "NO"
- "nope", "none"
- Phrases like "no thanks", "no that's all"

You can add more exit words by adding extra **Otherwise If** branches before the **Otherwise**.

### Response latency
LLM inference typically takes 5–30 seconds. The Shortcuts app has a URL request timeout of approximately 60 seconds. For very long model responses or slow hardware, this may time out.

### API key security
The API key is stored in plain text inside the shortcut file. Do not share the exported shortcut with others. If the key is compromised, generate a new one and update both the server environment variable and the shortcut.

### HTTPS requirement
iOS/macOS Shortcuts enforce HTTPS for outbound requests. The container itself only serves plain HTTP — put it behind a reverse proxy that handles TLS termination:

```nginx
# Example nginx config snippet
server {
    listen 443 ssl;
    server_name siri.example.com;
    ssl_certificate /etc/letsencrypt/live/siri.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/siri.example.com/privkey.pem;

    location / {
        proxy_pass http://localhost:8080;
        proxy_set_header Host $host;
        proxy_read_timeout 120s;
    }
}
```
