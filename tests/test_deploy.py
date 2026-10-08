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
PREVIOUS = "sha256:" + "c" * 64


@pytest.fixture
def candidate(tmp_path):
    archive = tmp_path / "image.tar.gz"
    archive.write_bytes(b"synthetic checked archive")
    compose = tmp_path / "compose.yaml"
    compose.write_text("synthetic private configuration")
    return argparse.Namespace(
        archive=archive,
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
        "SIDEWORD_DEPLOY_COMPOSE": "/example/private compose.yaml",
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
    state = {"active": False, "label": SHA, "version": candidate.version, "database": database}
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
                        "Id": IMAGE,
                        "Config": {"Labels": {"org.opencontainers.image.revision": state["label"]}},
                    }
                ]
            )
        if command[:2] == ["docker", "inspect"]:
            if not state["active"]:
                return json.dumps([previous])
            return json.dumps([{"Image": IMAGE, "State": {"Health": {"Status": "healthy"}}}])
        if command[:2] == ["docker", "run"]:
            return state["version"]
        if "up" in command:
            state["active"] = True
        if "stop" in command:
            state["active"] = False
        return ""

    monkeypatch.setattr(deploy, "run", fake_run)
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
        ("SIDEWORD_DEPLOY_COMPOSE", "relative/config.yaml"),
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


def test_advanced_commit_cleans_upload_without_activation(candidate, vm_config, monkeypatch):
    refs = iter([True, False])
    monkeypatch.setattr(deploy, "current_commit", lambda sha: next(refs))
    commands = []
    monkeypatch.setattr(
        deploy, "run", lambda command, **kwargs: commands.append((command, kwargs)) or ""
    )
    deploy.deploy_vm(candidate)
    assert len(commands) == 3
    assert commands[1][0][:3] == ["gcloud", "compute", "scp"]
    assert commands[-1][0][-1] == "sudo -n rm -rf -- /tmp/sideword-ci-123-2"
    assert not any("input" in kwargs for _, kwargs in commands)


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
    assert "'/example/private compose.yaml'" in activation[0][-1]
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
    assert capsys.readouterr().out == "Checked backend deployment verified.\n"


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
    times = iter([0, 181])
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


def test_host_archive_tampering_never_contacts_docker(candidate, monkeypatch):
    candidate.archive.write_bytes(b"unexpected bytes")
    monkeypatch.setattr(
        deploy, "run", lambda *args, **kwargs: pytest.fail("No Docker commands allowed")
    )
    with pytest.raises(deploy.DeploymentError, match="checksum"):
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


@pytest.fixture
def checked_ci(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/project")
    checked_run = {
        "id": 123,
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
    assert commands[1][-1] == "repos/example/project/actions/runs/123/jobs?per_page=100"
    assert capsys.readouterr().out == "Checked commit passed both CI jobs.\n"


@pytest.mark.parametrize("field,value", [
    ("head_sha", "d" * 40),
    ("head_branch", "main"),
    ("event", "pull_request"),
    ("head_repository", {"full_name": "untrusted/fork"}),
    ("head_repository", None),
    ("id", "123"),
    ("id", True),
])
def test_unrelated_or_untrusted_ci_cannot_authorize_deployment(checked_ci, field, value):
    checked_run, _, commands = checked_ci
    checked_run[field] = value
    with pytest.raises(deploy.DeploymentError, match="Both CI jobs"):
        deploy.verify_ci(SHA)
    assert len(commands) == 1


@pytest.mark.parametrize("field,value", [
    ("conclusion", "failure"),
    ("conclusion", "skipped"),
    ("conclusion", "cancelled"),
    ("status", "in_progress"),
    ("name", "unrelated-job"),
])
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


@pytest.mark.parametrize("sha,repository", [
    ("main", "example/project"),
    (SHA, ""),
    (SHA, "example/project/other"),
])
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
        "contents": "read", "actions": "read", "id-token": "write",
    }
    job = workflow["jobs"]["deploy"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert job["environment"] == "production"
    assert job["concurrency"]["cancel-in-progress"] == "false"
    ci = yaml.load((workflows / "test.yml").read_text(), Loader=yaml.BaseLoader)
    assert job["concurrency"]["group"] == ci["jobs"]["promote"]["concurrency"]["group"]
    assert {"smoke", "container"} <= ci["jobs"].keys()
    steps = job["steps"]
    gate = next(i for i, step in enumerate(steps) if "scripts.deploy ci " in step.get("run", ""))
    build = next(i for i, step in enumerate(steps) if "docker build " in step.get("run", ""))
    auth = next(i for i, step in enumerate(steps)
                if step.get("uses", "").startswith("google-github-actions/auth@"))
    assert gate < build < auth
    assert steps[gate]["if"] == "steps.current.outputs.deploy == 'true'"
    assert not any(step.get("continue-on-error") == "true" for step in steps)
    for path in workflows.glob("*.yml"):
        if path.name != "deploy.yml":
            assert "./.github/workflows/deploy.yml" not in path.read_text()
