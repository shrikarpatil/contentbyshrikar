"""Concatenate scene clips and mux captions and optional soundtrack assets."""

from __future__ import annotations

from pathlib import Path

from influencerai.config.settings import Settings
from influencerai.pipeline.limits import PREFERRED_VIDEO_DURATION_SECONDS
from influencerai.schemas.screenplay import Screenplay
from influencerai.services.audio_service import SoundEffect, find_or_download_music, find_sound_effect
from influencerai.utils.ffmpeg_utils import run_ffmpeg

MIN_BACKGROUND_MUSIC_GAIN = 0.12
MAX_BACKGROUND_MUSIC_GAIN = 0.18


def _background_music_gain(requested_volume: float) -> float:
    if requested_volume <= 0:
        return 0.0
    return min(max(requested_volume, MIN_BACKGROUND_MUSIC_GAIN), MAX_BACKGROUND_MUSIC_GAIN)


def _concat_visuals(executable: str, run_directory: Path, clip_paths: list[Path], logger) -> Path:
    concat_file = run_directory / "clips.txt"
    lines = [f"file '{path.relative_to(run_directory).as_posix()}'" for path in clip_paths]
    concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    silent_video = run_directory / "clips" / "visuals.mp4"
    run_ffmpeg(
        executable,
        [
            "-y", "-f", "concat", "-safe", "0", "-i", "clips.txt",
            "-an", "-c:v", "copy", "-movflags", "+faststart", str(silent_video),
        ],
        cwd=run_directory,
        stage="scene concatenation",
    )
    logger.info("Concatenated %d scene clips.", len(clip_paths))
    return silent_video


def _caption_filter() -> str:
    return "subtitles=filename='captions/captions.ass'"


def assemble_video(
    executable: str,
    screenplay: Screenplay,
    run_directory: Path,
    settings: Settings,
    clip_paths: list[Path],
    narration_path: Path,
    scene_offsets: list[float],
    total_duration: float,
    logger,
) -> Path:
    """Create the final portrait H.264/AAC MP4 with burned-in captions."""
    if total_duration <= 0:
        raise ValueError("Video duration must be positive.")
    if total_duration > PREFERRED_VIDEO_DURATION_SECONDS:
        logger.info(
            "Assembling %.2fs video, above the preferred %.1fs target; user allows longer videos when needed.",
            total_duration,
            PREFERRED_VIDEO_DURATION_SECONDS,
        )
    silent_video = _concat_visuals(executable, run_directory, clip_paths, logger)
    arguments = ["-y", "-i", str(silent_video), "-i", str(narration_path)]

    music = find_or_download_music(
        settings.music_directory,
        screenplay.background_music.mood,
        logger,
        auto_download=settings.auto_download_music,
    )
    if music:
        arguments.extend(("-stream_loop", "-1", "-i", str(music)))
        logger.info("Using soundtrack asset %s.", music.name)
    elif screenplay.background_music.mood:
        logger.warning("No local music found for mood '%s'; continuing without background music.", screenplay.background_music.mood)

    effects: list[SoundEffect] = []
    cursor = 0.0
    for scene, offset in zip(screenplay.scenes, scene_offsets):
        effect_path = find_sound_effect(settings.sound_effects_directory, scene.sound_effect or "")
        if scene.sound_effect and not effect_path:
            logger.warning("Sound effect '%s' was not found; skipping it.", scene.sound_effect)
        if effect_path:
            effects.append(SoundEffect(effect_path, offset))
        cursor = offset
    del cursor
    for effect in effects:
        arguments.extend(("-i", str(effect.path)))

    filters = [f"[0:v]{_caption_filter()}[vout]"]
    audio_labels = ["[1:a]"]
    if music:
        music_index = 2
        volume = _background_music_gain(screenplay.background_music.volume)
        filters.append(f"[{music_index}:a]volume={volume:.3f},atrim=duration={total_duration:.3f}[bg]")
        filters.append("[bg][1:a]sidechaincompress=threshold=0.04:ratio=6:attack=25:release=450[ducked]")
        audio_labels.append("[ducked]")
        next_input = 3
    else:
        next_input = 2

    for index, effect in enumerate(effects):
        delay_ms = round(effect.start_seconds * 1000)
        label = f"[effect{index}]"
        filters.append(f"[{next_input}:a]adelay=delays={delay_ms}:all=1,volume=0.35{label}")
        audio_labels.append(label)
        next_input += 1

    if len(audio_labels) == 1:
        filters.append("[1:a]loudnorm=I=-16:LRA=11:TP=-1.5[aout]")
    else:
        labels = "".join(audio_labels)
        filters.append(f"{labels}amix=inputs={len(audio_labels)}:duration=first:dropout_transition=2,loudnorm=I=-16:LRA=11:TP=-1.5[aout]")

    final_video = run_directory / "final.mp4"
    arguments.extend(
        [
            "-filter_complex", ";".join(filters),
            "-map", "[vout]", "-map", "[aout]",
            "-t", f"{total_duration:.3f}",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22", "-threads", "2",
            "-pix_fmt", "yuv420p", "-r", str(settings.video_fps),
            "-c:a", "aac", "-ar", "48000", "-b:a", "112k", "-movflags", "+faststart", str(final_video),
        ]
    )
    run_ffmpeg(executable, arguments, cwd=run_directory, stage="final video assembly")
    if not final_video.is_file() or final_video.stat().st_size < 1024:
        raise RuntimeError("Final MP4 was not created or is unexpectedly small.")
    run_ffmpeg(executable, ["-v", "error", "-i", str(final_video), "-f", "null", "-"], cwd=run_directory, stage="final MP4 validation")
    return final_video