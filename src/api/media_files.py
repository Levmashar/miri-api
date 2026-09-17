"""Serve generated images and videos over HTTP.

`response_format="url"` used to hand back the file's path ON DISK — inside the
container that is `/app/downloads/videos/clip.mp4`. Every OpenAI-compatible
client treats `data[].url` as a URL and fetches it, so the caller's own request
came back to this gateway as `GET /app/downloads/videos/clip.mp4` and 404'd:
nothing ever served that prefix. The bytes existed; there was no way to reach
them short of reading the container filesystem.

So generated media now gets a real URL:

    {API}/v1/files/videos/<name>.mp4?sig=<hmac>

The signature is an HMAC over "<kind>/<name>" keyed on the API token, which
makes the link a CAPABILITY: it is served without an Authorization header (see
BearerTokenMiddleware.OPEN_PREFIXES) so a browser can put it straight into
<video src> or <img src>, but it cannot be guessed, and it only ever names a
file this gateway itself generated. Nothing is kept in memory — the signature
is recomputed on each request — so links survive a restart and cost nothing.

Links outlive the file by exactly as long as the file lives: the janitor deletes
generated media after IMAGE_RETENTION_MINUTES, after which the route 404s.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse

from src.core.config import Config
from src.core.log import setup_logging

log = setup_logging("media_files")

media_router = APIRouter(tags=["files"])

# URL path segment -> directory on disk. Only these two are reachable.
_KINDS = {"images": "IMAGES_DIR", "videos": "VIDEOS_DIR"}

# A generated file name (see media_fetch.download_media): slug + hex + extension.
# No slash, no dot-dot, so a name can never climb out of its directory.
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,180}$")

_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "webp": "image/webp", "gif": "image/gif",
    "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
}

# Used only when no API token is configured (open gateway) — links still carry a
# signature so their shape never changes, it just isn't a secret then.
_FALLBACK_KEY = secrets.token_bytes(32)


def _key() -> bytes:
    token = Config.API_TOKEN or Config.ADMIN_TOKEN
    return token.encode("utf-8") if token else _FALLBACK_KEY


def sign(kind: str, name: str) -> str:
    return hmac.new(_key(), f"{kind}/{name}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _directory(kind: str) -> Path:
    return getattr(Config, _KINDS[kind])


def public_base(request: Request | None) -> str:
    """Origin to build absolute media URLs from.

    PUBLIC_BASE_URL wins (set it when the gateway sits behind a proxy or a
    tunnel). Otherwise the origin the caller actually used, honouring
    X-Forwarded-* so an https front door doesn't hand out http links.
    """
    if Config.PUBLIC_BASE_URL:
        return Config.PUBLIC_BASE_URL.rstrip("/")
    if request is None:
        return f"http://127.0.0.1:{Config.API_PORT}"
    headers = request.headers
    host = (headers.get("x-forwarded-host") or headers.get("host") or "").split(",")[0].strip()
    scheme = (headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
    if not host:
        return str(request.base_url).rstrip("/")
    return f"{scheme}://{host}"


def media_url(local_path: str, *, kind: str, request: Request | None = None) -> str:
    """Public URL for a file this gateway generated, or "" if it isn't one.

    Returning "" rather than a path is deliberate: a caller that cannot be given
    a working URL must not be handed a filesystem path dressed up as one.
    """
    if kind not in _KINDS or not local_path:
        return ""
    path = Path(local_path)
    name = path.name
    if not _NAME_RE.match(name):
        log.warning("Generated %s has a name that cannot be served: %r", kind, name)
        return ""
    try:
        # Only files that really live in the generated-media directory.
        path.resolve().relative_to(_directory(kind).resolve())
    except (ValueError, OSError):
        log.warning("Generated %s is outside %s: %s", kind, _KINDS[kind], local_path)
        return ""
    return f"{public_base(request)}/v1/files/{kind}/{quote(name)}?sig={sign(kind, name)}"


@media_router.get("/v1/files/{kind}/{name}", include_in_schema=False)
@media_router.head("/v1/files/{kind}/{name}", include_in_schema=False)
async def get_media_file(kind: str, name: str, request: Request) -> FileResponse:
    """Stream a generated image/video back to the caller.

    404 (never 403) for a bad signature: an attacker probing this route learns
    nothing about which files exist.
    """
    sig = request.query_params.get("sig", "")
    gone = HTTPException(
        status_code=404,
        detail=("No such generated file. Links expire with the file itself — generated "
                f"media is deleted after IMAGE_RETENTION_MINUTES ({Config.IMAGE_RETENTION_MINUTES:g} "
                "min). Request the media again, or use response_format=\"b64_json\" to get "
                "the bytes inline."),
    )
    if kind not in _KINDS or not _NAME_RE.match(name or ""):
        raise gone
    if not (sig and secrets.compare_digest(sig, sign(kind, name))):
        raise gone

    path = _directory(kind) / name
    try:
        resolved = path.resolve()
        resolved.relative_to(_directory(kind).resolve())
        if not resolved.is_file():
            raise gone
    except (ValueError, OSError):
        raise gone

    ext = resolved.suffix.lstrip(".").lower()
    return FileResponse(
        resolved,
        media_type=_MIME.get(ext, "application/octet-stream"),
        filename=name,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=300"},
    )
