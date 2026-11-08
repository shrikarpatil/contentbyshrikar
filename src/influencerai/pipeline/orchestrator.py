"""End-to-end screenplay-to-MP4 orchestration."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

from influencerai.config.settings import PROJECT_ROOT, Settings
from influencerai.pipeline.limits import (
    MAX_PRODUCTION_SCENES,
    MIN_PRODUCTION_SCENES,
    PREFERRED_NARRATION_WORDS,
    PREFERRED_VIDEO_DURATION_SECONDS,
)
from influencerai.providers.image_provider import ImageProvider
from influencerai.schemas.screenplay import Screenplay, load_screenplay
from influencerai.services.animation_service import animate_scene
from influencerai.services.audio_service import concatenate_narration
from influencerai.services.caption_service import CaptionCue, transcribe_scene, write_ass, write_srt
from influencerai.services.image_service import create_image_provider, generate_scene_images
from influencerai.services.video_assembly_service import assemble_video
from influencerai.services.voiceover_service import create_tts_provider, generate_voiceovers
from influencerai.utils.ffmpeg_utils import resolve_ffmpeg
from influencerai.utils.file_manager import create_run_directory
from influencerai.utils.logging import configure_run_logger


def _write_manifest(run_directory: Path, manifest: dict) -> None:
    path = run_directory / "pipeline-state.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _run_stage(run_directory: Path, manifest: dict, stage: str, action, logger):
    """Persist stage status before and after each expensive pipeline operation."""
    manifest["stages"][stage] = {"status": "running", "started_at": datetime.now().astimezone().isoformat()}
    _write_manifest(run_directory, manifest)
    try:
        result = action()
    except Exception as exc:
        manifest["stages"][stage] = {
            "status": "failed",
            "finished_at": datetime.now().astimezone().isoformat(),
            "error_type": type(exc).__name__,
        }
        _write_manifest(run_directory, manifest)
        logger.exception("Pipeline stage '%s' failed.", stage)
        raise
    manifest["stages"][stage] = {"status": "complete", "finished_at": datetime.now().astimezone().isoformat()}
    _write_manifest(run_directory, manifest)
    return result


def _caption_stage(
    screenplay: Screenplay,
    audio_paths: dict[int, Path],
    speech_durations: dict[int, float],
    scene_offsets: list[float],
    settings: Settings,
    run_directory: Path,
    logger,
) -> list[CaptionCue]:
    cues: list[CaptionCue] = []
    for scene, offset in zip(screenplay.scenes, scene_offsets):
        cues.extend(
            transcribe_scene(
                audio_paths[scene.scene_id],
                scene.voiceover,
                scene_start=offset,
                speech_duration=speech_durations[scene.scene_id],
                provider=settings.transcription_provider,
                model_name=settings.whisper_model,
                model_cache=settings.output_directory / ".cache" / "whisper",
                logger=logger,
            )
        )
    cues.sort(key=lambda cue: cue.start)
    write_srt(cues, run_directory / "captions" / "captions.srt")
    write_ass(cues, run_directory / "captions" / "captions.ass")
    return cues


def run_pipeline(
    screenplay_path: Path | None = None,
    *,
    output_directory: Path | None = None,
    image_provider: ImageProvider | None = None,
    settings: Settings | None = None,
) -> Path:
    """Generate an MP4 and return its path; default to the bundled sample screenplay."""
    settings = settings or Settings.from_env()
    screenplay_path = screenplay_path or PROJECT_ROOT / "input" / "screenplays" / "sample.json"
    screenplay = load_screenplay(screenplay_path)
    if not MIN_PRODUCTION_SCENES <= len(screenplay.scenes) <= MAX_PRODUCTION_SCENES:
        raise ValueError(
            f"Production videos require {MIN_PRODUCTION_SCENES}-{MAX_PRODUCTION_SCENES} scene images; "
            f"received {len(screenplay.scenes)}."
        )
    planned_duration = sum(scene.duration for scene in screenplay.scenes)
    narration_words = sum(len(scene.voiceover.split()) for scene in screenplay.scenes)
    if planned_duration > PREFERRED_VIDEO_DURATION_SECONDS or screenplay.target_duration > PREFERRED_VIDEO_DURATION_SECONDS:
        logger = logging.getLogger("influencerai.pipeline")
        logger.info(
            "Screenplay plans %.1fs (preferred target %.1fs); longer output is allowed.",
            max(planned_duration, screenplay.target_duration),
            PREFERRED_VIDEO_DURATION_SECONDS,
        )
    if narration_words > PREFERRED_NARRATION_WORDS:
        logging.getLogger("influencerai.pipeline").info(
            "Screenplay has %d narration words (preferred around %d); preserving natural narration.",
            narration_words,
            PREFERRED_NARRATION_WORDS,
        )
    if output_directory is not None:
        from dataclasses import replace

        settings = replace(settings, output_directory=output_directory)
    executable = resolve_ffmpeg(settings.ffmpeg_path)
    run_directory = create_run_directory(screenplay, settings.output_directory)
    logger = configure_run_logger(run_directory)
    manifest = {
        "video_title": screenplay.video_title,
        "screenplay_source": str(screenplay_path.resolve()),
        "image_provider": image_provider.name if image_provider else settings.image_provider,
        "created_at": datetime.now().astimezone().isoformat(),
        "stages": {},
    }
    _write_manifest(run_directory, manifest)
    logger.info("Starting video pipeline for %r.", screenplay.video_title)
    logger.info("Using FFmpeg at %s.", executable)

    tts_provider = create_tts_provider(settings)
    if settings.tts_provider == "openai":
        (run_directory / "voice-disclosure.txt").write_text(
            "This video contains AI-generated narration. Disclose synthetic narration to viewers when publishing.\n",
            encoding="utf-8",
        )
    voiceover_result = _run_stage(
        run_directory,
        manifest,
        "voiceovers",
        lambda: generate_voiceovers(screenplay, run_directory, tts_provider, logger),
        logger,
    )
    audio_paths, speech_durations, timed_audio = voiceover_result
    scene_durations = [segment.duration for segment in timed_audio]
    total_duration = sum(scene_durations)
    if total_duration > PREFERRED_VIDEO_DURATION_SECONDS:
        logger.info(
            "Voiceover timing produces %.2fs; preferred target is under 30s, but longer videos are allowed.",
            total_duration,
        )

    narration_path = run_directory / "audio" / "narration.wav"
    offsets = _run_stage(
        run_directory,
        manifest,
        "narration_timeline",
        lambda: concatenate_narration(timed_audio, narration_path),
        logger,
    )
    scene_offsets: list[float] = offsets

    selected_image_provider = image_provider or create_image_provider(settings)
    image_result = _run_stage(
        run_directory,
        manifest,
        "images",
        lambda: generate_scene_images(screenplay, run_directory, settings, selected_image_provider, logger),
        logger,
    )
    image_paths, image_stats = image_result
    manifest["image_usage"] = {
        "provider": image_stats.provider,
        "requests": image_stats.requests,
        "cache_hits": image_stats.cache_hits,
        "estimated_cost_usd": (
            image_stats.requests * settings.image_cost_per_image_usd
            if settings.image_cost_per_image_usd is not None and image_stats.provider == "openai"
            else None
        ),
    }
    _write_manifest(run_directory, manifest)

    _run_stage(
        run_directory,
        manifest,
        "captions",
        lambda: _caption_stage(screenplay, audio_paths, speech_durations, scene_offsets, settings, run_directory, logger),
        logger,
    )
    clip_paths: list[Path] = []

    def render_clips() -> list[Path]:
        for scene, duration in zip(screenplay.scenes, scene_durations):
            clip_path = run_directory / "clips" / f"scene_{scene.scene_id:03d}.mp4"
            animate_scene(
                executable,
                image_paths[scene.scene_id],
                clip_path,
                duration=duration,
                motion=scene.motion,
                transition=scene.transition,
                width=settings.video_width,
                height=settings.video_height,
                fps=settings.video_fps,
            )
            clip_paths.append(clip_path)
            logger.info("Rendered scene %03d (%.2fs, %s).", scene.scene_id, duration, scene.motion)
        return clip_paths

    _run_stage(run_directory, manifest, "animation", render_clips, logger)
    final_video = _run_stage(
        run_directory,
        manifest,
        "assembly_and_validation",
        lambda: assemble_video(
            executable,
            screenplay,
            run_directory,
            settings,
            clip_paths,
            narration_path,
            scene_offsets,
            total_duration,
            logger,
        ),
        logger,
    )
    manifest["output_video"] = str(final_video.resolve())
    manifest["duration_seconds"] = round(total_duration, 3)
    _write_manifest(run_directory, manifest)
    logger.info("Finished %.2fs video: %s", total_duration, final_video)
    return final_video


def main() -> int:
    """CLI entry point; no arguments generate the sample video."""
    parser = argparse.ArgumentParser(description="Generate a vertical video from screenplay JSON.")
    parser.add_argument(
        "screenplay",
        nargs="?",
        type=Path,
        default=PROJECT_ROOT / "input" / "screenplays" / "sample.json",
        help="Screenplay JSON (defaults to input/screenplays/sample.json).",
    )
    parser.add_argument("--output-dir", type=Path, help="Override OUTPUT_DIRECTORY.")
    parser.add_argument("--image-provider", choices=("local", "openai"), help="Override IMAGE_PROVIDER; OpenAI image requests are billed.")
    parser.add_argument("--image-model", help="Override OPENAI_IMAGE_MODEL (current default: gpt-image-2.6-flare).")
    parser.add_argument("--tts-provider", choices=("local", "openai"), help="Override TTS_PROVIDER; OpenAI speech requests are billed.")
    parser.add_argument("--tts-voice", help="Override TTS_VOICE (e.g. marin, cedar, coral, or a Windows SAPI voice name).")
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
        if args.image_provider or args.image_model:
            from dataclasses import replace

            settings = replace(
                settings,
                image_provider=args.image_provider or settings.image_provider,
                openai_image_model=args.image_model or settings.openai_image_model,
            )
        if args.tts_provider or args.tts_voice:
            from dataclasses import replace

            settings = replace(
                settings,
                tts_provider=args.tts_provider or settings.tts_provider,
                tts_voice=args.tts_voice or settings.tts_voice,
            )
        video_path = run_pipeline(args.screenplay, output_directory=args.output_dir, settings=settings)
        print(f"Video created: {video_path}")
        return 0
    except Exception as exc:
        logging.getLogger("influencerai").error("Video generation failed: %s", exc)
        print(f"Video generation failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())