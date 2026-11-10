"""No-network tests for the Instagram Reels publishing client."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock, patch

from influencerai.config.settings import Settings
from influencerai.schemas.screenplay import Screenplay
from influencerai.services.instagram_service import (
    _http_error_detail,
    _fit_video_for_upload,
    _upload_resumable,
    prepare_reel,
    publish_reel,
    validate_instagram_settings,
)


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload
        self.status = 200
        self.headers: dict[str, str] = {}

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

    def test_prepare_reel_processes_without_publishing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "test.mp4"
            video.write_bytes(b"test-video")
            settings = replace(
                Settings.from_env(project_root=root),
                instagram_access_token="fake-access-token",
                instagram_user_id="123456789012345",
                instagram_graph_api_version="v27.0",
            )
            responses = [
                {"id": "container-1", "uri": "https://rupload.facebook.com/ig-api-upload/v27.0/container-1"},
                {"success": True},
                {"status_code": "IN_PROGRESS"},
                {"status_code": "FINISHED"},
            ]
            requests = []

            def fake_opener(request, timeout):
                requests.append(request)
                return FakeResponse(responses.pop(0))

            result = prepare_reel(video, "Diagnostic", settings, opener=fake_opener, sleep=Mock(), poll_interval=0)
            self.assertEqual(result, "container-1")
            self.assertEqual(len(requests), 4)
            self.assertTrue(all("/media_publish" not in request.full_url for request in requests))
            self.assertEqual(parse_qs(requests[0].data.decode())["caption"], ["Diagnostic"])

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

    def test_transient_upload_failure_retries_with_new_container_before_publishing(self) -> None:
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
            body = b'{"debug_info":{"retriable":false,"type":"ProcessingFailedError"}}'
            uploads = 0
            requests = []

            def fake_opener(request, timeout):
                nonlocal uploads
                requests.append(request)
                path = urlparse(request.full_url).path
                if path.endswith("/media"):
                    container = f"container-{len([r for r in requests if r.full_url.endswith('/media')])}"
                    return FakeResponse({"id": container, "uri": f"https://rupload.facebook.com/ig-api-upload/v27.0/{container}"})
                if urlparse(request.full_url).hostname == "rupload.facebook.com":
                    uploads += 1
                    if uploads == 1:
                        raise HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(body))
                    return FakeResponse({"success": True})
                if path.endswith("/media_publish"):
                    return FakeResponse({"id": "media-9"})
                return FakeResponse({"status_code": "FINISHED", "permalink": "https://www.instagram.com/reel/x/"})

            sleep = Mock()
            result = publish_reel(video_path, screenplay_path, settings, opener=fake_opener, sleep=sleep, poll_interval=0)

            self.assertEqual(result.container_id, "container-2")
            self.assertEqual(sum(r.full_url.endswith("/media_publish") for r in requests), 1)
            sleep.assert_any_call(30.0)

    def test_non_transient_failure_is_not_retried(self) -> None:
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
            calls = 0

            def fake_opener(request, timeout):
                nonlocal calls
                calls += 1
                body = json.dumps({"error": {"code": 190, "message": "Invalid OAuth access token"}}).encode("utf-8")
                raise HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(body))

            with self.assertRaisesRegex(RuntimeError, "code=190"):
                publish_reel(video_path, screenplay_path, settings, opener=fake_opener, sleep=Mock())
            self.assertEqual(calls, 1)

    def test_api_access_blocked_is_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video_path = root / "final.mp4"
            video_path.write_bytes(b"mock-mp4-content")
            settings = replace(
                Settings.from_env(project_root=root),
                instagram_access_token="fake-access-token",
                instagram_user_id="123456789012345",
            )
            calls = 0

            def fake_opener(request, timeout):
                nonlocal calls
                calls += 1
                body = json.dumps({"error": {"code": 200, "message": "API access blocked."}}).encode("utf-8")
                raise HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(body))

            with self.assertRaisesRegex(RuntimeError, "API access blocked"):
                from influencerai.services.instagram_service import MAX_PUBLISH_ATTEMPTS, _prepare_reel_with_retries

                _prepare_reel_with_retries(
                    video_path, "caption", settings, opener=fake_opener, sleep=Mock(), poll_interval=0, max_polls=1
                )
            self.assertEqual(calls, MAX_PUBLISH_ATTEMPTS)

    def test_large_video_is_recompressed_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video_path = root / "final.mp4"
            video_path.write_bytes(b"x" * 2048)
            settings = Settings.from_env(project_root=root)

            self.assertEqual(_fit_video_for_upload(video_path, settings), video_path)

            def fake_ffmpeg(_exe, arguments, **_kwargs):
                Path(arguments[-1]).write_bytes(b"y" * 2048)

            with patch("influencerai.services.instagram_service.MAX_UPLOAD_BYTES_BEFORE_COMPRESSION", 1024), patch(
                "influencerai.utils.ffmpeg_utils.resolve_ffmpeg", return_value="ffmpeg"
            ), patch("influencerai.utils.ffmpeg_utils.run_ffmpeg", side_effect=fake_ffmpeg):
                result = _fit_video_for_upload(video_path, settings)

            self.assertEqual(result.name, "instagram-reel.mp4")

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

    def test_upload_diagnostics_report_safe_request_and_response_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "final.mp4"
            video_bytes = b"private-video-content"
            video.write_bytes(video_bytes)
            error = HTTPError(
                "https://rupload.facebook.com/ig-api-upload/v27.0/container-1",
                400,
                "Bad Request",
                {"Proxy-Status": "http_request_error; secret=fake-access-token",
                 "x-fb-request-id": "request-123"},
                io.BytesIO(b'{"debug_info":{"message":"Request processing failed"}}'),
            )
            with (
                patch.dict(os.environ, {"INSTAGRAM_UPLOAD_DIAGNOSTICS": "1", "GITHUB_ACTIONS": "true"}),
                patch("influencerai.services.instagram_service.getproxies",
                      return_value={"https": "http://secret-proxy.example"}),
                self.assertLogs("influencerai.instagram", level="WARNING") as logs,
                self.assertRaises(RuntimeError),
            ):
                _upload_resumable(
                    error.url, video, access_token="fake-access-token", opener=Mock(side_effect=error),
                )
            output = "\n".join(logs.output)
            self.assertIn("environment=github-actions", output)
            self.assertIn(f"bytes={len(video_bytes)}", output)
            self.assertIn(f"sha256={hashlib.sha256(video_bytes).hexdigest()}", output)
            self.assertIn("api_version=v27.0", output)
            self.assertIn("https_proxy_configured=True", output)
            self.assertIn("response_status=400", output)
            self.assertIn("proxy_http_request_error=True", output)
            self.assertIn("fb_request_id_present=True", output)
            self.assertNotIn("fake-access-token", output)
            self.assertNotIn("secret-proxy.example", output)
            self.assertNotIn(video_bytes.decode(), output)
            self.assertNotIn("container-1", output)

    def test_upload_diagnostics_are_off_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "final.mp4"
            video.write_bytes(b"video")
            with (
                patch.dict(os.environ, {"INSTAGRAM_UPLOAD_DIAGNOSTICS": ""}),
                patch("influencerai.services.instagram_service.LOGGER.warning") as warning,
            ):
                _upload_resumable(
                    "https://rupload.facebook.com/ig-api-upload/v27.0/container-1",
                    video,
                    access_token="fake-access-token",
                    opener=Mock(return_value=FakeResponse({"success": True})),
                )
            warning.assert_not_called()

    def test_upload_diagnostics_report_success_and_transport_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "final.mp4"
            video.write_bytes(b"video")
            response = FakeResponse({"success": True})
            with patch.dict(os.environ, {"INSTAGRAM_UPLOAD_DIAGNOSTICS": "1"}):
                with self.assertLogs("influencerai.instagram", level="WARNING") as logs:
                    _upload_resumable(
                        "https://rupload.facebook.com/ig-api-upload/v27.0/container-1",
                        video, access_token="fake-access-token", opener=Mock(return_value=response),
                    )
                self.assertIn("response_status=200", "\n".join(logs.output))
                with (
                    self.assertLogs("influencerai.instagram", level="WARNING") as logs,
                    self.assertRaises(RuntimeError),
                ):
                    _upload_resumable(
                        "https://rupload.facebook.com/ig-api-upload/v27.0/container-1",
                        video, access_token="fake-access-token", opener=Mock(side_effect=TimeoutError()),
                    )
                self.assertIn("transport_error=TimeoutError", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
