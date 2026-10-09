from datetime import date
from decimal import Decimal, ROUND_HALF_UP


# QAS requested GST presentation for invoices from this date; earlier invoices stay unchanged.
GST_DISPLAY_START_DATE = date(2026, 10, 9)


def invoice_gst_context(invoice_doc):
    from qas_custom.modules.billing.store_credit import get_invoice_total_amount

    return gst_inclusive_breakdown(invoice_doc.get("posting_date"), get_invoice_total_amount(invoice_doc))


def gst_inclusive_breakdown(posting_date, total):
    if not posting_date or date.fromisoformat(str(posting_date)[:10]) < GST_DISPLAY_START_DATE:
        return {"gst_included": False, "invoice_title": "Invoice"}
    amount = Decimal(str(total)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    gst = (amount / 11).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "gst_included": True,
        "invoice_title": "Tax Invoice",
        "gst_amount": float(gst),
        "subtotal_excluding_gst": float(amount - gst),
    }
