"""Narration WAV timing, concatenation, and optional licensed audio lookup."""

from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


MIXKIT_TRACK_URL = "https://assets.mixkit.co/music/614/614.mp3"
MIXKIT_TRACK_NAME = "silent-descent-eugenio-mininni-mixkit.mp3"
MIXKIT_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MUSIC_MOOD_TAGS = {
    "driving-ambition-ahjay-stelino-mixkit": {"uplifting", "inspirational", "inspiring", "motivational", "energetic", "ambitious"},
    "serene-view-arulo-mixkit": {"calm", "serene", "peaceful", "ambient", "reflective", "soft"},
    "silent-descent-eugenio-mininni-mixkit": {"mysterious", "mystery", "cinematic", "suspense", "dramatic", "tense"},
}
MIXKIT_ATTRIBUTION = """Music asset: “Silent Descent” — Eugenio Mininni
Downloaded automatically from Mixkit's free-stock-music cinematic catalog.
Direct audio: https://assets.mixkit.co/music/614/614.mp3
License: Mixkit Stock Music Free License — https://mixkit.co/license/#musicFree

Mixkit's license page states free-license stock music may be used in commercial and non-commercial projects, including social-media video posts, advertisements, and YouTube videos. The license does not permit standalone redistribution, claiming the track as your own, incorporating it into a music-only track/remix, or registering it with a rights-management service. The license includes additional terms and limits; review the linked current terms before publishing or monetizing.
"""


@dataclass(frozen=True, slots=True)
class TimedAudio:
    """One narration WAV and its final timeline duration."""

    path: Path
    duration: float


@dataclass(frozen=True, slots=True)
class SoundEffect:
    """An optional sound effect with its start offset in seconds."""

    path: Path
    start_seconds: float


def wav_duration(path: Path) -> float:
    """Return PCM WAV duration in seconds."""
    with wave.open(str(path), "rb") as audio:
        return audio.getnframes() / audio.getframerate()


def concatenate_narration(segments: list[TimedAudio], output_path: Path) -> list[float]:
    """Join scene narration and pad each segment with silence to its scene duration."""
    if not segments:
        raise ValueError("At least one narration segment is required.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    offsets: list[float] = []
    cursor = 0.0
    with wave.open(str(segments[0].path), "rb") as first:
        params = first.getparams()
    sample_rate = params.framerate
    frame_width = params.sampwidth * params.nchannels

    with wave.open(str(output_path), "wb") as output:
        output.setparams(params)
        for segment in segments:
            offsets.append(cursor)
            with wave.open(str(segment.path), "rb") as source:
                source_params = source.getparams()
                source_format = (source_params.nchannels, source_params.sampwidth, source_params.framerate, source_params.comptype)
                output_format = (params.nchannels, params.sampwidth, params.framerate, params.comptype)
                if source_format != output_format:
                    raise ValueError("Narration WAV files have incompatible sample formats.")
                frame_count = source.getnframes()
                frames = source.readframes(frame_count)
            target_frames = round(segment.duration * sample_rate)
            if frame_count > target_frames:
                raise ValueError("Scene duration is shorter than its narration; increase the scene duration.")
            output.writeframesraw(frames)
            silence_frames = target_frames - frame_count
            if silence_frames:
                output.writeframesraw(b"\x00" * silence_frames * frame_width)
            cursor += segment.duration
    return offsets


def find_music(directory: Path, mood: str) -> Path | None:
    """Select a local audio file by mood, without downloading unlicensed music."""
    extensions = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"}
    if not directory.is_dir():
        return None
    files = sorted(path for path in directory.iterdir() if path.is_file() and path.suffix.lower() in extensions)
    if not files:
        return None
    mood_words = {word.lower() for word in mood.split() if len(word) > 2}
    for path in files:
        stem_words = set(path.stem.lower().replace("_", " ").replace("-", " ").split())
        curated_tags = MUSIC_MOOD_TAGS.get(path.stem.lower(), set())
        if mood_words.intersection(stem_words | curated_tags):
            return path
    return files[0]


def download_mixkit_fallback(directory: Path, logger) -> Path | None:
    """Download one curated Mixkit Free License track; fail softly if offline."""
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / MIXKIT_TRACK_NAME
    attribution = directory / "ATTRIBUTION.txt"
    if destination.is_file() and destination.stat().st_size > 10_000:
        if not attribution.exists():
            attribution.write_text(MIXKIT_ATTRIBUTION, encoding="utf-8")
        return destination

    request = Request(MIXKIT_TRACK_URL, headers={"User-Agent": "InfluencerAI/0.1"})
    temporary_path = destination.with_suffix(destination.suffix + ".part")
    try:
        with urlopen(request, timeout=30) as response:
            content_type = response.headers.get_content_type()
            if content_type not in {"audio/mpeg", "application/octet-stream", "binary/octet-stream"}:
                raise ValueError(f"Expected MP3 audio from Mixkit, received {content_type}.")
            data = response.read(MIXKIT_MAX_DOWNLOAD_BYTES + 1)
        if len(data) <= 10_000 or len(data) > MIXKIT_MAX_DOWNLOAD_BYTES:
            raise ValueError("Mixkit returned an empty or unexpectedly large audio file.")
        if not (data.startswith(b"ID3") or (data[0] == 0xFF and data[1] & 0xE0 == 0xE0)):
            raise ValueError("Mixkit response did not contain recognizable MP3 audio.")
        temporary_path.write_bytes(data)
        temporary_path.replace(destination)
        if not attribution.exists():
            attribution.write_text(MIXKIT_ATTRIBUTION, encoding="utf-8")
        logger.info("Downloaded licensed fallback soundtrack %s from Mixkit.", destination.name)
        return destination
    except (OSError, URLError, ValueError) as exc:
        temporary_path.unlink(missing_ok=True)
        logger.warning("Could not download the licensed Mixkit fallback (%s); continuing without music.", exc)
        return None


def find_or_download_music(directory: Path, mood: str, logger, *, auto_download: bool = True) -> Path | None:
    """Prefer user-supplied local music, auto-downloading the licensed fallback only when empty."""
    local_track = find_music(directory, mood)
    if local_track is not None:
        return local_track
    if not auto_download:
        return None
    downloaded = download_mixkit_fallback(directory, logger)
    if downloaded is not None:
        logger.info("Using fallback soundtrack for requested mood '%s'.", mood or "unspecified")
    return downloaded


def find_sound_effect(directory: Path, name: str) -> Path | None:
    """Find a user-supplied effect by filename stem."""
    if not name or not directory.is_dir():
        return None
    for path in directory.iterdir():
        if path.is_file() and path.stem.lower() == name.lower():
            return path
    return None