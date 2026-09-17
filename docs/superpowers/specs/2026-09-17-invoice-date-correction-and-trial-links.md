# Invoice date correction and trial amendment links

Approved scope: edit a submitted invoice's due date without cancellation or a new invoice number; keep trial Inquiry links when an amendment is necessary; recover existing broken amendment links.

The School Admin endpoint requires the existing School Admin authorization and locks the invoice before reading it. It accepts only a submitted non-return invoice, rejects QAS payment plans and multiple payment schedule rows, and rejects dates before posting. It updates only due dates on the invoice, single schedule and accounting ledger entries, recalculates status and records an audit comment. It does not create invoices, alter totals, move payments or send parent invoice emails.

The submitted invoice UI exposes a dedicated date field and save action. The existing cancel-and-amend action remains for item/amount corrections and explicitly states that it creates a new number.

Reopening now repairs the trial Inquiry link within the same transaction. Migration recovers links only when a cancelled linked invoice has a unique active descendant through explicit amended_from ancestry. Paid and partially paid descendants are included, with payment records unchanged. Conflicting sources, multiple active descendants or links owned by another Inquiry are reported for review instead of guessed. Reruns are idempotent. No optional source_type column is queried.

Validation: backend unit tests cover date-only preservation, installment protection, amendment chains, ambiguity, idempotence and the Inquiry paid badge after link repair; these mock database access and are not a live migration test. Frontend production build verified. Deployment and live site verification remain pending. Bench application build and actual Site update/migration are distinct steps.
