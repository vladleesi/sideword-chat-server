"""Publish a CI image and deploy its registry digest without exporting runtime configuration."""

import argparse
import datetime
import hashlib
import json
import os
import re
import shlex
import sqlite3
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath

IMAGE_PATH = (
    r"[a-z][a-z0-9-]+-docker\.pkg\.dev/[a-z][a-z0-9-]{4,61}[a-z0-9]/"
    r"[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9._-]*"
)
HOST_PROGRESS_PATTERN = (
    r"Stage (pull|backup|health): (started\.|finished after [0-9]+\.[0-9]s\.)"
)
HOST_FAILURE_MESSAGES = {
    "Deployment downgrades are not allowed.",
    "Database recovery could not be completed safely; the recovery state was retained.",
    "The pulled image does not match the checked commit.",
    "The registry digest does not identify the CI image.",
    "Updated backend verification failed; keep the backup and previous image for recovery.",
    "Remove the private Compose curl health check; inherit the checked image's Python probe.",
    "The private Compose health check must not be disabled.",
    "The existing database schema is incompatible with the checked image. "
    "Configure a fresh persistent database and preserve the existing database separately.",
}


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
    operation = command[0]
    if operation == "docker":
        for parts in (
            ["load"],
            ["login"],
            ["push"],
            ["pull"],
            ["image", "inspect"],
            ["image", "tag"],
            ["manifest", "inspect"],
        ):
            if command[1 : 1 + len(parts)] == parts:
                operation = "docker " + " ".join(parts)
                break
    failure = f"Deployment command failed: {operation}."
    try:
        result = subprocess.run(command, input=input, capture_output=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DeploymentError(failure) from error
    if result.returncode:
        if command[:3] == ["gcloud", "compute", "ssh"]:
            for line in result.stdout.decode(errors="replace").splitlines():
                if re.fullmatch(HOST_PROGRESS_PATTERN, line):
                    print(line, flush=True)
                elif line in HOST_FAILURE_MESSAGES:
                    failure = line
        if (
            operation == "docker push"
            and b"artifactregistry.repositories.uploadArtifacts" in result.stderr
            and b"denied" in result.stderr.lower()
        ):
            raise DeploymentError(
                "Registry upload permission was denied for the deployment account."
            )
        raise DeploymentError(failure)
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
    project = os.environ["GCP_PROJECT_ID"]
    if (
        os.environ["GCP_ARTIFACT_IMAGE"].split("/")[1] != project
        or os.environ["GCP_SERVICE_ACCOUNT"].split("@", 1)[1]
        != f"{project}.iam.gserviceaccount.com"
    ):
        raise DeploymentError(
            "The deployment account and registry image must belong to GCP_PROJECT_ID."
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
        if {"smoke", "container", "Promote develop to main"} <= passed:
            print("Checked commit passed CI gates and promotion.")
            output = os.environ.get("GITHUB_OUTPUT")
            if output:
                with open(output, "a") as stream:
                    stream.write(f"run_id={run_id}\nrun_attempt={attempt}\n")
            return run_id
    raise DeploymentError(
        "CI gates and promotion must pass on this exact develop commit before deployment."
    )


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
                "-c",
                "import importlib.util; "
                "assert all(importlib.util.find_spec(name) is None "
                "for name in ('pip', 'ensurepip')), 'Build tools remain in the runtime image'; "
                "assert importlib.util.find_spec('scripts.create_admin') is not None, "
                "'Admin recovery command missing'; "
                "import fastapi, uvicorn, sqlalchemy, aiosqlite, pydantic, pydantic_settings, "
                "python_multipart, jinja2, itsdangerous, bcrypt, cryptography, jwt, dotenv",
            ]
        )
    validate_metadata(argparse.Namespace(sha=sha, image_id=image_id, version=version))
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a") as stream:
            stream.write(f"image_id={image_id}\n")
    if not retain:
        return image_id
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
    return image_id


def registry_config_digest(image_ref: str, image_id: str, environment: dict[str, str]) -> str:
    """Bind either Docker image-ID format to a single runnable registry image."""
    failure = "The registry digest does not identify the CI image."
    repository, root_digest = image_ref.split("@")
    manifest = json.loads(
        run(["docker", "manifest", "inspect", image_ref], env=environment)
    )
    manifest_digest = root_digest
    if "manifests" in manifest:
        # BuildKit indexes can also contain provenance with an unknown/unknown platform.
        runnable = [
            entry for entry in manifest["manifests"]
            if entry.get("platform", {}).get("os") != "unknown"
        ]
        if len(runnable) != 1:
            raise DeploymentError(failure)
        descriptor = runnable[0]
        manifest_digest = descriptor.get("digest", "")
        if (
            descriptor.get("platform", {}).get("os") != "linux"
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest_digest)
        ):
            raise DeploymentError(failure)
        manifest = json.loads(run(
            ["docker", "manifest", "inspect", f"{repository}@{manifest_digest}"],
            env=environment,
        ))
    config_digest = manifest.get("config", {}).get("digest", "")
    if (
        not re.fullmatch(r"sha256:[0-9a-f]{64}", config_digest)
        or image_id not in {root_digest, manifest_digest, config_digest}
    ):
        raise DeploymentError(failure)
    return config_digest


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
            candidate.image_id = registry_config_digest(
                candidate.image_ref, candidate.image_id, environment
            )
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
    with stage("activate"), tempfile.TemporaryDirectory() as directory:
        # gcloud's automatic key generation writes its banner to the remote response.
        # Capture key generation separately and remove the ephemeral key on every exit.
        key = str(Path(directory) / "deploy-key")
        run(
            [
                "ssh-keygen", "-q", "-t", "rsa", "-b", "3072", "-N", "",
                "-C", "github-actions", "-f", key,
            ],
            timeout=30,
        )
        result = run(
            [*ssh, "--ssh-key-file", key, "--command", command],
            input=Path(__file__).read_bytes(),
            timeout=900,
        )
        lines = result.strip().splitlines()
        if not lines or lines[-1] != "Checked backend deployment verified.":
            raise DeploymentError("The VM did not confirm a verified deployment.")
        for line in lines[:-1]:
            if not re.fullmatch(HOST_PROGRESS_PATTERN, line):
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
        if registry_config_digest(args.image_ref, args.image_id, environment) != args.image_id:
            raise DeploymentError("The registry digest does not identify the CI image.")


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


def database_path(container, *, allow_missing=False):
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
        if path.parent.resolve() != path.parent or not path.name:
            break
        if path.is_symlink() or (not path.is_file() and not (allow_missing and not path.exists())):
            break
        return path
    raise DeploymentError("The existing database must be a file in a persistent data mount.")


def version_tuple(version):
    if not isinstance(version, str) or not re.fullmatch(
        r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version
    ):
        raise DeploymentError("Cannot determine the installed backend version safely.")
    return tuple(map(int, version.split(".")))


def sync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def private_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise DeploymentError("Unsafe deployment recovery metadata.")


def write_deployment_state(path, value):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
            sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)


SQLITE_SUFFIXES = ("", "-journal", "-wal", "-shm")


class DatabaseReplacement:
    """A durable rename transaction; the only retained queue is the original database."""

    def __init__(self, compose_path, database):
        self.path = compose_path.parent / "database-deployment.json"
        self.database = database
        self.directory = database.parent / ".sideword-database-recovery"
        self.state = None
        if self.path.exists() or self.path.is_symlink():
            private_file(self.path)
            self.state = json.loads(self.path.read_text())
            if (
                self.state.get("database") != str(database)
                or self.state.get("phase") not in {
                    "prepared", "installing", "restoring", "restored", "verified",
                }
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.state.get("image", ""))
                or not isinstance(self.state.get("files"), dict)
                or not set(self.state["files"]) <= set(SQLITE_SUFFIXES)
            ):
                raise DeploymentError("Unsafe deployment recovery metadata.")
            version_tuple(self.state.get("version"))
            self.check_directory()

    def check_directory(self):
        info = self.directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise DeploymentError("Unsafe database recovery directory.")

    def save(self, phase):
        self.state["phase"] = phase
        write_deployment_state(self.path, self.state)

    def original(self, suffix):
        return Path(str(self.database) + suffix)

    def retained(self, suffix):
        return self.directory / ("database" + suffix)

    @staticmethod
    def identity(path):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DeploymentError("Unsafe database recovery file.")
        return [info.st_dev, info.st_ino]

    def prepare(self, image, version, configuration=None):
        if self.state is not None:
            raise DeploymentError("A database recovery transaction is already pending.")
        # This directory is reserved for this process; an orphaned nonempty one is never reused.
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.check_directory()
        sync_directory(self.database.parent)
        if any(self.directory.iterdir()):
            raise DeploymentError(
                "An unrecognized database recovery copy requires operator review."
            )
        files = {}
        for suffix in SQLITE_SUFFIXES:
            path = self.original(suffix)
            if path.exists() or path.is_symlink():
                files[suffix] = self.identity(path)
                # Old installations may have used a broader umask. Never retain a public copy.
                path.chmod(0o600)
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        self.state = {"database": str(self.database), "image": image,
                      "version": version, "files": files, "configuration": configuration}
        self.save("prepared")
        for suffix in files:
            os.rename(self.original(suffix), self.retained(suffix))
            sync_directory(self.directory)
            sync_directory(self.database.parent)
        self.save("installing")

    def restore(self):
        # Journal this before touching new files, so a crash mid-restore is retryable.
        prepared = self.state["phase"] == "prepared"
        if not prepared:
            self.save("restoring")
        for suffix in SQLITE_SUFFIXES:
            original, retained = self.original(suffix), self.retained(suffix)
            expected = self.state["files"].get(suffix)
            if retained.exists() or retained.is_symlink():
                if self.identity(retained) != expected:
                    raise DeploymentError("The database recovery copy changed unexpectedly.")
                if original.exists() or original.is_symlink():
                    if prepared:
                        raise DeploymentError("Ambiguous database recovery state.")
                    self.identity(original)
                    original.unlink()
                os.rename(retained, original)
            elif expected is not None:
                if self.identity(original) != expected:
                    raise DeploymentError("The previous database is not recoverable safely.")
            elif not prepared and (original.exists() or original.is_symlink()):
                self.identity(original)
                original.unlink()
            sync_directory(self.database.parent)
            sync_directory(self.directory)
        self.save("restored")

    def verified(self, image, version):
        self.state.update(activated_image=image, activated_version=version,
                          current=self.identity(self.database))
        self.save("verified")

    def finish(self):
        if self.state["phase"] == "verified":
            for suffix, expected in self.state["files"].items():
                path = self.retained(suffix)
                if path.exists() or path.is_symlink():
                    if self.identity(path) != expected:
                        raise DeploymentError("The database recovery copy changed unexpectedly.")
                    path.unlink()
                    sync_directory(self.directory)
        if any(self.directory.iterdir()):
            raise DeploymentError("Unrecognized files remain in the database recovery directory.")
        # Keep the directory: after a crash between these operations an empty directory is safe.
        self.path.unlink(missing_ok=True)
        sync_directory(self.path.parent)
        self.state = None


def stop_database_writers(compose, container_id, database, *, check_sqlite=True):
    run([*compose, "stop", "--timeout", "30", "sideword"])
    current = json.loads(run(["docker", "inspect", container_id]))[0]
    if current.get("State", {}).get("Running") is not False:
        raise DeploymentError("Database writers are still running.")
    # Refuse other running containers sharing this database's host storage.
    ids = run(["docker", "ps", "-q"]).split()
    for identifier in ids:
        if identifier == container_id:
            continue
        container = json.loads(run(["docker", "inspect", identifier]))[0]
        for mount in container.get("Mounts", []):
            if database.is_relative_to(Path(mount["Source"])):
                raise DeploymentError("Another running container shares the database storage.")
    if check_sqlite and database.exists():
        with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True)) as connection:
            connection.execute("BEGIN EXCLUSIVE")
            connection.rollback()


def wait_for_backend(compose, image_id, version):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        current_id = run([*compose, "ps", "-q", "sideword"]).strip()
        if current_id:
            current = json.loads(run(["docker", "inspect", current_id]))[0]
            if (current["Image"] == image_id
                and current["State"].get("Health", {}).get("Status") == "healthy"
                and health_matches(version)):
                verify_private_routes()
                return
        time.sleep(2)
    raise DeploymentError(
        "Updated backend verification failed; keep the backup and previous image for recovery."
    )


def recover_database(replacement, compose, container_id, image_tag):
    if replacement.state["phase"] == "verified":
        state = replacement.state
        if replacement.identity(replacement.database) != state["current"]:
            raise DeploymentError("The verified database changed unexpectedly.")
        wait_for_backend(compose, state["activated_image"], state["activated_version"])
        managed_backups(replacement.path.parent / "deployment-backups", cleanup=True)
        replacement.finish()
        return
    try:
        old_image, old_version = replacement.state["image"], replacement.state["version"]
        run(["docker", "image", "inspect", old_image])
        container_id = run([*compose, "ps", "-a", "-q", "sideword"]).strip()
        if not container_id or "\n" in container_id:
            raise DeploymentError("Cannot identify the database writer for recovery.")
        stop_database_writers(compose, container_id, replacement.database, check_sqlite=False)
        if replacement.state["phase"] != "restored":
            replacement.restore()
        elif "" in replacement.state["files"]:
            if replacement.identity(replacement.database) != replacement.state["files"][""]:
                raise DeploymentError("The restored database changed unexpectedly.")
        run(["docker", "image", "tag", old_image, image_tag])
        if "" in replacement.state["files"]:
            run([*compose, "up", "-d", "--no-build", "--pull", "never", "--no-deps", "sideword"])
            wait_for_backend(compose, old_image, old_version)
        replacement.finish()
    except Exception as error:
        # A failed rollback must remain offline with its transaction record intact.
        run([*compose, "stop", "--timeout", "30", "sideword"])
        raise DeploymentError(
            "Database recovery could not be completed safely; the recovery state was retained."
        ) from error


def managed_backups(directory, *, added=None, cleanup=False):
    """Only an explicit ownership ledger authorizes automatic backup deletion."""
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
        or info.st_mode & 0o077):
        raise DeploymentError("The deployment backup directory must be private.")
    ledger = directory / "managed.json"
    entries = {}
    if ledger.exists() or ledger.is_symlink():
        private_file(ledger)
        entries = json.loads(ledger.read_text())
    for name, family in entries.items():
        if not re.fullmatch(r"pre-deploy-[0-9]{8}T[0-9]{12}Z\.sqlite3", name):
            raise DeploymentError("Unsafe managed backup inventory.")
        if (not isinstance(family, dict) or "" not in family
            or not set(family) <= set(SQLITE_SUFFIXES)):
            raise DeploymentError("Unsafe managed backup inventory.")
        for suffix in SQLITE_SUFFIXES:
            path = directory / (name + suffix)
            if path.exists() or path.is_symlink():
                private_file(path)
                if DatabaseReplacement.identity(path) != family.get(suffix):
                    raise DeploymentError("A managed backup changed unexpectedly.")
                if cleanup:
                    path.unlink()
                    sync_directory(directory)
    if added is not None:
        entries[added.name] = {
            suffix: DatabaseReplacement.identity(Path(str(added) + suffix))
            for suffix in SQLITE_SUFFIXES if Path(str(added) + suffix).exists()
        }
    if cleanup:
        ledger.unlink(missing_ok=True)
        sync_directory(directory)
    elif added is not None:
        write_deployment_state(ledger, entries)


def initialize_database(database, image_id, user=None):
    if any(character in str(database.parent) for character in ",\n\r"):
        raise DeploymentError("Unsupported persistent database mount.")
    command = [
        "docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true", "--pids-limit", "64", "--memory", "128m",
        "--mount", f"type=bind,src={database.parent},dst=/sideword-init",
        "--env", "SIDEWORD_SECRET_KEY", "--env", "SIDEWORD_DB_PATH",
        "--entrypoint", "python",
    ]
    if user:
        command.extend(["--user", str(user)])
    run([*command, image_id, "-c",
         "import asyncio, os; os.umask(0o077)\n"
         "from app.db import init_db, engine\n"
         "async def initialize():\n"
         "    await init_db()\n"
         "    await engine.dispose()\n"
         "asyncio.run(initialize())"],
        env={**os.environ, "SIDEWORD_SECRET_KEY": os.urandom(32).hex(),
             "SIDEWORD_DB_PATH": "/sideword-init/" + database.name})
    info = database.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise DeploymentError("The initialized database must have private permissions.")
    with database.open("rb") as stream:
        os.fsync(stream.fileno())
    sync_directory(database.parent)


def verify_persistent_mount(service, configuration, previous, database):
    mounts = [mount for mount in previous["Mounts"]
              if database.is_relative_to(Path(mount["Source"]))]
    if not mounts:
        raise DeploymentError("Database replacement requires unchanged persistent storage.")
    actual = max(mounts, key=lambda mount: len(mount["Source"]))
    desired = next((mount for mount in service.get("volumes", [])
                    if mount.get("target") == actual["Destination"]), None)
    if not desired or desired.get("read_only") or desired.get("type") != actual["Type"]:
        raise DeploymentError("Database replacement requires unchanged persistent storage.")
    source = desired.get("source")
    if actual["Type"] == "volume":
        source = configuration.get("volumes", {}).get(source, {}).get("name", source)
        expected = actual.get("Name")
    else:
        expected = actual["Source"]
    if source != expected:
        raise DeploymentError("Database replacement requires unchanged persistent storage.")


def verify_database_schema(database, expected):
    if not isinstance(expected, dict) or not expected:
        raise DeploymentError("The checked image did not provide its database schema.")
    for name, columns in expected.items():
        if (
            not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
            or not isinstance(columns, list) or not columns
            or not all(isinstance(column, str) for column in columns)
        ):
            raise DeploymentError("The checked image provided an invalid database schema.")
    if not database.exists():
        return
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        for name, columns in expected.items():
            if name in tables:
                actual = {row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')}
                if actual != set(columns):
                    raise DeploymentError(
                        "The existing database schema is incompatible with the checked image. "
                        "Configure a fresh persistent database and preserve the existing "
                        "database separately."
                    )


def backup_database(source, destination):
    """Copy live configuration/metadata, never queue rows or database free pages."""
    source = source.resolve()
    # Exclusive creation prevents overwriting a backup or following an existing symlink.
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
            original.execute("PRAGMA temp_store=MEMORY")
            original.execute("BEGIN")  # One consistent read snapshot, including during writes.
            with closing(sqlite3.connect(destination)) as backup:
                backup.execute("PRAGMA journal_mode=TRUNCATE")
                backup.execute("PRAGMA secure_delete=ON")
                backup.execute("PRAGMA temp_store=MEMORY")
                # Stream logical rows. Never write queued ciphertext into the destination,
                # even temporarily; a page-level backup would also copy deleted remnants.
                for statement in original.iterdump():
                    if statement.startswith('INSERT INTO "pending_messages"'):
                        continue
                    backup.execute(statement)
                if backup.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise DeploymentError("Backup validation failed.")
    except BaseException:
        destination.unlink(missing_ok=True)
        Path(str(destination) + "-journal").unlink(missing_ok=True)
        raise


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
        configuration_digest = hashlib.sha256(
            json.dumps(configuration, sort_keys=True).encode()
        ).hexdigest()
        service = configuration.get("services", {}).get("sideword", {})
        image_tag = service.get("image")
        if not image_tag or image_tag.startswith("-") or "@" in image_tag:
            raise DeploymentError("The sideword service requires a dedicated mutable image tag.")
        healthcheck = service.get("healthcheck", {})
        if healthcheck.get("disable") or healthcheck.get("test") == ["NONE"]:
            raise DeploymentError("The private Compose health check must not be disabled.")
        if any(re.search(r"\bcurl\b", str(part)) for part in healthcheck.get("test", [])):
            raise DeploymentError(
                "Remove the private Compose curl health check; "
                "inherit the checked image's Python probe."
            )
        container_id = run([*compose, "ps", "-a", "-q", "sideword"]).strip()
        if "\n" in container_id:
            raise DeploymentError("At most one sideword container is required.")
        first_install = not container_id
        previous = json.loads(run(["docker", "inspect", container_id]))[0] if container_id else None
        if previous:
            database = database_path(previous, allow_missing=True)
            replacement = DatabaseReplacement(args.compose, database)
            if replacement.state:
                if replacement.state.get("configuration") != configuration_digest:
                    raise DeploymentError(
                        "Deployment configuration changed during database recovery."
                    )
                recover_database(replacement, compose, container_id, image_tag)
                container_id = run([*compose, "ps", "-a", "-q", "sideword"]).strip()
                previous = json.loads(run(["docker", "inspect", container_id]))[0]
            configured_image = json.loads(run(["docker", "image", "inspect", image_tag]))[0]
            if configured_image["Id"] != previous["Image"]:
                raise DeploymentError(
                    "The configured image tag must still identify the running backend."
                )
        elif (args.compose.parent / "database-deployment.json").exists():
            raise DeploymentError(
                "A recovery transaction requires its persistent service container."
            )
        backup_dir = args.compose.parent / "deployment-backups"
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        if backup_dir.is_symlink() or backup_dir.stat().st_mode & 0o077:
            raise DeploymentError("The deployment backup directory must be private.")
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        rollback_tag = f"sideword-rollback:{stamp.lower()}"
        backup = backup_dir / f"pre-deploy-{stamp}.sqlite3"
        pull_image(args)
        image = json.loads(run(["docker", "image", "inspect", args.image_ref]))[0]
        # Classic Docker uses the config digest as id; containerd uses the manifest digest.
        # Publication normalizes CI's ID to the config digest; pull_image rechecks that binding.
        host_image_id = image["Id"]
        if (
            host_image_id not in {args.image_id, args.image_ref.split("@")[1]}
            or args.image_ref not in image.get("RepoDigests", [])
            or (image["Config"].get("Labels") or {}).get("org.opencontainers.image.revision")
            != args.sha
        ):
            raise DeploymentError("The pulled image does not match the checked commit.")
        probe = json.loads(run(
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
                "--env",
                "SIDEWORD_SECRET_KEY",
                host_image_id,
                "-c",
                "import json; from app.version import __version__; "
                "from app.db import Base; from app import models; "
                "print(json.dumps({'version': __version__, 'tables': "
                "{table.name: list(table.columns.keys()) "
                "for table in Base.metadata.sorted_tables}}))",
            ],
            env={**os.environ, "SIDEWORD_SECRET_KEY": os.urandom(32).hex()},
        ))
        if probe.get("version") != args.version:
            raise DeploymentError("The checked image reports an unexpected backend version.")
        if first_install:
            run(["docker", "image", "tag", host_image_id, image_tag])
            run([
                *compose, "up", "--no-start", "--no-build", "--pull", "never", "--no-deps",
                "sideword",
            ])
            container_id = run([*compose, "ps", "-a", "-q", "sideword"]).strip()
            if not container_id or "\n" in container_id:
                raise DeploymentError("Cannot identify the persistent service container.")
            previous = json.loads(run(["docker", "inspect", container_id]))[0]
            database = database_path(previous, allow_missing=True)
            if database.exists():
                raise DeploymentError("Existing unversioned database requires operator review.")
            installed_version = args.version
            replacement = DatabaseReplacement(args.compose, database)
        else:
            installed_version = run([
                "docker", "run", "--rm", "--network", "none", "--read-only",
                "--entrypoint", "python", previous["Image"], "-c",
                "from app.version import __version__; print(__version__)",
            ]).strip()
        installed, target = version_tuple(installed_version), version_tuple(args.version)
        if target < installed:
            raise DeploymentError("Deployment downgrades are not allowed.")
        reset = installed[0] == target[0] == 0 and installed != target
        environment = dict(
            value.split("=", 1) for value in image["Config"].get("Env", []) if "=" in value
        )
        environment.update(service.get("environment", {}))
        candidate_database = database_path({
            "Config": {"Env": [f"{key}={value}" for key, value in environment.items()
                               if value is not None]},
            "Mounts": previous["Mounts"],
        }, allow_missing=True)
        replace = reset or (not candidate_database.exists() and candidate_database == database)
        # Do not guess which database a changed Compose path or mount belongs to.
        if replace and candidate_database != database:
            raise DeploymentError("Database replacement requires unchanged persistent storage.")
        if replace:
            verify_persistent_mount(service, configuration, previous, database)
        verify_database_schema(
            candidate_database if not replace else replacement.directory / "absent",
            probe.get("tables"),
        )
        run(["docker", "image", "tag", previous["Image"], rollback_tag])
        if replace:
            stopped = False
            try:
                stop_database_writers(compose, container_id, database)
                stopped = True
                replacement.prepare(previous["Image"], installed_version, configuration_digest)
                initialize_database(database, host_image_id, service.get("user"))
                verify_database_schema(database, probe.get("tables"))
                run(["docker", "image", "tag", host_image_id, image_tag])
                run([
                    *compose, "up", "-d", "--no-build", "--pull", "never", "--no-deps", "sideword",
                ])
                wait_for_backend(compose, host_image_id, args.version)
            except Exception:
                if replacement.state:
                    recover_database(replacement, compose, container_id, image_tag)
                elif stopped and database.exists():
                    # Preparation did not move a file or create a transaction record.
                    run([
                        *compose, "up", "-d", "--no-build", "--pull", "never", "--no-deps",
                        "sideword",
                    ])
                    try:
                        wait_for_backend(compose, previous["Image"], installed_version)
                    except Exception:
                        run([*compose, "stop", "--timeout", "30", "sideword"])
                        raise
                raise
            # A cleanup failure must never roll back a verified database after partial deletion.
            replacement.verified(host_image_id, args.version)
            managed_backups(backup_dir, cleanup=True)
            replacement.finish()
            print("Checked backend deployment verified.")
            return
        try:
            run([*compose, "stop", "--timeout", "30", "sideword"])
            with stage("backup"):
                backup_database(database, backup)
                managed_backups(backup_dir, added=backup)
            run(["docker", "image", "tag", host_image_id, image_tag])
        except Exception:
            # No new application ran against the database; restarting the old image is safe.
            run(["docker", "image", "tag", previous["Image"], image_tag])
            run([*compose, "up", "-d", "--no-build", "--pull", "never", "--no-deps", "sideword"])
            raise
        try:
            run([*compose, "up", "-d", "--no-build", "--pull", "never", "--no-deps", "sideword"])
            wait_for_backend(compose, host_image_id, args.version)
            print("Checked backend deployment verified.")
        except Exception:
            # Keep an unverified application offline without overwriting its database.
            run([*compose, "stop", "--timeout", "30", "sideword"])
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    backup = commands.add_parser("backup", help="Back up metadata without queued content")
    backup.add_argument("--source", type=Path, required=True)
    backup.add_argument("--destination", type=Path, required=True)
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
            "backup": lambda: backup_database(args.source, args.destination),
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
