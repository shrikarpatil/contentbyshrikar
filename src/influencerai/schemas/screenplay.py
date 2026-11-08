"""Pydantic models and JSON loading helpers for production screenplays."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


Motion = Literal[
    "slow_zoom_in",
    "slow_zoom_out",
    "pan_left",
    "pan_right",
    "pan_up",
    "pan_down",
    "subtle_zoom",
    "static",
]
Transition = Literal["cut", "fade", "dissolve", "wipe"]


class Scene(BaseModel):
    """One narrated, visually directed scene in a screenplay."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    scene_id: int = Field(gt=0)
    duration: float = Field(gt=0)
    voiceover: str = Field(min_length=1)
    image_prompt: str = Field(min_length=1)
    motion: Motion = "subtle_zoom"
    on_screen_text: str = ""
    sound_effect: str | None = None
    transition: Transition = "cut"


class BackgroundMusic(BaseModel):
    """Optional soundtrack direction and mix level."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    mood: str = ""
    volume: float = Field(default=0.14, ge=0, le=1)


class Screenplay(BaseModel):
    """Validated screenplay input consumed by the media pipeline."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    video_title: str = Field(min_length=1)
    target_duration: float = Field(gt=0)
    aspect_ratio: str = "9:16"
    scenes: list[Scene] = Field(min_length=1)
    background_music: BackgroundMusic = Field(default_factory=BackgroundMusic)

    @field_validator("scenes")
    @classmethod
    def scene_ids_must_be_unique(cls, scenes: list[Scene]) -> list[Scene]:
        scene_ids = [scene.scene_id for scene in scenes]
        if len(scene_ids) != len(set(scene_ids)):
            raise ValueError("scene_id values must be unique")
        return scenes

    @field_validator("aspect_ratio")
    @classmethod
    def aspect_ratio_must_be_width_by_height(cls, value: str) -> str:
        parts = value.split(":")
        if len(parts) != 2 or not all(part.isdigit() and int(part) > 0 for part in parts):
            raise ValueError("aspect_ratio must use the positive width:height format, e.g. 9:16")
        return value


def load_screenplay(path: Path) -> Screenplay:
    """Read and validate a JSON screenplay from ``path``."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in screenplay {path}: {exc.msg}") from exc
    except OSError as exc:
        raise ValueError(f"Could not read screenplay {path}: {exc}") from exc

    return Screenplay.model_validate(payload)