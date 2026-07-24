import importlib
import io
import json
import os
import unittest
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import bcrypt


os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "contractor-os-test-only"

app_module = importlib.import_module("backend.app")
app = app_module.app
db = app_module.db
BusinessRecord = app_module.BusinessRecord
Client = app_module.Client
Company = app_module.Company
Job = app_module.Job
User = app_module.User


class ContractorOSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with app.app_context():
            db.create_all()

    def setUp(self):
        unique = uuid.uuid4().hex[:10]
        with app.app_context():
            company = Company(
                name=f"Contractor OS {unique}",
                slug=f"contractor-os-{unique}",
                subscription_status="active",
            )
            db.session.add(company)
            db.session.flush()
            owner = User(
                company_id=company.id,
                email=f"owner-{unique}@test.local",
                password_hash=bcrypt.hashpw(b"password", bcrypt.gensalt()),
                name="Owner",
                role="owner",
            )
            client = Client(
                company_id=company.id,
                name="Jordan Client",
                company_name="Northstar Retail",
                email=f"client-{unique}@test.local",
                address="123 Market Street",
                portal_token=uuid.uuid4().hex,
            )
            db.session.add_all([owner, client])
            db.session.flush()
            job = Job(
                company_id=company.id,
                client_id=client.id,
                title="POS Installation",
                platform="direct",
                location=client.address,
                start_time=datetime(2026, 8, 3, 9),
                end_time=datetime(2026, 8, 3, 12),
                status="scheduled",
                job_pay=600,
                tech_pay=180,
                client_name=client.name,
                client_company=client.company_name,
                client_email=client.email,
                notes="Install and test two POS terminals.",
                closeout_checklist=json.dumps([{"label": "Test payment", "done": True}]),
            )
            db.session.add(job)
            db.session.commit()
            self.company_id = company.id
            self.owner_id = str(owner.id)
            self.client_id = client.id
            self.client_token = client.portal_token
            self.job_id = job.id
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session["_user_id"] = self.owner_id
            session["_fresh"] = True

    def tearDown(self):
        with app.app_context():
            records = BusinessRecord.query.filter_by(
                company_id=self.company_id,
                record_type="compliance",
            ).all()
            for record in records:
                filename = json.loads(record.data or "{}").get("filename")
                if filename:
                    app_module.storage.delete("compliance", filename)

    def _record(self, **overrides):
        payload = {
            "record_type": "change_order",
            "title": "Additional cable run",
            "job_id": self.job_id,
            "amount": 125,
            "description": "Run and certify one additional cable.",
        }
        payload.update(overrides)
        return self.client.post("/api/contractor-os/records", json=payload)

    def test_scope_change_order_is_publicly_approved_only_once(self):
        response = self._record()
        self.assertEqual(response.status_code, 200)
        record_id = response.get_json()["id"]
        with app.app_context():
            record = db.session.get(BusinessRecord, record_id)
            token = record.public_token
            job = db.session.get(Job, self.job_id)
            self.assertIsNotNone(job.scope_locked_at)
            self.assertEqual(job.original_scope, job.notes)

        first = self.client.post(f"/portal/record/{token}/approve")
        second = self.client.post(f"/portal/record/{token}/approve")
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        with app.app_context():
            self.assertEqual(db.session.get(Job, self.job_id).job_pay, 725)
            self.assertEqual(db.session.get(BusinessRecord, record_id).status, "approved")

    def test_estimate_deposit_uses_checkout_and_webhook_source_of_truth(self):
        response = self._record(
            record_type="estimate",
            title="Network Refresh",
            job_id=None,
            client_id=self.client_id,
            amount=1000,
            scope="Replace the switch and certify cabling.",
            deposit_percent="25",
        )
        record_id = response.get_json()["id"]
        with app.app_context():
            token = db.session.get(BusinessRecord, record_id).public_token
        self.client.post(f"/portal/record/{token}/approve")
        blocked = self.client.post(f"/api/estimates/{record_id}/convert")
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("deposit", blocked.get_json()["error"].lower())

        checkout = SimpleNamespace(id="cs_deposit_test", url="https://checkout.stripe.test/deposit")
        with patch.object(app_module.stripe.checkout.Session, "create", return_value=checkout) as create:
            deposit = self.client.post(f"/portal/record/{token}/deposit")
        self.assertEqual(deposit.status_code, 303)
        kwargs = create.call_args.kwargs
        self.assertNotIn("payment_method_types", kwargs)
        self.assertEqual(kwargs["line_items"][0]["price_data"]["unit_amount"], 25000)
        self.assertEqual(kwargs["metadata"]["business_record_id"], str(record_id))

        event = {
            "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_deposit_test",
                "amount_total": 25000,
                "metadata": {
                    "business_record_id": str(record_id),
                    "payment_kind": "estimate_deposit",
                },
            }},
        }
        old_secret = app_module.STRIPE_WEBHOOK_SECRET
        app_module.STRIPE_WEBHOOK_SECRET = "whsec_test"
        try:
            with patch.object(app_module.stripe.Webhook, "construct_event", return_value=event):
                webhook = self.client.post(
                    "/stripe/webhook",
                    data=b"{}",
                    headers={"Stripe-Signature": "signed"},
                )
        finally:
            app_module.STRIPE_WEBHOOK_SECRET = old_secret
        self.assertEqual(webhook.status_code, 200)
        with app.app_context():
            record = db.session.get(BusinessRecord, record_id)
            self.assertEqual(record.status, "deposit_paid")
            self.assertEqual(json.loads(record.data)["deposit_paid"], 250)

        converted = self.client.post(f"/api/estimates/{record_id}/convert")
        self.assertEqual(converted.status_code, 200)
        with app.app_context():
            created = db.session.get(Job, converted.get_json()["job_id"])
            self.assertEqual(created.original_scope, "Replace the switch and certify cabling.")
            self.assertIsNotNone(created.scope_locked_at)

    def test_profit_advisor_and_dispute_ready_pdf(self):
        response = self.client.post(
            f"/api/jobs/{self.job_id}/economics",
            json={
                "estimated_hours": 3,
                "travel_miles": 20,
                "materials_cost": 100,
                "platform_fees": 30,
                "other_costs": 10,
            },
        )
        self.assertEqual(response.status_code, 200)
        metrics = response.get_json()["metrics"]
        self.assertEqual(metrics["profit"], 266.6)
        self.assertGreater(metrics["effective_hourly"], 80)

        advice = self.client.post(
            "/api/job-acceptance-advisor",
            json={
                "job_pay": 150,
                "estimated_hours": 5,
                "travel_miles": 100,
                "materials_cost": 20,
                "platform_fee_percent": 10,
                "scope": "",
            },
        ).get_json()
        self.assertEqual(advice["verdict"], "decline")
        self.assertGreaterEqual(len(advice["warnings"]), 3)

        pdf = self.client.get(f"/api/jobs/{self.job_id}/proof-package.pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.mimetype, "application/pdf")
        self.assertTrue(pdf.data.startswith(b"%PDF"))
        self.assertGreater(len(pdf.data), 3000)

    def test_universal_intake_converts_to_locked_job(self):
        response = self.client.post(
            "/api/universal-intake",
            json={"text": "Router Replacement\nPay: $475\n2026-08-05 13:00\n90 Pine Road\nbuyer@example.test"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["extracted"]["job_pay"], 475)
        self.assertEqual(data["extracted"]["location"], "90 Pine Road")
        converted = self.client.post(f"/api/intake/{data['id']}/convert", json={})
        self.assertEqual(converted.status_code, 200)
        with app.app_context():
            job = db.session.get(Job, converted.get_json()["job_id"])
            self.assertEqual(job.location, "90 Pine Road")
            self.assertEqual(job.job_pay, 475)
            self.assertIsNotNone(job.scope_locked_at)

    def test_service_agreement_generates_due_job_and_advances_date(self):
        due = (datetime.utcnow() - timedelta(days=1)).date().isoformat()
        response = self._record(
            record_type="service_agreement",
            title="Monthly Network Maintenance",
            job_id=None,
            client_id=self.client_id,
            amount=225,
            due_at=due,
            cadence="monthly",
            duration_minutes="90",
            scope="Inspect network health and apply approved updates.",
        )
        record_id = response.get_json()["id"]
        generated = self.client.post("/api/service-agreements/run")
        self.assertEqual(generated.status_code, 200)
        self.assertEqual(generated.get_json()["created_count"], 1)
        with app.app_context():
            agreement = db.session.get(BusinessRecord, record_id)
            self.assertGreater(agreement.due_at, datetime.utcnow())
            job = db.session.get(Job, generated.get_json()["job_ids"][0])
            self.assertEqual(job.platform, "recurring")

    def test_contractor_compliance_payout_portal_and_assistant(self):
        contractor = self._record(
            record_type="contractor",
            title="Alex Technician",
            job_id=None,
            amount=0,
            email="alex@example.test",
            trade="Low voltage",
            hourly_rate="55",
        ).get_json()["id"]
        with patch.object(app_module, "send_email", return_value=True):
            offer = self.client.post(
                f"/api/contractors/{contractor}/assign",
                json={"job_id": self.job_id, "offered_pay": 220},
            )
        self.assertEqual(offer.status_code, 200)
        self.assertTrue(offer.get_json()["email_sent"])

        payout = self._record(
            record_type="payout",
            title="POS Installation payout",
            job_id=self.job_id,
            amount=220,
            contractor_name="Alex Technician",
        ).get_json()["id"]
        approved = self.client.post(f"/api/contractor-os/records/{payout}/approve")
        self.assertEqual(approved.get_json()["status"], "approved")

        document = (io.BytesIO(b"%PDF-1.4\ncompliance test\n%%EOF"), "insurance.pdf")
        compliance = self.client.post(
            "/api/contractor-os/compliance",
            data={
                "title": "Alex Liability Insurance",
                "document_type": "Insurance",
                "due_at": "2026-12-31",
                "document": document,
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(compliance.status_code, 200)

        portal = self.client.get(f"/portal/{self.client_token}")
        self.assertEqual(portal.status_code, 200)
        self.assertIn(b"Northstar Retail", portal.data)

        assistant = self.client.post(
            "/api/contractor-assistant",
            json={"question": "Which jobs are under $30 per hour?"},
        )
        self.assertEqual(assistant.status_code, 200)
        self.assertIn("profitability review", assistant.get_json()["answer"])

    def test_business_os_and_offline_field_shell_render(self):
        page = self.client.get("/contractor-os")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Run the Business, Not the Paperwork", page.data)
        self.assertIn(b"Job Acceptance Advisor", page.data)
        worker = self.client.get("/sw.js")
        self.assertEqual(worker.status_code, 200)
        self.assertIn(b"fieldbase-field-shell-v2", worker.data)
        self.assertIn(b"caches.match", worker.data)

    def test_paid_invoice_prepares_review_and_referral_request(self):
        created = self.client.post("/api/invoices", json={"job_id": self.job_id})
        self.assertEqual(created.status_code, 200)
        invoice_id = created.get_json()["id"]
        paid = self.client.post(f"/api/invoices/{invoice_id}/paid", json={})
        self.assertEqual(paid.status_code, 200)
        with app.app_context():
            review = BusinessRecord.query.filter_by(
                company_id=self.company_id,
                job_id=self.job_id,
                record_type="review_request",
            ).one()
            self.assertEqual(review.status, "draft")
            self.assertIsNotNone(review.public_token)


if __name__ == "__main__":
    unittest.main()
