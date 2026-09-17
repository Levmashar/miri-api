"""
OpenAI-compatible Pydantic schemas for /v1/chat/completions and /v1/models.

Mirrors the OpenAI Chat Completions API specification so that any OpenAI SDK
or LangChain client can talk to our browser-backed ChatGPT endpoint.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


# ── Tool / Function definitions ─────────────────────────────────


class FunctionDefinition(BaseModel):
    """Schema for a function the model may call."""
    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolDefinition(BaseModel):
    """A tool the model may use (only 'function' type supported)."""
    type: str = "function"
    function: FunctionDefinition


class FunctionCallInfo(BaseModel):
    """Info about a specific function call made by the model."""
    name: str
    arguments: str  # JSON string


class ToolCall(BaseModel):
    """A tool call returned by the model."""
    id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex[:24]}")
    type: str = "function"
    function: FunctionCallInfo


# ── Messages ────────────────────────────────────────────────────


class ChatMessage(BaseModel):
    """A single message in the conversation.
    
    Content can be:
    - A simple string
    - A list of content parts (OpenAI vision format + file attachments):
      [
        {"type": "text", "text": "..."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
        {"type": "file", "file": {"filename": "doc.pdf", "data": "<base64>", "mime_type": "application/pdf"}}
      ]
    """
    role: str  # system | user | assistant | tool
    content: Optional[Union[str, List[Any]]] = None
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[list[ToolCall]] = None


# ── Request ─────────────────────────────────────────────────────


class ChatCompletionRequest(BaseModel):
    """OpenAI-compatible chat completion request body."""
    model: str = "chatgpt-browser"
    messages: list[ChatMessage]
    tools: Optional[list[ToolDefinition]] = None
    tool_choice: Optional[Union[str, dict]] = None  # "auto" | "none" | {"type":"function","function":{"name":"..."}}
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    stop: Optional[Union[str, List[str]]] = None
    stream: Optional[bool] = False
    n: Optional[int] = 1
    user: Optional[str] = None


# ── Response ────────────────────────────────────────────────────


class UsageInfo(BaseModel):
    """Token usage (estimated — we don't have real token counts)."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChoiceMessage(BaseModel):
    """The assistant's message in a choice."""
    role: str = "assistant"
    content: Optional[str] = None
    tool_calls: Optional[list[ToolCall]] = None


class Choice(BaseModel):
    """A single completion choice."""
    index: int = 0
    message: ChoiceMessage
    finish_reason: str = "stop"  # "stop" | "tool_calls"


class ChatCompletionResponse(BaseModel):
    """OpenAI-compatible chat completion response."""
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:24]}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = "chatgpt-browser"
    choices: list[Choice]
    usage: UsageInfo = Field(default_factory=UsageInfo)


# ── Models endpoint ─────────────────────────────────────────────


class ModelObject(BaseModel):
    """A model object for /v1/models."""
    id: str
    object: str = "model"
    created: int = 1700000000
    owned_by: str = "chatgpt"


class ModelListResponse(BaseModel):
    """Response for GET /v1/models."""
    object: str = "list"
    data: list[ModelObject]


# ── Image Generation ────────────────────────────────────────────


class ImageGenerationRequest(BaseModel):
    """OpenAI-compatible image generation request (POST /v1/images/generations).

    Every documented OpenAI param is accepted so unmodified SDK clients work.
    The backend is the ChatGPT web UI, which exposes no knobs for most of them —
    see IMAGE_PARAM_SUPPORT in openai_routes.py for exactly which are honored,
    which become prompt hints, which are post-processed by us, and which are
    ignored. Unsupported values are never silently pretended into the response:
    the response echoes what was ACTUALLY produced.
    """
    prompt: str = Field(max_length=32000)
    # Reference image(s) uploaded to ChatGPT alongside the prompt, so the model
    # generates WITH them as visual context (style/subject reference). Each entry
    # is a base64 data URL ("data:image/png;base64,...") or an http(s) URL.
    # Accepts a single string or a list. This is an extension over the stock
    # OpenAI generations endpoint, which takes no images.
    image: Optional[Union[str, List[str]]] = None
    model: Optional[str] = "chatgpt-image-latest"
    n: Optional[int] = Field(default=1, ge=1, le=10)
    size: Optional[str] = "auto"          # auto|1024x1024|1536x1024|1024x1536|WxH
    quality: Optional[str] = "auto"       # low|medium|high|auto
    background: Optional[str] = "auto"    # transparent|opaque|auto
    output_format: Optional[str] = "png"  # png|jpeg|webp
    output_compression: Optional[int] = Field(default=None, ge=0, le=100)
    moderation: Optional[str] = "auto"    # auto|low
    stream: Optional[bool] = False
    partial_images: Optional[int] = Field(default=0, ge=0, le=3)
    # Legacy/dall-e params kept so older clients keep working.
    style: Optional[str] = None           # vivid|natural
    response_format: Optional[str] = "b64_json"  # "url" or "b64_json"
    user: Optional[str] = None


class ImageData(BaseModel):
    """A single generated image in the response."""
    # An absolute, directly fetchable URL served by THIS gateway
    # (…/v1/files/images/<name>?sig=…). It needs no Authorization header, so it
    # can be used as an <img src> as-is, and it stops working when the file is
    # swept (IMAGE_RETENTION_MINUTES).
    url: Optional[str] = None
    b64_json: Optional[str] = None
    # Where the file sits on the gateway's own disk. Diagnostics only — it is
    # NOT fetchable; earlier versions returned this in `url`, which is why
    # clients saw 404s for /app/downloads/….
    local_path: Optional[str] = None
    revised_prompt: Optional[str] = None


class ImageUsageDetails(BaseModel):
    """Token breakdown for image requests.

    ESTIMATED. The web UI never reports token usage, so these are derived from
    prompt/image size rather than measured. Treat as indicative, not billing-grade.
    """
    image_tokens: int = 0
    text_tokens: int = 0


class ImageUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    input_tokens_details: ImageUsageDetails = Field(default_factory=ImageUsageDetails)


class ImagesResponse(BaseModel):
    """OpenAI-compatible image generation response.

    background/output_format/quality/size report what was ACTUALLY produced,
    not what was requested.
    """
    created: int = Field(default_factory=lambda: int(time.time()))
    data: List[ImageData]
    background: Optional[str] = None
    output_format: Optional[str] = None
    quality: Optional[str] = None
    size: Optional[str] = None
    usage: Optional[ImageUsage] = None


# ── Video Generation (Seedream / Dreamina) ──────────────────────
#
# Dreamina exposes several distinct generation MODES, each with different image
# inputs. These endpoints surface every mode as explicit, typed fields so a
# caller controls exactly which one runs (see VIDEO_MODES / _resolve_video_mode
# in openai_routes.py):
#
#   text_to_video     prompt only, no images
#   image_to_video    one start image (`image`)
#   first_last_frame  a start image (`first_frame`) + end image (`last_frame`)
#   multiframe        2–10 keyframes (`frames`), with per-transition prompts
#   omni_reference    up to ~9 reference images (`reference_images`) with @-tags
#                     resolved from the prompt for subject/style/motion consistency
#
# An image field is a base64 data URL ("data:image/png;base64,...") or an
# http(s) URL. The mode is inferred from which fields are set, or forced with
# `mode`.


class VideoKeyframe(BaseModel):
    """One keyframe in a `multiframe` request.

    `image` is the still for this frame. `transition_prompt` describes the motion
    INTO the NEXT frame (Dreamina attaches prompts to the gap between frames, not
    to a frame itself), so it is ignored on the final frame. `duration_s` is this
    frame's on-screen time (Dreamina allows ~1–6s per keyframe)."""
    image: str = Field(description="data URL or http(s) URL of the keyframe still")
    transition_prompt: Optional[str] = Field(
        default=None, description="motion prompt from this frame to the next"
    )
    duration_s: Optional[float] = Field(default=None, ge=0.5, le=10)


class VideoGenerationRequest(BaseModel):
    """Request body for POST /v1/videos/generations (Seedream / Dreamina).

    Only `prompt` is always meaningful; which image fields you set selects the
    mode. Setting fields for more than one mode is an error unless you also set
    `mode` to disambiguate.
    """
    model: Optional[str] = None            # a Seedream video model id; default if omitted
    prompt: str = Field(default="", max_length=32000)
    # Force a specific mode. Omit to infer from the image fields.
    mode: Optional[str] = None             # text_to_video|image_to_video|first_last_frame|multiframe|omni_reference

    # image_to_video: a single start image (list accepted; first is used).
    image: Optional[Union[str, List[str]]] = None
    # first_last_frame: start and end stills. last_frame may be omitted (the UI
    # allows first-frame-only) — then it degrades to image_to_video semantics.
    first_frame: Optional[str] = None
    last_frame: Optional[str] = None
    # multiframe: ordered keyframes (2–10) with per-transition prompts.
    frames: Optional[List[VideoKeyframe]] = None
    # omni_reference: reference images the prompt tags as @Image1, @Image2, ...
    # (subject / style / motion consistency).
    reference_images: Optional[List[str]] = None

    # Common generation controls (best-effort — honored only where the UI exposes
    # a control; see VIDEO_PARAM_SUPPORT). The response reports what was produced.
    duration_s: Optional[float] = Field(default=None, ge=1, le=60)
    aspect_ratio: Optional[str] = None     # "16:9" | "9:16" | "1:1" | "4:3" | "3:4" | "21:9"
    resolution: Optional[str] = None       # "480p" | "720p" | "1080p"
    audio: Optional[bool] = None           # generate audio track if the model supports it
    seed: Optional[int] = None
    n: Optional[int] = Field(default=1, ge=1, le=4)
    response_format: Optional[str] = "url"  # "url" (default; videos are large) | "b64_json"
    user: Optional[str] = None


class VideoData(BaseModel):
    """A single generated video in the response."""
    # Absolute URL served by this gateway (…/v1/files/videos/<name>?sig=…).
    # Usable directly as a <video src> — no Authorization header needed — and
    # valid until the janitor sweeps the file (IMAGE_RETENTION_MINUTES).
    url: Optional[str] = None              # (response_format="url")
    b64_json: Optional[str] = None         # base64 mp4 (response_format="b64_json")
    local_path: Optional[str] = None       # gateway-side path; diagnostics, NOT fetchable
    revised_prompt: Optional[str] = None
    mime_type: str = "video/mp4"
    duration_s: Optional[float] = None


class VideosResponse(BaseModel):
    """Response for POST /v1/videos/generations.

    `model` and `mode` echo what actually ran; `size`/`duration_s` report the
    produced clip, not the request."""
    created: int = Field(default_factory=lambda: int(time.time()))
    data: List[VideoData]
    model: Optional[str] = None
    mode: Optional[str] = None
    size: Optional[str] = None
    duration_s: Optional[float] = None
    usage: Optional[ImageUsage] = None


# ── Responses API (/v1/responses) ───────────────────────────────


class ResponsesToolDefinition(BaseModel):
    """Flat tool definition used by the Responses API.

    Unlike the Chat Completions API which nests under `function:`,
    the Responses API uses a flat format:
      {"type": "function", "name": "...", "parameters": {...}, "description": "..."}

    Built-in tools are NOT function tools and carry no `name` — e.g.
      {"type": "image_generation", "quality": "high", "partial_images": 2}
    so `name` is optional and extra keys are kept rather than rejected.
    """
    model_config = ConfigDict(extra="allow")

    type: str = "function"
    # Required in practice for type="function", but absent on built-in tools
    # like image_generation. Validated in the route, where the type is known.
    name: Optional[str] = None
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    strict: Optional[bool] = None


class ResponsesInputMessage(BaseModel):
    """A message in the Responses API input array."""
    role: str  # "user" | "assistant" | "system" | "developer"
    content: Union[str, List[Any]]


class ResponsesFunctionCallInput(BaseModel):
    """A function_call item in the Responses API input (assistant called a tool)."""
    type: str = "function_call"
    id: Optional[str] = None
    call_id: str
    name: str
    arguments: str


class ResponsesFunctionCallOutputInput(BaseModel):
    """A function_call_output item in the Responses API input (tool result)."""
    type: str = "function_call_output"
    call_id: str
    output: str


class ResponsesRequest(BaseModel):
    """Request body for POST /v1/responses."""
    model: str = "chatgpt-browser"
    input: Union[str, List[Any]]  # string or array of messages/items
    instructions: Optional[str] = None  # system prompt
    tools: Optional[List[ResponsesToolDefinition]] = None
    tool_choice: Optional[Union[str, dict]] = None
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    max_output_tokens: Optional[int] = None
    top_p: Optional[float] = None
    previous_response_id: Optional[str] = None
    truncation: Optional[str] = None
    user: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    store: Optional[bool] = None


# ── Responses API output models ─────────────────────────────────


class ResponseOutputText(BaseModel):
    """Text content in a Responses API output message."""
    type: str = "output_text"
    text: str = ""
    annotations: List[Any] = Field(default_factory=list)


class ResponseOutputMessage(BaseModel):
    """A message output item in the Responses API."""
    id: str = Field(default_factory=lambda: f"msg_{uuid.uuid4().hex[:24]}")
    type: str = "message"
    role: str = "assistant"
    status: str = "completed"
    content: List[ResponseOutputText] = Field(default_factory=list)


class ResponseFunctionCall(BaseModel):
    """A function_call output item in the Responses API."""
    id: str = Field(default_factory=lambda: f"fc_{uuid.uuid4().hex[:24]}")
    type: str = "function_call"
    call_id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex[:24]}")
    name: str
    arguments: str
    status: str = "completed"


class ResponseUsage(BaseModel):
    """Usage info for the Responses API."""
    input_tokens: int = 0
    output_tokens: int = 0
    output_tokens_details: dict[str, int] = Field(
        default_factory=lambda: {"reasoning_tokens": 0}
    )
    total_tokens: int = 0


class ResponseObject(BaseModel):
    """The full response object returned by POST /v1/responses."""
    id: str = Field(default_factory=lambda: f"resp_{uuid.uuid4().hex[:24]}")
    object: str = "response"
    created_at: int = Field(default_factory=lambda: int(time.time()))
    status: str = "completed"
    completed_at: Optional[int] = None
    error: Optional[dict[str, Any]] = None
    incomplete_details: Optional[dict[str, Any]] = None
    instructions: Optional[str] = None
    max_output_tokens: Optional[int] = None
    model: str = "chatgpt-browser"
    output: List[Any] = Field(default_factory=list)
    output_text: Optional[str] = None
    parallel_tool_calls: bool = True
    previous_response_id: Optional[str] = None
    temperature: Optional[float] = 1.0
    text: dict[str, Any] = Field(default_factory=lambda: {"format": {"type": "text"}})
    tool_choice: Optional[Union[str, dict]] = "auto"
    tools: List[Any] = Field(default_factory=list)
    top_p: Optional[float] = 1.0
    truncation: Optional[str] = "disabled"
    usage: Optional[ResponseUsage] = None
    user: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
