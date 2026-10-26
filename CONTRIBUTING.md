# Contributing

Keep changes focused. Discuss API/storage/protocol changes first. Report
vulnerabilities through [SECURITY.md](SECURITY.md). Contributions follow the
[MIT license](LICENSE) and [code of conduct](CODE_OF_CONDUCT.md).

## Development setup

Follow [setup](README.md#setup), use Node.js 22 for client checks, then:

```sh
git switch develop
python -m pip install -r requirements-dev.txt
```

Local reload: `uvicorn app.main:app --reload` includes administration routes.
Never commit credentials, invite tokens, databases or exports.

## Checks

Use isolated databases and the [required checks](AGENTS.md#project-checks).
[CI](.github/workflows/test.yml) defines the full check list. For dependency/image
changes also run:

```sh
python -m pip check
python -m pip_audit --strict
docker build -t sideword-chat-server:test .
```

Add regression tests and update affected docs. Follow the version and commit
rules in [AGENTS.md](AGENTS.md#versions-and-releases) and
[Git guidance](AGENTS.md#git-and-authorization).

## Dependency monitoring

[Renovate](.github/renovate.json) monitors `develop` through one **Dependency
Dashboard** issue. All updates require approval; automatic merges and
vulnerability PRs are disabled. Leave checkboxes unchecked when implementing
upgrades manually. Checking one authorizes a Renovate PR.

- Install the [Renovate App](https://github.com/apps/renovate) and keep Issues enabled.
- Configuration must reach default branch `main` through promotion; analysis targets
  `develop`. An initial onboarding PR may appear; do not merge its defaults into `main`.
- Keep **Dependency graph** and **Dependabot alerts** enabled. Disable **Dependabot
  security updates** and rules that create PRs. Existing PRs are unaffected.
- Find **Dependency Dashboard** in Issues and review detection/lookup warnings.

Default managers cover requirements, Actions, Dockerfiles and Compose. Keep inline
pip constraints aligned manually; resolved and OS vulnerabilities need the scans below.
See [dashboard approval behavior](https://docs.renovatebot.com/key-concepts/dashboard/).

## Security checks

| Check | Scope | Promotion policy |
| --- | --- | --- |
| `smoke` | Tests, lint, compilation, JavaScript and pip-audit | Every failure/advisory blocks |
| `container` | Exact image; Trivy OS/application packages, all severities including unfixed | High/Critical findings and scan/report errors block |
| [CodeQL](.github/workflows/codeql.yml) | Python/JavaScript default queries; CI, Monday 07:47 UTC and manual runs | CI blocks High/Critical security findings, error-level findings without security scores, and analysis/report errors |
| [Daily security](.github/workflows/security.yml) | Main's Python dependencies and image; daily 06:23 UTC or manual run | Monitoring only; never promotes or deploys |

CodeQL evaluates current-run SARIF, including existing findings. Scheduled scans
reuse the same policies. The Alpine image installs binary wheels, checks dependencies
and removes pip/ensurepip. Pip-audit alone cannot detect vendored libraries.

Daily scans rebuild `main` unless `SECURITY_IMAGE_REF` selects a credential-free
`registry/repository@sha256:<64 lowercase hex digits>`. Keep it aligned with deployment.
Invalid/inaccessible digests fail without fallback; private-registry credentials
are not configured. A rebuild can differ from the deployed artifact.

Find concise results in job summaries and sanitized JSON in `security-*` artifacts,
retained 14 days even on failures. CodeQL alerts also appear under **Security →
Code scanning**. Reports exclude private image configuration and source snippets.

### GitHub settings

Keep **Secret Scanning**, **Push Protection** and vulnerability notifications enabled.
Keep CodeQL **default setup disabled** to avoid duplicating the advanced workflow.
Enable GitHub Actions failure notifications; scheduled notifications go to the last
cron editor. Schedules run from `main`, may be delayed, and can be disabled after
60 inactive days. See [notification settings](https://docs.github.com/en/actions/concepts/workflows-and-actions/notifications-for-workflow-runs).

## Pull requests and releases

Work on `develop`; PRs target `main`. CI runs on non-main pushes and main-bound PRs.
Actions promotes only the exact current commit after `smoke`, `container` and both
CodeQL analyses pass. Divergence fails; superseded runs skip promotion. Manual
deployment requires gates and promotion from the same run attempt.

CodeQL enforcement is in the workflow; the last verified repository rules require
smoke/container and linear history. Do not push `main` locally or bypass checks.

For behavior changes, update `app/version.py`, its dated [changelog](CHANGELOG.md)
section and affected docs. After promotion, [release.yml](.github/workflows/release.yml)
publishes `vX.Y.Z` using that section. Existing releases remain unchanged; rerun CI
to retry publication. Releases deploy neither the backend nor Pages.
