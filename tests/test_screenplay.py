"""Tests for screenplay input validation and run workspace setup."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from influencerai.schemas.screenplay import Screenplay, load_screenplay
from influencerai.utils.file_manager import create_run_directory


def valid_payload() -> dict[str, object]:
    return {
        "video_title": "A Test Video",
        "target_duration": 4,
        "aspect_ratio": "9:16",
        "scenes": [
            {
                "scene_id": 1,
                "duration": 4,
                "voiceover": "A short narration.",
                "image_prompt": "A cinematic test image.",
            }
        ],
    }


class ScreenplayTests(unittest.TestCase):
    def test_valid_screenplay_parses(self) -> None:
        screenplay = Screenplay.model_validate(valid_payload())
        self.assertEqual(screenplay.scenes[0].motion, "subtle_zoom")
        self.assertEqual(screenplay.background_music.volume, 0.14)

    def test_non_positive_scene_duration_is_rejected(self) -> None:
        payload = valid_payload()
        payload["scenes"][0]["duration"] = 0  # type: ignore[index]
        with self.assertRaises(ValidationError):
            Screenplay.model_validate(payload)

    def test_duplicate_scene_ids_are_rejected(self) -> None:
        payload = valid_payload()
        payload["scenes"].append(dict(payload["scenes"][0]))  # type: ignore[union-attr]
        with self.assertRaises(ValidationError):
            Screenplay.model_validate(payload)

    def test_invalid_music_volume_is_rejected(self) -> None:
        payload = valid_payload()
        payload["background_music"] = {"mood": "calm", "volume": 1.1}
        with self.assertRaises(ValidationError):
            Screenplay.model_validate(payload)

    def test_json_file_loads_and_invalid_json_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "screenplay.json"
            path.write_text(json.dumps(valid_payload()), encoding="utf-8")
            self.assertEqual(load_screenplay(path).video_title, "A Test Video")
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid JSON"):
                load_screenplay(path)

    def test_run_directory_stores_input_and_stage_folders(self) -> None:
        screenplay = Screenplay.model_validate(valid_payload())
        with tempfile.TemporaryDirectory() as temp_dir:
            run_directory = create_run_directory(screenplay, Path(temp_dir))
            self.assertTrue((run_directory / "screenplay.json").is_file())
            self.assertTrue((run_directory / "images").is_dir())
            self.assertTrue((run_directory / "logs").is_dir())


if __name__ == "__main__":
    unittest.main()