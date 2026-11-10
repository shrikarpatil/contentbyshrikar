"""Instagram Reels publishing through Meta's Instagram Graph API."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, getproxies, urlopen

from influencerai.config.settings import Settings
from influencerai.schemas.screenplay import Screenplay, load_screenplay

LOGGER = logging.getLogger("influencerai.instagram")
MAX_VIDEO_UPLOAD_BYTES = 1_000_000_000
CONTAINER_POLL_INTERVAL_SECONDS = 5
MAX_CONTAINER_POLLS = 60
MAX_PUBLISH_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (30.0, 90.0)
# Large single-request uploads are intermittently dropped by Meta's upload proxy.
MAX_UPLOAD_BYTES_BEFORE_COMPRESSION = 40_000_000
COMPRESSED_VIDEO_MAXRATE = "6M"
TRANSIENT_API_CODES = {1, 2, 4, 17, 32, 341, 368, 613}


class InstagramError(RuntimeError):
    """Instagram failure; ``retryable`` marks errors worth retrying with a fresh container."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class InstagramPublishResult:
    """Published Instagram media identifiers and optional permalink."""

    container_id: str
    media_id: str
    permalink: str | None


def validate_instagram_settings(settings: Settings) -> None:
    """Fail early when Reels publishing credentials are incomplete or malformed."""
    if not settings.instagram_access_token:
        raise ValueError("Set INSTAGRAM_ACCESS_TOKEN in .env or GitHub Actions secrets to publish Reels.")
    if not re.fullmatch(r"[!-~]+", settings.instagram_access_token):
        raise ValueError(
            "INSTAGRAM_ACCESS_TOKEN contains whitespace or invalid characters. "
            "Replace the secret with the access token only, on a single line."
        )
    if not settings.instagram_user_id or not settings.instagram_user_id.isdigit():
        raise ValueError("Set INSTAGRAM_USER_ID to the numeric Instagram Professional Account ID.")
    if not re.fullmatch(r"v\d+\.\d+", settings.instagram_graph_api_version):
        raise ValueError("INSTAGRAM_GRAPH_API_VERSION must look like v27.0.")


def build_caption(screenplay: Screenplay, screenplay_path: Path) -> str:
    """Build a concise Instagram caption from screenplay narration and researched tags."""
    narration = " ".join(scene.voiceover.strip() for scene in screenplay.scenes if scene.voiceover.strip())
    hashtags_path = screenplay_path.parent / "instagram-hashtags.txt"
    if not hashtags_path.is_file():
        hashtags_path = screenplay_path.parent / "hashtags.txt"
    hashtags = hashtags_path.read_text(encoding="utf-8").split() if hashtags_path.is_file() else []
    normalized_tags: list[str] = []
    for tag in hashtags:
        if not tag.startswith("#") or tag.lower() in {"#shorts", "#youtube", "#youtubeshorts"}:
            continue
        if tag.lower() not in {existing.lower() for existing in normalized_tags}:
            normalized_tags.append(tag)
    caption = f"{screenplay.video_title.strip()}\n\n{narration}\n\nAI-generated narration and visuals."
    if normalized_tags:
        caption += "\n\n" + " ".join(normalized_tags[:5])
    return caption[:2200]


def _response_json(response: Any) -> dict[str, Any]:
    try:
        payload = json.loads(response.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Instagram API returned an invalid JSON response.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Instagram API returned an unexpected response format.")
    return payload


def _api_error_message(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message", "unknown API error")
        return f"code={code}: {message}" if code is not None else str(message)
    return "unknown API error"


def _http_error_detail(exc: HTTPError, access_token: str) -> str:
    try:
        response_body = exc.read().decode("utf-8")
    except UnicodeDecodeError:
        response_body = ""
    finally:
        exc.close()
    try:
        payload = json.loads(response_body)
        detail = _api_error_message(payload) if isinstance(payload, dict) else response_body
        if detail == "unknown API error":
            detail = response_body
    except json.JSONDecodeError:
        detail = response_body or "non-JSON API error"
    detail = detail.replace(access_token, "[redacted]").strip() or "no response body"
    headers = exc.headers or {}
    request_ids = [
        f"{name}={headers.get(name)}"
        for name in ("x-fb-request-id", "x-fb-trace-id")
        if headers.get(name)
    ]
    if request_ids:
        detail = f"{detail} ({', '.join(request_ids)})"
    return detail[:500]


def _graph_request(
    method: str,
    path: str,
    *,
    api_version: str,
    access_token: str,
    fields: dict[str, str] | None = None,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    url = f"https://graph.facebook.com/{api_version}/{path.lstrip('/')}"
    encoded_fields = urlencode(fields or {}).encode("utf-8")
    if method.upper() == "GET" and encoded_fields:
        url = f"{url}?{encoded_fields.decode('ascii')}"
        data = None
    else:
        data = encoded_fields if method.upper() == "POST" else None
    headers = {"Authorization": f"Bearer {access_token}"}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = Request(url, data=data, headers=headers, method=method.upper())
    try:
        with opener(request, timeout=60) as response:
            payload = _response_json(response)
    except HTTPError as exc:
        error_code = None
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
            detail = _api_error_message(error_payload) if isinstance(error_payload, dict) else "API error"
            if isinstance(error_payload, dict) and isinstance(error_payload.get("error"), dict):
                error_code = error_payload["error"].get("code")
        except (UnicodeDecodeError, json.JSONDecodeError):
            detail = "API error"
        raise InstagramError(
            f"Instagram Graph API request failed (HTTP {exc.code}; {detail}).",
            retryable=exc.code >= 500 or _is_transient_api_error(error_code, detail),
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise InstagramError(f"Instagram Graph API request failed ({type(exc).__name__}).", retryable=True) from exc
    if "error" in payload:
        error = payload["error"]
        code = error.get("code") if isinstance(error, dict) else None
        message = _api_error_message(payload)
        raise InstagramError(
            f"Instagram Graph API request failed ({message}).",
            retryable=_is_transient_api_error(code, message),
        )
    return payload


def _is_transient_api_error(code: Any, message: str) -> bool:
    return code in TRANSIENT_API_CODES or "api access blocked" in message.lower()


def _upload_resumable(
    upload_uri: str,
    video_path: Path,
    *,
    access_token: str,
    opener: Callable[..., Any] = urlopen,
) -> None:
    video_size = video_path.stat().st_size
    if video_size <= 0 or video_size > MAX_VIDEO_UPLOAD_BYTES:
        raise ValueError(f"Instagram Reel MP4 must be between 1 byte and {MAX_VIDEO_UPLOAD_BYTES} bytes.")
    video_bytes = video_path.read_bytes()
    request = Request(
        upload_uri,
        data=video_bytes,
        headers={
            "Authorization": f"OAuth {access_token}",
            "offset": "0",
            "file_size": str(video_size),
            "Content-Type": "application/octet-stream",
        },
        method="POST",
    )
    diagnostics = os.environ.get("INSTAGRAM_UPLOAD_DIAGNOSTICS") == "1"
    if diagnostics:
        proxies = getproxies()
        LOGGER.warning(
            "Instagram upload diagnostics: environment=%s os=%s python=%s method=POST "
            "host=rupload.facebook.com api_version=%s bytes=%d sha256=%s "
            "content_type=application/octet-stream offset=0 authorization=OAuth "
            "http_proxy_configured=%s https_proxy_configured=%s",
            "github-actions" if os.environ.get("GITHUB_ACTIONS") == "true" else "other",
            platform.system(),
            platform.python_version(),
            urlsplit(upload_uri).path.split("/")[2],
            len(video_bytes),
            hashlib.sha256(video_bytes).hexdigest(),
            bool(proxies.get("http")),
            bool(proxies.get("https")),
        )
    try:
        with opener(request, timeout=300) as response:
            if diagnostics:
                _log_upload_response(getattr(response, "status", None), getattr(response, "headers", None))
            payload = _response_json(response)
    except HTTPError as exc:
        if diagnostics:
            _log_upload_response(exc.code, exc.headers)
        detail = _http_error_detail(exc, access_token)
        raise InstagramError(
            f"Instagram Reel upload failed (HTTP {exc.code}; {detail}).",
            retryable=exc.code >= 500 or "ProcessingFailedError" in detail,
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        if diagnostics:
            LOGGER.warning("Instagram upload diagnostics: transport_error=%s", type(exc).__name__)
        raise InstagramError(f"Instagram Reel upload failed ({type(exc).__name__}).", retryable=True) from exc
    if payload.get("success") is False:
        raise InstagramError("Instagram Reel upload was rejected by Meta.", retryable=True)


def _log_upload_response(status: int | None, headers: Any) -> None:
    proxy_status = headers.get("Proxy-Status", "") if headers else ""
    LOGGER.warning(
        "Instagram upload diagnostics: response_status=%s proxy_status_present=%s "
        "proxy_http_request_error=%s fb_request_id_present=%s fb_trace_id_present=%s",
        status,
        bool(proxy_status),
        "http_request_error" in proxy_status.lower(),
        bool(headers and headers.get("x-fb-request-id")),
        bool(headers and headers.get("x-fb-trace-id")),
    )


def _wait_until_ready(
    container_id: str,
    *,
    api_version: str,
    access_token: str,
    opener: Callable[..., Any],
    sleep: Callable[[float], None],
    poll_interval: float,
    max_polls: int,
) -> None:
    for poll_number in range(max_polls):
        payload = _graph_request(
            "GET",
            container_id,
            api_version=api_version,
            access_token=access_token,
            fields={"fields": "status_code,status"},
            opener=opener,
        )
        status = str(payload.get("status_code", "")).upper()
        if status == "FINISHED":
            return
        if status in {"ERROR", "EXPIRED"}:
            detail = payload.get("status", "no status details")
            raise InstagramError(
                f"Instagram Reel container processing ended with {status}: {detail}", retryable=True
            )
        if poll_number + 1 < max_polls:
            sleep(poll_interval)
    raise TimeoutError(f"Instagram Reel container {container_id} was not ready after {max_polls} checks.")


def prepare_reel(
    video_path: Path,
    caption: str,
    settings: Settings,
    *,
    opener: Callable[..., Any] = urlopen,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = CONTAINER_POLL_INTERVAL_SECONDS,
    max_polls: int = MAX_CONTAINER_POLLS,
) -> str:
    """Upload and process an Instagram Reel without publishing it."""
    validate_instagram_settings(settings)
    if not video_path.is_file():
        raise FileNotFoundError(f"Instagram Reel video was not found: {video_path}")
    if video_path.suffix.lower() != ".mp4":
        raise ValueError("Instagram Reels publishing requires an MP4 video.")

    api_version = settings.instagram_graph_api_version
    container = _graph_request(
        "POST",
        f"{settings.instagram_user_id}/media",
        api_version=api_version,
        access_token=settings.instagram_access_token,
        fields={
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": caption,
            "share_to_feed": "true",
            "is_ai_generated": "true",
        },
        opener=opener,
    )
    container_id = str(container.get("id", ""))
    if not container_id:
        raise RuntimeError("Instagram API created no Reel media container.")
    upload_uri = container.get("uri")
    if not isinstance(upload_uri, str):
        raise RuntimeError("Instagram API did not return a Reel upload URI.")
    parsed_uri = urlsplit(upload_uri)
    if (
        parsed_uri.scheme != "https"
        or parsed_uri.netloc.lower() != "rupload.facebook.com"
        or not re.fullmatch(r"/ig-api-upload/v\d+\.\d+/" + re.escape(container_id), parsed_uri.path)
        or parsed_uri.query
        or parsed_uri.fragment
    ):
        raise RuntimeError("Instagram API returned an invalid Reel upload URI.")

    _upload_resumable(
        upload_uri,
        video_path,
        access_token=settings.instagram_access_token,
        opener=opener,
    )
    _wait_until_ready(
        container_id,
        api_version=api_version,
        access_token=settings.instagram_access_token,
        opener=opener,
        sleep=sleep,
        poll_interval=poll_interval,
        max_polls=max_polls,
    )
    return container_id


def _fit_video_for_upload(video_path: Path, settings: Settings) -> Path:
    """Return a smaller re-encoded copy when the MP4 is large enough to risk upload rejection."""
    if video_path.stat().st_size <= MAX_UPLOAD_BYTES_BEFORE_COMPRESSION:
        return video_path
    from influencerai.utils.ffmpeg_utils import resolve_ffmpeg, run_ffmpeg

    compressed = video_path.with_name("instagram-reel.mp4")
    LOGGER.warning(
        "Reel MP4 is %.1f MB; re-encoding a smaller copy for Instagram upload.", video_path.stat().st_size / 1_000_000
    )
    run_ffmpeg(
        resolve_ffmpeg(settings.ffmpeg_path),
        [
            "-y", "-v", "error", "-i", str(video_path),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
            "-maxrate", COMPRESSED_VIDEO_MAXRATE, "-bufsize", "12M", "-pix_fmt", "yuv420p",
            "-c:a", "copy", "-movflags", "+faststart", str(compressed),
        ],
        cwd=video_path.parent,
        stage="Instagram upload compression",
    )
    if not compressed.is_file() or compressed.stat().st_size < 1024:
        raise RuntimeError("Compressed Instagram MP4 was not created or is unexpectedly small.")
    return compressed


def _prepare_reel_with_retries(
    video_path: Path,
    caption: str,
    settings: Settings,
    *,
    opener: Callable[..., Any],
    sleep: Callable[[float], None],
    poll_interval: float,
    max_polls: int,
) -> str:
    """Retry upload/processing with a fresh container; nothing is published until it succeeds."""
    for attempt in range(1, MAX_PUBLISH_ATTEMPTS + 1):
        try:
            return prepare_reel(
                video_path,
                caption,
                settings,
                opener=opener,
                sleep=sleep,
                poll_interval=poll_interval,
                max_polls=max_polls,
            )
        except InstagramError as exc:
            if not exc.retryable or attempt >= MAX_PUBLISH_ATTEMPTS:
                raise
            delay = RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
            LOGGER.warning(
                "Instagram upload attempt %d/%d failed (%s); retrying in %.0fs with a new container.",
                attempt, MAX_PUBLISH_ATTEMPTS, exc, delay,
            )
            sleep(delay)
    raise AssertionError("unreachable")


def publish_reel(
    video_path: Path,
    screenplay_path: Path,
    settings: Settings,
    *,
    opener: Callable[..., Any] = urlopen,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = CONTAINER_POLL_INTERVAL_SECONDS,
    max_polls: int = MAX_CONTAINER_POLLS,
) -> InstagramPublishResult:
    """Upload a local MP4 as an Instagram Reel and publish it."""
    validate_instagram_settings(settings)
    if not video_path.is_file():
        raise FileNotFoundError(f"Instagram Reel video was not found: {video_path}")
    if video_path.suffix.lower() != ".mp4":
        raise ValueError("Instagram Reels publishing requires an MP4 video.")
    screenplay = load_screenplay(screenplay_path)
    container_id = _prepare_reel_with_retries(
        _fit_video_for_upload(video_path, settings),
        build_caption(screenplay, screenplay_path),
        settings,
        opener=opener,
        sleep=sleep,
        poll_interval=poll_interval,
        max_polls=max_polls,
    )
    api_version = settings.instagram_graph_api_version
    published = _graph_request(
        "POST",
        f"{settings.instagram_user_id}/media_publish",
        api_version=api_version,
        access_token=settings.instagram_access_token,
        fields={"creation_id": container_id},
        opener=opener,
    )
    media_id = str(published.get("id", ""))
    if not media_id:
        raise RuntimeError("Instagram API did not return a published media ID.")

    permalink = None
    try:
        details = _graph_request(
            "GET",
            media_id,
            api_version=api_version,
            access_token=settings.instagram_access_token,
            fields={"fields": "permalink"},
            opener=opener,
        )
        permalink = str(details.get("permalink") or "") or None
    except Exception as exc:
        LOGGER.warning("Reel published as media %s, but permalink lookup failed (%s).", media_id, type(exc).__name__)
    LOGGER.info("Published Instagram Reel media_id=%s permalink=%s", media_id, permalink or "unavailable")
    return InstagramPublishResult(container_id, media_id, permalink)
