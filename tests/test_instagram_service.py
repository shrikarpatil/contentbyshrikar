"""No-network tests for the Instagram Reels publishing client."""

from __future__ import annotations

import json
import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock

from influencerai.config.settings import Settings
from influencerai.schemas.screenplay import Screenplay
from influencerai.services.instagram_service import (
    _http_error_detail,
    publish_reel,
    validate_instagram_settings,
)


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def sample_screenplay_path(directory: Path) -> Path:
    screenplay = Screenplay.model_validate(
        {
            "video_title": "A New Space Discovery",
            "target_duration": 25,
            "aspect_ratio": "9:16",
            "scenes": [
                {
                    "scene_id": 1,
                    "duration": 25,
                    "voiceover": "Scientists found a surprising new signal.",
                    "image_prompt": "A photorealistic space observatory at night.",
                }
            ],
        }
    )
    path = directory / "screenplay.json"
    path.write_text(screenplay.model_dump_json(indent=2), encoding="utf-8")
    (directory / "hashtags.txt").write_text("#YouTubeSpace #Science #Shorts #India #Extra", encoding="utf-8")
    (directory / "instagram-hashtags.txt").write_text(
        "#SpacePhotography #Astrophysics #IndiaScience #Cosmos #Astronomy",
        encoding="utf-8",
    )
    return path


class InstagramServiceTests(unittest.TestCase):
    def test_validate_instagram_settings_requires_token_and_numeric_account_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Settings.from_env(project_root=Path(temp_dir))
            with self.assertRaisesRegex(ValueError, "INSTAGRAM_ACCESS_TOKEN"):
                validate_instagram_settings(base)
            with self.assertRaisesRegex(ValueError, "numeric Instagram Professional Account ID"):
                validate_instagram_settings(
                    replace(base, instagram_access_token="test-token", instagram_user_id="not-numeric")
                )
            validate_instagram_settings(
                replace(
                    base,
                    instagram_access_token="test-token",
                    instagram_user_id="1234567890",
                    instagram_graph_api_version="v27.0",
                )
            )

    def test_malformed_token_is_rejected_without_exposing_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Settings.from_env(project_root=Path(temp_dir))
            for token in ("fake-token\n", "fake\rtoken", "fake token", "fake\ttoken", "fake\u00e9token"):
                with self.subTest(token_type=repr(token[-1])):
                    settings = replace(base, instagram_access_token=token, instagram_user_id="1234567890")
                    with self.assertRaisesRegex(ValueError, "INSTAGRAM_ACCESS_TOKEN contains whitespace or invalid characters") as caught:
                        validate_instagram_settings(settings)
                    self.assertNotIn(token, str(caught.exception))

    def test_publish_reel_creates_uploads_polls_and_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video_path = root / "final.mp4"
            video_bytes = b"mock-mp4-content"
            video_path.write_bytes(video_bytes)
            screenplay_path = sample_screenplay_path(root)
            settings = replace(
                Settings.from_env(project_root=root),
                instagram_access_token="fake-access-token",
                instagram_user_id="123456789012345",
                instagram_graph_api_version="v27.0",
            )
            responses = [
                {"id": "container-1", "uri": "https://rupload.facebook.com/ig-api-upload/v26.0/container-1"},
                {"success": True},
                {"status_code": "IN_PROGRESS", "status": "upload complete"},
                {"status_code": "FINISHED", "status": "ready"},
                {"id": "media-9"},
                {"permalink": "https://www.instagram.com/reel/shortcode/"},
            ]
            requests = []

            def fake_opener(request, timeout):
                requests.append(request)
                self.assertGreater(timeout, 0)
                return FakeResponse(responses.pop(0))

            sleep = Mock()
            result = publish_reel(
                video_path,
                screenplay_path,
                settings,
                opener=fake_opener,
                sleep=sleep,
                poll_interval=0,
                max_polls=3,
            )

            self.assertEqual(result.container_id, "container-1")
            self.assertEqual(result.media_id, "media-9")
            self.assertEqual(result.permalink, "https://www.instagram.com/reel/shortcode/")
            self.assertEqual(len(requests), 6)
            create_fields = parse_qs(requests[0].data.decode("utf-8"))
            self.assertEqual(create_fields["media_type"], ["REELS"])
            self.assertEqual(create_fields["upload_type"], ["resumable"])
            self.assertEqual(create_fields["is_ai_generated"], ["true"])
            self.assertIn("A New Space Discovery", create_fields["caption"][0])
            self.assertIn("#SpacePhotography", create_fields["caption"][0])
            self.assertNotIn("#YouTubeSpace", create_fields["caption"][0])
            self.assertNotIn("#Shorts", create_fields["caption"][0])
            upload = requests[1]
            self.assertEqual(urlparse(upload.full_url).hostname, "rupload.facebook.com")
            self.assertEqual(upload.full_url, "https://rupload.facebook.com/ig-api-upload/v26.0/container-1")
            self.assertEqual(upload.data, video_bytes)
            self.assertEqual(upload.get_header("Offset"), "0")
            self.assertEqual(upload.get_header("File_size"), str(len(video_bytes)))
            self.assertEqual(upload.get_header("Authorization"), "OAuth fake-access-token")
            self.assertEqual(requests[4].get_method(), "POST")
            self.assertTrue(requests[4].full_url.endswith("/123456789012345/media_publish"))
            self.assertEqual(parse_qs(requests[4].data.decode("utf-8"))["creation_id"], ["container-1"])
            sleep.assert_called_once_with(0)

    def test_missing_or_untrusted_upload_uri_is_rejected_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video_path = root / "final.mp4"
            video_path.write_bytes(b"mock-mp4-content")
            screenplay_path = sample_screenplay_path(root)
            settings = replace(
                Settings.from_env(project_root=root),
                instagram_access_token="fake-access-token",
                instagram_user_id="123456789012345",
            )
            for uri in (
                None,
                "http://rupload.facebook.com/ig-api-upload/v27.0/container-1",
                "https://attacker.example/ig-api-upload/v27.0/container-1",
                "https://rupload.facebook.com@attacker.example/ig-api-upload/v27.0/container-1",
                "https://rupload.facebook.com/ig-api-upload/v27.0/other",
                "https://rupload.facebook.com/ig-api-upload/v27.0/container-1?token=abc",
            ):
                with self.subTest(uri=uri):
                    opener = Mock(return_value=FakeResponse({"id": "container-1", "uri": uri}))
                    with self.assertRaisesRegex(RuntimeError, "upload URI"):
                        publish_reel(video_path, screenplay_path, settings, opener=opener)
                    self.assertEqual(opener.call_count, 1)

    def test_publish_reel_rejects_non_mp4_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video_path = root / "final.mov"
            video_path.write_bytes(b"video")
            screenplay_path = sample_screenplay_path(root)
            settings = replace(
                Settings.from_env(project_root=root),
                instagram_access_token="fake-access-token",
                instagram_user_id="123456789012345",
            )
            opener = Mock()
            with self.assertRaisesRegex(ValueError, "requires an MP4"):
                publish_reel(video_path, screenplay_path, settings, opener=opener)
            opener.assert_not_called()

    def test_upload_error_includes_meta_details_without_exposing_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video_path = root / "final.mp4"
            video_path.write_bytes(b"mock-mp4-content")
            screenplay_path = sample_screenplay_path(root)
            settings = replace(
                Settings.from_env(project_root=root),
                instagram_access_token="fake-access-token",
                instagram_user_id="123456789012345",
            )

            def fake_opener(request, timeout):
                self.assertGreater(timeout, 0)
                if urlparse(request.full_url).hostname == "rupload.facebook.com":
                    error_body = json.dumps(
                        {"error": {"code": 2207052, "message": "Unsupported video codec"}}
                    ).encode("utf-8")
                    raise HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(error_body))
                return FakeResponse({"id": "container-1", "uri": "https://rupload.facebook.com/ig-api-upload/v27.0/container-1"})

            with self.assertRaisesRegex(RuntimeError, "code=2207052: Unsupported video codec") as caught:
                publish_reel(video_path, screenplay_path, settings, opener=fake_opener)

            self.assertNotIn(settings.instagram_access_token, str(caught.exception))

    def test_nonstandard_http_error_keeps_redacted_response_and_request_id(self) -> None:
        response_body = json.dumps(
            {"message": "Missing upload offset", "debug": "fake-access-token"}
        ).encode("utf-8")
        error = HTTPError(
            "https://rupload.facebook.com/",
            400,
            "Bad Request",
            {"x-fb-request-id": "request-123"},
            io.BytesIO(response_body),
        )

        detail = _http_error_detail(error, "fake-access-token")

        self.assertIn("Missing upload offset", detail)
        self.assertIn("[redacted]", detail)
        self.assertIn("x-fb-request-id=request-123", detail)
        self.assertNotIn("fake-access-token", detail)


if __name__ == "__main__":
    unittest.main()
