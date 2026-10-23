# Contributing

Keep changes focused. Discuss major API/storage/protocol changes first; report
vulnerabilities privately through [SECURITY.md](SECURITY.md). Contributions use
the [MIT license](LICENSE) and [code of conduct](CODE_OF_CONDUCT.md).

## Development setup

Follow [setup](README.md#setup), then use `develop` and Node.js 22 for client checks:

```sh
git switch develop
python -m pip install -r requirements-dev.txt
```

Create `develop` if absent in a fresh checkout; fork contributors should clone
their fork. For local reload use `uvicorn app.main:app --reload`; that listener
includes admin routes. Never commit credentials, invite tokens, databases or exports.

## Checks

Use isolated databases and the commands in [AGENTS.md](AGENTS.md#checks).
[CI](.github/workflows/test.yml) defines the complete Python/JavaScript check list.
For dependency/image changes also run:

```sh
python -m pip check
python -m pip_audit --strict
docker build -t sideword-chat-server:test .
```

Add meaningful regression tests and update affected docs. Keep one reference per
topic; review all docs before preparing a commit and finish edits before final
validation. Update the landing page only when its published information changes.

## Dependency monitoring

[Renovate](.github/renovate.json) maintains one **Dependency Dashboard** issue for
`develop`. All release types require dashboard approval; auto-merge and Renovate
vulnerability PRs are disabled. Leave checkboxes unchecked for manual agent upgrades:
checking one authorizes a Renovate PR.

Default managers cover Python requirements, Actions, Dockerfiles and Compose.
The Compose image is locally built. Keep inline pip constraints aligned with
requirements manually; resolved transitive and OS vulnerabilities need the scans below.

Activation:

1. Promote configuration through `develop` to `main`. Renovate reads configuration
   from default branch `main` but analyzes `develop`; do not merge onboarding defaults directly into `main`.
2. Install the [Renovate App](https://github.com/apps/renovate) for this repository;
   keep Issues enabled. Installing before configuration reaches `main` may open
   a separate **Configure Renovate** onboarding PR; leave its defaults unmerged.
3. Keep **Dependency graph** and **Dependabot alerts** enabled. Disable **Dependabot
   security updates** and any Dependabot rules that create PRs. Removing
   `dependabot.yml` affects neither alerts nor existing PRs.
4. Enable repository Watch → **Security alerts**. Find **Dependency Dashboard** in
   Issues after the next App run; keep it open and check lookup warnings/detected files.

See [Renovate approval behavior](https://docs.renovatebot.com/key-concepts/dashboard/)
and [GitHub security settings](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-security-and-analysis-settings-for-your-repository).

## Security checks

| Check | Scope | Blocks automatic promotion? |
| --- | --- | --- |
| `smoke` | Regression tests, lint, compilation, JavaScript checks and strict installed-environment pip-audit | Yes; any advisory or check failure |
| `container` | Exact verified image; Trivy OS/application vulnerabilities at every severity, including unfixed | Yes; High/Critical findings or scanner/report errors |
| [CodeQL](.github/workflows/codeql.yml) | Python and JavaScript/TypeScript default queries; develop pushes, main-bound PRs, weekly Monday 07:47 UTC, manual runs | No; asynchronous alerts, no verified CodeQL enforcement |
| [Daily security](.github/workflows/security.yml) | Main's dependencies and selected image; daily 06:23 UTC or manual main run | No; independent monitoring, never promotion/deployment |

The Docker build uses Python 3.12 on Alpine/musl, installs binary wheels only,
applies Alpine updates and runs `pip check` before removing
pip/ensurepip and their vendored libraries. CI verifies final runtime imports,
version and tool removal. Compose inherits the Python health probe. Pip-audit
alone does not detect libraries vendored inside another package.

Daily scanning installs the audit environment once. It rebuilds the triggering
main commit once unless repository variable `SECURITY_IMAGE_REF` selects a
credential-free `registry/repository@sha256:<64 lowercase hex digits>`. An invalid
or inaccessible configured digest fails without fallback. Keep it synchronized
with deployments; no private-registry/cloud credentials are configured. A fresh
build can differ from the deployed image. Exact image Python inventory auditing
uses stdlib metadata without reinstalling/resolving dependencies.

Logs/summaries show up to 50 findings; sanitized JSON artifacts contain all
package/version/severity/advisory/fix fields, retained 14 days even on failures:
`security-python-*`, `security-container-*`, `security-daily-*`. Raw image
configuration/credentials are excluded. Pip-audit lacks severity, so every advisory blocks.

Enable GitHub Actions email/web notifications, optionally **failed workflows only**.
Scheduled notifications go to the last cron editor. Scheduled workflows activate
on default branch `main` after promotion; GitHub may delay runs or disable public
schedules after 60 inactive days. See [notifications](https://docs.github.com/en/actions/concepts/workflows-and-actions/notifications-for-workflow-runs).

### GitHub settings

Read-only verification on 2026-10-22 confirmed **Secret Scanning and Push Protection
enabled**. Keep both enabled in **Settings → Advanced Security**; supported-secret
detection is not exhaustive. CodeQL default setup was unconfigured. No settings
were changed by the coding agent.

One minimal advanced CodeQL workflow is supplied because settings changes were
excluded and `develop` needs explicit push coverage. It uses the official numeric
Action release, default queries and no dependency installs/builds. GitHub generally
[recommends default setup](https://docs.github.com/en/code-security/how-tos/find-and-fix-code-vulnerabilities/configure-code-scanning/configure-code-scanning).
If switching, remove the advanced workflow and enable default setup for Python and
JavaScript/TypeScript; ensure develop coverage (it was unprotected at verification).
Do not run both. CodeQL alert uploads alone do not block promotion; any future
ruleset enforcement must also cover direct automated promotion, not just PR merges.

## Pull requests and releases

Make changes on `develop`; use PRs targeting `main`. CI runs on non-main pushes
and main-bound PRs. Its `promote` job fast-forwards `main` only after `smoke` and
`container` pass on the exact current develop commit. Divergence fails; superseded
runs skip promotion. Existing repository rules require those checks and linear
history. Do not push `main` locally or bypass the gates.

For behavior changes, update `app/version.py`, its dated [changelog](CHANGELOG.md)
section and affected docs. Use the version policy in [AGENTS.md](AGENTS.md#ci-versions-and-git).
After promotion, [release.yml](.github/workflows/release.yml) publishes `vX.Y.Z`
with that section as notes; existing tags/releases are preserved. Rerun current CI
to retry publication. Jobs need their existing `GITHUB_TOKEN` write permissions;
no personal token is required. Publication deploys neither the backend nor Pages.
