"""Extract the current backend release's changelog section for GitHub."""

import argparse
import re
from datetime import date
from pathlib import Path

from app.version import __version__


def release_notes(changelog: str, version: str) -> str:
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        raise ValueError("Backend version must use MAJOR.MINOR.PATCH without leading zeroes")

    sections = re.split(r"(?m)^## ", changelog)[1:]
    matches = [
        section for section in sections if section.partition("\n")[0].split(" ", 1)[0] == version
    ]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one changelog section for {version}")

    heading, _, body = matches[0].partition("\n")
    expected = re.fullmatch(rf"{re.escape(version)} — ([0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})", heading)
    if expected is None:
        raise ValueError("Current release heading must include an ISO date")
    date.fromisoformat(expected[1])
    bullets = [line for line in body.splitlines() if line.strip()]
    if not bullets or any(not line.startswith("- ") or not line[2:].strip() for line in bullets):
        raise ValueError("Current release notes must contain nonempty single-line bullets")
    return f"## {heading}\n\n" + "\n".join(bullets) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    changelog = Path(__file__).resolve().parents[1] / "CHANGELOG.md"
    try:
        notes = release_notes(changelog.read_text(encoding="utf-8"), __version__)
    except ValueError as exc:
        parser.error(str(exc))
    args.output.write_text(notes, encoding="utf-8")
    print(f"v{__version__}")


if __name__ == "__main__":
    main()
