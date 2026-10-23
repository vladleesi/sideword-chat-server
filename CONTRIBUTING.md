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
python -m pip_audit --strict
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
node --test tests/client_retry.test.cjs tests/client_connection.test.cjs tests/client_delivery.test.cjs tests/client_message_status.test.cjs tests/client_crypto.test.cjs tests/client_identity.test.cjs tests/client_ui.test.cjs tests/client_theme.test.cjs tests/admin_links.test.cjs tests/invite_vault.test.cjs
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

## Dependency monitoring

[Renovate](.github/renovate.json) maintains one **Dependency Dashboard** issue for
`develop`. All routine updates require dashboard approval; automatic merging and
Renovate's separate vulnerability-fix PR mechanism are disabled. Leave approval
checkboxes unchecked when asking a coding agent to implement upgrades manually;
checking one authorizes Renovate to create an update branch/PR.

Default managers remain enabled without major/minor/patch filters. They cover
`requirements.txt`, `requirements-dev.txt`, GitHub Actions workflows, `Dockerfile`
and Docker Compose image references, plus other supported manifests added later.
The Compose image is built locally, not a separately maintained upstream image.
Inline shell installs are not a complete dependency inventory: keep the Dockerfile
and CI pip constraints aligned with `requirements-dev.txt` manually. Unpinned OS
packages and transitive vulnerabilities still require resolved-environment and
container scans; the dashboard is not a comprehensive vulnerability scanner.

To activate monitoring:

1. Promote this configuration from `develop` to `main` through the existing CI
   process. Renovate reads configuration from the default branch, `main`, while
   `baseBranchPatterns` selects `develop` for dependency analysis. Do not edit or
   merge an onboarding configuration directly into `main`.
2. Install the [Renovate GitHub App](https://github.com/apps/renovate) for this
   repository only, and keep repository Issues enabled. Installing before the
   configuration reaches `main` can create an initial **Configure Renovate** PR.
   This onboarding PR is separate from dependency updates; keep its proposed
   defaults unmerged and deliver this configuration through `develop` instead.
3. In **Settings > Advanced Security**, keep **Dependency graph** and **Dependabot
   alerts** enabled, and disable **Dependabot security updates**. Check Dependabot
   rules for separate PR-creation actions and disable those actions too. Removing
   `dependabot.yml` stops routine version updates, not security alerts or existing
   PRs. If an organization enforces a setting, its administrator must change it.
4. Enable **Security alerts** in the repository's Watch settings and choose
   notification delivery in your account settings. Use **Security > Dependabot**
   for vulnerability alerts; GitHub evaluates the default branch independently
   of Renovate's `develop` dashboard.
5. After the App's next run, find **Dependency Dashboard** under repository Issues.
   Review lookup warnings and detected files. Keep the issue open; it is updated
   on subsequent runs, including after manually implemented upgrades are pushed.

See Renovate's [approval behavior](https://docs.renovatebot.com/key-concepts/dashboard/)
and GitHub's [security-update settings](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/secure-your-dependencies/configure-security-updates)
and [alert settings](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/secure-your-dependencies/configure-dependabot-alerts).

## Vulnerability scans

CI audits the installed Python environment with `pip-audit --strict` and scans
the exact verified Docker image ID with Trivy. The
[scan workflow](.github/workflows/scan-container-vulnerabilities.yml) applies the
same policy during daily runs. Trivy scans OS and
application packages at every severity, including unfixed vulnerabilities.
High/Critical findings block the container job; any Python advisory blocks the
Python check. Scanner errors, incomplete reports and database download failures
also fail. Both successful jobs remain mandatory for promotion and release.
Only a successfully scanned CI image is retained for manual deployment.

The [daily workflow](.github/workflows/security.yml) runs at **06:23 UTC** and
supports manual runs on `main`. Checkout is pinned to the main commit recorded by
the triggering event. It needs only `contents: read`, never deploys or
promotes, and serializes scheduled/manual executions. It installs main's runtime
and development requirements once and audits that environment. By default it
also rebuilds `main` once, scans that image, and audits its exact installed Python
inventory without another dependency installation or resolution. Missing, extra,
duplicate or mismatched package entries in the audit report fail the check. A rebuild can
resolve different dependency ranges or base-image contents from a deployed artifact.

Optionally set the repository variable `SECURITY_IMAGE_REF` to the current deployed
image's full `registry/repository@sha256:<64 lowercase hex digits>` reference.
Keep it synchronized after deployments. The workflow pulls this immutable image
instead of rebuilding; a missing, invalid or inaccessible configured digest fails
without silently falling back. This configuration does not prove what is running
on a host. No private-registry credentials or cloud access are configured, so the
existing private deployment registry requires separately authorized read-only
authentication before this option can be used. Never put credentials in the variable.

Find package, installed version, severity, advisory ID and available fixes in job
summaries and logs. Download `security-python-*`, `security-container-*` or
`security-daily-*` artifacts for sanitized JSON, retained for 14 days even on
vulnerability/scanner failure. Only allowlisted finding fields are uploaded;
raw scanner reports, image environment/configuration and runtime credentials are
excluded. `pip-audit` does not supply severity, so its findings show `UNKNOWN`
and all block. Logs/summaries show at most 50 findings; JSON includes the full list.

Enable GitHub Actions email/web notifications and **Send notifications for failed
workflows only** in account notification settings. Scheduled notifications go to
the user who last changed the cron expression. The workflow must reach the default
branch through normal promotion before scheduling starts; GitHub may delay runs
and disables public-repository schedules after 60 days of inactivity. Check that
the schedule remains enabled. See [workflow notifications](https://docs.github.com/en/actions/concepts/workflows-and-actions/notifications-for-workflow-runs)
and [schedule limits](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

Scanning is not an independent audit or proof of exploitability. Known unresolved
base-image findings and remaining verification gaps are tracked in the
[security review](docs/SECURITY_REVIEW.md#prioritized-findings-and-improvements).
No vulnerability exclusions are configured; unfixed High/Critical findings keep
promotion blocked until remediation is verified.

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
