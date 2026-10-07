# Working agreement

## Scope and communication
- Follow the current user request; preserve unrelated changes and running services.
- Reply in the user's language, concisely. Report outcome, verification, and remaining limitations.
- Inspect before editing. Resolve routine choices autonomously; ask only when a missing decision materially changes scope or requires new authority.
- Do not create commits, push, deploy, restart services, or contact others unless authorized.
- Commit and push require the user's explicit approval for the current changes. A request to implement work or prepare it for commit/push is not approval to commit or push; approval for an earlier change does not carry over.
- Never open, control, or inspect a browser, including browser discovery, automation, screenshots, and browser-based verification. Use source inspection and non-browser automated checks instead.

## Efficient workflow
- Start with `git status --short` and targeted `rg` searches. Read relevant functions and nearby tests, not the whole repository.
- Batch independent reads/checks; keep dependent steps sequential. Bound tool output and avoid repetitive polling.
- Reuse existing code, dependencies, and scripts. Prefer the smallest complete fix; avoid speculative abstractions and unrelated refactors.
- For complex tasks, finish all planned implementation, tests, documentation, and configuration edits before running validation. Do not run checks between implementation stages.
- Use skills only when relevant; read required instructions once. Delegate only when explicitly authorized and the subtask is independent.
- Keep a brief working summary for long tasks: objective, decisions, files, verification, next action. Do not repeat completed exploration.
- Reuse session findings and passing check results. Do not reread unchanged files or rerun tests merely because a new turn, commit, push, or deployment was requested.
- Run all applicable non-browser checks in one final validation phase before reporting that everything is complete or ready for commit/push. If validation fails, finish the necessary fixes, then rerun only checks invalidated by those fixes or unresolved failures. Do not report readiness while required checks fail or remain unrun.
- Run dependency checks only when dependencies/environment change, compile checks when import/build behavior changes, and documentation checks only for changed documentation. Documentation-only edits do not require backend tests.
- Review unchanged landing metadata/social assets by their existing verification record; rerender or inspect them again only when their content or claims change.
- Prefer bounded search results, diff summaries, and failure summaries over full files, full test logs, and repeated status calls. Never suppress failures to save output.
- Keep progress updates short: new finding, material decision, or blocker. Avoid repeating plans and successful checks.
- For multi-stage security work, maintain a concise local progress note with completed stages and verification so context recovery does not restart exploration. Efficiency must not remove security regression coverage or isolated-database safeguards.
- Keep `docs/SECURITY_REVIEW.md` aligned with affected code and tests in the same change. Remove resolved or superseded open findings, narrow partially fixed findings, record verified protections and current verification evidence, and preserve unresolved limitations. Keep release history in `CHANGELOG.md`; do not claim deployment or audit evidence that was not obtained.
- After code changes, assess whether the GitHub Pages site in `docs/` needs updating. Update it in the same change only when affected API contracts, described capabilities/client behavior, architecture, security/retention behavior, or deployment steps make its published information inaccurate or incomplete. Internal refactors, tests, tooling, and maintenance that do not affect published information require no site edits. Review only affected landing copy, API/deployment snippets, links, SEO/structured data, and social assets; reuse existing verification for unchanged material and preserve the site's backend-focused positioning.

## Ponytail integration
- Adapted from https://github.com/DietrichGebert/ponytail/blob/main/skills/ponytail/SKILL.md. This section supplements the working agreement; all other repository rules take precedence over it. Upstream updates are not adopted automatically.
- Ponytail is licensed under the MIT License. Copyright (c) 2026 DietrichGebert. The full upstream notice is retained in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- Apply to coding, debugging, reviews, and design. Understand the affected flow and callers first; fix the shared root cause rather than patching one symptom. Keep investigation targeted.
- Choose the first adequate solution: existing repository code, standard library, native platform feature, installed dependency, then minimal custom code. Exclude speculative additions, never requested functionality.
- Prefer readable, small changes and fewer dependencies. Avoid one-use abstractions, future scaffolding, unrelated cleanup, and compressed one-liners that obscure intent.
- Preserve validation, security, accessibility, data-loss protection, and every explicit requirement. Document material simplification limits where useful.
- Use existing test infrastructure and meaningful regression coverage; no one-test ceiling or mandatory self-check scripts. Run checks only in the existing final validation phase.
- No automatic installation, hooks, delegation, browser use, commits, pushes, or deployment. Existing approval, privacy, branch, version, and changelog rules remain intact.
- `stop ponytail` or `normal mode` disables only this supplemental guidance; other instructions remain active. Requested intensity changes cannot override them.

## Repository map
- FastAPI entry point: `app/main.py`; settings: `app/config.py`; async SQLite: `app/db.py`, `app/models.py`.
- Authentication: `app/deps.py`, `app/security.py`; invites: `app/services.py`, `app/routers/links.py`.
- Delivery: `app/routers/chats.py`, `app/routers/ws.py`, `app/ws_manager.py`.
- Browser client: `app/static/client.js`, `client.css`, `app/templates/client.html`.
- Admin UI: `app/routers/admin_ui.py`, `app/templates/`, `app/static/admin.css`.
- Shared test hosting: `scripts/serve_shared.py`; local admin on 8000, restricted shared listener on 8001, shared process/connection registry.

## Invariants
- Never print or commit `.env`, private keys, passwords, invite tokens, databases, exports, personal paths, tunnel URLs, or runtime logs. Use placeholders in examples.
- Keep `AGENTS.md` tracked as shared repository guidance; use repository-relative paths and placeholders, and exclude private machine or runtime details.
- Keep public administration blocked on the shared listener, including HTTP and WebSocket paths.
- SQLite row IDs may be reused after deletion: deduplicate messages using chat, sender, and client message ID; receipts also include reader identity.
- Treat naive SQLite timestamps as UTC. Preserve chronological ordering across polling, WebSocket, and local history reload.
- Persist authenticated decrypted messages before acknowledging deletion. Serialize incoming processing and keep failed items retryable.
- Validate sessions in HTTP and WebSocket paths. Distinguish consumed invites from explicit revocation.
- Keep private browser keys non-exportable; render user content using textContent. Preserve CSP and no-store headers.
- Keep form errors inline and accessible; validate optional empty fields deliberately on the server.

## Checks and Git
- Make all code, documentation, and configuration changes on `develop` only; create/check out it before editing. Never work directly on `main`.
- CI runs on pushes to every branch except `main`, and only on pull requests targeting `main`; do not trigger CI on pushes to `main`.
- Promotion to `main` is performed by GitHub Actions only, after both checks pass on the exact `develop` commit. Push changes to `develop`; never push `main` locally. If branches diverge, integrate on `develop` and recheck. Keep `develop` checked out.
- Before committing changes to application behavior, logic, APIs, or functionality, increment the backend version in `app/version.py`, update `CHANGELOG.md`, and update affected documentation. Documentation-only, formatting, agent-instruction, and other nonfunctional maintenance changes must not bump the version or create a release section solely to record that maintenance. Always update affected documentation as needed (including README, API/protocol, security, upgrade/deployment guidance, and the `docs/` site). Complete required updates before final validation and reporting readiness for commit. Reuse an appropriate version bump already prepared for the current changes; do not bump again merely because a commit is retried. Explicit user approval is still required to commit or push.
- Backend versions use `MAJOR.MINOR.PATCH`, following [Semantic Versioning](https://semver.org/spec/v2.0.0.html) with the pre-1.0 project policy below. Backend release, HTTP API and encryption protocol versions remain independent.
- At `1.0.0` and later, increment MAJOR for incompatible public API/behavior changes, MINOR for backward-compatible functionality or API deprecation, and PATCH for backward-compatible bug/security fixes.
- Before `1.0.0`, project policy is to increment MINOR for new functionality, API deprecation, or breaking API/behavior changes; use PATCH only for backward-compatible bug/security fixes. Removing an endpoint requires a minor bump, e.g. `0.3.3` to `0.4.0`, not `0.3.4`.
- Select the highest required bump from the final combined change set. Reassess a prepared bump when scope changes; a security fix that breaks compatibility is not a PATCH. Reset PATCH to zero for MINOR bumps, and MINOR/PATCH to zero for MAJOR bumps. Do not advance to `1.0.0` solely to record a pre-1.0 breaking change.
- Preserve already published version contents and release history; corrections require a new version, not rewriting a pushed release or Git history. The documentation-only exemption above still applies.
- Release automation runs after both CI checks and exact-commit promotion from `develop` to `main`. Prepare `app/version.py` and its dated changelog section together; `.github/workflows/release.yml` creates `vX.Y.Z` and uses that section as the GitHub release notes. Preserve existing releases and tags; retry failures by rerunning CI for the current commit. Publication does not deploy the backend or Pages.
- Windows Python: `.\.venv\Scripts\python.exe`; elsewhere use the active virtualenv Python.
- Checks: `python -m ruff check .`, `python -m pytest`, `node --check app/static/client.js`, `node --check app/static/link-form.js`, `node --test tests/client_delivery.test.cjs`.
- Dependency/build checks when relevant: `python -m pip check`, `python -m compileall -q app scripts tests`.
- Use isolated test databases; never exercise destructive tests against the running instance.
- Before an authorized commit: inspect diff, staged filenames, privacy exclusions, and `git diff --cached --check`.
- Changelog style: preserve this format in future updates. List releases newest first with `## X.Y.Z — YYYY-MM-DD` headings and concise English `- ` bullets describing user-visible changes. Keep each bullet on one source line, with no blank lines between bullets; keep one blank line after headings and between release sections. Include `## Unreleased` only when it contains pending changes; remove it when empty after preparing a release. Preserve published release history and do not invent dates for older undated releases. Use the corresponding release section as the GitHub release notes.
- Commit style: concise English Conventional Commits, lowercase imperative subject, e.g. `feat: add ...`, `fix: handle ...`, `chore: update ...`.
- After a successful authorized push, stop and report the commit hash using information already available. Do not watch or poll CI, monitor promotion, check push/tracking status, fetch, or verify remote refs unless the user explicitly requests those follow-up checks. Never force-push or rewrite history without explicit authorization.
