"""Tests for media helpers and the end-to-end video orchestration path."""

from __future__ import annotations

import json
import logging
import tempfile
from email.message import Message
from types import SimpleNamespace
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from influencerai.config.settings import Settings
from influencerai.pipeline.orchestrator import run_pipeline
from influencerai.providers.image_provider import LocalIllustrationProvider, OpenAIImageProvider
from influencerai.providers.tts_provider import OpenAITTSProvider
from influencerai.schemas.screenplay import Screenplay
from influencerai.services.animation_service import build_animation_command
from influencerai.services.audio_service import (
    TimedAudio,
    concatenate_narration,
    find_music,
    find_or_download_music,
    find_sound_effect,
)
from influencerai.services.caption_service import estimate_cues, write_ass, write_srt
from influencerai.services.image_service import ImageGenerationStats
from influencerai.services.video_assembly_service import (
    MAX_BACKGROUND_MUSIC_GAIN,
    MIN_BACKGROUND_MUSIC_GAIN,
    _background_music_gain,
    _caption_filter,
    assemble_video,
)


def make_screenplay() -> Screenplay:
    return Screenplay.model_validate(
        {
            "video_title": "Pipeline Test",
            "target_duration": 26,
            "aspect_ratio": "9:16",
            "scenes": [
                {
                    "scene_id": scene_id,
                    "duration": 5.2,
                    "voiceover": f"Test scene {scene_id}.",
                    "image_prompt": f"Photorealistic documentary scene {scene_id}, detailed composition.",
                    "motion": "static",
                    "on_screen_text": f"SCENE {scene_id}",
                    "transition": "cut",
                }
                for scene_id in range(1, 6)
            ],
        }
    )


def make_wav(path: Path, duration: float = 1.0) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x01\x00" * round(8000 * duration))


class MediaServiceTests(unittest.TestCase):
    def test_animation_command_targets_vertical_h264_output(self) -> None:
        arguments = build_animation_command(
            Path("scene.png"),
            Path("scene.mp4"),
            duration=4,
            motion="slow_zoom_in",
            transition="fade",
            width=1080,
            height=1920,
            fps=30,
        )
        joined = " ".join(arguments)
        self.assertIn("zoompan=", joined)
        self.assertIn("fade=t=in", joined)
        self.assertIn("1080x1920", joined)
        self.assertIn("yuv420p", joined)
        self.assertIn("libx264", joined)

    def test_caption_helpers_write_srt_and_ass(self) -> None:
        cues = estimate_cues("A short caption test works very well.", 2, 1.5)
        self.assertGreater(len(cues), 1)
        self.assertEqual(cues[0].start, 2)
        with tempfile.TemporaryDirectory() as temp_dir:
            srt = Path(temp_dir) / "captions.srt"
            ass = Path(temp_dir) / "captions.ass"
            write_srt(cues, srt)
            write_ass(cues, ass)
            self.assertIn(" --> ", srt.read_text(encoding="utf-8-sig"))
            self.assertIn("[Events]", ass.read_text(encoding="utf-8"))

    def test_caption_style_uses_smaller_font_and_higher_safe_area(self) -> None:
        self.assertIn("captions/captions.ass", _caption_filter())
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "captions.ass"
            write_ass([estimate_cues("A caption style check.", 0, 1)[0]], path)
            style = next(line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("Style: Default"))
            self.assertIn("Arial,40,", style)
            self.assertTrue(style.endswith(",510,1"))
            content = path.read_text(encoding="utf-8")
            self.assertIn("PlayResX: 1080", content)
            self.assertIn("PlayResY: 1920", content)

    def test_background_music_is_capped_and_ducked_under_narration(self) -> None:
        screenplay_payload = make_screenplay().model_dump(mode="json")
        screenplay_payload["background_music"] = {"mood": "mysterious", "volume": 0.5}
        screenplay = Screenplay.model_validate(screenplay_payload)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_directory = root / "run"
            run_directory.mkdir()
            silent_video = run_directory / "visuals.mp4"
            silent_video.touch()
            final_video = run_directory / "final.mp4"
            settings = replace(
                Settings.from_env(project_root=root),
                music_directory=root / "music",
                sound_effects_directory=root / "effects",
            )
            logger = logging.getLogger("test.audio-mix")
            with (
                patch("influencerai.services.video_assembly_service._concat_visuals", return_value=silent_video),
                patch(
                    "influencerai.services.video_assembly_service.find_or_download_music",
                    return_value=root / "music" / "track.mp3",
                ),
                patch("influencerai.services.video_assembly_service.find_sound_effect", return_value=None),
                patch("influencerai.services.video_assembly_service.run_ffmpeg") as ffmpeg,
            ):
                def create_final_video(*_args, stage, **_kwargs):
                    if stage == "final video assembly":
                        final_video.write_bytes(b"video" * 300)

                ffmpeg.side_effect = create_final_video
                result = assemble_video(
                    "ffmpeg",
                    screenplay,
                    run_directory,
                    settings,
                    [run_directory / "scene.mp4"],
                    root / "narration.wav",
                    [0, 5.2, 10.4, 15.6, 20.8],
                    26,
                    logger,
                )

        self.assertEqual(result, final_video)
        filter_complex = ffmpeg.call_args_list[0].args[1][
            ffmpeg.call_args_list[0].args[1].index("-filter_complex") + 1
        ]
        self.assertIn(f"volume={MAX_BACKGROUND_MUSIC_GAIN:.3f}", filter_complex)
        self.assertIn("sidechaincompress=threshold=0.04:ratio=6:attack=25:release=450", filter_complex)
        self.assertEqual(_background_music_gain(0.05), MIN_BACKGROUND_MUSIC_GAIN)
        self.assertEqual(_background_music_gain(0.14), 0.14)
        self.assertEqual(_background_music_gain(0.5), MAX_BACKGROUND_MUSIC_GAIN)
        self.assertEqual(_background_music_gain(0), 0)

    def test_audio_timeline_pads_scene_to_requested_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.wav"
            second_source = Path(temp_dir) / "second.wav"
            output = Path(temp_dir) / "narration.wav"
            make_wav(source, duration=0.5)
            make_wav(second_source, duration=0.75)
            offsets = concatenate_narration([TimedAudio(source, 2.0), TimedAudio(second_source, 1.5)], output)
            with wave.open(str(output), "rb") as audio:
                self.assertEqual(audio.getnframes() / audio.getframerate(), 3.5)
            self.assertEqual(offsets, [0.0, 2.0])

    def test_local_image_provider_creates_portrait_png(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "scene.png"
            LocalIllustrationProvider().generate("A cinematic desert city.", output, 180, 320)
            from PIL import Image

            with Image.open(output) as image:
                self.assertEqual(image.size, (180, 320))
                self.assertEqual(image.format, "PNG")

    def test_openai_tts_provider_streams_wav_with_natural_voice_instructions(self) -> None:
        response_context = Mock()

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def stream_to_file(self, path):
                Path(path).write_bytes(b"\x00\x00" * 12000)

        response_context.return_value = FakeResponse()
        fake_client = SimpleNamespace(
            audio=SimpleNamespace(
                speech=SimpleNamespace(
                    with_streaming_response=SimpleNamespace(create=response_context),
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as temp_dir, patch("openai.OpenAI", return_value=fake_client):
            output = Path(temp_dir) / "narration.wav"
            provider = OpenAITTSProvider(
                "test-key",
                "gpt-4o-mini-tts",
                "marin",
                "Warm natural documentary narration with conversational delivery.",
            )
            provider.synthesize("A short narrated line.", output)
            with wave.open(str(output), "rb") as generated:
                self.assertEqual(generated.getnchannels(), 1)
                self.assertEqual(generated.getsampwidth(), 2)
                self.assertEqual(generated.getframerate(), 24000)
                self.assertEqual(generated.getnframes(), 12000)
        response_context.assert_called_once_with(
            model="gpt-4o-mini-tts",
            voice="marin",
            input="A short narrated line.",
            instructions="Warm natural documentary narration with conversational delivery.",
            response_format="pcm",
        )

    def test_openai_image_provider_uses_configured_current_model(self) -> None:
        image_generate = Mock(
            return_value=SimpleNamespace(data=[SimpleNamespace(b64_json="aGVsbG8=")]),
        )
        fake_client = SimpleNamespace(images=SimpleNamespace(generate=image_generate))
        with tempfile.TemporaryDirectory() as temp_dir, patch("openai.OpenAI", return_value=fake_client):
            output = Path(temp_dir) / "image.png"
            provider = OpenAIImageProvider("test-key", "gpt-image-2.6-flare", "medium", "1024x1536")
            provider.generate("A portrait cinematic image.", output, 1080, 1920)
            self.assertEqual(output.read_bytes(), b"hello")
        image_generate.assert_called_once_with(
            model="gpt-image-2.6-flare",
            prompt="A portrait cinematic image.",
            size="1024x1536",
            quality="medium",
            n=1,
        )

    def test_retired_image_model_setting_is_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {"OPENAI_IMAGE_MODEL": "gpt-image-1"},
        ):
            settings = Settings.from_env(project_root=Path(temp_dir))
        self.assertEqual(settings.openai_image_model, "gpt-image-2.6-flare")

    def test_music_picker_uses_packaged_mixkit_mood_track(self) -> None:
        music_path = Path(__file__).resolve().parents[1] / "assets" / "music"
        track = find_music(music_path, "mysterious")
        self.assertIsNotNone(track)
        self.assertTrue(track and track.is_file())
        self.assertEqual(track.name, "silent-descent-eugenio-mininni-mixkit.mp3")

    def test_curated_music_library_matches_requested_moods(self) -> None:
        music_path = Path(__file__).resolve().parents[1] / "assets" / "music"
        self.assertEqual(find_music(music_path, "uplifting inspirational" ).name, "driving-ambition-ahjay-stelino-mixkit.mp3")
        self.assertEqual(find_music(music_path, "calm peaceful" ).name, "serene-view-arulo-mixkit.mp3")
        self.assertEqual(find_music(music_path, "mysterious cinematic" ).name, "silent-descent-eugenio-mininni-mixkit.mp3")

    def test_screenplay_sound_effect_stems_resolve_inside_workspace(self) -> None:
        effects_path = Path(__file__).resolve().parents[1] / "assets" / "sound_effects"
        for stem in (
            "fast-rocket-whoosh",
            "fast-sweep-transition",
            "cinematic-swoosh-heartbeat",
            "cinematic-tunnel-whoosh",
        ):
            with self.subTest(stem=stem):
                effect = find_sound_effect(effects_path, stem)
                self.assertIsNotNone(effect)
                self.assertTrue(effect and effect.is_file())

    def test_empty_music_folder_downloads_curated_licensed_fallback(self) -> None:
        response = Mock()
        response.headers = Message()
        response.headers["Content-Type"] = "audio/mpeg"
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.read.return_value = b"ID3" + b"\x00" * 12_000
        logger = Mock()
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "influencerai.services.audio_service.urlopen",
            return_value=response,
        ) as urlopen:
            directory = Path(temp_dir) / "music"
            music = find_or_download_music(directory, "mysterious", logger)
            self.assertIsNotNone(music)
            self.assertEqual(music.name, "silent-descent-eugenio-mininni-mixkit.mp3")
            self.assertTrue((directory / "ATTRIBUTION.txt").is_file())
            self.assertIn("Mixkit Stock Music Free License", (directory / "ATTRIBUTION.txt").read_text(encoding="utf-8"))
            urlopen.assert_called_once()

    def test_music_auto_download_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "influencerai.services.audio_service.urlopen",
        ) as urlopen:
            result = find_or_download_music(Path(temp_dir) / "music", "mysterious", Mock(), auto_download=False)
            self.assertIsNone(result)
            urlopen.assert_not_called()

    def test_video_over_30_seconds_is_allowed_and_logged_as_preference(self) -> None:
        screenplay = make_screenplay()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            screenplay_path = root / "screenplay.json"
            screenplay_path.write_text(json.dumps(screenplay.model_dump(mode="json")), encoding="utf-8")
            settings = replace(
                Settings.from_env(project_root=root),
                output_directory=root / "output",
                tts_provider="local",
                video_width=180,
                video_height=320,
                video_fps=5,
            )
            voiceover_result = (
                {scene.scene_id: root / f"scene-{scene.scene_id}.wav" for scene in screenplay.scenes},
                {scene.scene_id: 5.8 for scene in screenplay.scenes},
                [TimedAudio(root / f"scene-{scene.scene_id}.wav", 6.0) for scene in screenplay.scenes],
            )
            image_provider = Mock()
            image_provider.name = "mock-paid-provider"
            image_paths = {scene.scene_id: root / f"scene-{scene.scene_id}.png" for scene in screenplay.scenes}
            image_result = (image_paths, ImageGenerationStats("mock-paid-provider", 5, 0))
            final_video = root / "final.mp4"
            with (
                patch("influencerai.pipeline.orchestrator.resolve_ffmpeg", return_value="mock-ffmpeg"),
                patch("influencerai.pipeline.orchestrator.configure_run_logger", return_value=Mock()),
                patch("influencerai.pipeline.orchestrator.generate_voiceovers", return_value=voiceover_result),
                patch("influencerai.pipeline.orchestrator.concatenate_narration", return_value=[0, 6, 12, 18, 24]),
                patch("influencerai.pipeline.orchestrator.generate_scene_images", return_value=image_result) as generate_images,
                patch("influencerai.pipeline.orchestrator._caption_stage", return_value=[]),
                patch("influencerai.pipeline.orchestrator.animate_scene"),
                patch("influencerai.pipeline.orchestrator.assemble_video", return_value=final_video) as assemble,
            ):
                result = run_pipeline(screenplay_path, image_provider=image_provider, settings=settings)
            self.assertEqual(result, final_video)
            generate_images.assert_called_once()
            self.assertEqual(assemble.call_args.args[7], 30.0)

    def test_end_to_end_pipeline_writes_video_and_stage_state(self) -> None:
        screenplay = make_screenplay()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            screenplay_path = root / "screenplay.json"
            screenplay_path.write_text(json.dumps(screenplay.model_dump(mode="json")), encoding="utf-8")
            settings = replace(
                Settings.from_env(project_root=root),
                output_directory=root / "output",
                video_width=180,
                video_height=320,
                video_fps=5,
                ffmpeg_path="ffmpeg",
                transcription_provider="screenplay",
            )
            provider = LocalIllustrationProvider()
            with patch("influencerai.pipeline.orchestrator.resolve_ffmpeg", return_value="mock-ffmpeg"), patch(
                "influencerai.pipeline.orchestrator.animate_scene"
            ) as animate, patch("influencerai.pipeline.orchestrator.assemble_video") as assemble:
                def create_test_voiceovers(screenplay_arg, run_directory, provider_arg, logger):
                    audio_paths = {}
                    speech_durations = {}
                    timeline = []
                    for scene in screenplay_arg.scenes:
                        audio_path = run_directory / "audio" / f"scene_{scene.scene_id:03d}.wav"
                        make_wav(audio_path, duration=0.5)
                        audio_paths[scene.scene_id] = audio_path
                        speech_durations[scene.scene_id] = 0.5
                        timeline.append(TimedAudio(audio_path, scene.duration))
                    return audio_paths, speech_durations, timeline

                def create_test_narration(segments, output_path):
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    make_wav(output_path, duration=3.0)
                    return [0.0, 5.2, 10.4, 15.6, 20.8]

                def create_test_video(*args, **kwargs):
                    result = args[2] / "final.mp4"
                    result.write_bytes(b"video" * 400)
                    return result

                with patch("influencerai.pipeline.orchestrator.generate_voiceovers", side_effect=create_test_voiceovers), patch(
                    "influencerai.pipeline.orchestrator.concatenate_narration", side_effect=create_test_narration
                ), patch("influencerai.pipeline.orchestrator.assemble_video", side_effect=create_test_video):
                    video_path = run_pipeline(screenplay_path, image_provider=provider, settings=settings)

                    logger = logging.getLogger(f"influencerai.run.{video_path.parent.name}")

                    def close_test_logger() -> None:
                        for handler in logger.handlers[:]:
                            handler.close()
                            logger.removeHandler(handler)

            self.assertTrue(video_path.is_file())
            manifest = json.loads((video_path.parent / "pipeline-state.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["stages"]["images"]["status"], "complete")
            self.assertEqual(manifest["stages"]["assembly_and_validation"]["status"], "complete")
            self.assertTrue((video_path.parent / "captions" / "captions.srt").is_file())
            self.assertEqual(animate.call_count, 5)  # mocked by orchestration test
            self.assertEqual(assemble.call_count, 0)  # mocked explicitly above
            close_test_logger()


if __name__ == "__main__":
    unittest.main()