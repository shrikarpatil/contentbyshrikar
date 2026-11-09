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
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
            detail = _api_error_message(error_payload) if isinstance(error_payload, dict) else "API error"
        except (UnicodeDecodeError, json.JSONDecodeError):
            detail = "API error"
        raise RuntimeError(f"Instagram Graph API request failed (HTTP {exc.code}; {detail}).") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Instagram Graph API request failed ({type(exc).__name__}).") from exc
    if "error" in payload:
        raise RuntimeError(f"Instagram Graph API request failed ({_api_error_message(payload)}).")
    return payload


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
        raise RuntimeError(f"Instagram Reel upload failed (HTTP {exc.code}; {detail}).") from exc
    except (URLError, TimeoutError, OSError) as exc:
        if diagnostics:
            LOGGER.warning("Instagram upload diagnostics: transport_error=%s", type(exc).__name__)
        raise RuntimeError(f"Instagram Reel upload failed ({type(exc).__name__}).") from exc
    if payload.get("success") is False:
        raise RuntimeError("Instagram Reel upload was rejected by Meta.")


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
            raise RuntimeError(f"Instagram Reel container processing ended with {status}: {detail}")
        if poll_number + 1 < max_polls:
            sleep(poll_interval)
    raise TimeoutError(f"Instagram Reel container {container_id} was not ready after {max_polls} checks.")


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
    api_version = settings.instagram_graph_api_version
    caption = build_caption(screenplay, screenplay_path)
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
