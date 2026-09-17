<p align="center">
  <img src="assets/miri-api_logo.jpeg" width="200" alt="miri-api logo" />
</p>

<h1 align="center">miri-api</h1>

<p align="center">
  <strong>Turn your existing AI subscriptions into one OpenAI-compatible API.</strong><br/>
  Multiple providers, multiple accounts, all at once. No API keys — just your browser logins.
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> &bull;
  <a href="#providers--models">Providers &amp; Models</a> &bull;
  <a href="#endpoints">Endpoints</a> &bull;
  <a href="#the-admin-panel">Admin Panel</a> &bull;
  <a href="#accounts-proxies--scaling">Accounts &amp; Scaling</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.9+-blue?style=flat-square" alt="Python 3.9+" />
  <img src="https://img.shields.io/badge/providers-ChatGPT_%7C_Gemini_%7C_Grok_%7C_Claude_%7C_DeepSeek_%7C_Qwen_%7C_Seedream-purple?style=flat-square" alt="Providers" />
  <img src="https://img.shields.io/badge/API-OpenAI_compatible-green?style=flat-square" alt="OpenAI Compatible" />
  <img src="https://img.shields.io/badge/docker-ready-blue?style=flat-square" alt="Docker" />
</p>

---

## What is this?

**miri-api** drives the real **web UIs** of ChatGPT, Gemini, Grok, Claude, DeepSeek, Qwen and Seedream (ByteDance Dreamina) through a headed browser in Docker, and exposes them all behind **one OpenAI-compatible API**. It uses your existing logins/subscriptions instead of paying per token.

- **Many providers at once** — ChatGPT, Gemini (incl. **Nano Banana Pro** image generation), Grok, Claude, **DeepSeek**, **Qwen** (incl. image + **video** generation) and **Seedream** (Dreamina **video** generation) run *simultaneously*, not as a toggle. Pick which one serves a request by the model id or a path prefix.
- **Many accounts per provider** — add several accounts of the same provider; requests spread across them, and traffic auto-switches when one hits a rate limit or fails.
- **A browser-based control panel** — add/log-in/edit accounts, watch the browsers, and manage a proxy pool, all from a UI.

```python
# Point any OpenAI client at your local gateway. The API key goes in the URL path.
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/<API_TOKEN>/v1", api_key="unused")

print(client.chat.completions.create(
    model="chatgpt-browser",           # or gemini-browser / grok-browser / claude-browser
    messages=[{"role": "user", "content": "Hello from my own API!"}],
).choices[0].message.content)
```

---

## Providers & Models

### Temporary chats

Set `TEMPORARY_CHATS=1` in the gateway `.env` to make native temporary/incognito
chats the initial policy (restart the gateway after changing it with
`docker compose up -d --build` after installing these code changes). An existing
admin-panel choice in `browser_data/settings.json` takes precedence on startup.

The admin panel also exposes two live request-policy toggles: **Start every
request in a new chat** and **Temporary chats**. The former starts each
generation in a fresh saved chat. The latter starts each generation in a fresh
native temporary/incognito chat and leaves that page after the response. When
both are enabled, temporary mode takes precedence: each request opens a new
temporary chat and closes it after the request. Turning on temporary mode alone
still opens a new chat first so the provider can enter temporary mode.
These choices are stored in `browser_data/settings.json`; the corresponding
environment variables (`NEW_CHAT_EVERY_REQUEST` and `TEMPORARY_CHATS`) provide
the initial defaults when no admin-panel choice has been saved yet.

For per-request opt-in, use `/temporary/v1/chat/completions`,
`/temporary/v1/images/generations`, `/temporary/v1/images/edits`, or
`/temporary/v1/videos/generations`; provider-prefixed endpoints work too.
Authentication is unchanged. `X-Temporary-Chat: 1` also opts in on an updated
gateway, but the `/temporary/` prefix is preferred: an older gateway rejects
that route instead of ignoring an unknown header. Miri uses this prefix when
its `OWN_TEMPORARY_CHATS=1` setting is enabled.

Every request gets a fresh chat. The gateway verifies an active native temporary
mode before uploading references or typing a prompt, and leaves that page after
extracting/downloading the result. A missing or unrecognised native mode fails
before sending; there is **no fallback to a saved chat**, and Dreamina/Seedream
is unsupported in this mode. UI changes or account restrictions can therefore
make a provider unavailable while this setting is on. Saved-thread continuation
(`previous_response_id`, `/thread/{id}/chat`) is incompatible: send the context
in `messages` instead. The same limitation applies when **Start every request in
a new chat** is enabled. These settings do not delete Miri's history or gateway
logs.

### ChatGPT model selection

ChatGPT now follows the same model-registry/picker flow as Qwen and DeepSeek:
`chatgpt-auto`, `chatgpt-instant`, `chatgpt-thinking`, `chatgpt-pro`, plus named
version/legacy entries in `src/providers/chatgpt/chat_models.py`.
`chatgpt-browser` leaves the current selection alone. `GET /v1/chatgpt/chat/models`
lists supported ids and slow-mode flags; it is a supported-model catalog, not
an account entitlement check. Named entries and the Legacy models submenu are
matched by exact titles so GPT-5 cannot accidentally select GPT-5.4. Missing
entries keep the current model and log a warning, consistent with other providers.

### Qwen text response checks

Qwen chat returns text in `choices[0].message.content`. Its text detector waits
for a new assistant response and for the stop button to disappear, rejects
timeouts/empty responses, and never extracts the previous turn after a failed
send. Media modes are reconciled again before a text request.

Multiline prompts are inserted as one text input operation, then the entire
composer draft is verified before a single send. This covers all seven browser
providers, including their image/video composers. Do not use `keyboard.type()` here:
newlines generate Enter key events and can submit each line as a separate chat
message. Regression tests exercise real browser input events with Enter-to-send,
including long prompts, Unicode, leftover drafts and truncated input.

ChatGPT's ProseMirror composer uses one whole-value fill action and is verified
against both its copied text and paragraph DOM. This preserves multiline prompts
when the editor rebuilds plain text into `<p>` elements. A failed check reports
only expected/observed character counts and the editor type, never prompt text.

Submission waits for a visible, enabled Send control and requires acknowledgment
(cleared draft, a new matching user message, a Stop control, or the media result
page). A disabled button is never bypassed with Enter. An unacknowledged click
fails with an explicit error instead of waiting for the generation deadline or
retrying and risking duplicate generations. Gemini/Grok completion also requires
a finished new reply; loaded image-only replies are supported.

Qwen image/video requests open the native **Select Mode** menu and verify the
removable **Create image / Create video** control before typing. This supports
the custom dropdown rows in Qwen web 0.2.91 as well as older tool buttons. Missing
tools log the selection stage and visible mode labels, without logging drafts.
The selected mode is cleared through its close control before returning to chat.

Media results are also captured from the browser's own matching completion and
Qwen task-status responses. A task id must belong to the current prompt; the
gateway never submits another generation to retry a download. Native generation
overlays and video covers are excluded from finished image results. Downloads
retry up to three times, fall back to a browser fetch after transport errors,
and validate media bytes before saving. `IMAGE_RESPONSE_TIMEOUT` (default
300000 ms) controls Qwen image generation; video uses `VIDEO_RESPONSE_TIMEOUT`.
Client/proxy deadlines must also allow time for queueing and downloading.

The admin **Logs** window shows in-progress requests, interrupted streams, and
completed responses. Search covers all retained entries; **Load older requests**
continues past the first 500. Refresh returns to the newest page. Redacted records
persist in `LOG_DIR/requests.sqlite3` (the existing Docker logs mount), subject
to `LOG_RETENTION_DAYS` and `LOG_MAX_ENTRIES`; **Clear** clears disk and memory.
Pending entries at restart are marked interrupted. Logs lost by an older gateway
restart cannot be recovered. Headers with credentials and base64 media remain
redacted; request/response text is size-capped and multipart bodies are omitted.

Qwen text can also be read from the completed browser response for the exact
current prompt when its answer markup is not recognized. This observes the UI's
own request; it does not make a second generation request.

Offline regression tests: `python -m unittest discover -s tests -v`.
DOM fixtures use an isolated headless Chrome with no provider login; set
`TEST_BROWSER_PATH` for another Chromium executable. Live account/model/UI
availability still requires a smoke test against your running gateway.

All providers run at the same time. Each account you add belongs to one provider.

| Provider | Chat model(s) | Image model(s) | Video model(s) | Notes |
|---|---|---|---|---|
| **ChatGPT** | `chatgpt-browser` | `chatgpt-image` (DALL·E) | — | vision, files, tools |
| **Gemini** | `gemini-browser` + **every model in the app's picker** — `gemini-fast`, `gemini-balanced`, `gemini-thinking`, `gemini-pro`, `gemini-deep-think`, `gemini-deep-research` | `nano-banana`, `nano-banana-pro` | — | `-pro` runs the "Redo with Nano Banana Pro" upgrade (paid Google plans) |
| **Grok** | `grok-browser` + **every model/mode in the composer** — `grok-auto`, `grok-fast`, `grok-expert`, `grok-heavy`, `grok-4.1`, `grok-4`, `grok-3`, `grok-think`, `grok-deepsearch`, `grok-deepersearch` | `grok-imagine` | — | requires a signed-in account |
| **Claude** | `claude-browser` | — | — | no image generation |
| **DeepSeek** | `deepseek-browser` + **both composer switches** — `deepseek-think` (DeepThink), `deepseek-search`, `deepseek-think-search` | — | — | text only; no model dropdown — the switches *are* the model choice |
| **Qwen** (Alibaba) | `qwen-browser` + **every model in the picker** — `qwen3-max`, `qwen3-max-thinking`, `qwen3-plus`, `qwen3-flash`, `qwen3-coder`, `qwen3-vl`, `qwen3-omni`, plus `qwen-thinking`, `qwen-search`, `qwen-deep-research` | `qwen-image`, `qwen-image-edit` | `qwen-video` (Wan) | chat + image + video. Video modes: text-to-video and image-to-video only |
| **Seedream** (Dreamina) | — | `seedream-4.5`, `seedream-5.0-pro`, `seedream-5.0-lite`, `seedream-4.0`, `seedream-3.0` | `seedance-2.0`, `seedance-2.5`, `seedance-2.0-mini`, `seedance-2.0-fast`, `seedance-1.5-pro`, `video-3.0`, `video-3.0-pro`, … | **image + video** (no chat). Image via `/v1/images/generations`; video via `/v1/videos/generations` (modes: first+last frame, multiframe, omni reference). Needs a signed-in CapCut/Dreamina account (residential proxy recommended) |

`GET /v1/models` returns the live list; `GET /v1/chat/models` describes the chat models and flags the slow ones. Image/video generation for a model is only available if that account's plan allows it — Seedream draws on Dreamina credits.

### Picking a chat model (Gemini, Grok, DeepSeek & Qwen)

These four let you choose a model **per message** in their web UI, so the gateway exposes each of those choices as its own model id. Send it as `model` on any text endpoint and the browser sets that up — clicking the picker entry, flipping the composer switches — before typing your prompt:

```bash
curl -X POST http://localhost:8000/$KEY/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-thinking","messages":[{"role":"user","content":"Prove it."}]}'

curl -X POST http://localhost:8000/$KEY/v1/grok/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"grok-expert","messages":[{"role":"user","content":"Prove it."}]}'
```

| Model id | What it selects |
|---|---|
| `gemini-browser` / `grok-browser` | Whatever the account already had selected — the picker is not touched |
| `gemini-fast` · `gemini-balanced` · `gemini-thinking` · `gemini-pro` | The Gemini app's speed/quality tiers (`gemini-3-pro`, `gemini-2.5-flash`, … resolve to these too) |
| `gemini-deep-think` | Extended reasoning (Google AI Ultra) — **slow** |
| `gemini-deep-research` | The Deep Research tool; the gateway also clicks "Start research" for you — **slow** |
| `grok-auto` · `grok-fast` · `grok-expert` · `grok-heavy` | Grok's tiers (`grok-4-heavy`, `grok-4.1-fast`, … resolve to these too) |
| `grok-4.1` · `grok-4` · `grok-3` | A specific version, where the picker lists them by name |
| `grok-think` | Think mode on top of the current model |
| `grok-deepsearch` · `grok-deepersearch` | Agentic web search — **slow** |
| `deepseek-think` · `deepseek-search` · `deepseek-think-search` | DeepSeek's DeepThink and Search switches, alone or together (`deepseek-r1` resolves to `deepseek-think`). The combination is **slow** |
| `qwen3-max` · `qwen3-plus` · `qwen3-flash` · `qwen3-coder` · `qwen3-vl` · `qwen3-omni` | Qwen's model picker (`qwen-max`, `qwen-turbo`, … resolve to these) |
| `qwen3-max-thinking` | Qwen3-Max with the Thinking switch on |
| `qwen-thinking` · `qwen-search` | Thinking / web search on the current Qwen model |
| `qwen-deep-research` | Qwen's Deep Research — **slow** |

Two things worth knowing:

- **Selection is best-effort.** If a model isn't offered to that account (wrong plan) or the vendor renamed the entry, the request is still answered on whatever model the UI already had, and the gateway logs a warning — it never fails a request over the picker.
- **Switches don't leak between requests.** DeepThink, Search, Thinking and friends are *sticky* in these web UIs — they stay on for the tab. The gateway therefore switches off any mode a previous request turned on, so a plain `deepseek-browser` call is really plain.
- **The slow ones need a longer deadline.** Models marked *slow* above work for minutes before writing an answer, so they're held to `LONG_MODE_TIMEOUT` (default 900 000 ms) instead of `RESPONSE_TIMEOUT`.

---

## Endpoints

The API key is a **path prefix** (`/<API_TOKEN>/...`) — this keeps it out of query strings and the docs CDN. A header (`Authorization: Bearer <API_TOKEN>`) also works.

### Which provider serves a request

Two equivalent ways — pick whichever suits your client:

1. **By model id** (default OpenAI shape) — send `"model": "gemini-browser"` to `/v1/chat/completions`. Any unmodified OpenAI SDK works.
2. **By path prefix** — call `/v1/gemini/chat/completions`. Explicit and self-documenting.

`<P>` below is a provider id: `chatgpt` · `gemini` · `grok` · `claude` · `deepseek` · `qwen` · `seedream`.

### OpenAI-compatible

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/<key>/v1/models` | List every provider's chat + image + video models |
| `GET` | `/<key>/v1/<P>/models` | **New.** Just one provider's models (chat + image + video) |
| `GET` | `/<key>/v1/chat/models` | **New.** The chat models per provider, each with a description and a `long` flag (slow modes) |
| `GET` | `/<key>/v1/<P>/chat/models` | **New.** Same, for one provider |
| `POST` | `/<key>/v1/chat/completions` | Chat. Routed by `model`, which also picks the Gemini/Grok/DeepSeek/Qwen UI model. Vision, file attachments, tool/function calling. |
| `POST` | `/<key>/v1/<P>/chat/completions` | Same, provider fixed by the path |
| `POST` | `/<key>/v1/images/generations` | **Text → image.** `model` picks provider + variant (`chatgpt-image`, `nano-banana`, `nano-banana-pro`, `grok-imagine`, `qwen-image`, `seedream-4.5`/`seedream-5.0-pro`/…) |
| `POST` | `/<key>/v1/<P>/images/generations` | Same, provider fixed by the path |
| `POST` | `/<key>/v1/images/edits` | **Image + text → image** (`multipart/form-data`, repeat `image`) |
| `POST` | `/<key>/v1/<P>/images/edits` | Same, provider fixed by the path |
| `POST` | `/<key>/v1/videos/generations` | **Text/image → video** (Seedream, Qwen). `model` picks provider + variant — requests naming no video model still go to Seedream; `mode` picks text/image-to-video, first+last frame, multiframe, or omni reference |
| `POST` | `/<key>/v1/<P>/videos/generations` | Same, provider fixed by the path (`P` = `seedream` or `qwen`) |
| `POST` | `/<key>/v1/responses` | Responses API. Multi-turn via `previous_response_id`; `image_generation` tool. |
| `POST` | `/<key>/v1/<P>/responses` | Same, provider fixed by the path |
| `GET` | `/v1/files/{images\|videos}/{name}?sig=…` | Serves generated media. Returned in `data[].url`; **no key needed** (the signature is the credential), so it drops straight into `<img src>` / `<video src>` |

### Getting the generated image or video back

`response_format` decides how media comes back:

- **`"b64_json"`** — the bytes are inline in the response. Default for images. Nothing can expire.
- **`"url"`** — an absolute link served by this gateway. Default for videos, which are too big to base64 comfortably.

```jsonc
// POST /v1/videos/generations  →
{"data": [{
  "url": "http://your-gateway:8000/v1/files/videos/a_cat_walking_7f3a91c0de.mp4?sig=8664cc43…",
  "local_path": "/app/downloads/videos/a_cat_walking_7f3a91c0de.mp4",  // diagnostics only
  "mime_type": "video/mp4"
}]}
```

Fetch `url` as-is. It carries an HMAC of the file name keyed on `API_TOKEN`, so it needs no `Authorization` header and can be used directly as a `<video src>`; it cannot be guessed, and it only ever names a file this gateway generated.

**The link dies with the file** — generated media is deleted `IMAGE_RETENTION_MINUTES` (default 10) after it is written, and the URL then returns 404. Download it promptly, raise the retention, or ask for `b64_json`.

`local_path` is where the file sits on the gateway's own disk. It is **not** fetchable — before this was split out, that path was returned in `url`, and clients dutifully requested `GET /app/downloads/videos/….mp4` from the gateway and got a 404 for a video that had generated perfectly.

Behind a reverse proxy or tunnel, set **`PUBLIC_BASE_URL`** so the links point at the address your clients actually use (otherwise they are built from each request's `Host`, honouring `X-Forwarded-Proto`/`-Host`).

### Control plane & health

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/admin` | The control-panel page (also embeds the browser viewer) |
| `GET`/`POST`/`PATCH`/`DELETE` | `/admin/api/...` | Manage accounts & proxies (needs `ADMIN_TOKEN`, or `API_TOKEN` if unset) |
| `GET` | `/healthz` | Unauthenticated liveness (tab counts, queue depth) — never touches the browser |

### Legacy (kept for compatibility)

`POST /<key>/chat` · `POST /<key>/thread/new` · `POST /<key>/thread/{id}/chat` · `GET /<key>/threads` · `GET /<key>/status` — a simpler non-OpenAI chat surface. New integrations should use the `/v1/...` endpoints.

### Examples

```bash
# chat, routed by model id
curl -X POST http://localhost:8000/$KEY/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-browser","messages":[{"role":"user","content":"Hi"}]}'

# chat on a specific UI model (Gemini's thinking tier)
curl -X POST http://localhost:8000/$KEY/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-thinking","messages":[{"role":"user","content":"Why is the sky blue?"}]}'

# DeepSeek with reasoning + web search
curl -X POST http://localhost:8000/$KEY/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-think-search","messages":[{"role":"user","content":"What changed in the news today?"}]}'

# Qwen, provider fixed by the path
curl -X POST http://localhost:8000/$KEY/v1/qwen/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3-coder","messages":[{"role":"user","content":"Write a bash one-liner to dedupe a file."}]}'

# Qwen image generation (Qwen-Image)
curl -X POST http://localhost:8000/$KEY/v1/qwen/images/generations \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen-image","prompt":"a paper lantern festival at dusk, watercolor"}'

# Qwen image → video (Wan), 9:16
curl -X POST http://localhost:8000/$KEY/v1/qwen/videos/generations \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen-video","prompt":"slow pan, lanterns drifting upward","image":"data:image/png;base64,...","aspect_ratio":"9:16"}'

# what can this provider be asked for?
curl http://localhost:8000/$KEY/v1/grok/chat/models

# image generation via the provider path
curl -X POST http://localhost:8000/$KEY/v1/gemini/images/generations \
  -H "Content-Type: application/json" \
  -d '{"model":"nano-banana","prompt":"a red apple on a wooden table"}'

# image edit (multipart)
curl -X POST http://localhost:8000/$KEY/v1/images/edits \
  -F "prompt=make the sky a sunset" -F "image=@photo.png" -F "model=chatgpt-image"

# multi-turn (Responses API) — chain with previous_response_id
curl -X POST http://localhost:8000/$KEY/v1/gemini/responses \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-browser","input":"remember the number 7","previous_response_id":null}'

# text → video (Seedream / Dreamina)
curl -X POST http://localhost:8000/$KEY/v1/videos/generations \
  -H "Content-Type: application/json" \
  -d '{"model":"seedance-2.0","prompt":"a butterfly landing on a cherry blossom, cinematic push-in","aspect_ratio":"16:9","resolution":"1080p","duration_s":5}'

# first + last frame → interpolated video
curl -X POST http://localhost:8000/$KEY/v1/seedream/videos/generations \
  -H "Content-Type: application/json" \
  -d '{"model":"seedance-2.0","mode":"first_last_frame","prompt":"smooth dolly-in","first_frame":"data:image/png;base64,...","last_frame":"data:image/png;base64,..."}'
```

### Seedream (Dreamina) video modes

The mode is inferred from which image fields you set, or forced with `"mode"`. Image fields accept a base64 data URL or an http(s) URL.

| `mode` | Fields it uses | What it does |
|---|---|---|
| `text_to_video` | `prompt` only | Generate a clip from text |
| `image_to_video` | `image` (one still) | Animate a single image |
| `first_last_frame` | `first_frame` (+ optional `last_frame`) | Interpolate motion between a start and end still |
| `multiframe` | `frames` (2–10 keyframes, each `{image, transition_prompt?, duration_s?}`) | One continuous multi-shot clip through the keyframes |
| `omni_reference` | `reference_images` (up to ~12) | Keep a subject/style/motion consistent; tag refs as `@Image1`, `@Image2`, … in the prompt |

Common controls (best-effort — honored where the Dreamina UI exposes them): `duration_s`, `aspect_ratio` (`16:9`, `9:16`, `1:1`, `4:3`, `3:4`, `21:9`), `resolution` (`720p`, `1080p`), `audio`, `n`, `response_format` (`url` default, or `b64_json`).

### Qwen image & video

Qwen generates media from the same composer as chat. The gateway selects Image
Generation, Image Edit, or Video Generation, including entries behind Tools / +.
It verifies the selected tool before entering the prompt. Missing or ineffective
tool controls fail before sending; media requests cannot silently fall back to
ordinary chat. Required reference upload failures also stop the request.

After updating the gateway code, rebuild its container in the gateway directory:
`docker compose up -d --build`. Updating Miri alone does not update this automation.

| Model id | Endpoint | What it does |
|---|---|---|
| `qwen-image` | `/v1/images/generations` | Text → image (Qwen-Image) |
| `qwen-image-edit` | `/v1/images/edits` | Image + text → image (Qwen-Image-Edit). Used **automatically** whenever reference images are attached, whichever image id you send |
| `qwen-video` | `/v1/videos/generations` | Text → video, or **one** start `image` → video (Wan). `wan` resolves here |

- **Video modes:** `text_to_video` and `image_to_video` only. `first_frame`/`frames`/`reference_images` get a `400` up front — those modes are Seedream's.
- **Controls:** `aspect_ratio` (`16:9`, `9:16`, `1:1`, `4:3`, `3:4`) is selected in the UI; Qwen exposes no duration, resolution or audio controls, so those are ignored (and logged as such).
- **Deadlines:** images use `RESPONSE_TIMEOUT`, video uses `VIDEO_RESPONSE_TIMEOUT`.
- **Modes don't leak:** the media mode is switched back off afterwards, so a tab that just made a picture answers the next chat request as chat.

---

## Quick Start

### Docker (recommended)

```bash
git clone <your-repo-url>
cd miri-api

cp .env.example .env
# Edit .env — set API_TOKEN (and optionally ADMIN_TOKEN, MAX_CONCURRENT_REQUESTS)

docker compose up --build -d
```

Then open the **admin panel** and log in your accounts:

1. Go to **http://localhost:8000/admin** (or the panel is also injected into **http://localhost:6080/vnc.html**). Enter your `API_TOKEN`/`ADMIN_TOKEN`.
2. Pick a **provider tab** (ChatGPT / Gemini / Grok / Claude / DeepSeek / Qwen / Seedream) → **Add account** → **Start** → **Log in** (opens the provider's login page in the viewer) → sign in → the account becomes active automatically (**I've logged in** remains a manual fallback).
   - Use **email + password** or Microsoft / Apple / a magic link. Some providers block automated Google/OAuth logins.
3. Send a request:

```bash
curl -H "Authorization: Bearer $API_TOKEN" http://localhost:8000/v1/models
```

Your logins persist in a Docker volume across restarts. Startup waits for the
provider page to restore its session. The account monitor rechecks pending logins
every few seconds, including sign-ins completed through noVNC in another tab of
the same account. You do not need to confirm an already restored login after a
VPS restart. Expired sessions still need sign-in; explicit logout, stopped/disabled
accounts and saved cooldowns are respected.

---

## The admin panel

Available at **`http://localhost:8000/admin`** and as an overlay inside the noVNC viewer on `:6080`.

- **Provider tabs** — each tab shows only that provider's accounts; switching tabs also brings that provider's browser windows to the front of the viewer.
- **Per account** — Start / Stop, **Log in** / **Log out**, **Edit** (a form, same as adding: provider, label, tabs, proxy slot, soft cap, order, proxy override), and **View / t1 / t2 …** to focus a specific account/tab in the viewer. The account **ID and timezone are assigned automatically** — timezone geo-detects from the account's proxy exit.
- **Proxy pool** — one editable list; account *N* uses proxy *((N‑1) mod count)+1* (wraps). Set proxies here, not in `.env`.
- **Live usage** — per-account request counts, limit hits and cooldowns (observed estimates — the web UIs expose no real quota).
- **CPU / RAM bars** — how full the *container* is (cgroup limits, not the host's totals), with the browsers' own process count and memory called out separately. That is the number that predicts an OOM kill, and it's the first thing to check when responses start timing out. Also shown compactly in the noVNC overlay.
- **Logs → ⬇ Download** — writes one JSON file holding the last *N* requests **in full** (headers and bodies, media stripped, keys redacted) plus, with **+server logs** ticked, the tail of every file in `LOG_DIR`. Pick the count from the dropdown; the table's filter applies to the download too. Use this instead of screenshotting the table — a single request touches several log files (a Qwen video writes to `qwen_client`, `qwen_detector`, `qwen_media_mode`, `worker_pool` and `openai_routes`), which is why reading just one of them looks like logs are missing.

The control plane is gated by `ADMIN_TOKEN` (falls back to `API_TOKEN`), and hard-refuses if neither is set.

---

## Accounts, Proxies & Scaling

- **Concurrency** — `MAX_CONCURRENT_REQUESTS` sets tabs per account; each tab serves one request at a time. Requests are never mixed between tabs, accounts, or providers.
- **Multiple accounts** — add several logged-in accounts per provider. Requests are shared round-robin at the account level for each provider/model, so an account with extra tabs does not receive a larger share of sequential requests.
- **Auto-switch and retry** — when an account hits a usage limit or a request fails, it is removed from routing and a stateless request is retried on the next eligible account of the same provider/model. Accounts that hit a limit return automatically after their cooldown; failed accounts stay offline until they are restarted or logged in again.
- **Conversation chaining** is pinned to the owning account (a thread's cookies live in one profile); an unavailable owner returns `409` rather than answering with no context.
- **Proxies** — each account can route through its own proxy via the pool (residential/sticky recommended; set the account timezone to match the exit region).

Key `.env` settings: `API_TOKEN`, `ADMIN_TOKEN`, `MAX_CONCURRENT_REQUESTS`, `ACCOUNT_SOFT_CAP`, `IMAGE_RETENTION_MINUTES`, `RESPONSE_TIMEOUT`, `LONG_MODE_TIMEOUT`, `GEMINI_USE_NANO_BANANA_PRO`. See [.env.example](.env.example) for all of them.

---

## How it works

```
Your app (OpenAI SDK / LangChain / curl)
   │
   ▼
miri-api  (FastAPI on :8000)
   │   routes by provider, pins conversations, sheds load with 429
   ▼
Worker pool  →  M browser contexts (one per account/login) × N tabs
   │
   ▼
chatgpt.com · gemini.google.com · grok.com · claude.ai
chat.deepseek.com · chat.qwen.ai · dreamina.capcut.com   (your logged-in sessions)
   │
   ▼
Response scraped from the page → OpenAI-shaped JSON → your app
```

One process, one Playwright driver, one Xvfb display. Each account is an isolated browser profile; a request checks out exactly one tab for its whole life and never crosses accounts or providers.

> **Honest limits.** This drives real web UIs, so throughput is bounded by each account's own rate limits and by a shared browser display — adding accounts buys quota headroom and resilience, not linear speedup. Usage numbers are observed estimates, and some providers actively challenge automated logins. Use responsibly and within each provider's terms.

---

## Project layout

```
src/
  core/        config, logging, janitor, clipboard lock, browser/
  providers/   base registry + model_picker/text_client/dom_detector/media_fetch (shared)
               + chatgpt/ gemini/ grok/ claude/ deepseek/ qwen/ seedream/
  accounts/    account manager, registry, usage/limits, proxy pool, monitor
  api/         server, routes, worker pool, admin panel
docker/        entrypoint, supervisord, noVNC panel + no-cache server
```

## License

See [LICENSE](LICENSE).
