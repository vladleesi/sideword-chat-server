# Contributing

Keep changes focused; discuss API/storage/protocol changes first. Follow the
[license](LICENSE), [conduct policy](CODE_OF_CONDUCT.md) and [repository rules](AGENTS.md).
Report vulnerabilities privately through [SECURITY.md](SECURITY.md).

## Development setup

Follow [setup](README.md#setup), use Node.js 22, then:

```sh
git switch develop
python -m pip install -r requirements-dev.txt
```

`uvicorn app.main:app --reload` supports local development and includes admin routes.
Never commit credentials, invite tokens, databases or exports.

## Checks

Run the [required checks](AGENTS.md#project-checks) with isolated databases;
[CI](.github/workflows/test.yml) lists all checks. Add regression tests and update
changed contracts/docs. Dependency/image changes also need `python -m pip check`,
`python -m pip_audit --strict` and a Docker build.

## Dependency monitoring

[Renovate](.github/renovate.json) targets `develop`; every update requires Dependency
Dashboard approval and auto-merge is disabled. Leave checkboxes unchecked for manual
upgrades. Keep Issues, the Renovate App, Dependency graph and Dependabot alerts enabled;
disable Dependabot PR automation. Configuration reaches `main` through promotion.

## Security checks

| Check | Blocks on |
| --- | --- |
| `smoke` | Tests, lint, compilation, JS or pip-audit failure/advisory |
| `container` | Trivy High/Critical findings, including unfixed, or scan/report errors |
| [CodeQL](.github/workflows/codeql.yml) | High/Critical security findings, unscored error-level findings or analysis/report errors |

CodeQL also runs weekly; [daily security](.github/workflows/security.yml) monitors
`main` without promotion. Optional `SECURITY_IMAGE_REF` must be a credential-free
image digest; invalid/inaccessible values fail without fallback. Rebuilt images may
not match deployment. Results appear in summaries and 14-day `security-*` artifacts.
Keep Secret Scanning, Push Protection and failure notifications enabled; leave
CodeQL default setup disabled to avoid duplicating the advanced workflow.

## Pull requests and releases

Work on `develop`; PRs target `main`. Actions promotes only the current exact commit
after smoke, container and both CodeQL analyses pass. Never push `main` locally.
Manual deployment requires promotion/gates from the same run attempt.

Behavior changes need `app/version.py`, a dated [changelog](CHANGELOG.md) entry and
affected docs. After promotion, [release.yml](.github/workflows/release.yml) publishes
`vX.Y.Z` from that entry; rerun CI to retry publication. Releases deploy neither backend
nor Pages. Existing releases/history stay unchanged.
