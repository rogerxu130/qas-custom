# Trial Special Needs Sync Design

## Goal

Make the Special Needs information submitted with a trial-class request available in both the Trial Inquiry and the linked Student record.  Teachers continue to see the same information in their existing student teaching-notes view.

## Terminology

`Student.teaching_notes` is the existing stored field.  It is the single source of truth for a student's Special Needs information, even where an older screen calls it “Teaching Notes”.  No second student field will be introduced.

## Data model

- Add `Inquiry.special_needs` as a `Small Text` field labelled **Special Needs**.
- Keep `Student.teaching_notes` as the stored student field, but label it **Special Needs** in School Admin-facing interfaces where it is shown or edited.
- The Trial Inquiry retains the value submitted with that request, so staff can see it directly on the request even after reviewing it.

## Ingestion and synchronisation

1. Normalise the trial-form Special Needs value from supported webhook/form aliases.
   The existing Trial Inquiry webhook accepts the canonical `special_needs` key, as
   well as `special_need` and `teaching_notes` for Make mappings.
2. Enforce a 500-character maximum on the server before any record is changed.
3. Store the normalised value on the newly created Inquiry.
4. After resolving the linked Student, set `Student.teaching_notes` to exactly that value.
5. An empty submitted value is meaningful: it clears `Student.teaching_notes` and leaves the Inquiry value empty.
6. This behaviour applies to new trial requests only; it does not retroactively change existing students or inquiries.

## Presentation

- The School Admin Trial Request detail payload and page expose **Special Needs** with the other submitted details.
- The existing teacher portal receives the same data through `Student.teaching_notes`; no duplicate teacher data path is added.
- The public/Fluent trial form should enforce the same 500-character limit and show a character counter.  The backend remains authoritative if a webhook bypasses the form limit.

## Failure handling

- A value longer than 500 characters is rejected with a clear validation message; no partial truncation is performed.
- Missing Student or Inquiry schema fields produce an explicit “run migrate” error rather than silently dropping the data.
- Existing student matching continues to use the present parent-and-date-of-birth matching rules.

## Verification

- New trial with `NDIS` populates both Inquiry Special Needs and Student Special Needs.
- New trial with `Special religion: ...` does the same.
- New trial with an empty value clears an existing student value.
- A 501-character value is rejected.
- School Admin Inquiry detail and teacher roster both display the expected value.
