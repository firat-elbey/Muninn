"""Publish bounded JUnit failures as GitHub Actions annotations."""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def _escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def report(path: Path) -> int:
    """Report failing case names and messages, excluding captured output."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        print("::warning::The test report is missing or invalid.")
        return 0
    count = 0
    for case in root.iter("testcase"):
        problems = case.findall("failure") + case.findall("error")
        if not problems:
            continue
        count += 1
        if count > 50:
            continue
        name = ".".join(filter(None, (case.get("classname"), case.get("name"))))[:200]
        problem = problems[0]
        message = (problem.get("message") or problem.text or "The test failed.")[:1000]
        print(f"::error::{_escape(name)}: {_escape(message)}")
    if count > 50:
        print(f"::warning::{count - 50} additional test failures are in the JUnit report.")
    return count


if __name__ == "__main__":
    report(Path(sys.argv[1]))
