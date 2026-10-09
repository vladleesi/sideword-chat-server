"""Deployment guards use fake commands and isolated databases, never a live VM."""

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from scripts import deploy

SHA = "a" * 40
IMAGE = "sha256:" + "b" * 64
REFERENCE = "region-docker.pkg.dev/example-project/backend/sideword@sha256:" + "d" * 64
PREVIOUS = "sha256:" + "c" * 64


@pytest.fixture
def candidate(tmp_path):
    archive = tmp_path / "image.tar.gz"
    archive.write_bytes(b"synthetic checked archive")
    compose = tmp_path / "compose.yaml"
    compose.write_text("synthetic private configuration")
    return argparse.Namespace(
        archive=archive,
        image_ref=REFERENCE,
        compose=compose,
        sha=SHA,
        image_id=IMAGE,
        checksum=hashlib.sha256(archive.read_bytes()).hexdigest(),
        version="0.6.0",
    )


@pytest.fixture
def vm_config(monkeypatch):
    values = {
        "GCP_PROJECT_ID": "example-project",
        "GCP_ZONE": "region-zone-a",
        "GCP_INSTANCE": "example-vm",
        "GCP_SERVICE_ACCOUNT": "deploy@example-project.iam.gserviceaccount.com",
        "GCP_WORKLOAD_IDENTITY_PROVIDER": (
            "projects/123/locations/global/workloadIdentityPools/github/providers/repo"
        ),
        "GCP_ARTIFACT_IMAGE": REFERENCE.split("@")[0],
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


@pytest.fixture
def host(monkeypatch, candidate, tmp_path):
    if sys.platform == "win32":
        pytest.skip("The host deployment target requires Unix file locking.")
    database = tmp_path / "data" / "sideword.sqlite3"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE messages (body TEXT)")
        connection.execute("INSERT INTO messages VALUES ('synthetic retained message')")
    commands = []
    state = {
        "active": False,
        "label": SHA,
        "version": candidate.version,
        "database": database,
        "image_id": IMAGE,
        "references": [REFERENCE],
    }
    previous = {
        "Image": PREVIOUS,
        "Config": {
            "Env": ["SIDEWORD_DB_PATH=/data/sideword.sqlite3", "PRIVATE_VALUE=synthetic-private"]
        },
        "Mounts": [{"Destination": "/data", "Source": str(database.parent), "Type": "bind"}],
    }

    def fake_run(command, **kwargs):
        commands.append(command)
        if "config" in command:
            return json.dumps({"services": {"sideword": {"image": "sideword:private"}}})
        if "ps" in command:
            return "container\n"
        if command[:3] == ["docker", "image", "inspect"]:
            if command[3] == "sideword:private":
                return json.dumps([{"Id": PREVIOUS}])
            return json.dumps(
                [
                    {
                        "Id": state["image_id"],
                        "RepoDigests": state["references"],
                        "Config": {"Labels": {"org.opencontainers.image.revision": state["label"]}},
                    }
                ]
            )
        if command[:2] == ["docker", "inspect"]:
            if not state["active"]:
                return json.dumps([previous])
            return json.dumps(
                [{"Image": state["image_id"], "State": {"Health": {"Status": "healthy"}}}]
            )
        if command[:2] == ["docker", "run"]:
            return state["version"]
        if "up" in command:
            state["active"] = True
        if "stop" in command:
            state["active"] = False
        return ""

    monkeypatch.setattr(deploy, "run", fake_run)
    monkeypatch.setattr(deploy, "pull_image", lambda args: None)
    monkeypatch.setattr(deploy, "health_matches", lambda version: True)
    monkeypatch.setattr(deploy, "verify_private_routes", lambda: None)
    previous_umask = os.umask(0o077)
    try:
        yield commands, state, previous
    finally:
        os.umask(previous_umask)


@pytest.mark.parametrize(
    "field,value",
    [
        ("sha", "main"),
        ("sha", SHA + "\n"),
        ("image_id", "latest"),
        ("checksum", "unknown"),
        ("version", "0.06.0"),
        ("version", "0.6.0; command"),
    ],
)
def test_invalid_metadata_is_rejected_before_commands(candidate, monkeypatch, field, value):
    setattr(candidate, field, value)
    monkeypatch.setattr(deploy, "run", lambda *args, **kwargs: pytest.fail("No commands allowed"))
    with pytest.raises(deploy.DeploymentError, match="metadata"):
        deploy.verify_archive(candidate)


def test_archive_tampering_is_rejected(candidate):
    candidate.archive.write_bytes(b"replaced archive")
    with pytest.raises(deploy.DeploymentError, match="checksum"):
        deploy.verify_archive(candidate)


def test_archive_symlink_is_rejected(candidate, tmp_path):
    linked = tmp_path / "linked.tar.gz"
    try:
        linked.symlink_to(candidate.archive)
    except OSError:
        pytest.skip("Symlinks are unavailable.")
    candidate.archive = linked
    with pytest.raises(deploy.DeploymentError, match="unsafe"):
        deploy.verify_archive(candidate)


def test_missing_configuration_names_only_the_missing_variable(vm_config, monkeypatch):
    monkeypatch.delenv("GCP_SERVICE_ACCOUNT")
    with pytest.raises(deploy.DeploymentError, match="GCP_SERVICE_ACCOUNT") as failure:
        deploy.validate_config()
    assert "example-project" not in str(failure.value)


@pytest.mark.parametrize(
    "name,value",
    [
        ("GCP_INSTANCE", "--another-instance"),
        ("GCP_ZONE", "bad\nzone"),
        ("GCP_ARTIFACT_IMAGE", "https://invalid/image"),
        ("GCP_WORKLOAD_IDENTITY_PROVIDER", "another/provider"),
    ],
)
def test_invalid_vm_configuration_is_rejected(vm_config, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(deploy.DeploymentError, match=name):
        deploy.validate_config()


def test_outdated_commit_never_contacts_vm(candidate, vm_config, monkeypatch):
    monkeypatch.setattr(deploy, "current_commit", lambda sha: False)
    monkeypatch.setattr(
        deploy, "run", lambda *args, **kwargs: pytest.fail("No cloud commands allowed")
    )
    deploy.deploy_vm(candidate)


def test_vm_success_requires_fixed_marker(candidate, vm_config, monkeypatch, capsys):
    monkeypatch.setattr(deploy, "current_commit", lambda sha: True)
    monkeypatch.setattr(deploy, "run", lambda *args, **kwargs: "synthetic-private")
    with pytest.raises(deploy.DeploymentError, match="confirm"):
        deploy.deploy_vm(candidate)
    assert "synthetic-private" not in capsys.readouterr().out


def test_vm_activation_quotes_configuration_and_requires_success_marker(
    candidate, vm_config, monkeypatch
):
    monkeypatch.setattr(deploy, "current_commit", lambda sha: True)
    commands = []

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        return "Checked backend deployment verified.\n" if "input" in kwargs else ""

    monkeypatch.setattr(deploy, "run", fake_run)
    deploy.deploy_vm(candidate)
    activation = next((command, kwargs) for command, kwargs in commands if "input" in kwargs)
    assert "--compose" not in activation[0][-1]
    assert REFERENCE in activation[0][-1]
    assert len(commands) == 1
    assert not any("scp" in command for command, _ in commands)
    assert activation[1]["input"] == Path(deploy.__file__).read_bytes()
    assert "--tunnel-through-iap" in activation[0]
    assert "--ssh-flag=-T" in activation[0]


def test_database_requires_persistent_mount_and_rejects_traversal(host):
    _, _, container = host
    assert deploy.database_path(container).name == "sideword.sqlite3"
    container["Config"]["Env"] = ["SIDEWORD_DB_PATH=/data/../private.sqlite3"]
    with pytest.raises(deploy.DeploymentError, match="persistent"):
        deploy.database_path(container)


def test_host_backup_precedes_activation_and_preserves_configuration(candidate, host, capsys):
    commands, state, _ = host
    original = candidate.compose.read_bytes()
    deploy.deploy_host(candidate)
    backups = list((candidate.compose.parent / "deployment-backups").glob("*.sqlite3"))
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("SELECT body FROM messages").fetchone() == (
            "synthetic retained message",
        )
    assert candidate.compose.read_bytes() == original
    assert state["active"]
    assert commands.index(
        next(command for command in commands if "stop" in command)
    ) < commands.index(next(command for command in commands if "up" in command))
    assert all(
        "--no-deps" in command and "--no-build" in command
        for command in commands
        if "up" in command
    )
    assert not any(
        "down" in command or "prune" in command or "build" in command for command in commands
    )
    assert capsys.readouterr().out.endswith("Checked backend deployment verified.\n")


def test_containerd_manifest_id_is_used_for_probe_activation_and_health(candidate, host):
    commands, state, _ = host
    manifest_id = REFERENCE.split("@")[1]
    state["image_id"] = manifest_id
    deploy.deploy_host(candidate)
    probe = next(command for command in commands if command[:2] == ["docker", "run"])
    assert manifest_id in probe and IMAGE not in probe
    assert ["docker", "image", "tag", manifest_id, "sideword:private"] in commands
    assert state["active"]


@pytest.mark.parametrize("field,value", [("image_id", PREVIOUS), ("references", [])])
def test_unbound_local_image_never_stops_backend(candidate, host, field, value):
    commands, state, _ = host
    state[field] = value
    with pytest.raises(deploy.DeploymentError, match="pulled image"):
        deploy.deploy_host(candidate)
    assert not any("stop" in command or "up" in command for command in commands)


@pytest.mark.parametrize("field,value", [("label", "d" * 40), ("version", "0.5.0")])
def test_candidate_mismatch_never_stops_backend(candidate, host, field, value):
    commands, state, _ = host
    state[field] = value
    with pytest.raises(deploy.DeploymentError):
        deploy.deploy_host(candidate)
    assert not any("stop" in command or "up" in command for command in commands)


def test_backup_failure_restarts_previous_image_without_starting_new_code(
    candidate, host, monkeypatch
):
    commands, _, _ = host
    monkeypatch.setattr(
        deploy, "backup_database", lambda *args: (_ for _ in ()).throw(OSError("synthetic-private"))
    )
    with pytest.raises(OSError):
        deploy.deploy_host(candidate)
    assert ["docker", "image", "tag", PREVIOUS, "sideword:private"] in commands
    assert ["docker", "image", "tag", IMAGE, "sideword:private"] not in commands
    assert any("up" in command for command in commands)


def test_failed_verification_preserves_new_data_backup_and_previous_image(
    candidate, host, monkeypatch
):
    commands, state, _ = host
    original_run = deploy.run

    def started(command, **kwargs):
        result = original_run(command, **kwargs)
        if "up" in command:
            with sqlite3.connect(state["database"]) as connection:
                connection.execute(
                    "INSERT INTO messages VALUES ('synthetic message after startup')"
                )
        return result

    monkeypatch.setattr(deploy, "run", started)
    times = iter([0, 0, 0, 181])
    monkeypatch.setattr(deploy.time, "monotonic", lambda: next(times))
    with pytest.raises(deploy.DeploymentError, match="verification failed"):
        deploy.deploy_host(candidate)
    assert not state["active"]
    assert sum("stop" in command for command in commands) == 2
    assert list((candidate.compose.parent / "deployment-backups").glob("*.sqlite3"))
    assert any(
        command[:4] == ["docker", "image", "tag", PREVIOUS]
        and command[4].startswith("sideword-rollback:")
        for command in commands
    )
    assert ["docker", "image", "tag", PREVIOUS, "sideword:private"] not in commands
    with sqlite3.connect(state["database"]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone() == (2,)
    backup = next((candidate.compose.parent / "deployment-backups").glob("*.sqlite3"))
    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone() == (1,)


def test_failed_private_route_check_stops_candidate_without_downgrade(candidate, host, monkeypatch):
    commands, state, _ = host

    def unblocked():
        raise deploy.DeploymentError("A private route was not blocked on the shared listener.")

    monkeypatch.setattr(deploy, "verify_private_routes", unblocked)
    with pytest.raises(deploy.DeploymentError, match="private route"):
        deploy.deploy_host(candidate)
    assert not state["active"]
    assert sum("stop" in command for command in commands) == 2
    assert ["docker", "image", "tag", PREVIOUS, "sideword:private"] not in commands
    assert list((candidate.compose.parent / "deployment-backups").glob("*.sqlite3"))


@pytest.mark.parametrize(
    "main,develop,expected",
    [
        (SHA, SHA, True),
        ("d" * 40, SHA, False),
        (SHA, "d" * 40, False),
    ],
)
def test_current_commit_requires_both_branch_refs(monkeypatch, main, develop, expected):
    values = {"origin/main": main, "origin/develop": develop}

    def fake_run(command, **kwargs):
        return values[command[-1]] if "rev-parse" in command else ""

    monkeypatch.setattr(deploy, "run", fake_run)
    assert deploy.current_commit(SHA) == expected


def test_database_outside_data_mount_is_rejected(host):
    _, _, container = host
    container["Config"]["Env"] = ["SIDEWORD_DB_PATH=/tmp/transient.sqlite3"]
    with pytest.raises(deploy.DeploymentError, match="persistent"):
        deploy.database_path(container)


def test_host_mutable_reference_never_contacts_docker(candidate, monkeypatch):
    candidate.image_ref = REFERENCE.split("@")[0] + ":latest"
    monkeypatch.setattr(deploy, "run", lambda *args, **kwargs: pytest.fail("No Docker commands"))
    with pytest.raises(deploy.DeploymentError, match="immutable"):
        deploy.deploy_host(candidate)


def test_host_concurrent_deployment_is_rejected_before_commands(candidate, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("The host deployment target requires Unix file locking.")
    import fcntl

    with (candidate.compose.parent / "ci-deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(
            deploy, "run", lambda *args, **kwargs: pytest.fail("No Docker commands allowed")
        )
        with pytest.raises(deploy.DeploymentError, match="already running"):
            deploy.deploy_host(candidate)


def test_health_requires_matching_versions_on_both_listeners(monkeypatch):
    class Response:
        status = 200

        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def response(url, **kwargs):
        version = "0.6.0" if ":8000" in url else "0.5.0"
        return Response(json.dumps({"status": "ok", "version": version}))

    monkeypatch.setattr(deploy.urllib.request, "urlopen", response)
    assert not deploy.health_matches("0.6.0")


@pytest.mark.parametrize("status", [200, 302, 401, 429, 500])
def test_private_route_verification_rejects_unblocked_or_unverified_statuses(monkeypatch, status):
    @contextmanager
    def response(request, **kwargs):
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "synthetic-private", {}, None)
        yield object()

    monkeypatch.setattr(deploy.urllib.request, "urlopen", response)
    with pytest.raises(deploy.DeploymentError, match="private route"):
        deploy.verify_private_routes()


def test_private_route_verification_includes_http_and_websocket_upgrades(monkeypatch):
    requests = []

    def blocked(request, **kwargs):
        requests.append(request)
        raise urllib.error.HTTPError(request.full_url, 404, "blocked", {}, None)

    monkeypatch.setattr(deploy.urllib.request, "urlopen", blocked)
    deploy.verify_private_routes()
    assert len(requests) == 10
    assert sum(request.get_header("Upgrade") == "websocket" for request in requests) == 5


def test_command_failures_do_not_disclose_private_output(monkeypatch):
    monkeypatch.setattr(
        deploy.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            1,
            b"synthetic-private",
            b"synthetic-private",
        ),
    )
    with pytest.raises(deploy.DeploymentError) as failure:
        deploy.run(["docker", "inspect", "synthetic-private"])
    assert str(failure.value) == "Deployment command failed: docker."


@pytest.mark.parametrize(
    "parts",
    [
        ["load"],
        ["login"],
        ["push"],
        ["pull"],
        ["image", "inspect"],
        ["image", "tag"],
        ["manifest", "inspect"],
    ],
)
def test_command_failures_name_only_known_docker_operations(monkeypatch, parts):
    monkeypatch.setattr(
        deploy.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, b"synthetic-private", b"synthetic-private"
        ),
    )
    with pytest.raises(deploy.DeploymentError) as failure:
        deploy.run(["docker", *parts, "synthetic-private"])
    assert str(failure.value) == f"Deployment command failed: docker {' '.join(parts)}."


def test_registry_upload_denial_does_not_disclose_private_output(monkeypatch):
    monkeypatch.setattr(
        deploy.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            1,
            b"synthetic-private",
            b'Permission "artifactregistry.repositories.uploadArtifacts" '
            b"denied on synthetic-private",
        ),
    )
    with pytest.raises(deploy.DeploymentError) as failure:
        deploy.run(["docker", "push", "synthetic-private"])
    assert str(failure.value) == "Registry upload permission was denied for the deployment account."


def test_unknown_docker_operation_does_not_disclose_arguments(monkeypatch):
    monkeypatch.setattr(
        deploy.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, b"", b"synthetic-private"),
    )
    with pytest.raises(deploy.DeploymentError) as failure:
        deploy.run(["docker", "synthetic-private"])
    assert str(failure.value) == "Deployment command failed: docker."


@pytest.mark.parametrize(
    "remote_message",
    [
        "The pulled image does not match the checked commit.",
        "The registry digest does not identify the CI image.",
        "Updated backend verification failed; "
        "keep the backup and previous image for recovery.",
        "synthetic-private",
    ],
)
def test_remote_failure_forwards_only_approved_progress_and_errors(
    monkeypatch, capsys, remote_message
):
    stdout = (
        "Stage pull: started.\n"
        "synthetic-private\n"
        "Stage pull: finished after synthetic-private.s.\n"
        "Stage pull: finished after 2.3s.\n"
        f"{remote_message}\n"
    ).encode()
    monkeypatch.setattr(
        deploy.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, stdout, b"synthetic-private"
        ),
    )
    with pytest.raises(deploy.DeploymentError) as failure:
        deploy.run(["gcloud", "compute", "ssh", "synthetic-private"])
    expected = (
        "Deployment command failed: gcloud."
        if remote_message == "synthetic-private"
        else remote_message
    )
    assert str(failure.value) == expected
    assert capsys.readouterr().out == (
        "Stage pull: started.\nStage pull: finished after 2.3s.\n"
    )


def test_remote_timeout_does_not_disclose_partial_output(monkeypatch, capsys):
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(
            command, 1, output=b"synthetic-private", stderr=b"synthetic-private"
        )

    monkeypatch.setattr(deploy.subprocess, "run", timeout)
    with pytest.raises(deploy.DeploymentError) as failure:
        deploy.run(["gcloud", "compute", "ssh", "synthetic-private"])
    assert str(failure.value) == "Deployment command failed: gcloud."
    assert capsys.readouterr().out == ""


@pytest.fixture
def checked_ci(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/project")
    checked_run = {
        "id": 123,
        "run_attempt": 2,
        "head_sha": SHA,
        "head_branch": "develop",
        "event": "push",
        "head_repository": {"full_name": "example/project"},
    }
    jobs = [
        {"name": name, "status": "completed", "conclusion": "success"}
        for name in ("smoke", "container")
    ]
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if any("/actions/workflows/test.yml/runs" in part for part in command):
            return json.dumps({"workflow_runs": [checked_run]})
        return json.dumps({"jobs": jobs})

    monkeypatch.setattr(deploy, "run", fake_run)
    return checked_run, jobs, commands


def test_manual_deployment_requires_both_checks_on_the_exact_develop_commit(checked_ci, capsys):
    _, _, commands = checked_ci
    deploy.verify_ci(SHA)
    assert commands[0][4] == "repos/example/project/actions/workflows/test.yml/runs"
    assert f"head_sha={SHA}" in commands[0]
    assert "branch=develop" in commands[0] and "event=push" in commands[0]
    assert commands[1][-1] == "repos/example/project/actions/runs/123/attempts/2/jobs?per_page=100"
    assert capsys.readouterr().out == "Checked commit passed both CI jobs.\n"


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_sha", "d" * 40),
        ("head_branch", "main"),
        ("event", "pull_request"),
        ("head_repository", {"full_name": "untrusted/fork"}),
        ("head_repository", None),
        ("id", "123"),
        ("id", True),
        ("run_attempt", None),
        ("run_attempt", True),
        ("run_attempt", 0),
        ("run_attempt", "2"),
    ],
)
def test_unrelated_or_untrusted_ci_cannot_authorize_deployment(checked_ci, field, value):
    checked_run, _, commands = checked_ci
    checked_run[field] = value
    with pytest.raises(deploy.DeploymentError, match="Both CI jobs"):
        deploy.verify_ci(SHA)
    assert len(commands) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("conclusion", "failure"),
        ("conclusion", "skipped"),
        ("conclusion", "cancelled"),
        ("status", "in_progress"),
        ("name", "unrelated-job"),
    ],
)
def test_incomplete_failed_or_missing_container_check_blocks_deployment(checked_ci, field, value):
    _, jobs, _ = checked_ci
    jobs[1][field] = value
    with pytest.raises(deploy.DeploymentError, match="Both CI jobs"):
        deploy.verify_ci(SHA)


def test_missing_ci_runs_cannot_authorize_deployment(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/project")
    monkeypatch.setattr(deploy, "run", lambda *args, **kwargs: '{"workflow_runs": []}')
    with pytest.raises(deploy.DeploymentError, match="Both CI jobs"):
        deploy.verify_ci(SHA)


@pytest.mark.parametrize(
    "sha,repository",
    [
        ("main", "example/project"),
        (SHA, ""),
        (SHA, "example/project/other"),
    ],
)
def test_invalid_ci_selection_never_queries_github(monkeypatch, sha, repository):
    monkeypatch.setenv("GITHUB_REPOSITORY", repository)
    monkeypatch.setattr(deploy, "run", lambda *args, **kwargs: pytest.fail("No commands allowed"))
    with pytest.raises(deploy.DeploymentError):
        deploy.verify_ci(sha)


def test_deployment_workflow_is_manual_and_independent_of_ci_and_release():
    workflows = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    workflow = yaml.load((workflows / "deploy.yml").read_text(), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {
        "contents": "read",
        "actions": "read",
        "id-token": "write",
    }
    job = workflow["jobs"]["deploy"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert "environment" not in job
    assert job["concurrency"]["cancel-in-progress"] == "false"
    ci = yaml.load((workflows / "test.yml").read_text(), Loader=yaml.BaseLoader)
    assert job["concurrency"]["group"] == ci["jobs"]["promote"]["concurrency"]["group"]
    assert {"smoke", "container"} <= ci["jobs"].keys()
    steps = job["steps"]
    gate = next(i for i, step in enumerate(steps) if "scripts.deploy ci " in step.get("run", ""))
    download = next(
        i
        for i, step in enumerate(steps)
        if step.get("uses", "").startswith("actions/download-artifact@")
    )
    auth = next(
        i
        for i, step in enumerate(steps)
        if step.get("uses", "").startswith("google-github-actions/auth@")
    )
    assert gate < download < auth
    assert not any("docker build" in step.get("run", "") for step in steps)
    assert steps[download]["with"]["run-id"] == "${{ steps.ci.outputs.run_id }}"
    assert steps[download]["with"]["name"] == "checked-image-${{ steps.ci.outputs.run_attempt }}"
    upload = next(
        step
        for step in ci["jobs"]["container"]["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact@")
    )
    assert upload["with"]["name"] == "checked-image-${{ github.run_attempt }}"
    assert "overwrite" not in upload["with"]
    for checked_workflow in [workflow, ci]:
        for checked_job in checked_workflow["jobs"].values():
            for step in checked_job.get("steps", []):
                if "uses" in step:
                    assert step["uses"].split("@")[1] in {"v3", "v4", "v5"}
    for name in job["env"]:
        if name.startswith("GCP_"):
            assert job["env"][name] == "${{ secrets." + name + " }}"
    assert "vars.GCP" not in (workflows / "deploy.yml").read_text()
    assert steps[gate]["if"] == "steps.current.outputs.deploy == 'true'"
    assert not any(step.get("continue-on-error") == "true" for step in steps)
    for path in workflows.glob("*.yml"):
        if path.name != "deploy.yml":
            assert "./.github/workflows/deploy.yml" not in path.read_text()


def test_ci_selection_exports_only_verified_run_id(checked_ci, monkeypatch, tmp_path):
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert deploy.verify_ci(SHA) == 123
    assert output.read_text() == "run_id=123\nrun_attempt=2\n"


def test_registry_mismatch_never_contacts_vm(candidate, vm_config, monkeypatch):
    candidate.image_ref = REFERENCE.replace("/backend/", "/other/")
    monkeypatch.setattr(deploy, "run", lambda *args, **kwargs: pytest.fail("No commands"))
    with pytest.raises(deploy.DeploymentError, match="outside"):
        deploy.deploy_vm(candidate)


def test_vm_never_relays_unexpected_output(candidate, vm_config, monkeypatch, capsys):
    monkeypatch.setattr(deploy, "current_commit", lambda sha: True)
    monkeypatch.setattr(
        deploy,
        "run",
        lambda *args, **kwargs: "synthetic-private\nChecked backend deployment verified.\n",
    )
    with pytest.raises(deploy.DeploymentError, match="unexpected"):
        deploy.deploy_vm(candidate)
    assert "synthetic-private" not in capsys.readouterr().out


def test_pull_failure_never_stops_backend(candidate, host, monkeypatch):
    commands, _, _ = host

    def failed(args):
        raise deploy.DeploymentError("Deployment command failed: docker.")

    monkeypatch.setattr(deploy, "pull_image", failed)
    with pytest.raises(deploy.DeploymentError):
        deploy.deploy_host(candidate)
    assert not any("stop" in command or "up" in command for command in commands)
    assert not list(candidate.compose.parent.glob("deployment-backups/*.sqlite3"))


@pytest.mark.parametrize("wrong_config", [False, True])
def test_vm_pull_uses_metadata_and_disposable_credentials(
    candidate, monkeypatch, capsys, wrong_config
):
    commands = []
    requests = []

    class Opener:
        @contextmanager
        def open(self, request, **kwargs):
            import io

            requests.append(request)
            yield io.StringIO('{"access_token": "synthetic-token"}')

    monkeypatch.setattr(deploy.urllib.request, "build_opener", lambda *args: Opener())
    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        if command[:3] == ["docker", "manifest", "inspect"]:
            return json.dumps({"config": {"digest": PREVIOUS if wrong_config else IMAGE}})
        return "synthetic-private"

    monkeypatch.setattr(deploy, "run", fake_run)
    if wrong_config:
        with pytest.raises(deploy.DeploymentError, match="registry digest"):
            deploy.pull_image(candidate)
    else:
        deploy.pull_image(candidate)
    assert requests[0].get_header("Metadata-flavor") == "Google"
    login, pull, manifest = commands
    assert login[1]["input"] == b"synthetic-token"
    assert "--password-stdin" in login[0]
    assert pull[0] == ["docker", "pull", REFERENCE]
    assert login[1]["env"]["DOCKER_CONFIG"] == pull[1]["env"]["DOCKER_CONFIG"]
    assert manifest[0] == ["docker", "manifest", "inspect", REFERENCE]
    assert manifest[1]["env"]["DOCKER_CONFIG"] == pull[1]["env"]["DOCKER_CONFIG"]
    assert not Path(login[1]["env"]["DOCKER_CONFIG"]).exists()
    output = capsys.readouterr().out
    assert "synthetic-token" not in output and "synthetic-private" not in output


@pytest.mark.parametrize("wrong_manifest", [False, True])
def test_publication_binds_digest_to_ci_image_and_cleans_credentials(
    candidate, vm_config, monkeypatch, tmp_path, capsys, wrong_manifest
):
    directory = candidate.archive.parent
    (directory / "image.json").write_text(
        json.dumps(
            {
                "image_id": IMAGE,
                "checksum": candidate.checksum,
                "version": candidate.version,
            }
        )
    )
    output = tmp_path / "github-env"
    monkeypatch.setenv("GITHUB_ENV", str(output))
    monkeypatch.setenv("GCP_ACCESS_TOKEN", "synthetic-token")
    commands = []

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        if command[:3] == ["docker", "image", "inspect"]:
            return json.dumps(
                [
                    {
                        "Id": IMAGE,
                        "RepoDigests": [REFERENCE],
                        "Config": {
                            "Labels": {"org.opencontainers.image.revision": SHA},
                        },
                    }
                ]
            )
        if command[:3] == ["docker", "manifest", "inspect"]:
            return json.dumps({"config": {"digest": PREVIOUS if wrong_manifest else IMAGE}})
        return "synthetic-private"

    monkeypatch.setattr(deploy, "run", fake_run)
    args = argparse.Namespace(directory=directory, sha=SHA)
    if wrong_manifest:
        with pytest.raises(deploy.DeploymentError, match="registry digest"):
            deploy.publish_image(args)
        assert not output.exists()
    else:
        deploy.publish_image(args)
        assert f"IMAGE_REF={REFERENCE}" in output.read_text()
    assert not any(command[0] == "gcloud" for command, _ in commands)
    login = next(kwargs for command, kwargs in commands if "login" in command)
    assert login["input"] == b"synthetic-token"
    assert not Path(login["env"]["DOCKER_CONFIG"]).exists()
    logs = capsys.readouterr().out
    assert "synthetic-token" not in logs and "synthetic-private" not in logs


def test_retention_policy_preserves_two_recent_versions():
    root = Path(__file__).resolve().parents[1]
    policies = json.loads((root / "scripts/artifact-cleanup.json").read_text())
    assert policies[0]["condition"] == {"tagState": "any", "olderThan": "86400s"}
    assert policies[1]["mostRecentVersions"]["keepCount"] == 2
    assert policies[1]["action"]["type"] == "Keep"


@pytest.mark.parametrize(
    "file_uid,parent_uid,file_mode,parent_mode,allowed,accepted",
    [
        (0, 0, 0o600, 0o700, True, True),
        (0, 1000, 0o600, 0o755, True, False),
        (1000, 0, 0o600, 0o700, True, False),
        (0, 0, 0o644, 0o700, True, False),
        (0, 0, 0o600, 0o777, True, False),
        (0, 0, 0o600, 0o700, False, False),
    ],
)
def test_host_allowlist_requires_private_root_owned_file_and_directory(
    candidate,
    tmp_path,
    monkeypatch,
    file_uid,
    parent_uid,
    file_mode,
    parent_mode,
    allowed,
    accepted,
):
    directory = tmp_path / "host-config"
    directory.mkdir(mode=parent_mode)
    directory.chmod(parent_mode)
    config = directory / "deploy.json"
    config.write_text(
        json.dumps(
            {
                "image": REFERENCE.split("@")[0] if allowed else "untrusted/image",
                "compose": str(candidate.compose),
            }
        )
    )
    config.chmod(file_mode)
    original_path = Path
    original_stat = Path.stat

    def fake_stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path in {config, directory}:
            values = list(result)
            values[4] = file_uid if path == config else parent_uid
            return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "stat", fake_stat)
    monkeypatch.setattr(
        deploy,
        "Path",
        lambda path: config if path == "/etc/sideword/deploy.json" else original_path(path),
    )
    expected_compose = candidate.compose
    candidate.compose = None
    if accepted:
        deploy.host_configuration(candidate)
        assert candidate.compose == expected_compose
    else:
        with pytest.raises(deploy.DeploymentError):
            deploy.host_configuration(candidate)
        assert candidate.compose is None


def test_missing_repository_secrets_report_all_names_without_values(vm_config, monkeypatch):
    monkeypatch.delenv("GCP_PROJECT_ID")
    monkeypatch.delenv("GCP_ARTIFACT_IMAGE")
    with pytest.raises(deploy.DeploymentError) as failure:
        deploy.validate_config()
    assert str(failure.value) == (
        "Missing deployment settings: GCP_PROJECT_ID, GCP_ARTIFACT_IMAGE. "
        "Configure matching GitHub Actions repository secrets."
    )
    assert all(value not in str(failure.value) for value in vm_config.values())


def test_invalid_repository_secret_names_setting_without_disclosing_value(vm_config, monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "synthetic-private;invalid")
    with pytest.raises(deploy.DeploymentError) as failure:
        deploy.validate_config()
    assert str(failure.value) == (
        "Invalid deployment setting: GCP_PROJECT_ID. Check its repository secret."
    )


@pytest.mark.parametrize(
    "name,value",
    [
        ("GCP_SERVICE_ACCOUNT", "deploy@another-project.iam.gserviceaccount.com"),
        ("GCP_ARTIFACT_IMAGE", "region-docker.pkg.dev/another-project/backend/sideword"),
    ],
)
def test_deployment_rejects_cross_project_settings(vm_config, monkeypatch, capsys, name, value):
    monkeypatch.setenv(name, value)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(deploy.DeploymentError) as failure:
        deploy.validate_config()
    assert str(failure.value) == (
        "The deployment account and registry image must belong to GCP_PROJECT_ID."
    )
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("token", [None, "", "synthetic token", "synthetic-token\n"])
def test_publication_requires_authentication_token_before_commands(
    vm_config, monkeypatch, tmp_path, token
):
    if token is None:
        monkeypatch.delenv("GCP_ACCESS_TOKEN", raising=False)
    else:
        monkeypatch.setenv("GCP_ACCESS_TOKEN", token)
    monkeypatch.setattr(deploy, "run", lambda *args, **kwargs: pytest.fail("No commands allowed"))
    args = argparse.Namespace(directory=tmp_path, sha=SHA)
    with pytest.raises(deploy.DeploymentError, match="short-lived access token"):
        deploy.publish_image(args)


def test_publication_uses_masked_auth_output_only_for_the_publish_step():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.load(
        (root / ".github/workflows/deploy.yml").read_text(), Loader=yaml.BaseLoader
    )
    steps = workflow["jobs"]["deploy"]["steps"]
    auth = next(step for step in steps if step.get("id") == "auth")
    assert auth["uses"] == "google-github-actions/auth@v3"
    assert auth["with"]["token_format"] == "access_token"
    assert auth["with"]["access_token_lifetime"] == "1800s"
    consumers = [step for step in steps if "GCP_ACCESS_TOKEN" in step.get("env", {})]
    assert len(consumers) == 1
    assert "scripts.deploy publish" in consumers[0]["run"]
    assert consumers[0]["env"]["GCP_ACCESS_TOKEN"] == "${{ steps.auth.outputs.access_token }}"


def test_infrastructure_masking_preserves_common_log_words(vm_config, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    deploy.validate_config()
    masks = {line.removeprefix("::add-mask::") for line in capsys.readouterr().out.splitlines()}
    assert {
        "com",
        "iam",
        "gserviceaccount",
        "pkg",
        "dev",
        "projects",
        "locations",
        "global",
        "providers",
        "workloadIdentityPools",
    }.isdisjoint(masks)
    assert set(vm_config[name] for name in vm_config if name.startswith("GCP_")) <= masks
    assert {"123", "github", "repo", "deploy", "backend", "sideword"} <= masks
