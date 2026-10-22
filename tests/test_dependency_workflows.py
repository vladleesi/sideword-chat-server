"""Compatibility of dependency updates with Pages and release/deployment gates."""

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_pages_upload_preserves_hidden_site_assets():
    workflow = yaml.load(
        (ROOT / ".github/workflows/pages.yml").read_text(), Loader=yaml.BaseLoader,
    )
    assert set(workflow["on"]) == {"workflow_dispatch"}
    job = workflow["jobs"]["deploy"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    upload = next(step for step in job["steps"] if step.get("uses", "").startswith(
        "actions/upload-pages-artifact@",
    ))
    assert upload["with"]["path"] == "_site"
    assert upload["with"]["include-hidden-files"] == "true"
    assert (ROOT / "docs/.nojekyll").exists()
    assert job["permissions"]["pages"] == "write"
    assert job["permissions"]["id-token"] == "write"


def test_workflows_use_numeric_action_releases_on_hosted_runners():
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        for job in workflow["jobs"].values():
            if "uses" in job:
                continue  # Local reusable release workflow.
            assert job["runs-on"] == "ubuntu-latest"
            for step in job.get("steps", []):
                if "uses" in step:
                    assert re.fullmatch(r"[\w-]+/[\w-]+@v\d+\.\d+\.\d+", step["uses"])


def test_dependency_monitoring_requires_approval_without_automatic_security_prs():
    config = json.loads((ROOT / ".github/renovate.json").read_text())
    assert config["extends"] == ["config:recommended"]
    assert config["baseBranchPatterns"] == ["develop"]
    assert config["dependencyDashboard"] is True
    assert config["dependencyDashboardApproval"] is True
    assert config["automerge"] is False
    assert config["vulnerabilityAlerts"]["enabled"] is False
    update_filters = {"enabledManagers", "ignoreDeps", "packageRules", "major", "minor", "patch"}
    assert not update_filters & config.keys()
    assert not (ROOT / ".github/dependabot.yml").exists()
