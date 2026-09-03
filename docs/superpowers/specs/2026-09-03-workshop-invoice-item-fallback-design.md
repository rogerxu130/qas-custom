# Workshop Invoice Item Fallback Design

## Goal

Allow Workshop draft invoices to use the existing `Workshop Fee` ERPNext Item without requiring a site-config key, while keeping each invoice specific and understandable to the parent.

## Selected Behaviour

Invoice Item resolution uses this order:

1. `qas_workshop_invoice_item`
2. `qas_default_invoice_item`
3. An existing Item whose Item Code is exactly `Workshop Fee`

Configured values remain authoritative. A configured Item that does not exist is treated as a configuration error rather than silently replaced. If no configuration is present and `Workshop Fee` does not exist, the error explains both available remedies.

The Sales Invoice line keeps `Workshop Fee` as its accounting Item Code. Its description continues to contain the Workshop title, student, campus, and session dates, so the parent can identify what the charge covers.

## Scope

This is a backend-only change. It does not create Item records, alter DocTypes, submit invoices, or change Workshop Enrollment and Attendance state.

## Verification

- Configured Workshop Item is preferred.
- Configured default Item is the second choice.
- Existing `Workshop Fee` is used only when neither key is configured.
- Missing or invalid Items produce actionable errors.
- Python syntax validation passes.
