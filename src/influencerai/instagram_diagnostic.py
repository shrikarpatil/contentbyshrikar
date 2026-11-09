"""Generate and upload a synthetic Instagram test video without publishing it."""

from __future__ import annotations

import argparse
from pathlib import Path

from influencerai.config.settings import Settings
from influencerai.services.instagram_service import prepare_reel, validate_instagram_settings
from influencerai.utils.ffmpeg_utils import resolve_ffmpeg, run_ffmpeg


def create_test_video(output_path: Path) -> None:
    """Render a six-second test pattern and tone without external APIs."""
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        resolve_ffmpeg("ffmpeg"),
        [
            "-y",
            "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30:duration=6",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22", "-threads", "2",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-b:a", "112k",
            "-movflags", "+faststart",
            str(output_path),
        ],
        cwd=output_path.parent,
        stage="Instagram diagnostic video",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("output/instagram-diagnostic/test.mp4"))
    args = parser.parse_args()
    settings = Settings.from_env()
    validate_instagram_settings(settings)
    create_test_video(args.output)
    print(f"Synthetic diagnostic video created: {args.output}", flush=True)
    prepare_reel(args.output, "Unpublished Instagram upload diagnostic.", settings)
    print("Instagram processed the diagnostic video. No Reel was published.", flush=True)


if __name__ == "__main__":
    main()
