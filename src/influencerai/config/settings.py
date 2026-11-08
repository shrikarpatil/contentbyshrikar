"""Environment-backed settings for the video generation pipeline."""

from __future__ import annotations

import os
import logging
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OPENAI_IMAGE_MODEL = "gpt-image-2.6-flare"
RETIRED_OPENAI_IMAGE_MODELS = {"gpt-image-1"}


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _optional_float(name: str) -> float | None:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return None
    value = float(raw_value)
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated configuration loaded from environment variables."""

    openai_api_key: str | None
    openai_model: str
    openai_image_model: str
    image_provider: str
    image_quality: str
    image_size: str
    image_cost_per_image_usd: float | None
    tts_provider: str
    tts_model: str | None
    tts_voice: str
    tts_instructions: str
    transcription_provider: str
    whisper_model: str
    ffmpeg_path: str
    output_directory: Path
    music_directory: Path
    auto_download_music: bool
    sound_effects_directory: Path
    video_width: int
    video_height: int
    video_fps: int
    default_video_duration: int

    @classmethod
    def from_env(cls, *, project_root: Path = PROJECT_ROOT) -> Settings:
        """Load settings, reading a project ``.env`` file when present."""
        load_dotenv(project_root / ".env", override=False)
        image_model = os.getenv("OPENAI_IMAGE_MODEL", DEFAULT_OPENAI_IMAGE_MODEL).strip()
        if image_model.lower() in RETIRED_OPENAI_IMAGE_MODELS:
            logging.getLogger("influencerai.config").warning(
                "OPENAI_IMAGE_MODEL=%s is retired; using %s. Update .env to keep this explicit.",
                image_model,
                DEFAULT_OPENAI_IMAGE_MODEL,
            )
            image_model = DEFAULT_OPENAI_IMAGE_MODEL
        tts_provider = os.getenv("TTS_PROVIDER", "local").lower()
        default_tts_voice = (
            "marin" if tts_provider == "openai" else "Microsoft Zira Desktop - English (United States)"
        )
        output_path = Path(os.getenv("OUTPUT_DIRECTORY", "output"))
        if not output_path.is_absolute():
            output_path = project_root / output_path

        return cls(
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            openai_image_model=image_model,
            image_provider=os.getenv("IMAGE_PROVIDER", "local").lower(),
            image_quality=os.getenv("IMAGE_QUALITY", "medium"),
            image_size=os.getenv("IMAGE_SIZE", "1024x1536"),
            image_cost_per_image_usd=_optional_float("IMAGE_COST_PER_IMAGE_USD"),
            tts_provider=tts_provider,
            tts_model=os.getenv("TTS_MODEL") or None,
            tts_voice=os.getenv("TTS_VOICE", default_tts_voice),
            tts_instructions=os.getenv(
                "TTS_INSTRUCTIONS",
                "Speak naturally like a warm, confident documentary narrator. Use conversational phrasing, "
                "gentle energy, clear articulation, and varied human-sounding intonation. Avoid sounding robotic, "
                "overly formal, or like an advertisement.",
            ),
            transcription_provider=os.getenv("TRANSCRIPTION_PROVIDER", "screenplay").lower(),
            whisper_model=os.getenv("WHISPER_MODEL", "base"),
            ffmpeg_path=os.getenv("FFMPEG_PATH", "ffmpeg"),
            output_directory=output_path,
            music_directory=project_root / os.getenv("MUSIC_DIRECTORY", "assets/music"),
            auto_download_music=os.getenv("AUTO_DOWNLOAD_MUSIC", "true").strip().lower() in {"1", "true", "yes", "on"},
            sound_effects_directory=project_root / os.getenv("SOUND_EFFECTS_DIRECTORY", "assets/sound_effects"),
            video_width=_positive_int("VIDEO_WIDTH", 1080),
            video_height=_positive_int("VIDEO_HEIGHT", 1920),
            video_fps=_positive_int("VIDEO_FPS", 30),
            default_video_duration=_positive_int("DEFAULT_VIDEO_DURATION", 30),
        )