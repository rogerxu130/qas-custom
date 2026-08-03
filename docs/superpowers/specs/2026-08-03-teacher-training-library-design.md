# Teacher Training Library Design

**Date:** 2026-08-03  
**Scope:** `qas_custom`, School Admin portal, and Teacher Portal

## Goal

Give every teacher a read-only Training tab containing published operational guidance. School Admin maintains the material in the existing Manage area. The first article is an NDIS guide covering common situations and how teachers should handle a student.

## Decisions

- Every active teacher can read every published article. There is no per-teacher targeting in this release.
- School Admin is the only role that can create, edit, publish, unpublish, or delete a training article.
- An article supports formatted text, multiple images, and multiple external links. YouTube links open in a new browser tab rather than being embedded.
- Draft and unpublished articles are never returned by a teacher-facing API.
- The Teacher Portal remains mobile-first. Training is the third primary tab, alongside the existing teacher home and classes views.

## Data model

Create a new `Teacher Training Article` DocType with these fields:

| Field | Purpose |
| --- | --- |
| `title` | Required article title. |
| `summary` | Short list-preview text. |
| `content` | Rich text body. |
| `images` | Child table of uploaded image files with optional captions and display order. |
| `links` | Child table with link label, URL, and display order. |
| `status` | `Draft` or `Published`. |
| `sort_order` | Ascending order for teachers. |
| `published_at` | Set when first published, used in the teacher-facing list. |

Child DocTypes keep images and links individually editable and sortable. Files use normal Frappe file storage and are shown through their public URL only when the article is published.

## APIs and authorization

### School Admin APIs

Add school-admin endpoints to list articles, retrieve one full article, save an article, and delete an article. Each endpoint requires the existing School Admin authorization helper. Save validates title, link URLs, status, image rows, and ordering.

### Teacher APIs

Add read-only teacher endpoints:

- list published articles with title, summary, first image if present, and published date;
- retrieve a single published article with body, images, and links.

Each endpoint resolves the current logged-in teacher using the existing Teacher Portal access rules before returning data. An article that is draft or unpublished returns no data to a teacher, even when its direct ID is known.

## School Admin experience

Within the existing `Manage` tab, add a `Teacher Training` section:

1. Left list of articles showing title, status, and last update.
2. Editor for title, summary, rich-text content, image rows, link rows, status, and ordering.
3. Explicit Publish/Unpublish action, with a visible published-state badge.
4. The NDIS guide is created as the first article through the same editor. No special hard-coded NDIS screen is introduced.

Saving an unpublished article is safe: it is visible only in the School Admin editor.

## Teacher experience

Add `Training` as the third top-level tab in the Teacher Portal. It loads published articles only.

- List view: title, summary, optional thumbnail, and last updated/published date.
- Detail view: back control, full formatted text, images in article order, then the link list.
- Each link includes its editor-provided label and opens externally in a new tab with safe link attributes.
- Empty state: `No training available yet` with a concise explanation.
- Failure state offers retry, without exposing internal server details.

Teachers receive no edit, publish, upload, or delete affordance.

## Content and rendering safety

Rich text is sanitized before teacher rendering. Image URLs and external URLs are validated. Unsupported URL schemes are rejected. YouTube is treated as an ordinary external URL in the first release; direct embedding is intentionally excluded to avoid iframe policy, mobile playback, and privacy complexity.

## Error handling

- Invalid or missing article ID returns a standard not-found result.
- Failed file or article save leaves the current School Admin form values intact and shows the returned validation message.
- Teacher API access requires a valid teacher-linked account, matching the existing Teacher Portal behavior.
- Removing an article from publication immediately removes it from the teacher list and blocks direct detail access.

## Validation

1. School Admin can save a Draft article with text, images, and links.
2. Draft article does not appear to a teacher.
3. Publishing makes the article appear in the Training tab; unpublishing removes it.
4. Teacher can read the full NDIS article and open a YouTube link.
5. A teacher cannot use APIs to retrieve a draft article or mutate any training content.
6. Build both portal frontends and run Python syntax checks for changed backend files.
