"""Scan isolated build inputs and publish only allowlisted vulnerability metadata."""

import argparse
import html
import json
import os
import re
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

from scripts import deploy

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")
IMAGE_ID = r"sha256:[0-9a-f]{64}"
IMAGE_REFERENCE = (
    r"(?:[a-z0-9][a-z0-9.-]*(?::[0-9]+)?/)?"
    r"[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*@sha256:[0-9a-f]{64}"
)


def image_id(value):
    if not re.fullmatch(IMAGE_ID, value):
        raise ValueError("An immutable local image ID is required.")
    return value


def directories():
    temporary = Path(os.environ["RUNNER_TEMP"])
    raw, reports = temporary / "security-raw", temporary / "security-reports"
    for directory in (raw, reports):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return raw, reports


def summary(text):
    print(text, flush=True)
    if output := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(output).open("a") as stream:
            stream.write(text + "\n")


def prepare():
    reference = os.environ.get("SECURITY_IMAGE_REF", "")
    if reference:
        if not re.fullmatch(IMAGE_REFERENCE, reference):
            raise ValueError("SECURITY_IMAGE_REF must be a credential-free image@sha256 digest.")
        deploy.run(["docker", "pull", reference], timeout=900)
        inspected = json.loads(deploy.run(
            ["docker", "image", "inspect", reference],
        ))
        if not isinstance(inspected, list) or len(inspected) != 1:
            raise ValueError("The selected image could not be identified.")
        selected = image_id(inspected[0]["Id"])
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
            stream.write(f"image_id={selected}\n")
        summary("Scanning the configured immutable registry image; no deployment is performed.")
    else:
        sha = deploy.run(["git", "rev-parse", "HEAD"]).strip()
        deploy.package_image(Path(os.environ["RUNNER_TEMP"]) / "security-image", sha, retain=False)
        summary("Scanning a fresh build of main, not the deployed artifact. "
                "Resolved dependency ranges and base-image contents may differ.")


def text(value):
    if not isinstance(value, str) or len(value) > 500 or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid vulnerability metadata.")
    return value


def trivy_findings(payload, expected_image):
    if (
        payload.get("SchemaVersion") != 2
        or payload.get("ArtifactType") != "container_image"
        or payload.get("ArtifactName") != image_id(expected_image)
        or not isinstance(payload.get("Results"), list)
        or not payload["Results"]
    ):
        raise ValueError("Missing or mismatched Trivy image report.")
    if not any(result.get("Class") == "os-pkgs" for result in payload["Results"]) or not any(
        result.get("Type") == "python-pkg" for result in payload["Results"]
    ):
        raise ValueError("Trivy did not identify both OS and Python packages.")
    findings = []
    for result in payload["Results"]:
        vulnerabilities = result.get("Vulnerabilities")
        if vulnerabilities is None:
            vulnerabilities = []
        if not isinstance(vulnerabilities, list):
            raise ValueError("Invalid Trivy vulnerability list.")
        for item in vulnerabilities:
            severity = text(item["Severity"])
            if severity not in SEVERITIES:
                raise ValueError("Unknown Trivy severity.")
            findings.append({
                "package": text(item["PkgName"]),
                "installed_version": text(item["InstalledVersion"]),
                "severity": severity,
                "vulnerability_id": text(item["VulnerabilityID"]),
                "fixed_version": text(item.get("FixedVersion", "")),
            })
    return findings


def python_findings(payload):
    if not isinstance(payload.get("dependencies"), list) or not payload["dependencies"]:
        raise ValueError("Missing Python dependency inventory.")
    findings = []
    for dependency in payload["dependencies"]:
        text(dependency["name"])
        text(dependency["version"])
        if dependency.get("skip_reason") or not isinstance(dependency.get("vulns"), list):
            raise ValueError("Incomplete Python dependency audit.")
        for item in dependency["vulns"]:
            findings.append({
                "package": text(dependency["name"]),
                "installed_version": text(dependency["version"]),
                # pip-audit does not supply severity; every finding blocks this check.
                "severity": "UNKNOWN",
                "vulnerability_id": text(item["id"]),
                "fixed_version": ", ".join(text(version) for version in item["fix_versions"]),
            })
    return findings


def cell(value):
    return html.escape(value or "—").replace("|", "&#124;").replace("`", "&#96;")


def publish(scanner, findings, failed, reports, *, selected_image=""):
    blocked = bool(findings) if scanner.startswith("pip-audit") else any(
        item["severity"] in {"HIGH", "CRITICAL"} for item in findings
    )
    status = "error" if failed else "blocked" if blocked else "passed"
    report = {
        "scanner": scanner,
        "status": status,
        "image_id": selected_image if re.fullmatch(IMAGE_ID, selected_image) else "",
        "vulnerabilities": findings,
    }
    # Raw Trivy reports include image configuration/environment; never upload them.
    (reports / f"{scanner}.json").write_text(json.dumps(report, indent=2) + "\n")
    lines = [f"### {scanner}: {status}"]
    if failed:
        lines.append("Scanner execution or report validation failed; this scan cannot pass.")
    counts = {severity: sum(item["severity"] == severity for item in findings)
              for severity in SEVERITIES}
    lines.append(" · ".join(f"{severity}: {count}" for severity, count in counts.items()))
    if findings:
        if scanner.startswith("codeql-"):
            headers = "| Location | Severity | Rule |"
            columns = ("location", "severity", "rule_id")
            ordering = ("location", "rule_id")
        else:
            headers = "| Package | Installed | Severity | Vulnerability | Fixed |"
            columns = (
                "package", "installed_version", "severity", "vulnerability_id", "fixed_version",
            )
            ordering = ("package", "vulnerability_id")
        lines.extend([
            "", headers, "| " + " | ".join("---" for _ in columns) + " |",
        ])
        ordered = sorted(findings, key=lambda item: (
            SEVERITIES.index(item["severity"]), *(item[key] for key in ordering),
        ))
        for item in ordered[:50]:
            lines.append("| " + " | ".join(cell(item[key]) for key in columns) + " |")
        if len(findings) > 50:
            lines.append("Showing the first 50 findings; the JSON artifact contains all findings.")
    summary("\n".join(lines) + "\n")
    return int(failed or blocked)


def report(selected_image):
    raw, reports = directories()
    findings = []
    failed = os.environ.get("TRIVY_OUTCOME") != "success"
    try:
        findings = trivy_findings(json.loads((raw / "trivy.json").read_text()), selected_image)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        failed = True
    return publish("trivy", findings, failed, reports, selected_image=selected_image)


def codeql_findings(payload):
    if (
        payload.get("version") != "2.1.0"
        or not isinstance(payload.get("runs"), list) or not payload["runs"]
    ):
        raise ValueError("Missing CodeQL SARIF runs.")
    findings = []
    for run in payload["runs"]:
        driver = run["tool"]["driver"]
        if driver["name"] != "CodeQL" or not isinstance(run.get("results"), list):
            raise ValueError("Incomplete CodeQL analysis.")
        for invocation in run.get("invocations", []):
            if invocation.get("executionSuccessful") is not True:
                raise ValueError("CodeQL execution failed.")
        rules = {}
        for component in [driver, *run["tool"].get("extensions", [])]:
            for rule in component.get("rules", []):
                identity = text(rule["id"])
                if identity in rules:
                    raise ValueError("Ambiguous CodeQL rule metadata.")
                rules[identity] = rule
        if not rules:
            raise ValueError("Missing CodeQL query inventory.")
        for result in run["results"]:
            identity = text(result["ruleId"])
            rule = rules[identity]
            properties = rule.get("properties", {})
            score = properties.get("security-severity")
            if score is not None:
                if not isinstance(score, str) or not re.fullmatch(
                    r"(?:[0-9](?:\.[0-9]+)?|10(?:\.0+)?)", score,
                ):
                    raise ValueError("Invalid CodeQL security severity.")
                rating = Decimal(score)
                if rating >= 9:
                    severity = "CRITICAL"
                elif rating >= 7:
                    severity = "HIGH"
                elif rating >= 4:
                    severity = "MEDIUM"
                else:
                    severity = "LOW"
            else:
                if "security" in properties.get("tags", []):
                    raise ValueError("Missing CodeQL security severity.")
                level = result.get(
                    "level", rule.get("defaultConfiguration", {}).get("level", "warning"),
                )
                severity = {
                    "error": "HIGH", "warning": "MEDIUM", "note": "LOW", "none": "LOW",
                }[level]
            locations = result.get("locations", [])
            location = ""
            if locations:
                physical = locations[0]["physicalLocation"]
                uri = text(physical["artifactLocation"]["uri"])
                # Publish only repository-relative paths, never source snippets or absolute URIs.
                if uri.startswith("/") or ":" in uri or ".." in uri.split("/"):
                    raise ValueError("CodeQL location is not repository-relative.")
                line = physical.get("region", {}).get("startLine", 1)
                if type(line) is not int or line < 1:
                    raise ValueError("Invalid CodeQL source line.")
                location = f"{uri}:{line}"
            findings.append({"rule_id": identity, "severity": severity, "location": location})
    return findings


def codeql():
    _, reports = directories()
    language = os.environ.get("CODEQL_LANGUAGE")
    if language not in {"python", "javascript-typescript"}:
        raise ValueError("Unsupported CodeQL language.")
    findings = []
    failed = os.environ.get("CODEQL_OUTCOME") != "success"
    try:
        files = list((Path(os.environ["RUNNER_TEMP"]) / "codeql-results").glob("*.sarif"))
        if len(files) != 1:
            raise ValueError("Missing or ambiguous CodeQL report.")
        findings = codeql_findings(json.loads(files[0].read_text()))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        failed = True
    return publish(f"codeql-{language}", findings, failed, reports)


def audit(selected_image):
    raw, reports = directories()
    findings, failed = [], False
    try:
        expected = None
        destination = raw / "pip-audit.json"
        destination.unlink(missing_ok=True)
        command = [sys.executable, "-m", "pip_audit", "--strict", "--format=json",
                   "--output", str(destination)]
        if selected_image:
            inventory = json.loads(deploy.run([
                "docker", "run", "--rm", "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--entrypoint", "python", image_id(selected_image),
                "-c", "import importlib.metadata, json; "
                "print(json.dumps([{'name': d.metadata['Name'], 'version': d.version} "
                "for d in importlib.metadata.distributions()]))",
            ]))
            if not isinstance(inventory, list) or not inventory:
                raise ValueError("Missing image package inventory.")
            pins = []
            expected = set()
            for package in inventory:
                if (
                    not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", package["name"])
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*", package["version"])
                ):
                    raise ValueError("Invalid image package inventory.")
                identity = (re.sub(r"[-_.]+", "-", package["name"]).lower(), package["version"])
                if identity in expected:
                    raise ValueError("Duplicate image package inventory entry.")
                expected.add(identity)
                pins.append(f"{package['name']}=={package['version']}")
            requirements = raw / "image-requirements.txt"
            requirements.write_text("\n".join(pins) + "\n")
            command.extend(["--no-deps", "--disable-pip", "-r", str(requirements)])
        result = subprocess.run(command, capture_output=True, timeout=600)
        payload = json.loads(destination.read_text())
        findings = python_findings(payload)
        if expected is not None:
            actual = [(re.sub(r"[-_.]+", "-", dependency["name"]).lower(), dependency["version"])
                      for dependency in payload["dependencies"]]
            if set(actual) != expected or len(actual) != len(expected):
                raise ValueError("Python audit does not cover the exact image inventory.")
        # Exit 1 means known vulnerabilities only when a complete report confirms them.
        failed = result.returncode != (1 if findings else 0)
    except (OSError, ValueError, KeyError, TypeError, AttributeError,
            subprocess.TimeoutExpired, deploy.DeploymentError):
        failed = True
    scanner = "pip-audit-image" if selected_image else "pip-audit"
    return publish(scanner, findings, failed, reports, selected_image=selected_image)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "init", "report", "audit", "codeql"))
    parser.add_argument("--image", default="")
    args = parser.parse_args()
    try:
        if args.operation == "prepare":
            prepare()
        elif args.operation == "init":
            image_id(args.image)
            raw, _ = directories()
            (raw / "trivy.json").unlink(missing_ok=True)
        elif args.operation == "report":
            return report(args.image)
        elif args.operation == "codeql":
            return codeql()
        else:
            return audit(args.image)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, deploy.DeploymentError):
        print("::error::Security scan preparation failed; no successful scan is established.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
