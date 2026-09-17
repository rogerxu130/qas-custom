# Trial invoice payment pilot

## Scope and current state

Implementation is restricted in backend code to the active Parent whose linked User is `rogerxu130@gmail.com`. The invoice customer must match that Parent. This check applies to invoice emails, parent/admin payment links, payment summaries, Checkout creation and payment posting. Logged-in staff identity, entered checkout email or request parameters cannot enable another family's invoice. There is no global rollout switch in this version; widening access requires the user's explicit instruction and a subsequent change.

Eligible records are submitted, unpaid AUD trial Sales Invoices (including replacement trial invoices), identified by trial source or the Inquiry's trial_invoice link. Existing eligible pilot invoices can also be paid. No mass resend is performed. Other invoices retain their existing offline flow.

## New invoice numbers

Normal new Sales Invoices use the current calendar year's last two digits plus a minimum five-digit global sequence: `2600001`, `2600002`; `2700001` in 2027. All invoice types share this sequence. Existing invoices and existing drafts retain their names. New amendments receive a fresh numeric number and retain their `amended_from` audit link. Explicit imported names are preserved. Existing names are checked before assignment, and numbers occupied by cancelled records are never reused. Beyond 99,999 the sequence expands rather than truncating.

The migration changes the default series while retaining legacy series options for saved records. No historical records are renamed. Newly generated names propagate naturally to the existing portal, emails, PDFs and bank-transfer references.

## Deploy and configure (not performed during development)

1. Deliver backend changes to qas_custom/main when publishing is authorized, then update the app and run the Frappe migration. Deliver frontend changes and verify its deployment separately. Do not enable payments until both deployments are verified.
2. In Frappe Desk, search for **QAS Stripe Settings**. Only System Manager can read/write this settings document. Leave **Enable trial payment pilot** off while configuring.
3. Set **Mode = Test**. Enter **Test Secret Key** (`sk_test_…`) from the existing Stripe account's test environment. Password fields are encrypted by Frappe and masked in the form; never paste secrets in chat, documents or git.
4. In Stripe's test environment, create a webhook endpoint using the exact **Webhook URL** displayed in the Frappe form after saving. Select `checkout.session.completed` and `checkout.session.async_payment_succeeded`. Paste that endpoint's `whsec_…` into **Test Webhook Signing Secret**. Do not use a local CLI signing secret for the deployed endpoint.
5. Run the `v2026_09_17_setup_stripe_accounts` migration. It creates/reuses the QAS AUD **Stripe Clearing** account and **Stripe** Bank payment mode, adds a company mapping when absent, and fills empty accounting references in **QAS Stripe Settings**. Existing references, payment-mode mappings, secrets, mode and enable switch remain unchanged. Verify the resulting account and payment mode in Desk. This feature records gross receipts into the clearing account. Fees, bank payouts, refunds and disputes require separate reconciliation; they are not treated as a customer's outstanding debt.
6. Save, then enable the pilot. If the site is staging, the existing payment-mutation environment setting must explicitly allow testing. Do not disable global CSRF protection.
7. Confirm Apple Pay is enabled/available in the Stripe account. Stripe-hosted Checkout presents Apple Pay only on supported devices/browsers with an eligible wallet; manual card entry remains available. No separate QAS portal-domain Apple Pay registration is needed for this hosted flow.

## Pilot acceptance

- Book a trial under the pilot parent and verify its submitted invoice receives a Pay online button in the invoice email and parent's invoice view. Existing unpaid pilot trial invoices can be opened from the parent view or the admin invoice's **Online payment link**.
- Log in as another parent: no online-payment link. Attempt a direct API call for another invoice: access denied. Test cancelled/draft/paid and non-trial records too.
- Test card payment in Stripe test mode on a fresh pilot trial invoice without store credit. A new full-flow attempt marks the invoice and Payment Entry as **Stripe Test Record**, submits one Payment Entry to **Stripe Test Clearing**, and updates the invoice to Paid. Verify the parent TEST receipt email/PDF and the admin TEST payment email. No actual money is collected. Use a fresh pilot invoice for another complete test.
- Test the Apple Pay flow on a compatible device following Stripe's current testing instructions; a desktop mock does not establish actual wallet availability.
- Test the same event delivered twice, a timeout followed by retry, a failed card payment, cancellation/back navigation, and the browser closed immediately after payment. Check the webhook delivery and **QAS Stripe Payment** records in Desk.
- Test an offline payment/invoice change while Checkout is open. Existing Stripe-hosted pages can remain open until expiration; if funds arrive against a changed invoice, the payment record becomes **Needs Review**. It must not silently create a duplicate allocation.
- Test balance changes before creating/reusing a Checkout: a stale open Session is expired before a replacement is used. Unknown session creation older than 23 hours is held for staff reconciliation rather than risk reuse of an expired Stripe idempotency key.
- If an event is **Retry**, fix the accounting/configuration failure and resend the event from Stripe. Do not manually mark paid and then blindly replay the webhook. **Needs Review** requires comparing the Stripe payment and Frappe ledger before any adjustment/refund.

## Live mode and release to other parents

Once test mode checks pass, enter Live Secret Key and the separate live endpoint signing secret. Switching Mode to Live authorizes real charges but **does not expand the pilot beyond the same parent**. Old test-mode links stop working; obtain a fresh link or explicitly resend that pilot invoice. A small live pilot payment is needed to confirm the actual receipt, ledger and payout reconciliation.

Only after the user confirms successful pilot testing and explicitly asks to open access should the backend gate be widened. Never enable other parents automatically after a date, number of successful payments, or a switch to Live mode.

## Development verification

Automated tests cover numeric naming and collision handling, signed links, pilot identity, disabled/support access, invoice eligibility, webhook signature/amount/mode validation, duplicate events, test-versus-live posting, uncertain Checkout retries and email button gating. Browser tests use mocked APIs for mobile/desktop layout, token and CSRF headers, failure states, test success, review states and missing links. These do not replace a deployed Frappe migration, real database ledger tests, Stripe test-account integration or Apple Pay device testing.

## Accounting bootstrap recovery

The migration requires a unique active asset Bank account group (or a uniquely named Bank Accounts group) for the configured company. It fails with an actionable error if the group is ambiguous or an existing account/payment mode is incompatible; it does not reclassify existing objects. Select a valid clearing account or resolve the group, then rerun migration. If the QAS company did not exist when migration ran, it leaves the site unchanged; after company creation run:

```sh
bench --site YOUR_SITE execute qas_custom.patches.v2026_09_17_setup_stripe_accounts.execute
```

The command is safe to rerun: existing records and administrator configuration are preserved. This migration does not enable the pilot or create payments. Stripe end-to-end testing is still required after deployment.

## Full test workflow update — 17 September 2026

Run the new `v2026_09_17_stripe_full_test_flow` migration. It adds visible test markers and a separate AUD Stripe Test Clearing account, without changing existing keys, mode or enable switch. Set School Email in QAS Invoice Settings: this is the admin payment notification recipient.

Payment links now check status and redirect automatically to English Stripe Checkout. Paid invoices show an English paid message. The return page polls for confirmation; cancellation allows an explicit retry without redirect loops. All payment UI copy is English.

Only **new** attempts opt into the full test workflow. Historic Test Paid records (including invoice 2600012 if already used) are not retroactively posted or emailed. Create a fresh trial invoice for the same pilot parent. Duplicate webhooks do not duplicate Payment Entries, receipts or admin emails. Test-marked invoices are blocked from Live Checkout. Test payments cannot consume store credit, grant real bonuses or report paid-invoice ad conversions.

These are actual marked accounting records in the current site, not an isolated database: ordinary reports may include them until cleanup. After acceptance, cancel the test Payment Entry first, then cancel the test invoice through normal accounting controls; retain the audit trail and coordinate any linked trial booking cleanup. Never reset balances using direct database writes. Test receipts say TEST / no real money collected. Do not use these invoices for actual tuition collection.

Acceptance checks: automatic Checkout; TEST receipt received with PDF; admin email received at School Email; invoice Paid/zero outstanding; one submitted marked Payment Entry to the test ledger; second delivery creates no duplicates; reopening invoice shows paid. Queued email is not evidence of delivery—check Email Queue/notification audit if mail is absent. Actual Stripe/Apple Pay and deployment verification must be performed after release.
