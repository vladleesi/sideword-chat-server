"""Deploy a checked image through IAP without exporting runtime configuration."""

import argparse
import datetime
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath


class DeploymentError(Exception):
    """A fixed, non-sensitive deployment explanation."""


def run(command, *, input=None, timeout=300):
    try:
        result = subprocess.run(command, input=input, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DeploymentError(f"Deployment command failed: {command[0]}.") from error
    if result.returncode:
        raise DeploymentError(f"Deployment command failed: {command[0]}.")
    return result.stdout.decode()


def validate_metadata(args):
    for value, pattern in [
        (args.sha, r"[0-9a-f]{40}"),
        (args.image_id, r"sha256:[0-9a-f]{64}"),
        (args.checksum, r"[0-9a-f]{64}"),
        (args.version, r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"),
    ]:
        if not re.fullmatch(pattern, value):
            raise DeploymentError("Invalid checked-image metadata.")


def verify_archive(args):
    validate_metadata(args)
    if args.archive.is_symlink() or not args.archive.is_file():
        raise DeploymentError("The checked image archive is missing or unsafe.")
    with args.archive.open("rb") as archive:
        actual = hashlib.file_digest(archive, "sha256").hexdigest()
    if actual != args.checksum:
        raise DeploymentError("The image archive does not match the CI checksum.")


def validate_config():
    patterns = {
        "GCP_PROJECT_ID": r"[a-z][a-z0-9-]{4,61}[a-z0-9]",
        "GCP_ZONE": r"[a-z][a-z0-9-]+",
        "GCP_INSTANCE": r"[a-z][a-z0-9-]{0,61}[a-z0-9]|[a-z]",
        "GCP_WORKLOAD_IDENTITY_PROVIDER": (
            r"projects/[0-9]+/locations/global/workloadIdentityPools/[a-z0-9-]+/providers/[a-z0-9-]+"
        ),
        "GCP_SERVICE_ACCOUNT": r"[a-z0-9-]+@[a-z0-9-]+\.iam\.gserviceaccount\.com",
        "SIDEWORD_DEPLOY_COMPOSE": r"/[^\x00\r\n]+",
    }
    for name, pattern in patterns.items():
        if not re.fullmatch(pattern, os.environ.get(name, "")):
            raise DeploymentError(f"Set a valid {name} in the production environment.")


def current_commit(sha):
    run(["git", "fetch", "origin", "main", "develop"])
    return all(
        run(["git", "rev-parse", f"origin/{branch}"]).strip() == sha
        for branch in ["main", "develop"]
    )


def verify_ci(sha):
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise DeploymentError("Invalid checked commit.")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository):
        raise DeploymentError("A valid GitHub repository is required.")
    response = json.loads(run([
        "gh", "api", "--method", "GET",
        f"repos/{repository}/actions/workflows/test.yml/runs",
        "-f", f"head_sha={sha}", "-f", "branch=develop", "-f", "event=push",
        "-f", "per_page=100",
    ]))
    for checked_run in response.get("workflow_runs", []):
        if (checked_run.get("head_sha") != sha or checked_run.get("head_branch") != "develop"
                or checked_run.get("event") != "push"
                or (checked_run.get("head_repository") or {}).get("full_name") != repository):
            continue
        run_id = checked_run.get("id")
        if type(run_id) is not int or run_id <= 0:
            continue
        jobs = json.loads(run([
            "gh", "api", f"repos/{repository}/actions/runs/{run_id}/jobs?per_page=100",
        ]))
        passed = {
            job.get("name") for job in jobs.get("jobs", [])
            if job.get("status") == "completed" and job.get("conclusion") == "success"
        }
        if {"smoke", "container"} <= passed:
            print("Checked commit passed both CI jobs.")
            return
    raise DeploymentError("Both CI jobs must pass on this exact develop commit before deployment.")


def deploy_vm(args):
    validate_config()
    verify_archive(args)
    if not current_commit(args.sha):
        print("The checked commit is no longer current; skipping deployment.")
        return
    project = os.environ["GCP_PROJECT_ID"]
    zone = os.environ["GCP_ZONE"]
    instance = os.environ["GCP_INSTANCE"]
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    if not re.fullmatch(r"[0-9]+", run_id) or not re.fullmatch(r"[0-9]+", attempt):
        raise DeploymentError("Deployment requires a GitHub Actions run identity.")
    stage = f"/tmp/sideword-ci-{run_id}-{attempt}"
    common = [
        "--project",
        project,
        "--zone",
        zone,
        "--tunnel-through-iap",
        "--quiet",
        "--verbosity=error",
    ]
    ssh = ["gcloud", "compute", "ssh", instance, *common, "--ssh-flag=-T"]
    run([*ssh, "--command", shlex.join(["mkdir", "-m", "700", "--", stage])])
    try:
        remote_archive = f"{stage}/image.tar.gz"
        run(
            [
                "gcloud",
                "compute",
                "scp",
                *common,
                str(args.archive),
                f"{instance}:{remote_archive}",
            ],
            timeout=600,
        )
        if not current_commit(args.sha):
            print("The checked commit advanced during transfer; skipping activation.")
            return
        command = shlex.join(
            [
                "sudo",
                "-n",
                "python3",
                "-",
                "host",
                "--archive",
                remote_archive,
                "--compose",
                os.environ["SIDEWORD_DEPLOY_COMPOSE"],
                "--sha",
                args.sha,
                "--image-id",
                args.image_id,
                "--checksum",
                args.checksum,
                "--version",
                args.version,
            ]
        )
        result = run([*ssh, "--command", command], input=Path(__file__).read_bytes(), timeout=600)
        # Only the fixed success marker is relayed; command output/configuration stays private.
        if result.strip() != "Checked backend deployment verified.":
            raise DeploymentError("The VM did not confirm a verified deployment.")
        print("Checked backend deployment verified.")
    finally:
        try:
            # A root-owned process may have read the upload; remove it through sudo as well.
            run([*ssh, "--command", shlex.join(["sudo", "-n", "rm", "-rf", "--", stage])])
        except DeploymentError:
            print(
                "::warning::Deployment staging cleanup failed; remove the private upload on the VM."
            )


def database_path(container):
    environment = dict(
        value.split("=", 1) for value in container["Config"].get("Env", []) if "=" in value
    )
    database = PurePosixPath(environment.get("SIDEWORD_DB_PATH", "/data/sideword.sqlite3"))
    for mount in sorted(
        container["Mounts"], key=lambda item: len(item["Destination"]), reverse=True
    ):
        try:
            relative = database.relative_to(mount["Destination"])
        except ValueError:
            continue
        if ".." in relative.parts or mount.get("Type") not in {"bind", "volume"}:
            break
        path = Path(mount["Source"]) / relative
        if path.is_symlink() or not path.is_file():
            break
        return path
    raise DeploymentError("The existing database must be a file in a persistent data mount.")


def backup_database(source, destination):
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original:
        with sqlite3.connect(destination) as backup:
            original.backup(backup)
    destination.chmod(0o600)


def health_matches(version):
    try:
        for port in [8000, 8001]:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
                data = json.load(response)
                if (
                    response.status != 200
                    or not isinstance(data, dict)
                    or data.get("status") != "ok"
                    or data.get("version") != version
                ):
                    return False
    except (OSError, ValueError):
        return False
    return True


def verify_private_routes():
    for path in ["/admin", "/admin/login", "/docs", "/redoc", "/openapi.json"]:
        for headers in [
            {},
            {
                "Connection": "Upgrade",
                "Upgrade": "websocket",
                "Sec-WebSocket-Version": "13",
                "Sec-WebSocket-Key": "MDEyMzQ1Njc4OWFiY2RlZg==",
            },
        ]:
            request = urllib.request.Request(f"http://127.0.0.1:8001{path}", headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=3):
                    pass
            except urllib.error.HTTPError as error:
                if error.code in {403, 404}:
                    continue
            except OSError:
                pass
            raise DeploymentError("A private route was not blocked on the shared listener.")


def deploy_host(args):
    verify_archive(args)

    import fcntl

    if args.compose.is_symlink() or not args.compose.is_absolute() or not args.compose.is_file():
        raise DeploymentError("The existing private Compose configuration is required.")
    if args.compose.stat().st_mode & 0o022 or args.compose.parent.stat().st_mode & 0o022:
        raise DeploymentError(
            "The private Compose configuration must not be writable by other users."
        )
    os.umask(0o077)
    with (args.compose.parent / "ci-deploy.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise DeploymentError("Another backend deployment is already running.") from error
        compose = ["docker", "compose", "-f", str(args.compose)]
        configuration = json.loads(run([*compose, "config", "--format", "json"]))
        image_tag = configuration.get("services", {}).get("sideword", {}).get("image")
        if not image_tag or image_tag.startswith("-") or "@" in image_tag:
            raise DeploymentError("The sideword service requires a dedicated mutable image tag.")
        container_id = run([*compose, "ps", "-q", "sideword"]).strip()
        if not container_id or "\n" in container_id:
            raise DeploymentError("Exactly one existing sideword container is required.")
        previous = json.loads(run(["docker", "inspect", container_id]))[0]
        configured_image = json.loads(run(["docker", "image", "inspect", image_tag]))[0]
        if configured_image["Id"] != previous["Image"]:
            raise DeploymentError(
                "The configured image tag must still identify the running backend."
            )
        database = database_path(previous)
        backup_dir = args.compose.parent / "deployment-backups"
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        if backup_dir.is_symlink() or backup_dir.stat().st_mode & 0o077:
            raise DeploymentError("The deployment backup directory must be private.")
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        rollback_tag = f"sideword-rollback:{stamp.lower()}"
        backup = backup_dir / f"pre-deploy-{stamp}.sqlite3"
        run(["docker", "image", "tag", previous["Image"], rollback_tag])
        run(["docker", "load", "--input", str(args.archive)])
        image = json.loads(run(["docker", "image", "inspect", args.image_id]))[0]
        if (
            image["Id"] != args.image_id
            or (image["Config"].get("Labels") or {}).get("org.opencontainers.image.revision")
            != args.sha
        ):
            raise DeploymentError("The loaded image does not match the checked commit.")
        version = run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--pids-limit",
                "64",
                "--memory",
                "128m",
                "--entrypoint",
                "python",
                args.image_id,
                "-c",
                "from app.version import __version__; print(__version__)",
            ]
        ).strip()
        if version != args.version:
            raise DeploymentError("The checked image reports an unexpected backend version.")
        try:
            run([*compose, "stop", "--timeout", "30", "sideword"])
            backup_database(database, backup)
            run(["docker", "image", "tag", args.image_id, image_tag])
        except Exception:
            # No new application ran against the database; restarting the old image is safe.
            run(["docker", "image", "tag", previous["Image"], image_tag])
            run([*compose, "up", "-d", "--no-build", "--pull", "never", "--no-deps", "sideword"])
            raise
        try:
            run([*compose, "up", "-d", "--no-build", "--pull", "never", "--no-deps", "sideword"])
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                current_id = run([*compose, "ps", "-q", "sideword"]).strip()
                if current_id:
                    current = json.loads(run(["docker", "inspect", current_id]))[0]
                    if (
                        current["Image"] == args.image_id
                        and current["State"].get("Health", {}).get("Status") == "healthy"
                        and health_matches(args.version)
                    ):
                        verify_private_routes()
                        print("Checked backend deployment verified.")
                        return
                time.sleep(2)
            raise DeploymentError(
                "Updated backend verification failed; "
                "keep the backup and previous image for recovery."
            )
        except Exception:
            # Keep an unverified application offline without overwriting its database.
            run([*compose, "stop", "--timeout", "30", "sideword"])
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    ci = commands.add_parser("ci")
    ci.add_argument("--sha", required=True)
    for name in ["verify", "vm", "host"]:
        command = commands.add_parser(name)
        command.add_argument("--archive", type=Path, required=True)
        command.add_argument("--sha", required=True)
        command.add_argument("--image-id", required=True)
        command.add_argument("--checksum", required=True)
        command.add_argument("--version", required=True)
        if name == "host":
            command.add_argument("--compose", type=Path, required=True)
    args = parser.parse_args()
    try:
        {
            "validate": validate_config,
            "ci": lambda: verify_ci(args.sha),
            "verify": lambda: verify_archive(args),
            "vm": lambda: deploy_vm(args),
            "host": lambda: deploy_host(args),
        }[args.command]()
    except Exception as error:
        message = (
            str(error)
            if isinstance(error, DeploymentError)
            else "Deployment failed; private details were not printed."
        )
        print(message)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
