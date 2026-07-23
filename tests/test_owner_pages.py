import importlib
import os
import unittest
import uuid

import bcrypt


os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "owner-pages-test-only"

app_module = importlib.import_module("backend.app")
Company = app_module.Company
User = app_module.User
app = app_module.app
db = app_module.db


class OwnerPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with app.app_context():
            db.create_all()

    def setUp(self):
        unique = uuid.uuid4().hex[:10]
        with app.app_context():
            company = Company(
                name=f"Owner Pages {unique}",
                slug=f"owner-pages-{unique}",
                subscription_status="active",
            )
            db.session.add(company)
            db.session.flush()
            owner = User(
                company_id=company.id,
                email=f"owner-{unique}@pages.test",
                password_hash=bcrypt.hashpw(b"password", bcrypt.gensalt()),
                name="Page Test Owner",
                role="owner",
            )
            employee = User(
                company_id=company.id,
                email=f"employee-{unique}@pages.test",
                password_hash=bcrypt.hashpw(b"password", bcrypt.gensalt()),
                name="Page Test Technician",
                role="employee",
            )
            db.session.add_all([owner, employee])
            db.session.commit()
            self.owner_id = str(owner.id)
            self.employee_id = str(employee.id)

        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session["_user_id"] = self.owner_id
            session["_fresh"] = True

    def test_owner_workflow_pages_render(self):
        routes = (
            "/",
            "/calendar",
            "/job-brief",
            "/invoice",
            "/team",
            "/clients",
            "/job-templates",
            "/reports",
            "/receipts",
            "/tech-standards",
            "/work-log",
            "/settings",
        )

        for route in routes:
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200)
                self.assertIn(b'id="main-content"', response.data)
                self.assertIn(b'aria-label="Primary navigation"', response.data)

        billing_response = self.client.get("/billing")
        self.assertEqual(billing_response.status_code, 302)
        self.assertTrue(billing_response.headers["Location"].endswith("/settings"))

    def test_employee_dashboard_renders_accessible_main_content(self):
        with self.client.session_transaction() as session:
            session["_user_id"] = self.employee_id
            session["_fresh"] = True

        response = self.client.get("/employee")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="main-content"', response.data)
        self.assertIn(b'aria-live="polite"', response.data)


if __name__ == "__main__":
    unittest.main()
