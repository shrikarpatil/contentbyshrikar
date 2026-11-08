"""Generate CPU-only animated scene clips from still images."""

from __future__ import annotations

from pathlib import Path

from influencerai.schemas.screenplay import Motion, Transition
from influencerai.utils.ffmpeg_utils import run_ffmpeg


def build_animation_command(
    image_path: Path,
    output_path: Path,
    *,
    duration: float,
    motion: Motion,
    transition: Transition,
    width: int,
    height: int,
    fps: int,
) -> list[str]:
    """Build a deterministic FFmpeg argument list for one scene."""
    frame_count = max(1, round(duration * fps))
    zoom_step = 0.08 / frame_count
    zoom_expressions = {
        "slow_zoom_in": (f"min(1+on*{zoom_step:.8f},1.08)", "iw/2-iw/zoom/2", "ih/2-ih/zoom/2"),
        "slow_zoom_out": (f"max(1,1.08-on*{zoom_step:.8f})", "iw/2-iw/zoom/2", "ih/2-ih/zoom/2"),
        "subtle_zoom": (f"min(1+on*{zoom_step * 0.4:.8f},1.032)", "iw/2-iw/zoom/2", "ih/2-ih/zoom/2"),
        "pan_left": ("1.35", f"(iw-iw/zoom)*on/{frame_count}", "ih/2-ih/zoom/2"),
        "pan_right": ("1.35", f"(iw-iw/zoom)*(1-on/{frame_count})", "ih/2-ih/zoom/2"),
        "pan_up": ("1.35", "iw/2-iw/zoom/2", f"(ih-ih/zoom)*on/{frame_count}"),
        "pan_down": ("1.35", "iw/2-iw/zoom/2", f"(ih-ih/zoom)*(1-on/{frame_count})"),
        "static": ("1", "iw/2-iw/zoom/2", "ih/2-ih/zoom/2"),
    }
    zoom, x, y = zoom_expressions[motion]
    filters = [
        f"scale={width}:{height}:force_original_aspect_ratio=increase",
        f"crop={width}:{height}",
        f"zoompan=z='{zoom}':x='{x}':y='{y}':d=1:s={width}x{height}:fps={fps}",
    ]
    if transition in {"fade", "dissolve"}:
        fade_duration = min(0.35, duration / 4)
        filters.extend((f"fade=t=in:st=0:d={fade_duration:.3f}", f"fade=t=out:st={max(0, duration - fade_duration):.3f}:d={fade_duration:.3f}"))
    filters.append("format=yuv420p")

    return [
        "-y",
        "-loop", "1",
        "-framerate", str(fps),
        "-i", str(image_path),
        "-vf", ",".join(filters),
        "-t", f"{duration:.3f}",
        "-an",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "24",
        "-threads", "2",
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-movflags", "+faststart",
        str(output_path),
    ]


def animate_scene(
    executable: str,
    image_path: Path,
    output_path: Path,
    *,
    duration: float,
    motion: Motion,
    transition: Transition,
    width: int,
    height: int,
    fps: int,
) -> None:
    """Render a single still image into a vertical H.264 scene clip."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    arguments = build_animation_command(
        image_path,
        output_path,
        duration=duration,
        motion=motion,
        transition=transition,
        width=width,
        height=height,
        fps=fps,
    )
    run_ffmpeg(executable, arguments, cwd=output_path.parent, stage=f"scene animation {output_path.stem}")