import importlib
import os
import unittest
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

import bcrypt


os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "growth-test-only"

app_module = importlib.import_module("backend.app")
Company = app_module.Company
MarketingEvent = app_module.MarketingEvent
User = app_module.User
app = app_module.app
db = app_module.db


class GrowthFunnelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with app.app_context():
            db.create_all()

    def setUp(self):
        self.client = app.test_client()

    def test_landing_view_is_recorded_once_per_visitor(self):
        self.client.get("/?utm_source=fieldnation&utm_medium=organic")
        self.client.get("/")

        with app.app_context():
            events = MarketingEvent.query.filter_by(
                event_name="landing_view",
                source="fieldnation",
            ).all()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].medium, "organic")

    def test_registration_preserves_first_touch_attribution(self):
        unique = uuid.uuid4().hex[:10]
        source = f"guide-{unique}"
        self.client.get(
            f"/register?utm_source={source}&utm_medium=content&utm_campaign=invoice-guide"
        )
        response = self.client.post(
            "/register",
            data={
                "company_name": f"Growth Test {unique}",
                "name": "Test Owner",
                "email": f"{unique}@growth.test",
                "password": "strong-password",
                "trade_type": "low_voltage",
            },
        )

        self.assertEqual(response.status_code, 302)
        with app.app_context():
            company = Company.query.filter_by(slug=f"growth-test-{unique}").one()
            self.assertEqual(company.acquisition_source, source)
            self.assertEqual(company.acquisition_medium, "content")
            self.assertEqual(company.acquisition_campaign, "invoice-guide")
            self.assertEqual(company.trade_type, "low_voltage")
            event = MarketingEvent.query.filter_by(
                event_name="registration_completed",
                company_id=company.id,
            ).one()
            self.assertEqual(event.source, source)

    def test_growth_dashboard_is_hidden_without_admin_configuration(self):
        unique = uuid.uuid4().hex[:10]
        with app.app_context():
            company = Company(name=f"Admin Test {unique}", slug=f"admin-test-{unique}")
            db.session.add(company)
            db.session.flush()
            user = User(
                company_id=company.id,
                email=f"{unique}@admin.test",
                password_hash=bcrypt.hashpw(b"password", bcrypt.gensalt()),
                name="Admin Test",
                role="owner",
            )
            db.session.add(user)
            db.session.commit()
            user_id = str(user.id)

        with self.client.session_transaction() as session:
            session["_user_id"] = user_id
            session["_fresh"] = True

        response = self.client.get("/growth")
        self.assertEqual(response.status_code, 404)

    def test_public_acquisition_surfaces_render(self):
        paths = [
            "/for/workmarket-contractors",
            "/for/field-nation-contractors",
            "/for/low-voltage-contractors",
            "/guides/field-service-invoicing",
            "/guides/prevent-double-booking-field-technicians",
            "/tools/double-booking-cost-calculator",
            "/templates/field-job-template",
            "/privacy",
            "/terms",
            "/robots.txt",
            "/sitemap.xml",
        ]
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)

    def test_job_template_download_is_csv(self):
        response = self.client.get("/templates/field-job-template.csv")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/csv")
        self.assertIn(
            "attachment; filename=field-service-job-template.csv",
            response.headers["Content-Disposition"],
        )
        self.assertIn(b"Assigned technician", response.data)

    def test_lifecycle_runner_is_hidden_without_secret(self):
        response = self.client.post("/internal/lifecycle/run")
        self.assertEqual(response.status_code, 404)

    def test_lifecycle_email_is_behavior_based_and_deduplicated(self):
        unique = uuid.uuid4().hex[:10]
        with app.app_context():
            company = Company(
                name=f"Lifecycle {unique}",
                slug=f"lifecycle-{unique}",
                subscription_status="trialing",
                trial_ends_at=datetime.utcnow() + timedelta(days=12),
                created_at=datetime.utcnow() - timedelta(days=2),
            )
            db.session.add(company)
            db.session.flush()
            owner = User(
                company_id=company.id,
                email=f"{unique}@lifecycle.test",
                password_hash=bcrypt.hashpw(b"password", bcrypt.gensalt()),
                name="Lifecycle Owner",
                role="owner",
            )
            db.session.add(owner)
            db.session.commit()
            company_id = company.id

            with app.test_request_context("/internal/lifecycle/run", method="POST"), patch.object(
                app_module,
                "send_email",
                return_value=True,
            ) as send:
                first = app_module._run_lifecycle_emails()
                second = app_module._run_lifecycle_emails()

            event_count = MarketingEvent.query.filter_by(
                company_id=company_id,
                event_name="lifecycle_email_first_job",
            ).count()
            self.assertEqual(event_count, 1)
            self.assertEqual(first["sent"], 1)
            self.assertEqual(second["sent"], 0)
            send.assert_called()


if __name__ == "__main__":
    unittest.main()
