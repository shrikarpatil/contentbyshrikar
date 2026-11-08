"""Create and validate per-scene WAV narration files."""

from __future__ import annotations

import wave
from pathlib import Path

from influencerai.config.settings import Settings
from influencerai.providers.tts_provider import OpenAITTSProvider, TTSProvider, WindowsSapiTTSProvider
from influencerai.schemas.screenplay import Screenplay
from influencerai.services.audio_service import TimedAudio, wav_duration


def create_tts_provider(settings: Settings) -> TTSProvider:
    """Build the configured local TTS provider."""
    if settings.tts_provider == "local":
        return WindowsSapiTTSProvider(settings.tts_voice)
    if settings.tts_provider == "openai":
        return OpenAITTSProvider(
            settings.openai_api_key,
            settings.tts_model or "gpt-4o-mini-tts",
            settings.tts_voice,
            settings.tts_instructions,
        )
    raise ValueError(f"Unsupported TTS_PROVIDER: {settings.tts_provider}")


def generate_voiceovers(
    screenplay: Screenplay,
    run_directory: Path,
    provider: TTSProvider,
    logger,
) -> tuple[dict[int, Path], dict[int, float], list[TimedAudio]]:
    """Synthesize narration and extend scene timing rather than speeding up speech."""
    audio_paths: dict[int, Path] = {}
    speech_durations: dict[int, float] = {}
    timeline: list[TimedAudio] = []
    for scene in screenplay.scenes:
        path = run_directory / "audio" / f"scene_{scene.scene_id:03d}.wav"
        logger.info("Creating local voiceover for scene %03d.", scene.scene_id)
        provider.synthesize(scene.voiceover, path)
        with wave.open(str(path), "rb") as audio:
            if audio.getnframes() == 0:
                raise RuntimeError(f"Voiceover for scene {scene.scene_id} is empty.")
        speech_duration = wav_duration(path)
        final_duration = max(scene.duration, speech_duration + 0.25)
        if final_duration > scene.duration:
            logger.warning(
                "Scene %03d extended from %.2fs to %.2fs so narration is not cut off.",
                scene.scene_id,
                scene.duration,
                final_duration,
            )
        audio_paths[scene.scene_id] = path
        speech_durations[scene.scene_id] = speech_duration
        timeline.append(TimedAudio(path, final_duration))
    return audio_paths, speech_durations, timeline