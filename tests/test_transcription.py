import unittest

import requests

from backend.transcription import (
    MAX_AUDIO_BYTES,
    TranscriptionConfigurationError,
    TranscriptionProviderError,
    get_transcription_config,
    normalize_audio_file,
    transcribe_audio,
    transcription_is_configured,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"text": "Test transcript"}

    def json(self):
        return self._payload


class TranscriptionConfigTests(unittest.TestCase):
    def test_unconfigured_without_api_key(self):
        self.assertFalse(transcription_is_configured({}))
        with self.assertRaises(TranscriptionConfigurationError):
            get_transcription_config({})

    def test_uses_openai_compatible_configuration(self):
        config = get_transcription_config({
            "OPENAI_API_KEY": "test-key",
            "OPENAI_BASE_URL": "https://provider.example/v1/",
            "OPENAI_TRANSCRIPTION_MODEL": "provider-transcribe",
        })
        self.assertEqual(config.base_url, "https://provider.example/v1")
        self.assertEqual(config.model, "provider-transcribe")

    def test_rejects_invalid_base_url(self):
        environ = {
            "OPENAI_API_KEY": "test-key",
            "OPENAI_BASE_URL": "provider.example/v1",
        }
        self.assertFalse(transcription_is_configured(environ))
        with self.assertRaises(TranscriptionConfigurationError):
            get_transcription_config(environ)


class AudioValidationTests(unittest.TestCase):
    def test_normalizes_mobile_media_types(self):
        self.assertEqual(
            normalize_audio_file("recording", "audio/webm;codecs=opus"),
            ("voice-note.webm", "audio/webm"),
        )
        self.assertEqual(
            normalize_audio_file("recording.m4a", "application/octet-stream"),
            ("voice-note.m4a", "application/octet-stream"),
        )

    def test_rejects_non_audio_upload(self):
        with self.assertRaises(ValueError):
            normalize_audio_file("payload.html", "text/html")

    def test_rejects_oversized_audio_before_provider_call(self):
        called = False

        def fake_post(*args, **kwargs):
            nonlocal called
            called = True
            return FakeResponse()

        with self.assertRaises(ValueError):
            transcribe_audio(
                b"x" * (MAX_AUDIO_BYTES + 1),
                "voice.webm",
                "audio/webm",
                environ={"OPENAI_API_KEY": "test-key"},
                http_post=fake_post,
            )
        self.assertFalse(called)


class ProviderRequestTests(unittest.TestCase):
    def test_posts_bounded_multipart_request_and_returns_text(self):
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return FakeResponse(payload={"text": "  Replace access panel  "})

        transcript = transcribe_audio(
            b"audio-data",
            "unsafe-name.webm",
            "audio/webm;codecs=opus",
            environ={"OPENAI_API_KEY": "test-key"},
            http_post=fake_post,
        )

        self.assertEqual(transcript, "Replace access panel")
        self.assertEqual(captured["url"], "https://api.openai.com/v1/audio/transcriptions")
        self.assertEqual(captured["data"]["model"], "gpt-4o-mini-transcribe")
        self.assertEqual(captured["files"]["file"][0], "voice-note.webm")
        self.assertEqual(captured["timeout"], (5, 90))
        self.assertEqual(captured["headers"]["Authorization"], "Bearer test-key")

    def test_provider_errors_are_sanitized(self):
        def fake_post(*args, **kwargs):
            return FakeResponse(status_code=429, payload={"error": {"message": "sensitive detail"}})

        with self.assertRaises(TranscriptionProviderError) as raised:
            transcribe_audio(
                b"audio-data",
                "voice.mp4",
                "audio/mp4",
                environ={"OPENAI_API_KEY": "test-key"},
                http_post=fake_post,
            )
        self.assertEqual(raised.exception.status_code, 429)
        self.assertNotIn("sensitive detail", str(raised.exception))

    def test_timeout_is_mapped_to_provider_error(self):
        def fake_post(*args, **kwargs):
            raise requests.Timeout("provider timeout")

        with self.assertRaisesRegex(TranscriptionProviderError, "timed out"):
            transcribe_audio(
                b"audio-data",
                "voice.ogg",
                "audio/ogg",
                environ={"OPENAI_API_KEY": "test-key"},
                http_post=fake_post,
            )


if __name__ == "__main__":
    unittest.main()
