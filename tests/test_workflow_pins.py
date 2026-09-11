"""No workflow here reads the consumer register from a branch (T012, FR-002).

The register is read at an immutable governance tag by everything except the sweep, which runs
beside the file. A workflow that checked this repository out at `main` to read it would make every
consumer's verdict change without any change landing in the consumer, which is exactly the unpinned
read FR-002 forbids and research R11 moved onto the `uses:` line to prevent.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
#: The workflows allowed to use the working tree, because they run beside the file rather than reading
#: it from somewhere else: the sweep reports on it, governance.yml validates it on a pull request, and
#: record-adoption.yml WRITES it, opening a pull request that goes through the same review as any edit.
BESIDE_THE_FILE = {"sweep-consumers.yml", "governance.yml", "record-adoption.yml"}


def _checkouts_of_this_repository(text: str) -> list[str]:
    """The `ref:` of every checkout step that names idfkit-conformance."""
    refs = []
    for block in re.split(r"\n\s*- (?:name:|uses:)", text):
        if "actions/checkout" in block and "idfkit/idfkit-conformance" in block:
            match = re.search(r"^\s*ref:\s*(\S+)", block, flags=re.MULTILINE)
            refs.append(match.group(1) if match else "")
    return refs


def test_there_are_workflows_to_check() -> None:
    assert {w.name for w in WORKFLOWS} >= {"check-consumer.yml", "rehearse.yml", "sweep-consumers.yml"}


def test_every_checkout_of_the_register_is_at_a_tag() -> None:
    for workflow in WORKFLOWS:
        for ref in _checkouts_of_this_repository(workflow.read_text()):
            assert ref.startswith("refs/tags/"), f"{workflow.name} checks out idfkit-conformance at {ref!r}, not at a tag"


def test_only_the_readers_beside_the_file_read_it_unpinned() -> None:
    for workflow in WORKFLOWS:
        text = workflow.read_text()
        if "consumers.toml" in text and workflow.name not in BESIDE_THE_FILE:
            assert "refs/tags/" in text, f"{workflow.name} reads consumers.toml without pinning a tag"


def test_no_workflow_calls_itself_at_a_branch() -> None:
    for workflow in WORKFLOWS:
        for ref in re.findall(r"idfkit-conformance/\.github/workflows/[\w-]+\.yml@(\S+)", workflow.read_text()):
            assert re.fullmatch(r"governance-\d{4}\.\d+", ref), f"{workflow.name} uses a reusable workflow at {ref!r}"


def test_the_self_check_refuses_a_branch_as_its_level() -> None:
    text = (ROOT / ".github" / "workflows" / "check-consumer.yml").read_text()
    assert r"^governance-[0-9]{4}\.[0-9]+$" in text
