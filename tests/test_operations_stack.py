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
os.environ["SECRET_KEY"] = "operations-stack-test-only"

app_module = importlib.import_module("backend.app")
Client = app_module.Client
Company = app_module.Company
InvoiceRecord = app_module.InvoiceRecord
Job = app_module.Job
JobTemplate = app_module.JobTemplate
User = app_module.User
app = app_module.app
db = app_module.db


class OperationsStackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with app.app_context():
            db.create_all()

    def setUp(self):
        unique = uuid.uuid4().hex[:10]
        with app.app_context():
            company = Company(
                name=f"Operations {unique}",
                slug=f"operations-{unique}",
                subscription_status="active",
            )
            db.session.add(company)
            db.session.flush()
            owner = User(
                company_id=company.id,
                email=f"owner-{unique}@operations.test",
                password_hash=bcrypt.hashpw(b"password", bcrypt.gensalt()),
                name="Operations Owner",
                role="owner",
            )
            employee = User(
                company_id=company.id,
                email=f"employee-{unique}@operations.test",
                password_hash=bcrypt.hashpw(b"password", bcrypt.gensalt()),
                name="Closeout Technician",
                role="employee",
            )
            db.session.add_all([owner, employee])
            db.session.commit()
            self.company_id = company.id
            self.owner_id = str(owner.id)
            self.employee_id = str(employee.id)
        self.client = app.test_client()
        self._login(self.owner_id)

    def _login(self, user_id):
        with self.client.session_transaction() as session:
            session["_user_id"] = user_id
            session["_fresh"] = True

    def _build_job_from_template(self):
        response = self.client.post(
            "/clients",
            data={
                "name": "Client Contact",
                "company_name": "Client Company",
                "email": "client@example.test",
                "address": "123 Test Street",
            },
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.post(
            "/job-templates",
            data={
                "name": "Standard Install",
                "title": "Install Workstation",
                "duration_minutes": "90",
                "default_job_pay": "425",
                "checklist": "Photograph installation\nTest connectivity",
                "require_signature": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            client_id = Client.query.filter_by(company_id=self.company_id).one().id
            template_id = JobTemplate.query.filter_by(company_id=self.company_id).one().id
        response = self.client.post(
            "/api/jobs",
            json={
                "template_id": template_id,
                "client_id": client_id,
                "tech": "Closeout Technician",
                "start": "2026-07-25T09:00",
                "end": "2026-07-25T10:30",
            },
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()["id"]

    def test_template_client_closeout_signature_and_invoice_flow(self):
        job_id = self._build_job_from_template()
        with app.app_context():
            job = db.session.get(Job, job_id)
            self.assertEqual(job.client_company, "Client Company")
            self.assertEqual(job.job_pay, 425)
            self.assertTrue(job.signature_required)
            self.assertEqual(len(json.loads(job.closeout_checklist)), 2)

        self._login(self.employee_id)
        blocked = self.client.post(f"/api/jobs/{job_id}/complete")
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("closeout", blocked.get_json()["error"])

        response = self.client.put(
            f"/api/jobs/{job_id}/closeout",
            json={
                "checklist": [
                    {"label": "Photograph installation", "done": True},
                    {"label": "Test connectivity", "done": True},
                ],
            },
        )
        self.assertEqual(response.status_code, 200)
        blocked = self.client.post(f"/api/jobs/{job_id}/complete")
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("signature", blocked.get_json()["error"])

        png = b"\x89PNG\r\n\x1a\n" + b"signature-bytes"
        response = self.client.post(
            f"/api/jobs/{job_id}/signature",
            data={
                "signer_name": "Authorized Client",
                "signature": (io.BytesIO(png), "signature.png"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        response = self.client.post(f"/api/jobs/{job_id}/complete")
        self.assertEqual(response.status_code, 200)

        self._login(self.owner_id)
        response = self.client.post("/api/invoices", json={"job_id": job_id})
        self.assertEqual(response.status_code, 200)
        invoice_id = response.get_json()["id"]

        with patch.object(
            app_module.stripe.checkout.Session,
            "create",
            return_value=SimpleNamespace(id="cs_invoice_test", url="https://checkout.test/invoice"),
        ) as checkout, patch.object(app_module, "send_email", return_value=True):
            response = self.client.post(f"/api/invoices/{invoice_id}/send")
        self.assertEqual(response.status_code, 200)
        kwargs = checkout.call_args.kwargs
        self.assertEqual(kwargs["mode"], "payment")
        self.assertNotIn("payment_method_types", kwargs)
        self.assertEqual(kwargs["metadata"]["invoice_id"], str(invoice_id))

        response = self.client.post(
            f"/api/invoices/{invoice_id}/paid",
            json={"amount": 425},
        )
        self.assertEqual(response.status_code, 200)
        with app.app_context():
            invoice = db.session.get(InvoiceRecord, invoice_id)
            job = db.session.get(Job, job_id)
            self.assertEqual(invoice.status, "paid")
            self.assertTrue(job.payment_received)
            if job.signature_filename:
                app_module.storage.delete("signatures", job.signature_filename)

    def test_weekly_recurring_job_creation(self):
        response = self.client.post(
            "/api/jobs",
            json={
                "title": "Weekly Maintenance",
                "start": "2026-07-25T09:00",
                "end": "2026-07-25T10:00",
                "repeat": "weekly",
                "repeat_count": 3,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["created_count"], 3)
        with app.app_context():
            jobs = Job.query.filter_by(
                company_id=self.company_id,
                title="Weekly Maintenance",
            ).order_by(Job.start_time).all()
            self.assertEqual(len(jobs), 3)
            self.assertEqual((jobs[1].start_time - jobs[0].start_time).days, 7)

    def test_opt_in_automatic_invoice_reminder(self):
        with app.app_context():
            company = db.session.get(Company, self.company_id)
            company.invoice_reminders_enabled = True
            invoice = InvoiceRecord(
                company_id=self.company_id,
                number=f"INV-REM-{uuid.uuid4().hex[:6]}",
                status="sent",
                due_date=datetime.utcnow() - timedelta(days=1),
                client_name="Reminder Client",
                client_email="reminder@example.test",
                total=125,
                stripe_checkout_url="https://checkout.test/reminder",
            )
            db.session.add(invoice)
            db.session.commit()
            invoice_id = invoice.id
            with app.test_request_context("/internal/lifecycle/run", method="POST"), patch.object(
                app_module,
                "send_email",
                return_value=True,
            ):
                result = app_module._run_invoice_reminders(datetime.utcnow())
            refreshed = db.session.get(InvoiceRecord, invoice_id)
            self.assertEqual(result["sent"], 1)
            self.assertEqual(refreshed.status, "overdue")
            self.assertIsNotNone(refreshed.reminder_sent_at)


if __name__ == "__main__":
    unittest.main()
