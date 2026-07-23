import importlib
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch


os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "billing-test-only"

app_module = importlib.import_module("backend.app")
Company = app_module.Company
User = app_module.User
app = app_module.app
db = app_module.db


class BillingCheckoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with app.app_context():
            db.create_all()
            company = Company(
                name="Billing Test Co",
                slug="billing-test",
                stripe_customer_id="cus_billing_test",
            )
            db.session.add(company)
            db.session.flush()
            user = User(
                company_id=company.id,
                email="owner@billing.test",
                password_hash=b"not-used",
                name="Billing Test Owner",
                role="owner",
            )
            db.session.add(user)
            db.session.commit()
            cls.user_id = str(user.id)

    def setUp(self):
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session["_user_id"] = self.user_id
            session["_fresh"] = True

    def _checkout(self, payload=None):
        with patch.object(app_module, "STRIPE_PRICE_ID", "price_monthly"), patch.object(
            app_module, "STRIPE_ANNUAL_PRICE_ID", "price_annual"
        ), patch.object(
            app_module.stripe.checkout.Session,
            "create",
            return_value=SimpleNamespace(url="https://checkout.test/session"),
        ) as create:
            response = self.client.post("/billing/create-checkout", json=payload)
        return response, create

    def test_monthly_plan_remains_default(self):
        response, create = self._checkout()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"url": "https://checkout.test/session"})
        self.assertEqual(create.call_args.kwargs["line_items"], [
            {"price": "price_monthly", "quantity": 1}
        ])
        self.assertNotIn("payment_method_types", create.call_args.kwargs)

    def test_founding_annual_plan_uses_annual_price(self):
        response, create = self._checkout({"plan": "founding_annual"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(create.call_args.kwargs["mode"], "subscription")
        self.assertEqual(create.call_args.kwargs["line_items"], [
            {"price": "price_annual", "quantity": 1}
        ])

    def test_unknown_plan_is_rejected_before_stripe(self):
        response, create = self._checkout({"plan": "free_forever"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {"error": "Unknown billing plan."})
        create.assert_not_called()

    def test_unconfigured_annual_plan_fails_closed(self):
        with patch.object(app_module, "STRIPE_ANNUAL_PRICE_ID", ""), patch.object(
            app_module.stripe.checkout.Session, "create"
        ) as create:
            response = self.client.post(
                "/billing/create-checkout",
                json={"plan": "founding_annual"},
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json(), {"error": "Billing not configured yet."})
        create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
