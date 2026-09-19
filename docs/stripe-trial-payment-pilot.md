# Trial invoice Stripe payments

## Scope and current state

As of the 19 September 2026 rollout, Live payments are available to every eligible active Parent with an enabled linked User and a matching invoice Customer. Test payments remain restricted to the linked User `rogerxu130@gmail.com`; a matching contact email does not qualify another account. This mode-aware check applies to invoice emails, parent/admin payment links, payment summaries, Checkout creation and payment posting.

Eligible records are submitted, unpaid, positive AUD trial Sales Invoices for the configured company (including replacement trials), identified by trial source or the Inquiry's trial_invoice link. Returns, drafts, cancelled invoices, ordinary course, Workshop and store invoices are excluded. Live Checkout and settlement reject test-marked invoices. Existing eligible unpaid trial invoices can obtain a link through the parent or admin view; historical emails are not resent.

Signed links remain invoice/Parent/mode scoped bearer links. Their format and signing secret are unchanged, so existing unexpired Live links remain valid if the owner and invoice still qualify. Altered or mismatched Parent links are denied; switching mode invalidates links from the other mode. Parent invoice listing remains scoped to the authenticated Parent's Customer; possession of a valid emailed payment link continues to allow guest payment.

## Deploy the 19 September Live rollout

Update QAS Custom from main and run the normal Frappe migration to synchronize the Stripe Settings label and explanatory text. Existing enable value, mode, keys, signing secret and accounting references are preserved; no new setup or accounting patch is needed for this rollout. An already enabled Live site opens eligible trial payments when the new backend code is deployed. Do not switch a configured Live site to Test merely to deploy this update.

The Parent Portal already consumes backend payment URLs and has no email allowlist. This release changes its operations document only; the existing English direct-Checkout and paid-state interface is retained. Verify the deployed frontend contains `e234e95` or a successor (including the earlier invoice-deadline settings). GitHub delivery is not evidence of Frappe/Netlify deployment or migration.

After deployment, verify a non-test parent's ordinary and replacement trial invoice shows the payment link, non-trial and other-family invoices remain inaccessible through that parent's account, and the authorized payment completes with one Payment Entry, receipt/PDF and admin notification. Confirm actual email delivery, not only queueing. Do not create charges or send historical emails in bulk. Test mode verification belongs on controlled test records and remains limited to the original test account.

## New invoice numbers

Normal new Sales Invoices use the current calendar year's last two digits plus a minimum five-digit global sequence: `2600001`, `2600002`; `2700001` in 2027. All invoice types share this sequence. Existing invoices and existing drafts retain their names. New amendments receive a fresh numeric number and retain their `amended_from` audit link. Explicit imported names are preserved. Existing names are checked before assignment, and numbers occupied by cancelled records are never reused. Beyond 99,999 the sequence expands rather than truncating.

The migration changes the default series while retaining legacy series options for saved records. No historical records are renamed. Newly generated names propagate naturally to the existing portal, emails, PDFs and bank-transfer references.

## Initial setup for a new installation (not required again for this rollout)

1. Deliver backend changes to qas_custom/main when publishing is authorized, then update the app and run the Frappe migration. Deliver frontend changes and verify its deployment separately. Do not enable payments until both deployments are verified.
2. In Frappe Desk, search for **QAS Stripe Settings**. Only System Manager can read/write this settings document. Leave **Enable trial payments** off while configuring.
3. Set **Mode = Test**. Enter **Test Secret Key** (`sk_test_…`) from the existing Stripe account's test environment. Password fields are encrypted by Frappe and masked in the form; never paste secrets in chat, documents or git.
4. In Stripe's test environment, create a webhook endpoint using the exact **Webhook URL** displayed in the Frappe form after saving. Select `checkout.session.completed` and `checkout.session.async_payment_succeeded`. Paste that endpoint's `whsec_…` into **Test Webhook Signing Secret**. Do not use a local CLI signing secret for the deployed endpoint.
5. Run the `v2026_09_17_setup_stripe_accounts` migration. It creates/reuses the QAS AUD **Stripe Clearing** account and **Stripe** Bank payment mode, adds a company mapping when absent, and fills empty accounting references in **QAS Stripe Settings**. Existing references, payment-mode mappings, secrets, mode and enable switch remain unchanged. Verify the resulting account and payment mode in Desk. This feature records gross receipts into the clearing account. Fees, bank payouts, refunds and disputes require separate reconciliation; they are not treated as a customer's outstanding debt.
6. Save, then enable trial payments. If the site is staging, the existing payment-mutation environment setting must explicitly allow testing. Do not disable global CSRF protection.
7. Confirm Apple Pay is enabled/available in the Stripe account. Stripe-hosted Checkout presents Apple Pay only on supported devices/browsers with an eligible wallet; manual card entry remains available. No separate QAS portal-domain Apple Pay registration is needed for this hosted flow.

## Payment acceptance

- In Live mode, book a trial under an eligible parent and verify its submitted invoice receives a Pay online button in the invoice email and parent's invoice view. Existing unpaid eligible trial invoices can be opened from the parent view or the admin invoice's **Online payment link**.
- In Live mode, another eligible parent sees links for their own trial invoices. Cross-family invoice access remains denied. In Test mode, only the original test parent sees links. Test cancelled/draft/paid and non-trial records too.
- Test card payment in Stripe test mode on a fresh pilot trial invoice without store credit. A new full-flow attempt marks the invoice and Payment Entry as **Stripe Test Record**, submits one Payment Entry to **Stripe Test Clearing**, and updates the invoice to Paid. Verify the parent TEST receipt email/PDF and the admin TEST payment email. No actual money is collected. Use a fresh pilot invoice for another complete test.
- Test the Apple Pay flow on a compatible device following Stripe's current testing instructions; a desktop mock does not establish actual wallet availability.
- Test the same event delivered twice, a timeout followed by retry, a failed card payment, cancellation/back navigation, and the browser closed immediately after payment. Check the webhook delivery and **QAS Stripe Payment** records in Desk.
- Test an offline payment/invoice change while Checkout is open. Existing Stripe-hosted pages can remain open until expiration; if funds arrive against a changed invoice, the payment record becomes **Needs Review**. It must not silently create a duplicate allocation.
- Test balance changes before creating/reusing a Checkout: a stale open Session is expired before a replacement is used. Unknown session creation older than 23 hours is held for staff reconciliation rather than risk reuse of an expired Stripe idempotency key.
- If an event is **Retry**, fix the accounting/configuration failure and resend the event from Stripe. Do not manually mark paid and then blindly replay the webhook. **Needs Review** requires comparing the Stripe payment and Frappe ledger before any adjustment/refund.

## Live and Test separation

Live uses the Live Secret Key, Live Webhook Signing Secret and live clearing account. Test uses separate credentials and Stripe Test Clearing. The rollout does not change any saved settings. Switching mode requires a fresh link for that mode; old Test links cannot authorize Live payments. Webhooks still validate the actual event environment and finish matching in-flight attempts even when the selected settings mode has changed or Checkout was disabled.

## Development verification

Automated tests cover numeric naming and collision handling, signed links, mode-aware owner eligibility, disabled/support access, invoice eligibility, webhook signature/amount/mode validation, duplicate events, test-versus-live posting, uncertain Checkout retries and email button gating. Browser tests use mocked APIs for mobile/desktop layout, token and CSRF headers, failure states, test success, review states and missing links. These do not replace a deployed Frappe migration, real database ledger tests, Stripe test-account integration or Apple Pay device testing.

## Accounting bootstrap recovery

The migration requires a unique active asset Bank account group (or a uniquely named Bank Accounts group) for the configured company. It fails with an actionable error if the group is ambiguous or an existing account/payment mode is incompatible; it does not reclassify existing objects. Select a valid clearing account or resolve the group, then rerun migration. If the QAS company did not exist when migration ran, it leaves the site unchanged; after company creation run:

```sh
bench --site YOUR_SITE execute qas_custom.patches.v2026_09_17_setup_stripe_accounts.execute
```

The command is safe to rerun: existing records and administrator configuration are preserved. This migration does not enable payments or create payments. Stripe end-to-end testing is still required after deployment.

## Full test workflow update — 17 September 2026

Run the new `v2026_09_17_stripe_full_test_flow` migration. It adds visible test markers and a separate AUD Stripe Test Clearing account, without changing existing keys, mode or enable switch. Set School Email in QAS Invoice Settings: this is the admin payment notification recipient.

Payment links now check status and redirect automatically to English Stripe Checkout. Paid invoices show an English paid message. The return page polls for confirmation; cancellation allows an explicit retry without redirect loops. All payment UI copy is English.

Only **new** attempts opt into the full test workflow. Historic Test Paid records (including invoice 2600012 if already used) are not retroactively posted or emailed. Create a fresh trial invoice for the same pilot parent. Duplicate webhooks do not duplicate Payment Entries, receipts or admin emails. Test-marked invoices are blocked from Live Checkout. Test payments cannot consume store credit, grant real bonuses or report paid-invoice ad conversions.

These are actual marked accounting records in the current site, not an isolated database: ordinary reports may include them until cleanup. After acceptance, cancel the test Payment Entry first, then cancel the test invoice through normal accounting controls; retain the audit trail and coordinate any linked trial booking cleanup. Never reset balances using direct database writes. Test receipts say TEST / no real money collected. Do not use these invoices for actual tuition collection.

Acceptance checks: automatic Checkout; TEST receipt received with PDF; admin email received at School Email; invoice Paid/zero outstanding; one submitted marked Payment Entry to the test ledger; second delivery creates no duplicates; reopening invoice shows paid. Queued email is not evidence of delivery—check Email Queue/notification audit if mail is absent. Actual Stripe/Apple Pay and deployment verification must be performed after release.

## Rollout regression coverage — 19 September 2026

The Stripe test module has 35 passing tests, including 11 added rollout cases for ordinary/replacement Live links, owner/customer/user checks, unique-customer fallback, Test rejection at link/Checkout/settlement, old Live token compatibility, cross-parent tokens, non-trial exclusion, test-marked Live settlement exclusion, ordinary-parent Checkout reuse, and exactly-once Live posting/receipt/admin notification after Checkout is disabled. These are local tests with mocked database/Stripe boundaries, not production payment or email-delivery verification.
