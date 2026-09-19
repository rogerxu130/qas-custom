# Stripe Live trial rollout

Implement the scope approved in the Stripe/invoice-deadline handover and the request to implement now. Live qualifies all active Parents with enabled linked Users and matching invoice Customers; Test remains restricted to the original linked test User. Keep all trial source, company, AUD, submitted/unpaid, non-return, positive-balance, support-view and token checks. Reject test-marked invoices at both Live Checkout and settlement.

Use one mode-aware owner helper across URL creation, authorization and settlement. Preserve signed-token format, keys, configuration fields and values. Keep the existing English frontend flow and receipt/admin notification pipeline. No generalized rollout toggle or separate payment flow is needed. DocType metadata changes only update the enable label and explanatory HTML; normal migration syncs these without resetting settings.

Validate real owner-gate behavior with mocked infrastructure across links, authorization, Checkout and posting; retain existing signature, duplicate-event, Test bookkeeping and Checkout retry regressions. Regress invoice-deadline tests. Update both operations documents; publish backend first, then frontend documentation, to origin/main without unrelated working-tree changes. The operator deploys and migrates production. Remove the superseded one-time automation after delivery.
