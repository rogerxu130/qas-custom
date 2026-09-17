# Stripe accounting initialization

Approved in conversation: initialize accounting objects in code instead of asking the operator to create them manually. Use a post-model-sync migration, not fixtures or an every-deploy overwrite.

- Use the configured company, otherwise Queensland Art School. If absent, leave the site unchanged; the patch can be rerun explicitly after company setup.
- Preserve configured references. Otherwise reuse the company's uniquely named Stripe Clearing account, or create an AUD Bank asset ledger under a unique active Bank group (fallback: Bank Accounts group).
- Reject ambiguous groups and incompatible existing accounts with an actionable migration error. Do not silently reclassify accounts or change historical ledger data.
- Reuse/create enabled Bank payment mode Stripe. Add a company mapping if missing; preserve existing mappings.
- Fill only empty company, clearing account, payment mode and mode settings. Never write credentials, enable the pilot, switch existing Test/Live mode, or widen the email allowlist.
- Validate reruns, partial configuration, existing mappings, invalid account/mode and ambiguous groups through mocked database tests. Production migration and actual Stripe payment remain separate verification steps.

Implementation plan: add migration and registry entry, add behavioral tests, update operator runbook, run new and existing Stripe regression tests, publish backend main following QAS delivery rules. No frontend change is required.
