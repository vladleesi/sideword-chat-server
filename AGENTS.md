# Working agreement

## Authority and workflow
- Follow the current request; preserve unrelated changes and running services. Reply concisely in the user's language with outcomes, verification and limits.
- Inspect before editing. Decide routine details autonomously; ask only for missing decisions that change scope or authority.
- Work on `develop` only and leave it checked out. Never edit or push `main` locally.
- Do not commit, push, deploy, restart services or contact others without authorization. Commit/push approval must cover the current changes; preparing them or approving an earlier change is not permission.
- Never open, inspect or automate a browser. Use source inspection and non-browser checks.
- Start with `git status --short` and targeted `rg` searches. Read affected flows, callers and nearby tests; avoid whole-repository reads unless the task requires them.
- Batch independent reads/checks; sequence dependent work. Reuse existing code, tools and passing evidence. Avoid unnecessary dependencies, one-use abstractions, speculative changes and unrelated refactoring.
- Use relevant skills; delegate only when explicitly authorized. No automatic installations or hooks.
- Finish implementation, tests, configuration and documentation before one final validation phase. After failures, fix them and rerun only invalidated or unresolved checks. Never report readiness with required checks failing or unrun.
- Use isolated test databases, never a running instance's data. Bound output without hiding failures. Keep progress updates short and maintain a local progress note for multi-stage security work.
- Reuse unchanged verification across turns. Run dependency checks when dependencies/environment change, compilation when import/build behavior changes, and documentation checks for changed docs. Documentation-only work needs no backend tests.

## Security and data invariants
- Never print or commit `.env`, keys, passwords, invite tokens, databases, exports, personal paths, tunnel URLs or runtime logs. Use placeholders and repository-relative paths; keep this file tracked and free of machine details.
- Block public administration on the shared listener for both HTTP and WebSocket paths.
- Validate live sessions in HTTP and WebSocket paths. Consumed invites differ from explicitly revoked invites.
- SQLite row IDs can be reused. Deduplicate messages by chat, sender and client message ID; receipts also bind reader identity. Preserve exact delivery references.
- Treat naive SQLite timestamps as UTC; preserve ordering across polling, WebSocket and local history.
- Authenticate, decrypt and persist locally before deletion ACKs. Serialize incoming processing and keep failures retryable.
- Keep browser private keys non-exportable, render user content with `textContent`, and preserve CSP/no-store headers.
- Keep form errors inline and accessible; validate optional empty fields deliberately on the server.
- Each pre-release uses fresh state and current APIs/formats only. Do not add legacy fallbacks or automatic migrations.

## Repository map
- Entry/settings/database: `app/main.py`, `app/config.py`, `app/db.py`, `app/models.py`.
- Auth/invites: `app/deps.py`, `app/security.py`, `app/routers/sessions.py`, `app/services.py`, `app/routers/links.py`.
- Delivery: `app/routers/chats.py`, `app/routers/ws.py`, `app/ws_manager.py`.
- Client: `app/static/client.js`, `client.css`, `app/templates/client.html`; admin: `app/routers/admin_ui.py`, templates and `admin.css`.
- Shared runner: `scripts/serve_shared.py`; local admin on 8000, restricted public listener on 8001, one process/connection registry.

## Documentation
- Keep one authoritative explanation per topic: README for quick start, API for requests, PROTOCOL for cryptography/client requirements, SESSION_LIFECYCLE for access/device behavior, UPGRADING for operations, SECURITY_REVIEW for current evidence/limits, CHANGELOG for release history. Link instead of repeating.
- Use concise English, short paragraphs and descriptive headings. Keep UI descriptions brief; cosmetic changes belong in the changelog unless needed for correct use.
- Update `docs/SECURITY_REVIEW.md` with affected security code/tests: remove resolved findings, narrow partial ones, retain limits and record only obtained evidence. Do not claim deployment or independent audit results.
- Before preparing a commit, review all repository documentation against the final change; fix stale guidance, duplication and affected links before validation. Reuse valid evidence for unchanged material.
- Update the `docs/` site when published API, capabilities, client behavior, architecture, security/retention or deployment guidance changes. Preserve its backend focus and visual identity. Review affected copy, snippets, links and metadata; reuse unchanged social-asset verification. Internal maintenance alone needs no site edits.

## CI, versions and Git
- Use full numeric GitHub Action release tags, such as `actions/checkout@v7.0.1`; never SHA references or moving major-only tags.
- Use the [shared Renovate preset](https://github.com/vladleesi/renovate-config), overriding only the target branch to `develop`. Inherit dashboard approval for every update, including vulnerability updates, and disabled auto-merge. Preserve GitHub dependency graph/Dependabot alerts and do not restore routine Dependabot PR automation.
- Test CI runs on pushes except `main` and PRs targeting `main`. Promotion is Actions-only after `smoke`, `container` and both CodeQL analyses succeed on the exact `develop` commit. CodeQL blocks High/Critical security findings, error-level findings without security scores, and analysis/report errors. Manual deployment also requires successful promotion. Integrate divergence on `develop` and recheck. Scheduled scans are independent monitoring, not promotion gates.
- Before committing behavior/API/functionality changes, update `app/version.py`, its dated changelog section and affected docs. Reuse the prepared bump; reassess only when scope changes. Documentation, formatting, instructions and other nonfunctional maintenance need no version bump or release section solely for maintenance.
- Use `MAJOR.MINOR.PATCH` ([SemVer](https://semver.org/spec/v2.0.0.html)). Before 1.0, MINOR covers features, deprecations and breaking changes; PATCH covers compatible bug/security fixes. At 1.0+, MAJOR covers breaking changes, MINOR compatible features/deprecations, PATCH compatible fixes. Choose the highest required bump and reset lower components. Do not advance to 1.0 solely for a pre-release break. Backend, HTTP API and encryption versions are independent.
- Preserve published changelog entries, releases, tags and Git history. Corrections need a new release. Changelog sections use `## X.Y.Z — YYYY-MM-DD`, newest first, with concise English bullets on single source lines, no blank lines between bullets, and one blank line after headings/between releases. Keep `## Unreleased` only while nonempty; never invent old release dates.
- After exact-commit promotion, `release.yml` creates `vX.Y.Z` and publishes that changelog section. Retry failed publication by rerunning CI for the current commit. Publication deploys neither backend nor Pages.
- Before an authorized commit, inspect the diff, staged filenames, privacy exclusions and `git diff --cached --check`. Use concise lowercase imperative Conventional Commit subjects, e.g. `fix: handle expired invites`.
- After a successful authorized push, stop and report the known commit hash. Do not poll CI/promotion, fetch or verify remote refs unless requested. Never force-push or rewrite history without explicit permission.

## Checks

Use the active virtualenv Python; on Windows use `.\.venv\Scripts\python.exe`.

```sh
python -m ruff check .
python -m pytest
node --check app/static/client.js
node --check app/static/link-form.js
node --test tests/client_delivery.test.cjs
```

When relevant: `python -m pip check`, `python -m compileall -q app scripts tests`, and the additional checks in [CI](.github/workflows/test.yml).

## Ponytail integration

Adapted from [Ponytail](https://github.com/DietrichGebert/ponytail/blob/main/skills/ponytail/SKILL.md), MIT, copyright 2026 DietrichGebert; full notice in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Upstream changes are not adopted automatically.

Understand callers and fix shared causes. Choose the first adequate existing code, standard library, native feature, installed dependency, then minimal custom code. Keep changes readable and preserve validation, security, accessibility, data-loss protection and explicit requirements. Use meaningful tests without an arbitrary test ceiling or mandatory self-check scripts. All rules above take precedence. `stop ponytail` or `normal mode` disables only this supplement; intensity changes cannot override permissions or repository rules.
