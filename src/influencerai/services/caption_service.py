"""Generate SRT/ASS captions from optional local Whisper timestamps."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class CaptionCue:
    """Caption text with absolute timeline timestamps in seconds."""

    start: float
    end: float
    text: str


def _chunks(words: list[str], max_words: int = 5, max_chars: int = 38) -> list[list[str]]:
    result: list[list[str]] = []
    current: list[str] = []
    for word in words:
        if current and (len(current) >= max_words or len(" ".join([*current, word])) > max_chars):
            result.append(current)
            current = []
        current.append(word)
    if current:
        result.append(current)
    return result


def estimate_cues(text: str, start: float, speech_duration: float) -> list[CaptionCue]:
    """Estimate readable cue timing from narration text when no ASR is enabled."""
    words = text.split()
    chunks = _chunks(words)
    if not chunks:
        return []
    weights = [sum(max(1, len(word.strip(".,!?;:"))) ** 0.65 for word in chunk) for chunk in chunks]
    total_weight = sum(weights)
    cues: list[CaptionCue] = []
    cursor = start
    for chunk, weight in zip(chunks, weights):
        end = cursor + max(0.2, speech_duration * weight / total_weight)
        cues.append(CaptionCue(cursor, end, " ".join(chunk)))
        cursor = end
    last = cues[-1]
    cues[-1] = CaptionCue(last.start, max(last.start + 0.25, start + speech_duration), last.text)
    return cues


def transcribe_scene(
    audio_path: Path,
    fallback_text: str,
    *,
    scene_start: float,
    speech_duration: float,
    provider: str,
    model_name: str,
    model_cache: Path,
    logger: Any,
) -> list[CaptionCue]:
    """Use Faster-Whisper if explicitly enabled and installed; otherwise estimate from text."""
    if provider != "whisper":
        return estimate_cues(fallback_text, scene_start, speech_duration)
    try:
        from faster_whisper import WhisperModel

        model_cache.mkdir(parents=True, exist_ok=True)
        model = WhisperModel(model_name, device="cpu", compute_type="int8", download_root=str(model_cache))
        segments, _info = model.transcribe(str(audio_path), word_timestamps=True, beam_size=1, vad_filter=True)
        words: list[CaptionCue] = []
        for segment in segments:
            if segment.words:
                words.extend(
                    CaptionCue(
                        scene_start + float(word.start),
                        scene_start + float(word.end),
                        word.word.strip(),
                    )
                    for word in segment.words
                    if word.word.strip()
                )
            elif segment.text.strip():
                words.extend(estimate_cues(segment.text.strip(), scene_start + segment.start, segment.end - segment.start))
        if words:
            return _group_word_cues(words)
    except ImportError:
        logger.warning("Faster-Whisper is not installed; using screenplay-timed captions for this run.")
    except Exception as exc:
        logger.warning("Whisper transcription failed (%s); using screenplay-timed captions.", type(exc).__name__)
    return estimate_cues(fallback_text, scene_start, speech_duration)


def _group_word_cues(words: list[CaptionCue]) -> list[CaptionCue]:
    cues: list[CaptionCue] = []
    group: list[CaptionCue] = []
    for word in words:
        if group and (len(group) >= 5 or len(" ".join(item.text for item in group + [word])) > 38):
            cues.append(CaptionCue(group[0].start, group[-1].end, " ".join(item.text for item in group)))
            group = []
        group.append(word)
    if group:
        cues.append(CaptionCue(group[0].start, group[-1].end, " ".join(item.text for item in group)))
    return cues


def _timestamp(seconds: float, *, ass: bool = False) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    whole_seconds, fractional = divmod(remainder, 100)
    separator = "." if ass else ","
    if ass:
        return f"{hours}:{minutes:02}:{whole_seconds:02}.{fractional:02}"
    return f"{hours:02}:{minutes:02}:{whole_seconds:02}{separator}{fractional:03}"


def write_srt(cues: list[CaptionCue], path: Path) -> None:
    """Write standard SubRip captions."""
    blocks = [
        f"{index}\n{_timestamp(cue.start)} --> {_timestamp(cue.end)}\n{cue.text}"
        for index, cue in enumerate(cues, start=1)
    ]
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8-sig")


def write_ass(cues: list[CaptionCue], path: Path) -> None:
    """Write mobile-safe Advanced SubStation Alpha captions."""
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1080",
        "PlayResY: 1920",
        "WrapStyle: 2",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,Arial,40,&H00FFFFFF,&H0000FFFF,&H00000000,&H78000000,-1,0,0,0,100,100,0,0,1,2,0,2,70,70,510,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for cue in cues:
        text = re.sub(r"\s+", " ", cue.text).replace("{", "\\{").replace("}", "\\}")
        lines.append(f"Dialogue: 0,{_timestamp(cue.start, ass=True)},{_timestamp(cue.end, ass=True)},Default,,0,0,0,,{text}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")