"""YouTube Data API upload support (OAuth + resumable upload)."""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
OAUTH_PORT = 8080
RETRIABLE_STATUS = {500, 502, 503, 504}
MAX_UPLOAD_RETRIES = 6
LOGGER = logging.getLogger("influencerai.youtube")


@dataclass(frozen=True, slots=True)
class YouTubeSettings:
    client_secret_path: Path
    token_path: Path
    privacy_status: str = "public"
    category_id: str = "28"  # Science & Technology
    default_tags: tuple[str, ...] = ("shorts", "india")


def find_client_secret(project_root: Path) -> Path | None:
    matches = sorted(project_root.glob("client_secret*.json"))
    return matches[0] if matches else None


def get_credentials(settings: YouTubeSettings) -> Any:
    """Return valid OAuth credentials, running the browser consent flow only when needed."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    credentials = None
    if settings.token_path.exists():
        credentials = Credentials.from_authorized_user_file(str(settings.token_path), SCOPES)
    if credentials and credentials.valid:
        return credentials
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    else:
        if not settings.client_secret_path.exists():
            raise FileNotFoundError(f"YouTube client secret not found: {settings.client_secret_path}")
        flow = InstalledAppFlow.from_client_secrets_file(str(settings.client_secret_path), SCOPES)
        credentials = flow.run_local_server(port=OAUTH_PORT, access_type="offline", prompt="consent")
    settings.token_path.parent.mkdir(parents=True, exist_ok=True)
    settings.token_path.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


def build_metadata(
    screenplay_path: Path,
    settings: YouTubeSettings,
    topic_text: str = "",
    hashtags: list[str] | None = None,
) -> dict[str, Any]:
    """Build a YouTube Shorts upload body from the screenplay."""
    data = json.loads(screenplay_path.read_text(encoding="utf-8"))
    hashtags = [tag for tag in (hashtags or []) if tag.startswith("#")]
    title = str(data.get("video_title", "Untitled")).strip()
    if "#shorts" not in title.lower():
        title = f"{title[:90].rstrip()} #Shorts"
    narration = " ".join(scene.get("voiceover", "") for scene in data.get("scenes", []))
    tag_line = " ".join(["#Shorts", "#India", *[t for t in hashtags if t.lower() not in {"#shorts", "#india"}]])
    description = (
        f"{narration}\n\n"
        "Narration and visuals in this video are AI-generated.\n"
        f"{tag_line}"
    )
    if topic_text:
        description = f"{topic_text.strip()}\n\n{description}"
    tags = list(dict.fromkeys([*settings.default_tags, *[t.lstrip("#").lower() for t in hashtags]]))
    return {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "tags": tags,
            "categoryId": settings.category_id,
        },
        "status": {
            "privacyStatus": settings.privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }


def upload_video(
    video_path: Path,
    body: dict[str, Any],
    settings: YouTubeSettings,
    *,
    service: Any | None = None,
    sleep: Any = time.sleep,
) -> str:
    """Upload with a resumable request, retrying transient errors. Returns the video ID."""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    if service is None:
        from googleapiclient.discovery import build

        service = build("youtube", "v3", credentials=get_credentials(settings))
    media = MediaFileUpload(str(video_path), mimetype="video/mp4", chunksize=-1, resumable=True)
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    attempt = 0
    while response is None:
        try:
            _, response = request.next_chunk()
        except HttpError as exc:
            if exc.resp.status not in RETRIABLE_STATUS:
                raise
            attempt = _backoff(attempt, sleep, exc)
        except (ConnectionError, TimeoutError, OSError) as exc:
            attempt = _backoff(attempt, sleep, exc)
    video_id = response["id"]
    LOGGER.info("Uploaded to YouTube: https://youtu.be/%s", video_id)
    return str(video_id)


def _backoff(attempt: int, sleep: Any, exc: Exception) -> int:
    attempt += 1
    if attempt > MAX_UPLOAD_RETRIES:
        raise RuntimeError("YouTube upload failed after repeated retries.") from exc
    delay = random.random() * (2**attempt)
    LOGGER.warning("Transient upload error (%s); retry %d in %.1fs", exc, attempt, delay)
    sleep(delay)
    return attempt
