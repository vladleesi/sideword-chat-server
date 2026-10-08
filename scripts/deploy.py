"""Publish a CI image and deploy its registry digest without exporting runtime configuration."""

import argparse
import datetime
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

IMAGE_PATH = (
    r"[a-z][a-z0-9-]+-docker\.pkg\.dev/[a-z][a-z0-9-]{4,61}[a-z0-9]/"
    r"[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9._-]*"
)


class DeploymentError(Exception):
    """A fixed, non-sensitive deployment explanation."""


@contextmanager
def stage(name):
    started = time.monotonic()
    print(f"Stage {name}: started.", flush=True)
    try:
        yield
    finally:
        print(f"Stage {name}: finished after {time.monotonic() - started:.1f}s.", flush=True)


def run(command, *, input=None, timeout=300, env=None):
    try:
        result = subprocess.run(command, input=input, capture_output=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DeploymentError(f"Deployment command failed: {command[0]}.") from error
    if result.returncode:
        raise DeploymentError(f"Deployment command failed: {command[0]}.")
    return result.stdout.decode()


def validate_metadata(args):
    for value, pattern in [
        (args.sha, r"[0-9a-f]{40}"),
        (args.image_id, r"sha256:[0-9a-f]{64}"),
        (args.version, r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"),
    ]:
        if not re.fullmatch(pattern, value):
            raise DeploymentError("Invalid checked-image metadata.")


def verify_archive(args):
    validate_metadata(args)
    if args.archive.is_symlink() or not args.archive.is_file():
        raise DeploymentError("The checked image archive is missing or unsafe.")
    if not re.fullmatch(r"[0-9a-f]{64}", args.checksum):
        raise DeploymentError("Invalid checked-image metadata.")
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
        "GCP_ARTIFACT_IMAGE": IMAGE_PATH,
    }
    missing = [name for name in patterns if not os.environ.get(name)]
    if missing:
        raise DeploymentError(
            "Missing deployment settings: "
            + ", ".join(missing)
            + ". Configure matching GitHub Actions repository secrets."
        )
    for name, pattern in patterns.items():
        if not re.fullmatch(pattern, os.environ[name]):
            raise DeploymentError(
                f"Invalid deployment setting: {name}. Check its repository secret."
            )
    if os.environ.get("GITHUB_ACTIONS") == "true":
        masks = {os.environ[name] for name in patterns}
        provider = os.environ["GCP_WORKLOAD_IDENTITY_PROVIDER"].split("/")
        masks.update(provider[index] for index in [1, 5, 7])
        account, domain = os.environ["GCP_SERVICE_ACCOUNT"].split("@")
        masks.update([account, domain.split(".iam.")[0]])
        masks.update(os.environ["GCP_ARTIFACT_IMAGE"].split("/"))
        masks.add(os.environ["GCP_ZONE"].rsplit("-", 1)[0])
        for value in sorted(masks):
            print(f"::add-mask::{value}")


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
    response = json.loads(
        run(
            [
                "gh",
                "api",
                "--method",
                "GET",
                f"repos/{repository}/actions/workflows/test.yml/runs",
                "-f",
                f"head_sha={sha}",
                "-f",
                "branch=develop",
                "-f",
                "event=push",
                "-f",
                "per_page=100",
            ]
        )
    )
    for checked_run in response.get("workflow_runs", []):
        if (
            checked_run.get("head_sha") != sha
            or checked_run.get("head_branch") != "develop"
            or checked_run.get("event") != "push"
            or (checked_run.get("head_repository") or {}).get("full_name") != repository
        ):
            continue
        run_id = checked_run.get("id")
        attempt = checked_run.get("run_attempt")
        if type(run_id) is not int or run_id <= 0 or type(attempt) is not int or attempt <= 0:
            continue
        jobs = json.loads(
            run(
                [
                    "gh",
                    "api",
                    f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100",
                ]
            )
        )
        passed = {
            job.get("name")
            for job in jobs.get("jobs", [])
            if job.get("status") == "completed" and job.get("conclusion") == "success"
        }
        if {"smoke", "container"} <= passed:
            print("Checked commit passed both CI jobs.")
            output = os.environ.get("GITHUB_OUTPUT")
            if output:
                with open(output, "a") as stream:
                    stream.write(f"run_id={run_id}\nrun_attempt={attempt}\n")
            return run_id
    raise DeploymentError("Both CI jobs must pass on this exact develop commit before deployment.")


def validate_reference(args):
    validate_metadata(args)
    if not re.fullmatch(IMAGE_PATH + r"@sha256:[0-9a-f]{64}", args.image_ref):
        raise DeploymentError("Invalid immutable image reference.")


def artifact_args(directory, sha):
    metadata = json.loads((directory / "image.json").read_text())
    args = argparse.Namespace(archive=directory / "image.tar.gz", sha=sha, **metadata)
    verify_archive(args)
    return args


def package_image(directory, sha, retain=True):
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise DeploymentError("Invalid checked commit.")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with stage("build"):
        run(
            [
                "docker",
                "build",
                "--tag",
                "sideword-ci:checked",
                "--label",
                f"org.opencontainers.image.revision={sha}",
                ".",
            ],
            timeout=600,
        )
        image_id = json.loads(run(["docker", "image", "inspect", "sideword-ci:checked"]))[0]["Id"]
        version = run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "python",
                image_id,
                "-c",
                "from app.version import __version__; print(__version__)",
            ]
        ).strip()
        run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "python",
                image_id,
                "-m",
                "pip",
                "check",
            ]
        )
    if not retain:
        return
    with stage("package"):
        archive = directory / "image.tar"
        run(["docker", "save", "--output", str(archive), image_id], timeout=600)
        run(["gzip", "-1", str(archive)], timeout=300)
        with archive.with_suffix(".tar.gz").open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        (directory / "image.json").write_text(
            json.dumps(
                {
                    "image_id": image_id,
                    "checksum": checksum,
                    "version": version,
                }
            )
        )
        artifact_args(directory, sha)


def publish_image(args):
    validate_config()
    token = os.environ.get("GCP_ACCESS_TOKEN", "")
    if not token or any(character.isspace() for character in token):
        raise DeploymentError(
            "Publication requires a short-lived access token from the Google authentication step."
        )
    candidate = artifact_args(args.directory, args.sha)
    image_path = os.environ["GCP_ARTIFACT_IMAGE"]
    registry = image_path.split("/")[0]
    with stage("publish"):
        run(["docker", "load", "--input", str(candidate.archive)], timeout=600)
        image = json.loads(run(["docker", "image", "inspect", candidate.image_id]))[0]
        if (
            image["Id"] != candidate.image_id
            or (image["Config"].get("Labels") or {}).get("org.opencontainers.image.revision")
            != args.sha
        ):
            raise DeploymentError("The CI image does not match the checked commit.")
        # Temporary Docker credentials never enter the checkout or persist after publication.
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, "DOCKER_CONFIG": directory}
            run(
                [
                    "docker",
                    "login",
                    "--username",
                    "oauth2accesstoken",
                    "--password-stdin",
                    registry,
                ],
                input=token.encode(),
                env=environment,
            )
            run_id = os.environ.get("GITHUB_RUN_ID", "")
            if not re.fullmatch(r"[0-9]+", run_id):
                raise DeploymentError("Publication requires a GitHub Actions run identity.")
            tag = f"{image_path}:ci-{args.sha}-{run_id}"
            run(["docker", "image", "tag", candidate.image_id, tag])
            run(["docker", "push", tag], env=environment, timeout=600)
            published = json.loads(run(["docker", "image", "inspect", tag]))[0]
            references = [
                ref for ref in published.get("RepoDigests", []) if ref.startswith(image_path + "@")
            ]
            if published["Id"] != candidate.image_id or len(references) != 1:
                raise DeploymentError("Cannot verify the published image digest.")
            candidate.image_ref = references[0]
            validate_reference(candidate)
            manifest = json.loads(
                run(["docker", "manifest", "inspect", candidate.image_ref], env=environment)
            )
            if manifest.get("config", {}).get("digest") != candidate.image_id:
                raise DeploymentError("The registry digest does not identify the CI image.")
    with open(os.environ["GITHUB_ENV"], "a") as output:
        # Mask the reference before it becomes an environment value in later steps.
        print(f"::add-mask::{candidate.image_ref}")
        output.write(
            f"IMAGE_REF={candidate.image_ref}\nIMAGE_ID={candidate.image_id}\n"
            f"BACKEND_VERSION={candidate.version}\n"
        )


def deploy_vm(args):
    validate_config()
    validate_reference(args)
    if args.image_ref.split("@")[0] != os.environ["GCP_ARTIFACT_IMAGE"]:
        raise DeploymentError("The image is outside the configured registry repository.")
    if not current_commit(args.sha):
        print("The checked commit is no longer current; skipping deployment.")
        return
    ssh = [
        "gcloud",
        "compute",
        "ssh",
        os.environ["GCP_INSTANCE"],
        "--project",
        os.environ["GCP_PROJECT_ID"],
        "--zone",
        os.environ["GCP_ZONE"],
        "--tunnel-through-iap",
        "--quiet",
        "--verbosity=error",
        "--ssh-flag=-T",
        "--ssh-key-expire-after=5m",
    ]
    command = shlex.join(
        [
            "sudo",
            "-n",
            "python3",
            "-",
            "host",
            "--image-ref",
            args.image_ref,
            "--sha",
            args.sha,
            "--image-id",
            args.image_id,
            "--version",
            args.version,
        ]
    )
    with stage("activate"):
        result = run([*ssh, "--command", command], input=Path(__file__).read_bytes(), timeout=900)
        lines = result.strip().splitlines()
        if not lines or lines[-1] != "Checked backend deployment verified.":
            raise DeploymentError("The VM did not confirm a verified deployment.")
        for line in lines[:-1]:
            if not re.fullmatch(
                r"Stage (pull|backup|health): (started\.|finished after [0-9]+\.[0-9]s\.)",
                line,
            ):
                raise DeploymentError("The VM returned an unexpected deployment response.")
        for line in lines:
            print(line)


def pull_image(args):
    registry = args.image_ref.split("/")[0]
    with stage("pull"), tempfile.TemporaryDirectory() as directory:
        # Use only the VM's attached identity; never copy GitHub credentials to the host.
        request = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
            headers={"Metadata-Flavor": "Google"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=10) as response:
            token = json.load(response)["access_token"]
        environment = {**os.environ, "DOCKER_CONFIG": directory}
        run(
            ["docker", "login", "--username", "oauth2accesstoken", "--password-stdin", registry],
            input=token.encode(),
            env=environment,
        )
        run(["docker", "pull", args.image_ref], env=environment, timeout=600)


def host_configuration(args):
    if args.compose is not None:
        return
    path = Path("/etc/sideword/deploy.json")
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_uid != 0
        or path.stat().st_mode & 0o077
        or path.parent.stat().st_uid != 0
        or path.parent.stat().st_mode & 0o022
    ):
        raise DeploymentError("A root-owned private host deployment configuration is required.")
    configuration = json.loads(path.read_text())
    if args.image_ref.split("@")[0] != configuration["image"]:
        raise DeploymentError("The image is outside the host's allowed repository.")
    args.compose = Path(configuration["compose"])


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
    validate_reference(args)
    host_configuration(args)

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
        pull_image(args)
        image = json.loads(run(["docker", "image", "inspect", args.image_ref]))[0]
        if (
            image["Id"] != args.image_id
            or (image["Config"].get("Labels") or {}).get("org.opencontainers.image.revision")
            != args.sha
        ):
            raise DeploymentError("The pulled image does not match the checked commit.")
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
        run(["docker", "image", "tag", previous["Image"], rollback_tag])
        try:
            run([*compose, "stop", "--timeout", "30", "sideword"])
            with stage("backup"):
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
    for name in ["package", "publish", "artifact"]:
        command = commands.add_parser(name)
        command.add_argument("--directory", type=Path, required=True)
        command.add_argument("--sha", required=True)
        if name == "package":
            command.add_argument("--retain", choices=["true", "false"], default="true")
    for name in ["verify", "vm", "host"]:
        command = commands.add_parser(name)
        command.add_argument("--sha", required=True)
        command.add_argument("--image-id", required=True)
        command.add_argument("--version", required=True)
        if name == "verify":
            command.add_argument("--archive", type=Path, required=True)
            command.add_argument("--checksum", required=True)
        else:
            command.add_argument("--image-ref", required=True)
        if name == "host":
            command.set_defaults(compose=None)
    args = parser.parse_args()
    try:
        {
            "validate": validate_config,
            "artifact": lambda: artifact_args(args.directory, args.sha),
            "package": lambda: package_image(args.directory, args.sha, args.retain == "true"),
            "publish": lambda: publish_image(args),
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
