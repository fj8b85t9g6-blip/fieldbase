import importlib
import os
import unittest
import uuid


os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "registration-conversion-test"

app_module = importlib.import_module("backend.app")
Company = app_module.Company
MarketingEvent = app_module.MarketingEvent
User = app_module.User
app = app_module.app
db = app_module.db


class RegistrationConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with app.app_context():
            db.create_all()

    def setUp(self):
        self.client = app.test_client()

    def test_signup_page_explains_trial_and_outcome(self):
        response = self.client.get("/register")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"14 days free", response.data)
        self.assertIn(b"No card required", response.data)
        self.assertIn(b"Start My Free Trial", response.data)
        self.assertIn(b"Put your first field job on one crew calendar", response.data)

    def test_form_submission_is_measured_and_valid_registration_completes(self):
        unique = uuid.uuid4().hex[:10]
        self.client.get(
            "/register?utm_source=registration-test&utm_medium=internal"
        )

        response = self.client.post(
            "/register",
            data={
                "company_name": f"Registration Test {unique}",
                "name": "Test Owner",
                "email": f"{unique}@registration.test",
                "password": "strong-password",
                "trade_type": "low_voltage",
            },
        )

        self.assertEqual(response.status_code, 302)
        with app.app_context():
            company = Company.query.filter_by(
                slug=f"registration-test-{unique}"
            ).one()
            submitted = MarketingEvent.query.filter_by(
                event_name="registration_form_submitted",
                source="registration-test",
            ).count()
            completed = MarketingEvent.query.filter_by(
                event_name="registration_completed",
                company_id=company.id,
            ).count()

        self.assertEqual(submitted, 1)
        self.assertEqual(completed, 1)

    def test_validation_error_preserves_safe_fields_but_not_password(self):
        unique = uuid.uuid4().hex[:10]
        email = f"{unique}@registration.test"
        self.client.get("/register")

        response = self.client.post(
            "/register",
            data={
                "company_name": "Preserved Field Services",
                "name": "Preserved Owner",
                "email": email,
                "password": "short",
                "trade_type": "security_cameras",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Use at least 8 characters", response.data)
        self.assertIn(b' value="Preserved Field Services"', response.data)
        self.assertIn(b' value="Preserved Owner"', response.data)
        self.assertIn(f' value="{email}"'.encode(), response.data)
        self.assertIn(
            b'<option value="security_cameras" selected>',
            response.data,
        )
        self.assertNotIn(b' value="short"', response.data)

        with app.app_context():
            submitted = MarketingEvent.query.filter_by(
                event_name="registration_form_submitted",
            ).count()
        self.assertGreaterEqual(submitted, 1)


if __name__ == "__main__":
    unittest.main()
