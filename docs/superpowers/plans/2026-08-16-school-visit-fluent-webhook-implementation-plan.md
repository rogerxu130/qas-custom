# School Visit Fluent Webhook Implementation Plan

1. Add a dedicated School Visit webhook service entry point that validates the existing secret, normalises Fluent payload aliases, pins the inquiry type/source, requests Parent creation, preserves raw form data, and delegates to the shared Inquiry core.
2. Expose that entry point from `qas_custom.api.inquiry` as a guest webhook method.  Do not add a new doctype or a Course Session parameter.
3. Extend the shared normalisation aliases for the exact School Visit form labels and preserve the existing `form_id + submission_id` idempotency contract.
4. Add focused unit tests for adapter normalisation, idempotent duplicate handling, and the no-session/no-trial-invoice response shape; retain the existing controller test for School Admin notification behaviour.
5. Run Python compilation and the focused inquiry tests, then commit only the School Visit implementation files.
