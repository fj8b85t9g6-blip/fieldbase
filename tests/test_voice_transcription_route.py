import os
import unittest
from io import BytesIO
from unittest.mock import patch


os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "route-test-only"
os.environ.pop("OPENAI_API_KEY", None)

from backend.app import Company, User, app, db  # noqa: E402


class VoiceTranscriptionRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with app.app_context():
            company = Company(name="Route Test Co", slug="route-test")
            db.session.add(company)
            db.session.flush()
            user = User(
                company_id=company.id,
                email="owner@route.test",
                password_hash=b"not-used",
                name="Route Test Owner",
                role="owner",
            )
            db.session.add(user)
            db.session.commit()
            cls.user_id = str(user.id)

    def setUp(self):
        self.client = app.test_client()

    def _sign_in_owner(self):
        with self.client.session_transaction() as session:
            session["_user_id"] = self.user_id
            session["_fresh"] = True

    def test_capability_route_requires_login(self):
        response = self.client.get("/api/voice/transcription")
        self.assertEqual(response.status_code, 302)

    def test_capability_reports_unconfigured_without_exposing_config(self):
        self._sign_in_owner()
        response = self.client.get("/api/voice/transcription")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"configured": False})

    def test_unconfigured_upload_preserves_fallback(self):
        self._sign_in_owner()
        response = self.client.post("/api/voice/transcription")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {
            "error": "Server transcription is not configured.",
            "fallback_available": True,
        })

    def test_configured_upload_returns_mocked_transcript(self):
        self._sign_in_owner()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), patch(
            "backend.app.transcribe_audio",
            return_value="Install two access readers",
        ) as transcribe:
            response = self.client.post(
                "/api/voice/transcription",
                data={"audio": (BytesIO(b"audio-data"), "voice-note.webm", "audio/webm")},
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"transcript": "Install two access readers"})
        transcribe.assert_called_once_with(
            b"audio-data",
            filename="voice-note.webm",
            content_type="audio/webm",
        )


if __name__ == "__main__":
    unittest.main()
