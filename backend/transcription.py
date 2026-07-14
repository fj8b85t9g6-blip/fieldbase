"""Server-side audio transcription with an OpenAI-compatible provider."""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests


MAX_AUDIO_BYTES = 15 * 1024 * 1024
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini-transcribe"
SUPPORTED_AUDIO_TYPES = {
    "audio/flac": "flac",
    "audio/mp4": "mp4",
    "audio/mpeg": "mp3",
    "audio/mpga": "mpga",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/webm": "webm",
    "audio/x-m4a": "m4a",
    "audio/x-wav": "wav",
    "video/mp4": "mp4",
    "video/webm": "webm",
}
SUPPORTED_EXTENSIONS = {"flac", "m4a", "mp3", "mp4", "mpeg", "mpga", "ogg", "wav", "webm"}


class TranscriptionConfigurationError(RuntimeError):
    pass


class TranscriptionProviderError(RuntimeError):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class TranscriptionConfig:
    api_key: str
    base_url: str
    model: str


def transcription_is_configured(environ=None):
    try:
        get_transcription_config(environ)
        return True
    except TranscriptionConfigurationError:
        return False


def get_transcription_config(environ=None):
    env = environ if environ is not None else os.environ
    api_key = (env.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise TranscriptionConfigurationError("Server transcription is not configured.")

    base_url = (env.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise TranscriptionConfigurationError("OPENAI_BASE_URL must be an HTTP(S) URL.")

    model = (env.get("OPENAI_TRANSCRIPTION_MODEL") or DEFAULT_MODEL).strip()
    if not model:
        raise TranscriptionConfigurationError("OPENAI_TRANSCRIPTION_MODEL cannot be empty.")
    return TranscriptionConfig(api_key=api_key, base_url=base_url, model=model)


def normalize_audio_file(filename, content_type):
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    suffix = Path(filename or "").suffix.lower().lstrip(".")

    if media_type in SUPPORTED_AUDIO_TYPES:
        extension = SUPPORTED_AUDIO_TYPES[media_type]
    elif media_type in ("", "application/octet-stream") and suffix in SUPPORTED_EXTENSIONS:
        extension = suffix
    else:
        raise ValueError("Unsupported audio format. Use MP4, M4A, MP3, OGG, WAV, FLAC, or WebM.")

    outgoing_type = media_type if media_type in SUPPORTED_AUDIO_TYPES else "application/octet-stream"
    return f"voice-note.{extension}", outgoing_type


def transcribe_audio(audio_bytes, filename, content_type, environ=None, http_post=None):
    if not audio_bytes:
        raise ValueError("The audio recording is empty.")
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise ValueError("Audio recording exceeds the 15 MB upload limit.")

    config = get_transcription_config(environ)
    safe_filename, safe_content_type = normalize_audio_file(filename, content_type)
    post = http_post or requests.post

    try:
        response = post(
            f"{config.base_url}/audio/transcriptions",
            headers={"Authorization": f"Bearer {config.api_key}"},
            data={"model": config.model, "language": "en", "response_format": "json"},
            files={"file": (safe_filename, audio_bytes, safe_content_type)},
            timeout=(5, 90),
        )
    except requests.Timeout as exc:
        raise TranscriptionProviderError("The transcription provider timed out.") from exc
    except requests.RequestException as exc:
        raise TranscriptionProviderError("The transcription provider is unavailable.") from exc

    if not 200 <= response.status_code < 300:
        raise TranscriptionProviderError(
            "The transcription provider rejected the recording.",
            status_code=response.status_code,
        )

    try:
        transcript = (response.json().get("text") or "").strip()
    except (ValueError, AttributeError) as exc:
        raise TranscriptionProviderError("The transcription provider returned an invalid response.") from exc
    if not transcript:
        raise TranscriptionProviderError("No speech was detected in the recording.")
    return transcript
