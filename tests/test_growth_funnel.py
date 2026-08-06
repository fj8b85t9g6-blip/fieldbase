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
InvoiceRecord = app_module.InvoiceRecord
Job = app_module.Job
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

    def test_growth_dashboard_includes_first_assignment_stage(self):
        unique = uuid.uuid4().hex[:10]
        admin_email = f"{unique}@admin.test"
        with app.app_context():
            company = Company(name=f"Admin Test {unique}", slug=f"admin-test-{unique}")
            db.session.add(company)
            db.session.flush()
            user = User(
                company_id=company.id,
                email=admin_email,
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

        with patch.dict(os.environ, {"FIELD_BASE_ADMIN_EMAILS": admin_email}):
            response = self.client.get("/growth")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"First jobs assigned", response.data)
        self.assertIn(b"Interactive demo experiment", response.data)

    def test_demo_runs_sequentially_without_creating_operational_records(self):
        unique = uuid.uuid4().hex[:10]
        with app.app_context():
            companies_before = Company.query.count()
            jobs_before = Job.query.count()
            invoices_before = InvoiceRecord.query.count()

        response = self.client.get(
            f"/demo?utm_source=demo-{unique}&utm_medium=test"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Receive sample job", response.data)

        for action, next_copy in [
            ("receive", b"Assign Jordan"),
            ("assign", b"Mark work complete"),
            ("complete", b"Create and send invoice"),
            ("invoice", b"Job moved from scheduled to invoiced"),
        ]:
            response = self.client.post(
                "/demo",
                data={"action": action},
                follow_redirects=True,
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn(next_copy, response.data)

        with app.app_context():
            self.assertEqual(Company.query.count(), companies_before)
            self.assertEqual(Job.query.count(), jobs_before)
            self.assertEqual(InvoiceRecord.query.count(), invoices_before)
            self.assertEqual(MarketingEvent.query.filter_by(
                event_name="demo_viewed", source=f"demo-{unique}"
            ).count(), 1)
            self.assertEqual(MarketingEvent.query.filter_by(
                event_name="demo_started", source=f"demo-{unique}"
            ).count(), 1)
            self.assertEqual(MarketingEvent.query.filter_by(
                event_name="demo_completed", source=f"demo-{unique}"
            ).count(), 1)

    def test_demo_cannot_skip_steps_and_trial_click_requires_completion(self):
        response = self.client.post("/demo", data={"action": "invoice"})
        self.assertEqual(response.status_code, 400)

        response = self.client.get("/demo/register")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/demo"))

    def test_completed_demo_records_trial_intent_once(self):
        unique = uuid.uuid4().hex[:10]
        self.client.get(f"/demo?utm_source=demo-click-{unique}")
        for action in ("receive", "assign", "complete", "invoice"):
            self.client.post("/demo", data={"action": action})

        first = self.client.get("/demo/register")
        second = self.client.get("/demo/register")
        self.assertEqual(first.status_code, 302)
        self.assertIn("/register?", first.location)
        self.assertEqual(second.status_code, 302)
        with app.app_context():
            self.assertEqual(MarketingEvent.query.filter_by(
                event_name="demo_registration_clicked",
                source=f"demo-click-{unique}",
            ).count(), 1)

    def test_public_profit_check_delivers_value_without_creating_records(self):
        unique = uuid.uuid4().hex[:10]
        with app.app_context():
            companies_before = Company.query.count()
            jobs_before = Job.query.count()
            invoices_before = InvoiceRecord.query.count()

        response = self.client.get(
            f"/tools/job-profit-check?utm_source=profit-{unique}&utm_medium=test"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Know whether the job is worth taking", response.data)

        response = self.client.post(
            "/tools/job-profit-check",
            data={
                "job_pay": "300",
                "estimated_hours": "4",
                "travel_miles": "50",
                "materials_cost": "25",
                "platform_fee_percent": "10",
                "helper_pay": "80",
                "scope": "Replace and test the network switch.",
                "payment_terms": "Net 14 after approved closeout",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"$131.50", response.data)
        self.assertIn(b"$32.88", response.data)
        self.assertIn(b"$368.50", response.data)
        self.assertIn(b"Review the price or terms", response.data)

        with app.app_context():
            self.assertEqual(Company.query.count(), companies_before)
            self.assertEqual(Job.query.count(), jobs_before)
            self.assertEqual(InvoiceRecord.query.count(), invoices_before)
            self.assertEqual(MarketingEvent.query.filter_by(
                event_name="profit_check_viewed", source=f"profit-{unique}"
            ).count(), 1)
            self.assertEqual(MarketingEvent.query.filter_by(
                event_name="profit_check_completed", source=f"profit-{unique}"
            ).count(), 1)

    def test_profit_check_trial_click_requires_a_completed_calculation(self):
        response = self.client.get("/tools/job-profit-check/register")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/tools/job-profit-check"))

    def test_profit_check_trial_click_is_measured_once(self):
        unique = uuid.uuid4().hex[:10]
        self.client.get(f"/tools/job-profit-check?utm_source=profit-click-{unique}")
        self.client.post(
            "/tools/job-profit-check",
            data={
                "job_pay": "685",
                "estimated_hours": "4",
                "travel_miles": "36",
                "materials_cost": "45",
                "platform_fee_percent": "10",
                "helper_pay": "160",
                "scope": "Replace and test the network switch.",
                "payment_terms": "Net 14",
            },
        )
        first = self.client.get("/tools/job-profit-check/register")
        second = self.client.get("/tools/job-profit-check/register")
        self.assertEqual(first.status_code, 302)
        self.assertIn("/register?", first.location)
        self.assertEqual(second.status_code, 302)
        with app.app_context():
            self.assertEqual(MarketingEvent.query.filter_by(
                event_name="profit_check_registration_clicked",
                source=f"profit-click-{unique}",
            ).count(), 1)

    def test_growth_dashboard_excludes_internal_qa_sources(self):
        unique = uuid.uuid4().hex[:10]
        admin_email = f"{unique}@admin.test"
        with app.app_context():
            admin_company = Company(
                name=f"Admin Test {unique}",
                slug=f"admin-test-{unique}",
            )
            external_company = Company(
                name=f"External Test {unique}",
                slug=f"external-test-{unique}",
            )
            internal_company = Company(
                name=f"Internal Test {unique}",
                slug=f"internal-test-{unique}",
            )
            db.session.add_all(
                [admin_company, external_company, internal_company]
            )
            db.session.flush()
            admin = User(
                company_id=admin_company.id,
                email=admin_email,
                password_hash=bcrypt.hashpw(b"password", bcrypt.gensalt()),
                name="Admin Test",
                role="owner",
            )
            db.session.add(admin)
            db.session.add_all([
                MarketingEvent(
                    company_id=external_company.id,
                    visitor_id=str(uuid.uuid4()),
                    event_name="registration_completed",
                    source=f"external-{unique}",
                ),
                MarketingEvent(
                    company_id=internal_company.id,
                    visitor_id=str(uuid.uuid4()),
                    event_name="registration_completed",
                    source="codex_audit",
                ),
            ])
            db.session.commit()
            user_id = str(admin.id)

        with self.client.session_transaction() as session:
            session["_user_id"] = user_id
            session["_fresh"] = True

        with patch.dict(os.environ, {"FIELD_BASE_ADMIN_EMAILS": admin_email}):
            response = self.client.get("/growth")

        self.assertEqual(response.status_code, 200)
        self.assertIn(f"external-{unique}".encode(), response.data)
        self.assertNotIn(b"codex_audit", response.data)

    def test_public_acquisition_surfaces_render(self):
        paths = [
            "/demo",
            "/tools/job-profit-check",
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
