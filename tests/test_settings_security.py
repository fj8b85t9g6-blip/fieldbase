import importlib
import os
import unittest
import uuid
from datetime import datetime, timedelta

import bcrypt


os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "settings-test-only"

app_module = importlib.import_module("backend.app")
Company = app_module.Company
PlatformCredential = app_module.PlatformCredential
User = app_module.User
app = app_module.app
db = app_module.db


class SettingsSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with app.app_context():
            db.create_all()

    def setUp(self):
        unique = uuid.uuid4().hex[:10]
        with app.app_context():
            company = Company(
                name=f"Settings {unique}",
                slug=f"settings-{unique}",
                subscription_status="trialing",
                trial_ends_at=datetime.utcnow() + timedelta(days=14),
            )
            db.session.add(company)
            db.session.flush()
            user = User(
                company_id=company.id,
                email=f"{unique}@settings.test",
                password_hash=bcrypt.hashpw(b"password", bcrypt.gensalt()),
                name="Settings Owner",
                role="owner",
            )
            credential = PlatformCredential(
                company_id=company.id,
                platform="workmarket",
                api_key="saved-api-key",
                api_secret="saved-api-secret",
                enabled=True,
            )
            db.session.add_all([user, credential])
            db.session.commit()
            self.company_id = company.id
            self.user_id = str(user.id)

        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session["_user_id"] = self.user_id
            session["_fresh"] = True

    def test_saved_credentials_are_not_rendered_into_html(self):
        response = self.client.get("/settings")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"saved-api-key", response.data)
        self.assertNotIn(b"saved-api-secret", response.data)
        self.assertIn(b"A secret is saved. Leave blank to keep it.", response.data)

    def test_blank_credential_fields_preserve_saved_values(self):
        response = self.client.post(
            "/settings",
            data={
                "workmarket_enabled": "on",
                "workmarket_key": "",
                "workmarket_secret": "",
                "fieldnation_key": "",
                "fieldnation_secret": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        with app.app_context():
            credential = PlatformCredential.query.filter_by(
                company_id=self.company_id,
                platform="workmarket",
            ).one()
            self.assertEqual(credential.api_key, "saved-api-key")
            self.assertEqual(credential.api_secret, "saved-api-secret")


if __name__ == "__main__":
    unittest.main()
