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


# ── The adoption workflow and its callers (feature 012) ────────────────────────────────────────────

WORKSPACE = ROOT.parent
_CALL = re.compile(r"idfkit-conformance/\.github/workflows/adopt-energyplus\.yml@(\S+)")


def _adopt_callers() -> list[Path]:
    """Every `adopt-energyplus.yml` in a sibling checkout. Empty where the workspace is not checked out."""
    return sorted(path for path in WORKSPACE.glob("*/.github/workflows/adopt-energyplus.yml") if path.parent.parent.parent != ROOT)


def caller_problems(text: str) -> list[str]:
    """What is wrong with one caller of the adoption workflow. Empty when it is well formed."""
    refs = _CALL.findall(text)
    if len(refs) != 1:
        return [f"calls adopt-energyplus.yml {len(refs)} time(s), expected 1"]
    problems = []
    if not re.fullmatch(r"governance-\d{4}\.\d+", refs[0]):
        problems.append(f"uses the reusable workflow at {refs[0]!r}, not at a governance tag")
    levels = re.findall(r"^\s*governance_level:\s*(\S+)", text, flags=re.MULTILINE)
    if levels != [refs[0]]:
        problems.append(f"passes governance_level {levels}, which is not the tag on uses: ({refs[0]})")
    if "run-name: Adopt EnergyPlus ${{ inputs.release }}" not in text:
        problems.append("does not set run-name, so an evaluation cannot find the run for a release")
    if re.search(r"id-token|packages:\s*write|deployments:", text):
        problems.append("grants a permission that publishing or deploying needs")
    return problems


GOOD_CALLER = """\
name: adopt-energyplus
run-name: Adopt EnergyPlus ${{ inputs.release }}
jobs:
  adopt:
    uses: idfkit/idfkit-conformance/.github/workflows/adopt-energyplus.yml@governance-2026.23
    with:
      project: idfkit-mcp
      governance_level: governance-2026.23
"""


def test_a_well_formed_caller_has_no_problems() -> None:
    assert caller_problems(GOOD_CALLER) == []


def test_a_caller_at_a_branch_or_with_two_different_pins_is_rejected() -> None:
    assert "not at a governance tag" in caller_problems(GOOD_CALLER.replace("governance-2026.23", "main"))[0]
    assert "is not the tag on uses" in caller_problems(GOOD_CALLER.replace("governance_level: governance-2026.23", "governance_level: governance-2026.22"))[0]
    assert caller_problems(GOOD_CALLER.replace("run-name", "name-of-run"))
    assert caller_problems(GOOD_CALLER + "permissions:\n  id-token: write\n")


def test_every_caller_in_the_workspace_pins_one_governance_tag() -> None:
    for caller in _adopt_callers():
        assert caller_problems(caller.read_text()) == [], f"{caller.parent.parent.parent.name}: {caller_problems(caller.read_text())}"


def test_the_adoption_workflow_requests_nothing_that_publishes_or_deploys() -> None:
    text = (ROOT / ".github" / "workflows" / "adopt-energyplus.yml").read_text()
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "id-token" not in body and "packages:" not in body and "deployments:" not in body
    assert "gh pr merge" not in body and "--auto" not in body.replace("set-head origin --auto", "")
    assert r"^governance-[0-9]{4}\.[0-9]+$" in body and "refs/tags/${{ inputs.governance_level }}" in body
