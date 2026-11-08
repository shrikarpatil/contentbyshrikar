"""Safe FFmpeg discovery and subprocess execution helpers."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Sequence


def resolve_ffmpeg(configured_path: str) -> str:
    """Resolve configured FFmpeg, falling back to the bundled imageio binary."""
    discovered = shutil.which(configured_path)
    if discovered:
        return discovered
    configured_file = Path(configured_path)
    if configured_file.is_file():
        return str(configured_file.resolve())
    if configured_path.lower() not in {"ffmpeg", "ffmpeg.exe"}:
        raise FileNotFoundError(f"Configured FFmpeg executable was not found: {configured_path}")
    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError) as exc:
        raise FileNotFoundError("FFmpeg is missing; install imageio-ffmpeg or set FFMPEG_PATH.") from exc
    if not Path(bundled).is_file():
        raise FileNotFoundError("The bundled FFmpeg executable is missing; reinstall imageio-ffmpeg.")
    return bundled


def run_ffmpeg(
    executable: str,
    arguments: Sequence[str],
    *,
    cwd: Path,
    stage: str,
) -> subprocess.CompletedProcess[str]:
    """Run FFmpeg without a shell and return a useful, bounded failure message."""
    result = subprocess.run(
        [executable, *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        details = (result.stderr or result.stdout).strip().splitlines()
        tail = "\n".join(details[-12:])
        raise RuntimeError(f"FFmpeg failed during {stage} (exit {result.returncode}):\n{tail}")
    return result