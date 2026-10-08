# Contributing

Keep changes focused. Search existing issues and discuss significant API,
storage, or protocol changes before implementation. Report vulnerabilities
privately using the [security policy](SECURITY.md).

## Development setup

Follow the [setup instructions](README.md#setup), then install development
dependencies. Frontend checks require Node.js 22.

```sh
git switch develop
python -m pip install -r requirements-dev.txt
```

If `develop` does not exist in a fresh checkout, create it with
`git switch -c develop`. Make changes on `develop`, never directly on `main`.
For contributions from a fork, clone your fork in the setup step.

For automatic reload, run `uvicorn app.main:app --reload` instead of the shared
runner. This single listener includes admin routes; keep it local.
Never commit `.env`, credentials, invite tokens, databases, or exports.

## Checks

Run the same checks as CI, using isolated test databases:

```sh
python -m pip check
python -m ruff check .
python -m compileall -q app scripts tests
python -m pytest
node --check app/static/client.js
node --check app/static/appearance.js
node --check app/static/client-protocol.js
node --check app/static/link-form.js
node --check app/static/admin-links.js
node --check app/static/invite-vault.js
node --check docs/theme.js
node --test tests/client_retry.test.cjs tests/client_delivery.test.cjs tests/client_message_status.test.cjs tests/client_crypto.test.cjs tests/client_identity.test.cjs tests/client_ui.test.cjs tests/client_theme.test.cjs tests/admin_links.test.cjs tests/invite_vault.test.cjs
node --test tests/site_theme.test.cjs
docker build -t sideword-chat-server:test .
```

Add tests for behavioral changes and update affected documentation. Keep the
`docs/` landing page current when capabilities, API contracts, security, or
deployment instructions change.
Keep documentation focused: README for setup/use, API for request contracts,
PROTOCOL for wire/client requirements, UPGRADING for operator procedures, and
SECURITY_REVIEW for current protections, remaining work and latest verification.
Link to the owning document instead of repeating it; keep release history in
CHANGELOG and replace obsolete status notes rather than appending work diaries.
Before preparing a commit, review all repository documentation against the final
changes and correct stale guidance, duplication and affected links. Reuse valid
review evidence for unchanged material, and finish edits before final checks.

## Pull requests and releases

Open a pull request targeting `main` that describes the problem, change, and
validation. Use concise Conventional Commit subjects, such as `fix: handle expired invites`.

CI checks pushes to every branch except `main` and pull requests targeting
`main`. After both check jobs pass on the exact `develop` commit, GitHub Actions
promotes it to `main`. Other branches and pull requests run checks only.
Never push `main` locally. If history diverges, integrate on `develop` and rerun
checks; outdated runs are skipped. Repository rules must allow the promotion
job's `GITHUB_TOKEN` to push with `contents: write`.

For a release, update `app/version.py` and the matching dated section in
[CHANGELOG.md](CHANGELOG.md) before pushing to `develop`. After successful checks
and promotion, the [release workflow](.github/workflows/release.yml) creates
`vX.Y.Z` on that exact commit and publishes a GitHub release using that section
as its notes. It skips outdated runs and existing releases, fails on missing or
invalid notes, and refuses to move a tag belonging to another commit. Rerun the
CI workflow for the current commit to retry a failed publication. Repository
rules must also allow the job's `GITHUB_TOKEN` to create tags and releases with
`contents: write`; no personal access token is needed.

Use `MAJOR.MINOR.PATCH`: patch versions for compatible fixes, minor versions for
features, deprecations or pre-1.0 breaking changes, and major versions for stable
breaking changes. The backend version is exposed in `/health` and OpenAPI;
API `/api/v1` and encryption envelope versions are independent. Promotion and
release publication do not deploy the backend or GitHub Pages.

Contributions use the project's [MIT license](LICENSE). Follow the
[code of conduct](CODE_OF_CONDUCT.md).
