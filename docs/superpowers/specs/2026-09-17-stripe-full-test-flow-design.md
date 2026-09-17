# Approved Stripe payment corrections

The user approved correcting English-only payment copy, automatic Checkout navigation, and a complete test payment workflow including invoice settlement, parent receipt and admin notification.

## Implementation

- Keep the existing fixed pilot parent and trial-invoice ownership checks.
- Automatically open English Stripe Checkout on an eligible unpaid link; show paid status without creating Checkout when settled. Poll after payment return; distinguish cancellation to avoid redirect loops. Store return tokens per attempt.
- Only newly created Test attempts carry full_test_flow. Mark their invoice and submitted Payment Entry with qas_stripe_test. Use a separate Stripe Test Clearing AUD ledger. Existing test-only callbacks remain historical and are never retroactively posted.
- Use the same Payment Entry submission and parent receipt queue as Live. Prefix test email/PDF receipts and admin email with TEST and disclose simulated funds. Admin email uses School Email in QAS Invoice Settings. Keep webhook identity, locks and duplicate guards.
- Reject real store-credit use in Test, block test invoices from Live Checkout, suppress test-funded bonus grants and paid-invoice advertising conversions.
- This is a marked, cleanable full workflow in the same ledger, not full database isolation. Cancel test Payment Entries and test invoices after acceptance. Never silently erase records or modify live invoices during deployment.

## Verification and delivery

Run mocked Stripe settlement/duplicate tests, receipt email/PDF label and admin recipient tests, test ledger and side-effect guards, frontend auto-navigation/return/cancel tests, build and syntax checks. Publish backend main first, then frontend main. User performs Frappe Cloud update/migration and real test-card acceptance. No production browser operation or emails are performed during development.
