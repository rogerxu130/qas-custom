# QAS delivery rules

## GitHub and production branch

- When the user says "推 GitHub", "推github", "上线", or asks to publish these QAS changes, the authorized destination is `origin/main` unless the user explicitly names another branch.
- Pushing a feature branch alone does not complete that request. Do not describe it as ready for production or delivered.
- Fetch and inspect the latest `origin/main` before publishing. Preserve its existing changes. If the working branch diverges, use an isolated worktree based on `origin/main` and cherry-pick only the task's commits; resolve conflicts and validate the integrated result.
- Commit only task-related files. Preserve unrelated working-tree changes. Never force-push `main`.
- For features spanning Parent Portal and `qas_custom`, push the required changes to `main` in both repositories. Prefer backend first when the frontend needs a new API.
- Verify the actual remote `main` commit after pushing. Report its commit hash and distinguish GitHub delivery from Frappe Cloud / Netlify deployment status. Never claim deployment succeeded without checking it.
- Frappe Cloud QAS Custom tracks `main`. Publishing a different branch will not expose an update there. After pushing, state any remaining Frappe Cloud update/migration and frontend deployment steps accurately.

This is an explicit user preference recorded on 2026-09-08. Do not ask the user to repeat it.
