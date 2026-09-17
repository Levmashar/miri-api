# API Reference

miri-api exposes an OpenAI-compatible API. Any client that works with the OpenAI API works here.

---

## Table of Contents

- [Base URL](#base-url)
- [Authentication](#authentication)
- [OpenAI-Compatible Endpoints](#openai-compatible-endpoints)
  - [Chat Completions](#chat-completions)
  - [Tool / Function Calling](#tool--function-calling)
  - [Image Input (Vision)](#image-input-vision)
  - [File Attachments](#file-attachments)
  - [Image Generation (ChatGPT only)](#image-generation-chatgpt-only)
  - [Choosing a Chat Model (Gemini, Grok, DeepSeek & Qwen)](#choosing-a-chat-model-gemini-grok-deepseek--qwen)
  - [Qwen Image & Video Generation](#qwen-image--video-generation)
  - [List Models](#list-models)
- [Custom REST API](#custom-rest-api)
- [TUI Terminal Client](#tui-terminal-client)
- [Provider Differences](#provider-differences)

---

## Base URL

```
http://localhost:8000/v1
```

## Authentication

Include the Bearer token (default `dummy123`) in every request:

```bash
Authorization: Bearer dummy123
```

With the OpenAI SDK:

```python
client = OpenAI(base_url="http://localhost:8000/v1", api_key="dummy123")
```

Open paths (no auth needed): `/docs`, `/redoc`, `/openapi.json`, `/healthz`

---

## OpenAI-Compatible Endpoints

### Chat Completions

**`POST /v1/chat/completions`**

Standard OpenAI chat completion request.

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="dummy123")

response = client.chat.completions.create(
    model="claude-browser",  # or "chatgpt-browser"
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is quantum computing?"}
    ]
)
print(response.choices[0].message.content)
```

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dummy123" \
  -d '{
    "model": "claude-browser",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

**Request body:**

| Field | Type | Required | Description |
|---|---|---|---|
| `model` | string | yes | `claude-browser` or `chatgpt-browser` |
| `messages` | array | yes | Array of message objects |
| `tools` | array | no | Tool/function definitions |
| `tool_choice` | string/object | no | `auto`, `none`, `required`, or specific function |
| `temperature` | float | no | Ignored (browser controls this) |
| `max_tokens` | int | no | Ignored |
| `stream` | bool | no | Must be `false` (streaming not supported) |

**Response:**

```json
{
  "id": "chatcmpl-abc123...",
  "object": "chat.completion",
  "created": 1716025800,
  "model": "claude-browser",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Quantum computing uses quantum bits...",
        "tool_calls": null
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 25,
    "completion_tokens": 150,
    "total_tokens": 175
  }
}
```

---

### Tool / Function Calling

Define tools in the request and the model will call them when appropriate.

**Request with tools:**

```python
response = client.chat.completions.create(
    model="claude-browser",
    messages=[{"role": "user", "content": "What's the weather in Paris?"}],
    tools=[{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather for a city",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name"}
                },
                "required": ["city"]
            }
        }
    }]
)
```

**Response when model calls a tool:**

```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": null,
      "tool_calls": [
        {
          "id": "call_a1b2c3d4e5f6...",
          "type": "function",
          "function": {
            "name": "get_weather",
            "arguments": "{\"city\": \"Paris\"}"
          }
        }
      ]
    },
    "finish_reason": "tool_calls"
  }]
}
```

**Sending tool results back:**

```python
# After executing the tool, send the result back
response = client.chat.completions.create(
    model="claude-browser",
    messages=[
        {"role": "user", "content": "What's the weather in Paris?"},
        {"role": "assistant", "tool_calls": [
            {"id": "call_a1b2c3...", "type": "function",
             "function": {"name": "get_weather", "arguments": "{\"city\": \"Paris\"}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_a1b2c3...", "content": "Sunny, 25C"}
    ]
)
# Model responds with natural language summary
```

**LangChain example (full round-trip):**

```python
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool

@tool
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"Sunny, 25C in {city}"

@tool
def add_numbers(a: int, b: int) -> str:
    """Add two numbers together."""
    return str(a + b)

llm = ChatOpenAI(model="claude-browser", base_url="http://localhost:8000/v1", api_key="dummy123")
llm_with_tools = llm.bind_tools([get_weather, add_numbers])

# Step 1: Model decides to call tools
response = llm_with_tools.invoke([
    HumanMessage(content="Weather in Tokyo and what's 42+58?")
])

# Step 2: Execute tools and send results
messages = [HumanMessage(content="Weather in Tokyo and what's 42+58?"), response]
tool_map = {"get_weather": get_weather, "add_numbers": add_numbers}

for tc in response.tool_calls:
    result = tool_map[tc["name"]].invoke(tc["args"])
    messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))

# Step 3: Model summarizes results
final = llm_with_tools.invoke(messages)
print(final.content)
# "It's sunny and 25C in Tokyo, and 42 + 58 = 100."
```

**`tool_choice` options:**

| Value | Behavior |
|---|---|
| `"auto"` (default) | Model decides whether to call tools or answer directly |
| `"required"` | Model must call at least one tool |
| `"none"` | Tools are ignored, model answers directly |
| `{"type":"function","function":{"name":"X"}}` | Model must call the specified function |

---

### Image Input (Vision)

Send images using the standard OpenAI vision format.

```python
import base64

with open("photo.png", "rb") as f:
    img_b64 = base64.b64encode(f.read()).decode()

response = client.chat.completions.create(
    model="claude-browser",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "Describe this image in detail."},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
        ]
    }]
)
```

**Multiple images:**

```python
response = client.chat.completions.create(
    model="claude-browser",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "Compare these two images."},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img1_b64}"}},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img2_b64}"}},
        ]
    }]
)
```

HTTP URLs also work:

```python
{"type": "image_url", "image_url": {"url": "https://example.com/photo.jpg"}}
```

---

### File Attachments

Send PDFs, DOCX, TXT, CSV, and other files via a custom `file` content type.

```python
import base64

with open("document.pdf", "rb") as f:
    pdf_b64 = base64.b64encode(f.read()).decode()

response = client.chat.completions.create(
    model="claude-browser",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "Summarize this PDF."},
            {"type": "file", "file": {
                "filename": "document.pdf",
                "data": pdf_b64,
                "mime_type": "application/pdf"
            }},
        ]
    }]
)
```

Alternative data-URL format:

```json
{"type": "file", "file": {"filename": "doc.pdf", "url": "data:application/pdf;base64,..."}}
```

---

### Image Generation (ChatGPT only)

**`POST /v1/images/generations`**

Generate images via DALL-E. Only available when `PROVIDER=chatgpt`. Returns HTTP 501 for Claude.

```python
response = client.images.generate(
    model="dall-e-3",
    prompt="A cyberpunk cat hacking a mainframe",
    n=1,
    size="1024x1024",
    response_format="b64_json",
)

# Save the image
import base64
with open("output.png", "wb") as f:
    f.write(base64.b64decode(response.data[0].b64_json))
```

```bash
curl -X POST http://localhost:8000/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dummy123" \
  -d '{"prompt": "A cat in space", "n": 1, "response_format": "b64_json"}'
```

**Request parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `prompt` | string | required | Text description of the image |
| `model` | string | `dall-e-3` | Model name (ignored, uses ChatGPT's DALL-E) |
| `n` | int | `1` | Number of images (1-4) |
| `size` | string | `1024x1024` | Requested size (hint to ChatGPT) |
| `quality` | string | `standard` | `standard` or `hd` |
| `style` | string | `vivid` | `vivid` or `natural` |
| `response_format` | string | `b64_json` | `b64_json` (bytes inline) or `url` (an absolute link this gateway serves — see [Getting media back](#getting-media-back)) |

---

### Getting media back

`response_format` decides how a generated image or video reaches you.

**`b64_json`** (default for images) — the bytes are inline in the response:

```jsonc
{"data": [{"b64_json": "iVBORw0KGgo...", "local_path": "/app/downloads/images/cat_7f3a91c0de.png"}]}
```

**`url`** (default for videos, which are large) — an absolute link served by
this gateway:

```jsonc
{"data": [{
  "url": "http://localhost:8000/v1/files/videos/waves_at_sunset_7f3a91c0de.mp4?sig=8664cc43...",
  "local_path": "/app/downloads/videos/waves_at_sunset_7f3a91c0de.mp4",
  "mime_type": "video/mp4",
  "duration_s": 5.0
}]}
```

Fetch `url` exactly as given. It carries an HMAC of the file name keyed on
`API_TOKEN`, so:

- it needs **no** `Authorization` header and works as an `<img src>` /
  `<video src>` straight out of the response;
- it cannot be guessed, and only ever names a file this gateway generated;
- it stops working when the file is swept — `IMAGE_RETENTION_MINUTES` after the
  file was written (default 10). After that the route returns `404` with a
  message saying so. Fetch promptly, raise the retention, or use `b64_json`.

`local_path` is where the file sits on the gateway's own disk. It is reported
for diagnostics and **is not fetchable** — a client that requests it gets
`GET /app/downloads/videos/....mp4 -> 404`.

Behind a reverse proxy or tunnel, set `PUBLIC_BASE_URL` so links point at the
address clients actually use; otherwise they are built from each request's
`Host`, honouring `X-Forwarded-Proto` / `X-Forwarded-Host`.

---

### Choosing a Chat Model (Gemini, Grok, DeepSeek & Qwen)

The Gemini, Grok, DeepSeek and Qwen web UIs let you pick a model per message, so
each of those choices is exposed as its own model id. Pass it as `model` on
`/v1/chat/completions` or `/v1/responses`; the browser clicks that entry in the
picker before typing the prompt. Every other provider serves a single chat model
and ignores the extra ids.

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" -H "Authorization: Bearer dummy123" \
  -d '{"model": "gemini-thinking", "messages": [{"role": "user", "content": "Prove it."}]}'
```

| Provider | Model ID | Selects | Slow? |
|---|---|---|---|
| Gemini | `gemini-browser` | Whatever the account has selected (picker untouched) | |
| Gemini | `gemini-fast` | The Flash/Fast tier | |
| Gemini | `gemini-balanced` | The middle tier, where the app offers one | |
| Gemini | `gemini-thinking` | The reasoning tier | |
| Gemini | `gemini-pro` | The Pro tier (paid Google AI plans) | |
| Gemini | `gemini-deep-think` | Extended reasoning (Google AI Ultra) | yes |
| Gemini | `gemini-deep-research` | Deep Research, incl. the "Start research" confirmation | yes |
| Grok | `grok-browser` | Whatever the account has selected (picker untouched) | |
| Grok | `grok-auto` | Grok routes the request itself | |
| Grok | `grok-fast` | Fastest tier | |
| Grok | `grok-expert` | Reasoning tier | |
| Grok | `grok-heavy` | Multi-agent tier (SuperGrok Heavy) | yes |
| Grok | `grok-4.1`, `grok-4`, `grok-3` | A named version, where the picker lists them | |
| Grok | `grok-think` | Think mode on top of the current model | |
| Grok | `grok-deepsearch`, `grok-deepersearch` | Agentic web search | yes |
| DeepSeek | `deepseek-browser` | Plain chat — both composer switches off | |
| DeepSeek | `deepseek-think` | The DeepThink switch (reasoning model) | |
| DeepSeek | `deepseek-search` | The Search switch (web grounding) | |
| DeepSeek | `deepseek-think-search` | Both switches at once | yes |
| Qwen | `qwen-browser` | Whatever the account has selected (picker untouched) | |
| Qwen | `qwen3-max`, `qwen3-plus`, `qwen3-flash` | Qwen's speed/quality tiers | |
| Qwen | `qwen3-coder`, `qwen3-vl`, `qwen3-omni` | The specialised models | |
| Qwen | `qwen3-max-thinking` | Qwen3-Max with the Thinking switch on | |
| Qwen | `qwen-thinking`, `qwen-search` | Thinking / web search on the current model | |
| Qwen | `qwen-deep-research` | Qwen's Deep Research | yes |

Version-explicit spellings resolve to the same entries — `gemini-3-pro` and
`gemini-2.5-pro` both mean `gemini-pro`, `grok-4-heavy` means `grok-heavy`,
`deepseek-r1` means `deepseek-think`, `qwen-max` means `qwen3-max`.

DeepSeek and Qwen express part (DeepSeek: all) of their model choice as composer
switches, which those UIs keep switched on for the tab. The gateway therefore
turns off any switch a previous request on that tab turned on, so a request for
`deepseek-browser` is answered with DeepThink and Search actually off.

Two rules govern selection:

- **Best-effort.** An entry the account's plan does not offer (or one the vendor
  has renamed) is skipped with a logged warning and the request still runs on
  whatever model the UI already had. An **unrecognised** model id never switches
  anything either — it falls back to the account's current model rather than
  guessing.
- **Slow models get a longer deadline.** The ones marked *slow* above run for
  minutes, so they are held to `LONG_MODE_TIMEOUT` (default `900000` ms) instead
  of `RESPONSE_TIMEOUT`.

---

### Qwen Image & Video Generation

Qwen (chat.qwen.ai) serves the standard media endpoints. The gateway switches
the composer into the matching mode, sends, waits for the new result to
appear, downloads it, and switches the mode back off.

| Model ID | Endpoint | Input |
|---|---|---|
| `qwen-image` | `POST /v1/images/generations` (or `/v1/qwen/images/generations`) | prompt |
| `qwen-image-edit` | `POST /v1/images/edits` (or `/v1/qwen/images/edits`) | prompt + image(s) — chosen automatically whenever images are attached |
| `qwen-video` | `POST /v1/videos/generations` (or `/v1/qwen/videos/generations`) | prompt, optionally one `image` |

```bash
curl -X POST http://localhost:8000/v1/qwen/videos/generations \
  -H "Content-Type: application/json" -H "Authorization: Bearer dummy123" \
  -d '{"model": "qwen-video", "prompt": "waves at sunset, drone shot", "aspect_ratio": "16:9"}'
```

- Video `mode` must be `text_to_video` or `image_to_video`; `first_frame`,
  `frames` and `reference_images` return `400` before any work starts.
- `aspect_ratio` is selected in the UI (`16:9`, `9:16`, `1:1`, `4:3`, `3:4`);
  `duration_s`, `resolution` and `audio` have no Qwen control and are ignored.
- A video request that names no video model and no provider path still goes
  to Seedream, as before.
- No media back → `422` with Qwen's own reply; a usage-limit message → `503`
  and the account cools down.

---

### List Models

**`GET /v1/models`**

Every model every enabled provider can serve — chat, image and video — in
OpenAI's `ModelObject` shape.

```bash
curl http://localhost:8000/v1/models -H "Authorization: Bearer dummy123"
```

**`GET /v1/{provider}/models`**

The same list narrowed to one provider (`chatgpt` · `gemini` · `grok` ·
`claude` · `deepseek` · `qwen` · `seedream`). Unknown provider → `404`.

```bash
curl http://localhost:8000/v1/grok/models -H "Authorization: Bearer dummy123"
```

**`GET /v1/chat/models`** · **`GET /v1/{provider}/chat/models`**

The chat models per provider, with what each one is for and whether it is one of
the slow modes — the detail an OpenAI `ModelObject` has nowhere to put.

```bash
curl http://localhost:8000/v1/gemini/chat/models -H "Authorization: Bearer dummy123"
```

```json
{
  "object": "list",
  "data": [
    {
      "provider": "gemini",
      "label": "Gemini",
      "default": "gemini-browser",
      "models": [
        {"id": "gemini-browser", "description": "Whatever model the account currently has selected (no switching)", "long": false},
        {"id": "gemini-thinking", "description": "Reasoning tier — slower, better on hard problems", "long": false},
        {"id": "gemini-deep-research", "description": "Multi-step web research; answers in minutes, not seconds", "long": true}
      ]
    }
  ]
}
```

---

## Custom REST API

In addition to the OpenAI-compatible endpoints, miri-api exposes a simpler custom API:

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/chat` | Send a message in the current conversation |
| `POST` | `/thread/new` | Start a new conversation |
| `POST` | `/thread/{id}/chat` | Send a message in a specific thread |
| `GET` | `/threads` | List recent threads |
| `GET` | `/status` | Health check, login status, current thread |

```bash
# Chat in current thread
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dummy123" \
  -d '{"message": "Hello!"}'

# Start new thread
curl -X POST http://localhost:8000/thread/new \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dummy123" \
  -d '{"message": "New conversation"}'

# Check status
curl -H "Authorization: Bearer dummy123" http://localhost:8000/status
```

---

## TUI Terminal Client

miri-api includes a terminal chat interface with a cyberpunk theme, built with Textual.

```bash
python -m src.cli.app
```

### Commands

| Command | Description |
|---|---|
| `/new` | Start a fresh conversation |
| `/threads` | List recent threads |
| `/thread <id>` | Switch to a thread |
| `/images` | List downloaded DALL-E images |
| `/status` | Connection details |
| `/clear` | Clear chat display |
| `/help` | Show commands |
| `/exit` | Quit |

Shortcuts: `Ctrl+N` (new), `Ctrl+T` (threads), `Ctrl+L` (clear), `Ctrl+Q` (quit)

---

## Provider Differences

| Behavior | Claude | ChatGPT |
|---|---|---|
| Model ID | `claude-browser` | `chatgpt-browser` |
| Image generation | Not supported (501) | Supported (DALL-E) |
| Table rendering | Tab-separated text | Markdown with pipes |
| Avg response time | 15-20s | 7-10s |
| Tool calling prompt | Collaborative framing | Direct instruction |
| `tool_choice` support | Yes | Yes |
| Vision input | Yes | Yes |
| File attachments | Yes | Yes |
