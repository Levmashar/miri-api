"""
Data models for ChatGPT interactions.
"""

from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, Field


class ImageInfo(BaseModel):
    """Metadata for a generated image."""
    url: str = Field(description="Original image URL from ChatGPT/DALL-E")
    alt: str = Field(default="", description="Alt text / image description")
    local_path: str = Field(default="", description="Local file path after download")
    prompt_title: str = Field(default="", description="Image generation title shown by ChatGPT")


class VideoInfo(BaseModel):
    """Metadata for a generated video (e.g. Dreamina / Seedream).

    Parallels ImageInfo. `local_path` is the downloaded .mp4 on disk; `url` is
    the original blob:/CDN source it was pulled from (kept for debugging only —
    blob URLs are page-scoped and not fetchable after the tab moves on)."""
    url: str = Field(default="", description="Original video URL (blob:/CDN)")
    alt: str = Field(default="", description="Description / prompt title")
    local_path: str = Field(default="", description="Local .mp4 path after download")
    prompt_title: str = Field(default="", description="Title shown in the web UI")
    mime_type: str = Field(default="video/mp4", description="Container MIME type")
    duration_s: float = Field(default=0.0, description="Clip length in seconds, 0 if unknown")


class Message(BaseModel):
    """A single message in a conversation."""
    role: str = Field(description="'user' or 'assistant'")
    content: str = Field(description="Message text content")
    timestamp: datetime = Field(default_factory=datetime.now)
    images: list[ImageInfo] = Field(default_factory=list, description="Images in this message")


class ChatResponse(BaseModel):
    """Response from a chat interaction."""
    message: str = Field(description="Assistant's response text")
    thread_id: str = Field(default="", description="Conversation thread ID from URL")
    response_time_ms: int = Field(default=0, description="Time taken for response in ms")
    images: list[ImageInfo] = Field(default_factory=list, description="Generated images")
    has_images: bool = Field(default=False, description="Whether the response contains images")
    # Generated videos (Seedream/Dreamina). Empty for chat/image providers.
    videos: list[VideoInfo] = Field(default_factory=list, description="Generated videos")
    has_videos: bool = Field(default=False, description="Whether the response contains videos")
    # Usage-limit detection (best-effort scrape of the throttle banner). See
    # src/accounts/limit_detector.py. limit_hit means this account is throttled.
    limit_hit: bool = Field(default=False, description="Reply was a usage-limit banner")
    limit_kind: str = Field(default="", description="image|message|generic")
    limit_reset_seconds: float = Field(default=0.0, description="Seconds until reset, 0 if unknown")


class Thread(BaseModel):
    """A conversation thread."""
    id: str = Field(description="Thread ID (from URL /c/{id})")
    title: str = Field(default="", description="Thread title from sidebar")
    url: str = Field(default="", description="Full URL")
    messages: list[Message] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
