"""Tests for the unpublished, no-OpenAI Instagram diagnostic."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from influencerai.instagram_diagnostic import create_test_video, main
from influencerai.utils.ffmpeg_utils import resolve_ffmpeg, run_ffmpeg


class InstagramDiagnosticTests(unittest.TestCase):
    def test_synthetic_video_is_a_playable_vertical_mp4_with_aac(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "test.mp4"
            create_test_video(video)
            self.assertTrue(video.is_file())
            result = run_ffmpeg(
                resolve_ffmpeg("ffmpeg"),
                ["-i", str(video), "-f", "null", "-"],
                cwd=video.parent,
                stage="diagnostic video verification",
            )
            self.assertIn("1080x1920", result.stderr)
            self.assertIn("Audio: aac", result.stderr)
            self.assertIn("48000 Hz", result.stderr)

    def test_main_prepares_but_never_publishes(self) -> None:
        with (
            patch("sys.argv", ["instagram_diagnostic", "--output", "test.mp4"]),
            patch("influencerai.instagram_diagnostic.Settings.from_env") as settings,
            patch("influencerai.instagram_diagnostic.validate_instagram_settings"),
            patch("influencerai.instagram_diagnostic.create_test_video") as render,
            patch("influencerai.instagram_diagnostic.prepare_reel") as prepare,
        ):
            main()
        render.assert_called_once_with(Path("test.mp4"))
        prepare.assert_called_once_with(Path("test.mp4"), "Unpublished Instagram upload diagnostic.", settings.return_value)


if __name__ == "__main__":
    unittest.main()
