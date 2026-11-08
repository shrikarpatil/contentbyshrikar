"""Helpers for creating reproducible per-video workspaces."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from influencerai.schemas.screenplay import Screenplay


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48].rstrip("-") or "video"


def create_run_directory(screenplay: Screenplay, output_root: Path) -> Path:
    """Create a unique run folder and save the validated input screenplay."""
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    run_directory = output_root / f"{timestamp}-{_slugify(screenplay.video_title)}"
    run_directory.mkdir(parents=True, exist_ok=False)

    for folder in ("images", "audio", "captions", "clips", "logs"):
        (run_directory / folder).mkdir()

    screenplay_path = run_directory / "screenplay.json"
    screenplay_path.write_text(
        json.dumps(screenplay.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return run_directory