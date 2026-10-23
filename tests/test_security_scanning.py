"""Security gates fail closed using synthetic reports and isolated build commands."""

import json
import os
import re
import subprocess
import sys
import venv
from pathlib import Path

import pytest
import yaml

from scripts import deploy, security_scan

IMAGE = "sha256:" + "a" * 64
SHA = "b" * 40
REFERENCE = "ghcr.io/example/sideword@sha256:" + "c" * 64
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def scan(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "outputs"))
    monkeypatch.setenv("TRIVY_OUTCOME", "success")
    monkeypatch.delenv("SECURITY_IMAGE_REF", raising=False)
    security_scan.directories()
    return tmp_path


def trivy_payload(severity="LOW"):
    return {
        "SchemaVersion": 2,
        "ArtifactType": "container_image",
        "ArtifactName": IMAGE,
        "Metadata": {"ImageConfig": {"Env": ["PASSWORD=synthetic-private"]}},
        "Results": [
            {"Class": "os-pkgs", "Vulnerabilities": [{
                "PkgName": "example-package", "InstalledVersion": "1.0",
                "Severity": severity, "VulnerabilityID": "CVE-2026-0001",
                "FixedVersion": "", "Description": "synthetic-private",
            }]},
            {"Class": "lang-pkgs", "Type": "python-pkg", "Vulnerabilities": None},
        ],
    }


def save_trivy(scan, payload):
    (scan / "security-raw/trivy.json").write_text(json.dumps(payload))


@pytest.mark.parametrize("severity,code", [
    ("CRITICAL", 1), ("HIGH", 1), ("MEDIUM", 0), ("LOW", 0), ("UNKNOWN", 0),
])
def test_all_severities_retained_and_unfixed_high_critical_block(scan, severity, code):
    save_trivy(scan, trivy_payload(severity))
    assert security_scan.report(IMAGE) == code
    result = json.loads((scan / "security-reports/trivy.json").read_text())
    assert result["status"] == ("blocked" if code else "passed")
    assert result["vulnerabilities"][0]["severity"] == severity
    assert result["vulnerabilities"][0]["fixed_version"] == ""
    assert result["image_id"] == IMAGE
    assert "synthetic-private" not in json.dumps(result)
    assert "synthetic-private" not in (scan / "summary.md").read_text()


@pytest.mark.parametrize("failure", ["missing", "truncated", "wrong-image", "no-packages",
                                     "no-python", "unknown-severity", "scanner-error",
                                     "empty-object-findings", "false-findings"])
def test_scanner_errors_or_incomplete_reports_never_pass(scan, monkeypatch, failure):
    payload = trivy_payload()
    if failure == "wrong-image":
        payload["ArtifactName"] = "sideword-ci:stale"
    if failure == "no-packages":
        payload["Results"] = []
    if failure == "no-python":
        payload["Results"].pop()
    if failure == "unknown-severity":
        payload["Results"][0]["Vulnerabilities"][0]["Severity"] = "UNRECOGNIZED"
    if failure in {"empty-object-findings", "false-findings"}:
        payload["Results"][0]["Vulnerabilities"] = (
            {} if failure == "empty-object-findings" else False
        )
    if failure not in {"missing", "truncated"}:
        save_trivy(scan, payload)
    if failure == "truncated":
        (scan / "security-raw/trivy.json").write_text('{"SchemaVersion":')
    if failure == "scanner-error":
        monkeypatch.setenv("TRIVY_OUTCOME", "failure")
    assert security_scan.report(IMAGE) == 1
    result = json.loads((scan / "security-reports/trivy.json").read_text())
    assert result["status"] == "error"


def test_summary_is_bounded_escaped_and_full_findings_remain_in_artifact(scan):
    payload = trivy_payload("HIGH")
    item = payload["Results"][0]["Vulnerabilities"][0]
    item["PkgName"] = "<img>|`example`"
    item["FixedVersion"] = "2.0"
    payload["Results"][0]["Vulnerabilities"] = [item] * 60
    save_trivy(scan, payload)
    assert security_scan.report(IMAGE) == 1
    output = (scan / "summary.md").read_text()
    assert "<img>" not in output
    assert "&lt;img&gt;&#124;&#96;example&#96;" in output
    assert "first 50" in output and "2.0" in output
    assert len(json.loads((scan / "security-reports/trivy.json").read_text())[
        "vulnerabilities"
    ]) == 60


@pytest.mark.parametrize("severity,code", [("LOW", 0), ("HIGH", 1), ("CRITICAL", 1)])
def test_report_cli_returns_blocking_exit_status(scan, severity, code):
    save_trivy(scan, trivy_payload(severity))
    result = subprocess.run([
        sys.executable, "-m", "scripts.security_scan", "report", "--image", IMAGE,
    ], capture_output=True, text=True, env=os.environ.copy(), timeout=30)
    assert result.returncode == code


def python_payload(vulnerable=False):
    return {"dependencies": [{"name": "example", "version": "1.0", "vulns": [
        {"id": "PYSEC-2026-1", "fix_versions": ["2.0"], "description": "synthetic-private"},
    ] if vulnerable else []}]}


@pytest.mark.parametrize("returncode,vulnerable,status", [
    (0, False, "passed"), (1, True, "blocked"), (0, True, "error"),
    (1, False, "error"), (2, True, "error"),
])
def test_python_audit_preserves_exit_status_and_sanitizes_reports(
    scan, monkeypatch, returncode, vulnerable, status,
):
    def execute(command, **kwargs):
        assert "--strict" in command and "--ignore-vuln" not in command
        output = Path(command[command.index("--output") + 1])
        output.write_text(json.dumps(python_payload(vulnerable)))
        return subprocess.CompletedProcess(command, returncode, b"", b"synthetic-private")

    monkeypatch.setattr(security_scan.subprocess, "run", execute)
    assert security_scan.audit("") == int(status != "passed")
    result = (scan / "security-reports/pip-audit.json").read_text()
    assert json.loads(result)["status"] == status
    assert "synthetic-private" not in result


@pytest.mark.parametrize("failure", ["no-report", "skip", "timeout"])
def test_python_scanner_failure_does_not_reuse_old_report(scan, monkeypatch, failure):
    destination = scan / "security-raw/pip-audit.json"
    destination.write_text(json.dumps(python_payload()))

    def execute(command, **kwargs):
        assert not destination.exists()
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 600)
        if failure == "skip":
            destination.write_text(json.dumps({"dependencies": [{
                "name": "example", "skip_reason": "synthetic-private",
            }]}))
        return subprocess.CompletedProcess(command, 1, b"", b"synthetic-private")

    monkeypatch.setattr(security_scan.subprocess, "run", execute)
    assert security_scan.audit("") == 1
    assert json.loads((scan / "security-reports/pip-audit.json").read_text())["status"] == "error"


def test_image_inventory_is_exact_isolated_and_audited_without_reinstallation(scan, monkeypatch):
    def docker(command, **kwargs):
        assert command[:3] == ["docker", "run", "--rm"]
        assert command[command.index("--network") + 1] == "none"
        assert "--read-only" in command and "no-new-privileges" in command
        assert IMAGE in command
        assert command[-3:-1] == [IMAGE, "-c"]
        assert "importlib.metadata.distributions()" in command[-1]
        return json.dumps([{"name": "example", "version": "1.0"},
                           {"name": "transitive-example", "version": "2.0"}])

    def audit(command, **kwargs):
        assert {"--strict", "--no-deps", "--disable-pip"} <= set(command)
        pins = Path(command[command.index("-r") + 1]).read_text()
        assert pins == "example==1.0\ntransitive-example==2.0\n"
        payload = python_payload()
        payload["dependencies"].append({
            "name": "transitive-example", "version": "2.0", "vulns": [],
        })
        Path(command[command.index("--output") + 1]).write_text(json.dumps(payload))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(deploy, "run", docker)
    monkeypatch.setattr(security_scan.subprocess, "run", audit)
    assert security_scan.audit(IMAGE) == 0


def test_inventory_command_works_without_pip(scan, monkeypatch):
    environment = scan / "runtime"
    venv.EnvBuilder(with_pip=False, symlinks=sys.platform != "win32").create(environment)
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    site = Path(subprocess.check_output([
        str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))",
    ], text=True).strip())
    metadata = site / "runtime_example-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: runtime-example\nVersion: 1.0\n")
    captured = []
    run = subprocess.run

    def docker(command, **kwargs):
        assert command[-2] == "-c"
        inventory = run([str(python), "-c", command[-1]], capture_output=True,
                        text=True, check=True).stdout
        captured.extend(json.loads(inventory))
        return inventory

    def audit(command, **kwargs):
        payload = {"dependencies": [{"name": "runtime-example", "version": "1.0", "vulns": []}]}
        Path(command[command.index("--output") + 1]).write_text(json.dumps(payload))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(deploy, "run", docker)
    monkeypatch.setattr(security_scan.subprocess, "run", audit)
    assert security_scan.audit(IMAGE) == 0
    assert captured == [{"name": "runtime-example", "version": "1.0"}]


@pytest.mark.parametrize("failure", ["missing", "extra", "wrong-version", "duplicate"])
def test_image_audit_requires_every_exact_inventory_entry(scan, monkeypatch, failure):
    monkeypatch.setattr(deploy, "run", lambda *args, **kwargs: json.dumps([
        {"name": "example", "version": "1.0"},
        {"name": "transitive-example", "version": "2.0"},
    ]))

    def audit(command, **kwargs):
        payload = python_payload()
        payload["dependencies"].append({
            "name": "transitive-example", "version": "2.0", "vulns": [],
        })
        if failure == "missing":
            payload["dependencies"].pop()
        elif failure == "extra":
            payload["dependencies"].append({"name": "unrequested", "version": "1.0", "vulns": []})
        elif failure == "wrong-version":
            payload["dependencies"][1]["version"] = "1.0"
        else:
            payload["dependencies"].append(payload["dependencies"][0])
        Path(command[command.index("--output") + 1]).write_text(json.dumps(payload))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(security_scan.subprocess, "run", audit)
    assert security_scan.audit(IMAGE) == 1
    report = json.loads((scan / "security-reports/pip-audit-image.json").read_text())
    assert report["status"] == "error"


def test_image_audit_accepts_canonical_python_package_names(scan, monkeypatch):
    monkeypatch.setattr(deploy, "run", lambda *args, **kwargs: json.dumps([
        {"name": "Example_Package", "version": "1.0"},
    ]))

    def audit(command, **kwargs):
        payload = {"dependencies": [{"name": "example-package", "version": "1.0", "vulns": []}]}
        Path(command[command.index("--output") + 1]).write_text(json.dumps(payload))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(security_scan.subprocess, "run", audit)
    assert security_scan.audit(IMAGE) == 0


def test_inventory_option_injection_and_invalid_image_ids_fail_without_audit(scan, monkeypatch):
    monkeypatch.setattr(deploy, "run", lambda *args, **kwargs: json.dumps([
        {"name": "example", "version": "1.0\n--index-url synthetic-private"},
    ]))
    monkeypatch.setattr(security_scan.subprocess, "run", lambda *a, **kw: pytest.fail("No audit"))
    assert security_scan.audit(IMAGE) == 1
    assert security_scan.audit("synthetic-private") == 1
    assert "synthetic-private" not in (scan / "security-reports/pip-audit-image.json").read_text()


@pytest.mark.parametrize("reference", ["example:latest", "https://example/image", "--help",
                                       "https://example/image@sha256:" + "d" * 64,
                                       "user:synthetic-private@example/image@sha256:" + "d" * 64])
def test_registry_selection_rejects_tags_credentials_and_options(scan, monkeypatch, reference):
    monkeypatch.setenv("SECURITY_IMAGE_REF", reference)
    monkeypatch.setattr(deploy, "run", lambda *a, **kw: pytest.fail("No commands allowed"))
    with pytest.raises(ValueError):
        security_scan.prepare()


def test_registry_selection_pulls_once_and_exports_immutable_local_id(scan, monkeypatch, capsys):
    monkeypatch.setenv("SECURITY_IMAGE_REF", REFERENCE)
    commands = []

    def docker(command, **kwargs):
        commands.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Id": IMAGE}])
        return ""

    monkeypatch.setattr(deploy, "run", docker)
    monkeypatch.setattr(deploy, "package_image", lambda *a, **kw: pytest.fail("No rebuild"))
    security_scan.prepare()
    assert commands == [["docker", "pull", REFERENCE], ["docker", "image", "inspect", REFERENCE]]
    assert (scan / "outputs").read_text() == f"image_id={IMAGE}\n"
    assert REFERENCE not in capsys.readouterr().out


@pytest.mark.parametrize("inspected", [[], {}, [{"Id": "latest"}], [None]])
def test_invalid_registry_inspection_fails_without_exporting_an_image_id(
    scan, monkeypatch, inspected,
):
    monkeypatch.setenv("SECURITY_IMAGE_REF", REFERENCE)

    def docker(command, **kwargs):
        if command[:3] == ["docker", "image", "inspect"]:
            return json.dumps(inspected)
        return ""

    monkeypatch.setattr(deploy, "run", docker)
    monkeypatch.setattr(sys, "argv", ["security_scan", "prepare"])
    assert security_scan.main() == 1
    assert not (scan / "outputs").exists()


def test_unavailable_configured_digest_never_falls_back_to_another_image(scan, monkeypatch):
    monkeypatch.setenv("SECURITY_IMAGE_REF", REFERENCE)

    def fail(*args, **kwargs):
        raise deploy.DeploymentError("Deployment command failed: docker pull.")

    monkeypatch.setattr(deploy, "run", fail)
    monkeypatch.setattr(deploy, "package_image", lambda *a, **kw: pytest.fail("No fallback"))
    with pytest.raises(deploy.DeploymentError):
        security_scan.prepare()
    assert not (scan / "outputs").exists()


def test_main_fallback_builds_once_without_packaging_or_deployment(scan, monkeypatch, capsys):
    monkeypatch.setattr(deploy, "run", lambda *a, **kw: SHA)
    calls = []
    monkeypatch.setattr(deploy, "package_image", lambda *a, **kw: calls.append((a, kw)))
    security_scan.prepare()
    assert calls == [((scan / "security-image", SHA), {"retain": False})]
    assert "not the deployed artifact" in capsys.readouterr().out


@pytest.mark.parametrize("retain", [False, True])
def test_ci_exports_the_same_image_id_it_verifies_and_retains(scan, monkeypatch, retain):
    commands = []

    def execute(command, **kwargs):
        commands.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Id": IMAGE}])
        if command[:2] == ["docker", "run"] and command[-2] == "-c":
            return "0.13.0\n"
        if command[:2] == ["docker", "save"]:
            Path(command[command.index("--output") + 1]).write_bytes(b"synthetic image")
        if command[0] == "gzip":
            Path(command[-1] + ".gz").write_bytes(b"synthetic archive")
        return ""

    monkeypatch.setattr(deploy, "run", execute)
    assert deploy.package_image(scan / "checked", SHA, retain) == IMAGE
    assert (scan / "outputs").read_text() == f"image_id={IMAGE}\n"
    containers = [cmd for cmd in commands if cmd[:2] == ["docker", "run"]]
    assert len(containers) == 2 and all(IMAGE in cmd for cmd in containers)
    assert "Build tools remain in the runtime image" in containers[1][-1]
    assert "Admin recovery command missing" in containers[1][-1]
    assert "import fastapi, uvicorn" in containers[1][-1]
    saves = [cmd for cmd in commands if cmd[:2] == ["docker", "save"]]
    assert len(saves) == int(retain)
    if retain:
        assert saves[0][-1] == IMAGE
        assert deploy.artifact_args(scan / "checked", SHA).image_id == IMAGE


def workflow(name):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


def test_failed_image_scan_blocks_retention_promotion_and_release():
    ci = workflow("test.yml")
    steps = ci["jobs"]["container"]["steps"]
    build = next(i for i, step in enumerate(steps) if step.get("id") == "image")
    scan = next(i for i, step in enumerate(steps) if step.get("id") == "trivy")
    gate = next(i for i, step in enumerate(steps)
                if "scripts.security_scan report" in step.get("run", ""))
    upload = next(i for i, step in enumerate(steps)
                  if step.get("with", {}).get("name", "").startswith("checked-image-"))
    assert build < scan < gate < upload
    assert steps[scan]["with"]["image-ref"] == "${{ steps.image.outputs.image_id }}"
    assert "always" not in steps[upload].get("if", "")
    assert ci["jobs"]["promote"]["needs"] == ["smoke", "container"]
    assert ci["jobs"]["release"]["needs"] == ["smoke", "container", "promote"]
    assert not any("continue-on-error" in step for step in steps)


def test_codeql_analyzes_both_languages_without_builds_or_duplicate_gates():
    config = workflow("codeql.yml")
    assert config["on"]["push"]["branches"] == ["develop"]
    assert config["on"]["pull_request"]["branches"] == ["main"]
    assert config["on"]["schedule"] == [{"cron": "47 7 * * 1"}]
    assert config["permissions"] == {"contents": "read"}
    assert list(config["jobs"]) == ["analyze"]
    job = config["jobs"]["analyze"]
    assert job["permissions"] == {"contents": "read", "security-events": "write"}
    assert job["strategy"]["matrix"]["language"] == ["python", "javascript-typescript"]
    assert job["strategy"]["fail-fast"] == "false"
    steps = job["steps"]
    assert [step["uses"] for step in steps] == [
        "actions/checkout@v7.0.1", "github/codeql-action/init@v4.39.1",
        "github/codeql-action/analyze@v4.39.1",
    ]
    assert steps[0]["with"]["persist-credentials"] == "false"
    assert steps[1]["with"] == {"languages": "${{ matrix.language }}", "build-mode": "none"}
    assert steps[2]["with"]["category"] == "/language:${{ matrix.language }}"
    assert "codeql" not in workflow("test.yml")["jobs"]["promote"]["needs"]


def test_workflows_share_severity_policy_and_retain_failure_reports():
    for name, job in [("test.yml", "container"),
                      ("scan-container-vulnerabilities.yml", "security")]:
        steps = workflow(name)["jobs"][job]["steps"]
        trivy = next(step for step in steps if step.get("id") == "trivy")
        assert trivy["uses"] == "aquasecurity/trivy-action@v0.36.0"
        assert trivy["with"]["version"] == "v0.76.1"
        assert set(trivy["with"]["severity"].split(",")) == set(security_scan.SEVERITIES)
        assert trivy["with"]["vuln-type"] == "os,library"
        assert trivy["with"]["scanners"] == "vuln"
        assert trivy["with"]["ignore-unfixed"] == "false"
        assert trivy["with"]["cache"] == "false"
        assert trivy["env"]["TRIVY_IMAGE_SRC"] == "docker"
        assert trivy["env"]["TRIVY_IGNOREFILE"] == "/dev/null"
        gate = next(step for step in steps
                    if "scripts.security_scan report" in step.get("run", ""))
        assert gate["if"] == "always()" and "TRIVY_OUTCOME" in gate["env"]
        upload = next(step for step in steps if step.get("with", {}).get("path", "").endswith(
            "/security-reports/"))
        assert upload["if"] == "always()"
        assert upload["with"]["if-no-files-found"] == "error"
        for step in steps:
            assert "continue-on-error" not in step
            if "uses" in step:
                assert re.fullmatch(r"[\w-]+/[\w-]+@v\d+\.\d+\.\d+", step["uses"])


def test_daily_workflow_is_main_only_read_only_and_has_no_deployment_path():
    daily = workflow("security.yml")
    assert set(daily["on"]) == {"schedule", "workflow_dispatch"}
    assert daily["on"]["schedule"] == [{"cron": "23 6 * * *"}]
    assert daily["permissions"] == {"contents": "read"}
    assert daily["concurrency"] == {"group": "daily-security-scan", "cancel-in-progress": "false"}
    job = daily["jobs"]["security"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert job["uses"] == "./.github/workflows/scan-container-vulnerabilities.yml"
    scan = workflow("scan-container-vulnerabilities.yml")
    assert set(scan["on"]) == {"workflow_call"}
    assert scan["permissions"] == {"contents": "read"}
    job = scan["jobs"]["security"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    checkout = job["steps"][0]
    assert checkout["with"] == {"ref": "${{ github.sha }}", "persist-credentials": "false"}
    image_audit = next(step for step in job["steps"]
                       if "scripts.security_scan audit --image" in step.get("run", ""))
    assert "steps.image.outcome == 'success'" in image_audit["if"]
    assert image_audit["env"]["SECURITY_IMAGE_ID"] == "${{ steps.image.outputs.image_id }}"
    source = "\n".join((ROOT / ".github/workflows" / name).read_text() for name in [
        "security.yml", "scan-container-vulnerabilities.yml",
    ])
    assert "secrets." not in source and "id-token" not in source
    assert "scripts.deploy" not in source and "git push" not in source
    assert "promote" not in daily["jobs"] and "release" not in daily["jobs"]
