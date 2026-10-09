from datetime import date
from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

import frappe
from jinja2 import Environment

from qas_custom.modules.billing.invoice_gst import gst_inclusive_breakdown, invoice_gst_context
from qas_custom.modules.billing.presentation import build_parent_invoice_context
from qas_custom.modules.notifications.commands import _invoice_pdf_html, _invoice_email_message
from qas_custom.patches.v2026_06_28_parent_invoice_format import _parent_invoice_print_html


def sample_context(posting_date="2026-10-09", total=330):
    return {
        "invoice": "QAS-GST-SAMPLE", "school_name": "Queensland Art School",
        "legal_name": "Example Art Pty Ltd", "abn": "TEST ABN",
        "customer": "Example Parent", "recipient_name": "Example Parent",
        "posting_date": posting_date, "due_date": "2026-10-16", "total": total,
        "store_credit_applied": 110, "payable_amount": total - 110,
        "accepted_payment_methods": "bank transfer", "invoice_link": "", "payment_link": "",
        "items": [{"student": "Example Student", "description": "Art tuition", "amount": total}],
        "adjustments": [], "payment_plan": {},
        **gst_inclusive_breakdown(posting_date, total),
    }


class InvoiceGSTDisplayTest(TestCase):
    def setUp(self):
        translation = patch("qas_custom.modules.notifications.commands._", side_effect=lambda text: text)
        translation.start()
        self.addCleanup(translation.stop)

    def test_cutoff_and_missing_date(self):
        for posting in (None, "2026-10-08"):
            self.assertEqual(gst_inclusive_breakdown(posting, 330), {"gst_included": False, "invoice_title": "Invoice"})
        for posting in ("2026-10-09", date(2026, 10, 9), "2027-01-01"):
            self.assertTrue(gst_inclusive_breakdown(posting, 330)["gst_included"])

    def test_inclusive_gst_rounding_and_reconciliation(self):
        for total, gst in [(330, 30), (300, 27.27), (536.67, 48.79), (0.055, 0.01), (0, 0), (-330, -30)]:
            with self.subTest(total=total):
                result = gst_inclusive_breakdown("2026-10-09", total)
                self.assertEqual(result["gst_amount"], gst)
                self.assertEqual(round(result["subtotal_excluding_gst"] + gst, 2), float(Decimal(str(total)).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")))

    def test_credit_and_payments_do_not_reduce_tax(self):
        for payable, credit in [(330, 0), (220, 110), (0, 330), (0, 0)]:
            invoice = frappe._dict(posting_date="2026-10-09", grand_total=330, docstatus=1, rounded_total=0, outstanding_amount=payable, qas_store_credit_applied=credit)
            self.assertEqual(invoice_gst_context(invoice)["gst_amount"], 30)

    @patch("qas_custom.modules.billing.presentation.formatdate", side_effect=str)
    @patch("qas_custom.modules.billing.presentation._invoice_recipient_name", return_value="Example Parent")
    @patch("qas_custom.modules.billing.presentation.get_invoice_settings", return_value={})
    @patch("qas_custom.modules.billing.presentation.get_invoice_payment_context", return_value={})
    @patch("qas_custom.modules.billing.payment_plans.payment_plan_payload", return_value={})
    @patch("qas_custom.services.stripe_trial_payments.payment_url", return_value="")
    def test_parent_context_preserves_discounted_total_and_payable(self, *_mocks):
        invoice = frappe._dict(name="GST-TEST", posting_date="2026-10-09", grand_total=300, rounded_total=0, docstatus=1, items=[], taxes=[])
        result = build_parent_invoice_context(invoice, store_credit_applied=100, payable_amount=200, include_portal_link=False)
        self.assertEqual((result["total"], result["payable_amount"], result["gst_amount"]), (300, 200, 27.27))
        self.assertEqual(invoice.grand_total, 300)

    def test_pdf_contains_tax_identity_and_unchanged_amounts(self):
        html = _invoice_pdf_html(sample_context())
        for value in ("Tax Invoice", "Example Art Pty Ltd", "TEST ABN", "Bill to: Example Parent", "Subtotal (excl. GST)", "Included GST (10%)", "AUD $30.00", "AUD $330.00", "AUD $220.00"):
            self.assertIn(value, html)

    def test_historical_pdf_and_print_have_no_gst(self):
        context = sample_context("2026-10-08")
        for html in (_invoice_pdf_html(context), self.render_print(context)):
            self.assertNotIn("Tax Invoice", html)
            self.assertNotIn("Included GST", html)
            self.assertNotIn("Bill to:", html)

    def test_desk_print_uses_same_breakdown(self):
        html = self.render_print(sample_context())
        for value in ("Tax Invoice", "Included GST (10%)", "AUD $30.00", "AUD $330.00", "AUD $220.00", "Bill to: Example Parent"):
            self.assertIn(value, html)

    @patch("qas_custom.modules.notifications.commands.build_parent_invoice_context")
    def test_invoice_email_uses_same_breakdown(self, build):
        for posting in ("2026-10-08", "2026-10-09"):
            build.return_value = sample_context(posting)
            html = _invoice_email_message(frappe._dict(name="GST-TEST"), "approved", 110, 220, "")
            self.assertEqual("Tax Invoice" in html, posting == "2026-10-09")
            self.assertEqual("Included GST (10%)" in html, posting == "2026-10-09")
            self.assertIn("AUD $330.00", html)
            self.assertIn("AUD $220.00", html)

    @staticmethod
    def render_print(context):
        doc = frappe._dict(name=context["invoice"], posting_date=context["posting_date"], due_date=context["due_date"], items=context["items"], taxes=[])
        # Dict.items is a method, so use an attribute object for Jinja's doc.items.
        from types import SimpleNamespace
        doc = SimpleNamespace(**doc)
        return Environment(autoescape=True).from_string(_parent_invoice_print_html()).render(doc=doc, qas_invoice_print_amounts=lambda _: context)
