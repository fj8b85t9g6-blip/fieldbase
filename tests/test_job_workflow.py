import importlib
import os
import unittest
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

import bcrypt


os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "workflow-test-only"

app_module = importlib.import_module("backend.app")
Company = app_module.Company
Job = app_module.Job
MarketingEvent = app_module.MarketingEvent
User = app_module.User
app = app_module.app
db = app_module.db


class JobWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with app.app_context():
            db.create_all()

    def setUp(self):
        unique = uuid.uuid4().hex[:10]
        with app.app_context():
            company = Company(
                name=f"Workflow {unique}",
                slug=f"workflow-{unique}",
                subscription_status="trialing",
                trial_ends_at=datetime.utcnow() + timedelta(days=14),
            )
            other_company = Company(
                name=f"Other {unique}",
                slug=f"other-{unique}",
                subscription_status="trialing",
                trial_ends_at=datetime.utcnow() + timedelta(days=14),
            )
            db.session.add_all([company, other_company])
            db.session.flush()
            owner = User(
                company_id=company.id,
                email=f"owner-{unique}@workflow.test",
                password_hash=bcrypt.hashpw(b"password", bcrypt.gensalt()),
                name="Workflow Owner",
                role="owner",
            )
            employee = User(
                company_id=company.id,
                email=f"employee-{unique}@workflow.test",
                password_hash=bcrypt.hashpw(b"password", bcrypt.gensalt()),
                name="Assigned Tech",
                role="employee",
            )
            outsider = User(
                company_id=other_company.id,
                email=f"outsider-{unique}@workflow.test",
                password_hash=bcrypt.hashpw(b"password", bcrypt.gensalt()),
                name="Assigned Tech",
                role="employee",
            )
            db.session.add_all([owner, employee, outsider])
            db.session.commit()
            self.company_id = company.id
            self.owner_id = str(owner.id)
            self.employee_id = str(employee.id)
            self.outsider_id = str(outsider.id)

        self.client = app.test_client()
        self._login(self.owner_id)

    def _login(self, user_id):
        with self.client.session_transaction() as session:
            session["_user_id"] = user_id
            session["_fresh"] = True

    def _create_job(self, assigned=True):
        payload = {
            "title": "Network closet repair",
            "platform": "workmarket",
            "start": "2026-07-24T09:00",
            "end": "2026-07-24T11:00",
            "tech": "Assigned Tech" if assigned else "",
            "job_pay": 450,
            "client_email": "client@example.test",
        }
        response = self.client.post("/api/jobs", json=payload)
        self.assertEqual(response.status_code, 200)
        return response.get_json()["id"]

    def test_create_job_records_activation_once(self):
        self._create_job()
        self._create_job(assigned=False)

        with app.app_context():
            first_job_events = MarketingEvent.query.filter_by(
                company_id=self.company_id,
                event_name="first_job_created",
            ).count()
            first_assignment_events = MarketingEvent.query.filter_by(
                company_id=self.company_id,
                event_name="first_job_assigned",
            ).count()
            self.assertEqual(first_job_events, 1)
            self.assertEqual(first_assignment_events, 1)

    def test_invalid_job_time_is_rejected(self):
        response = self.client.post(
            "/api/jobs",
            json={
                "title": "Invalid",
                "start": "2026-07-24T11:00",
                "end": "2026-07-24T09:00",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "End time must be after start time.")

    def test_employee_cannot_act_on_another_company_job(self):
        job_id = self._create_job()
        self._login(self.outsider_id)

        response = self.client.post(f"/api/jobs/{job_id}/complete")
        self.assertEqual(response.status_code, 404)

    def test_job_moves_from_employee_review_to_owner_completion(self):
        job_id = self._create_job()
        self._login(self.employee_id)
        response = self.client.post(f"/api/jobs/{job_id}/complete")
        self.assertEqual(response.status_code, 200)

        with app.app_context():
            self.assertEqual(db.session.get(Job, job_id).status, "awaiting_review")

        self._login(self.owner_id)
        with patch.object(app_module, "_send_auto_invoice"):
            response = self.client.post(f"/api/jobs/{job_id}/close-and-invoice")
        self.assertEqual(response.status_code, 200)

        with app.app_context():
            self.assertEqual(db.session.get(Job, job_id).status, "complete")
            event = MarketingEvent.query.filter_by(
                company_id=self.company_id,
                event_name="first_job_completed",
            ).one()
            self.assertIsNotNone(event)


if __name__ == "__main__":
    unittest.main()
