"""Local text-to-speech provider backed by Windows SAPI."""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Protocol


class TTSProvider(Protocol):
    """Minimal interface for producing narration WAV files."""

    name: str

    def synthesize(self, text: str, output_path: Path) -> None:
        """Speak ``text`` to a PCM WAV at ``output_path``."""


class WindowsSapiTTSProvider:
    """Use the built-in Windows speech engine without a cloud API."""

    name = "windows-sapi"

    def __init__(self, voice_name: str | None = None) -> None:
        self.voice_name = voice_name

    def synthesize(self, text: str, output_path: Path) -> None:
        if not text.strip():
            raise ValueError("Cannot synthesize empty narration.")
        if output_path.exists():
            return
        try:
            import pythoncom
            import win32com.client
        except ImportError as exc:
            raise RuntimeError("Local TTS requires pywin32 and the Windows Speech API.") from exc

        output_path.parent.mkdir(parents=True, exist_ok=True)
        pythoncom.CoInitialize()
        stream = None
        voice = None
        available_voices = None
        match = None
        try:
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            if self.voice_name:
                available_voices = voice.GetVoices()
                match = next(
                    (
                        available_voices.Item(index)
                        for index in range(available_voices.Count)
                        if self.voice_name.casefold() in available_voices.Item(index).GetDescription().casefold()
                    ),
                    None,
                )
                if match is None:
                    names = [available_voices.Item(index).GetDescription() for index in range(available_voices.Count)]
                    raise ValueError(f"TTS voice '{self.voice_name}' is unavailable. Installed voices: {names}")
                voice.Voice = match
            stream = win32com.client.Dispatch("SAPI.SpFileStream")
            stream.Open(str(output_path), 3, False)  # SSFMCreateForWrite
            voice.AudioOutputStream = stream
            voice.Rate = -1
            voice.Speak(text, 0)
        except Exception as exc:
            output_path.unlink(missing_ok=True)
            raise RuntimeError("Windows local speech synthesis failed.") from exc
        finally:
            if voice is not None:
                voice.AudioOutputStream = None
            if stream is not None:
                stream.Close()
            match = None
            available_voices = None
            voice = None
            stream = None
            pythoncom.CoUninitialize()


class OpenAITTSProvider:
    """Natural neural narration via the official OpenAI speech endpoint."""

    name = "openai-tts"

    def __init__(
        self,
        api_key: str | None,
        model: str,
        voice: str,
        instructions: str,
    ) -> None:
        if not api_key:
            raise ValueError("TTS_PROVIDER=openai requires OPENAI_API_KEY to be set in .env")
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)
        self._model = model
        self._voice = voice
        self._instructions = instructions

    def synthesize(self, text: str, output_path: Path) -> None:
        """Generate and store one WAV voiceover; API use is billable."""
        if not text.strip():
            raise ValueError("Cannot synthesize empty narration.")
        if output_path.exists():
            return
        output_path.parent.mkdir(parents=True, exist_ok=True)
        pcm_path = output_path.with_suffix(".pcm.tmp")
        try:
            with self._client.audio.speech.with_streaming_response.create(
                model=self._model,
                voice=self._voice,
                input=text,
                instructions=self._instructions,
                response_format="pcm",
            ) as response:
                response.stream_to_file(pcm_path)
            with pcm_path.open("rb") as pcm_file, wave.open(str(output_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(24000)
                while chunk := pcm_file.read(64 * 1024):
                    wav_file.writeframesraw(chunk)
        except Exception as exc:
            output_path.unlink(missing_ok=True)
            pcm_path.unlink(missing_ok=True)
            # Do not log request payloads, response bodies, or credentials.
            raise RuntimeError(
                f"OpenAI speech generation failed (model={self._model}, voice={self._voice}). "
                "Check API access, voice availability, and model settings."
            ) from exc
        finally:
            pcm_path.unlink(missing_ok=True)