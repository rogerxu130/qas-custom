# Same-number invoice reopen

User requirement: whenever Reopen as Draft is allowed, retain the invoice's existing name for editing and subsequent submission, including item, discount, amount and date edits. No amendment invoice is created.

The School Admin service locks the invoice and retains payment, store-credit and active-plan restrictions. Returns and invoices with reduced outstanding balances are also rejected. Already-draft retries return the existing draft; previously cancelled documents are not resurrected.

Within one transaction, normal ERPNext cancellation runs first to reverse accounting and validate dependencies. After success, the existing parent and child records are explicitly reset to Draft, rather than copied. The full pre-reopen invoice snapshot and reason are kept in an audit comment. Enrollment status snapshots are refreshed under the same invoice id; Inquiry pointers stay unchanged. Failure rolls the transaction back. Draft saving and resubmission use the standard validation and posting paths.

Approval notification deduplication includes the reopen audit revision so the revised invoice can be issued under the same number, while repeat attempts within that revision stay deduplicated. Reopening itself sends no parent invoice email.

Validation:
- 76 backend unit tests pass, including blocking payment/store credit, rollback and revision notification keys.
- Real local qas-restore.test integration: two consecutive submit/reopen/edit/submit cycles with changes to rate, discount and date, same invoice name, unchanged Inquiry link, distinct audit revisions, zero active receivable while Draft, GL receivable and outstanding equal revised totals after each submission, and zero amendment invoices.
- Local integration is rollback-only. Legacy test-site email Server Scripts were suppressed; mail and commits were prohibited. Production scripts/configuration and deployed operation have not been verified.
- Frontend production build passes.

Historical amendment numbers are retained; the prior repair migration reconnects their Inquiry links. This change does not rename historical invoices. Publish both repositories; update the actual Frappe Site, not only the Bench build.
