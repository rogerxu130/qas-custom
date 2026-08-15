# Material Orders — School Admin Design

## Purpose and scope

Queensland Art School will sell art-material sets to existing families. This
first release provides the School Admin workflow and the supporting backend;
it does **not** provide a parent ordering page yet. Inventory quantities are
also out of scope.

School Admin users can:

- maintain a catalogue of active material products, including an image,
  description, price, and display order;
- create an order for an existing family with one or more products and
  quantities;
- select one of the family's future class sessions as the pickup appointment;
- see the generated material invoice and its real payment state; and
- move the order through `Ordered`, `Ready for collection`, `Collected`, or
  `Cancelled`.

The selected session supplies the pickup campus, date, and time. The user does
not choose a separate campus.

## Non-goals

- No parent-facing product list, checkout, or parent order history in this
  release.
- No stock ledger, warehouse, reservation, or quantity-on-hand management.
- No automatic refund or Store Credit issuance after a paid order is
  cancelled.

## Data model

### Material Product

A setup document containing:

- product name, description, image attachment, unit price, active flag, and
  display order;
- an ERPNext Item link, created/updated as a non-stock item; and
- audit fields supplied by Frappe.

Changing a product must not rewrite existing order lines. Inactive products
remain visible on historical orders but cannot be selected for new orders.

### Material Order

The order is a separate business document, not an Enrollment or an Invoice.
It stores:

- family/parent/customer references;
- the selected future Course Session;
- pickup campus, pickup date, and pickup time copied from that session at
  creation time;
- the linked Sales Invoice;
- order status (`Ordered`, `Ready for collection`, `Collected`, or
  `Cancelled`); and
- action timestamps and actors for ready, collection, and cancellation.

### Material Order Item

A child table storing the Material Product reference, product-name snapshot,
ERPNext Item snapshot, unit-price snapshot, quantity, and line amount.

## Invoice and payment rules

Creating an order creates and emails a submitted Sales Invoice labelled
`Material Order`. Its line items are material items and its totals come from
the frozen order-line prices and quantities.

Material invoices are isolated from tuition billing:

- they cannot consume Store Credit;
- their payment cannot create a Store Credit bonus;
- they are never included in automatic tuition Store Credit application; and
- they remain visible in normal invoice/payment records for audit purposes.

Invoice payment state is determined from the linked Sales Invoice, not copied
into the order as a source of truth. The Admin order page shows `Unpaid` or
`Paid` beside the order status.

School Admin may mark an invoice paid through the existing controlled payment
workflow, including when a family collects goods and pays at the front desk.

## State rules

Order state and payment state are intentionally independent.

| Order state | Invoice can be | Allowed next action |
| --- | --- | --- |
| Ordered | Unpaid or Paid | Ready for collection, Cancel |
| Ready for collection | Unpaid or Paid | Collected, Cancel |
| Collected | Unpaid or Paid | No order-state change; School Admin may still later mark the invoice paid |
| Cancelled | Unpaid or Paid | No order-state change |

This allows a family to collect goods after paying cash at the front desk even
when the School Admin must record the payment later.

Cancellation is deliberately conservative:

- if the linked invoice is unpaid, cancel both the order and that invoice;
- if the invoice is paid, cancel the order only; any refund or Store Credit is
  a separate, deliberate School Admin action.

## School Admin UI

Add a `Materials` entry to the School Admin portal with two views.

### Products

The catalogue view lists active/inactive products and supports creating and
editing name, image, description, price, display order, and active state.

### Orders

The orders view lists family, products/quantities, total, pickup session/date/
campus, order status, invoice number, and live payment state. It provides a
new-order form and the allowed state actions. Order detail keeps the item and
pickup snapshots visible even if the source product or course changes later.

For an Admin-created order, the form only offers future sessions belonging to
the chosen family. A server-side validation repeats this check; the browser is
not trusted.

## Services and permissions

Dedicated material-order services will own product setup, order creation,
invoice creation, state transitions, invoice-payment integration, and
cancellation. Existing generic manual-invoice defaults must not be reused
without explicitly setting the material-invoice flags.

Only School Admin users may create/edit products, create/manage orders, or
record material-invoice payments. Parent-facing read/order APIs will be added
in the later parent-ordering release.

## Error handling and audit

- Reject empty orders, non-positive quantities, inactive products, missing
  family data, and non-future or unrelated pickup sessions.
- Reject state transitions that are not allowed from the current order state.
- Prevent duplicate invoice generation for an existing order.
- Retain the invoice, item, price, pickup, operator, and time audit trail.
- Surface concise validation errors near the relevant Admin form; errors must
  not appear in unrelated portal panels.

## Verification

Automated coverage will include product/item creation, multiple item lines,
price snapshots, family-session validation, invoice generation/email queueing,
Store Credit exclusion (both application and bonus), order transitions,
unpaid/paid cancellation behavior, and later payment recording after
collection. The School Admin UI will be verified through its production build
and targeted interaction tests.
