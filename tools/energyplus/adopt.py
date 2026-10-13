"""Adopt one EnergyPlus release inside one project's working tree.

contracts/adopt-workflow.md, steps 3 to 8. The program changes files and runs the commands the
roster declares. It never pushes, opens a pull request, merges, publishes or deploys: the reusable
workflow around it pushes the branch and opens the pull request, and nothing else is done by a tool.

    3  stop when the default branch's own checks are failing, and report it as pre-existing
    4  stop when the upstream source differs from what the tracking issue recorded
    5  run each step's command, in roster order, for the steps that do not already hold
    6  rewrite every declared statement to the release, or none of them
    7  run the project's gate
    8  leave an adoption branch alone when a person has committed to it

The only commands executed are `step.run` and `project.gate`. A change to a path under a `pin` fails
the run (FR-015). When everything already holds, nothing is written (FR-016).

    python -m tools.energyplus.adopt --project ID --release X.Y.Z --tree . --body body.md
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from sweep_consumers import Source, Unreachable

from .evaluate import CONTENT_KINDS, CheckOutcome, Evaluator, GhHub, HttpPackages, Hub, Packages, Recorded, branch_for, consumed, source_changes
from .roster import Project, PublishedCarries, Release, Roster, Step, StepClass, Target, members
from .scan import GitHubArchive, glob_pattern
from .upstream import GhReleases, UpstreamRelease, find, releases

#: The identities whose commits on an adoption branch are the workflow's own.
WORKFLOW_AUTHORS = ("github-actions[bot]", "idfkit-bot[bot]")


class Result(Enum):
    PROPOSED = "proposed"
    NOTHING_TO_DO = "nothing-to-do"
    WAITING = "waiting"
    LAG = "lag"
    BRANCH_EDITED = "branch-edited"
    PRE_EXISTING = "pre-existing"
    SOURCE_CHANGED = "source-changed"
    STATEMENT_MISMATCH = "statement-mismatch"
    STEP_FAILED = "step-failed"
    PIN_TOUCHED = "pin-touched"
    GATE_FAILED = "gate-failed"

    def failed(self) -> bool:
        return self in (Result.PRE_EXISTING, Result.SOURCE_CHANGED, Result.STATEMENT_MISMATCH, Result.STEP_FAILED, Result.PIN_TOUCHED, Result.GATE_FAILED)


@dataclass(frozen=True, slots=True)
class AdoptionResult:
    result: Result
    detail: str
    performed: tuple[str, ...] = ()
    present: tuple[str, ...] = ()
    waiting: tuple[str, ...] = ()
    statements_rewritten: int = 0
    files_changed: tuple[str, ...] = ()
    #: For `BRANCH_EDITED`: the files where this generation differs from the branch a person edited.
    differing: tuple[str, ...] = ()
    log: str = ""


@dataclass(frozen=True, slots=True)
class Ran:
    returncode: int
    output: str


class Runner(Protocol):
    """Executes a roster command in the tree. The only way this program runs anything a roster declares."""

    def run(self, command: str, cwd: Path) -> Ran: ...


class ShellRunner:
    def run(self, command: str, cwd: Path) -> Ran:
        done = subprocess.run(["bash", "-euo", "pipefail", "-c", command], cwd=cwd, capture_output=True, text=True, check=False)  # noqa: S603, S607
        return Ran(done.returncode, (done.stdout + done.stderr)[-4000:])


class TreeSource:
    """The project's working tree for its own repository, and another source for every other one."""

    def __init__(self, repository: str, tree: Path, others: Source) -> None:
        self.repository = repository
        self.tree = tree
        self.others = others

    def read(self, repository: str, path: str) -> str:
        if repository != self.repository:
            return self.others.read(repository, path)
        target = self.tree / path
        if not target.is_file():
            raise FileNotFoundError(path)
        return target.read_text(encoding="utf-8")

    def files(self, repository: str) -> list[str]:
        if repository != self.repository:
            return self.others.files(repository)
        return [name for name in _git(self.tree, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0") if name]

    def repositories(self) -> list[str]:
        return []


def _git(tree: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(tree), *args], capture_output=True, text=True, check=False)  # noqa: S603, S607
    return done.stdout if done.returncode == 0 else ""


def changed_files(tree: Path) -> tuple[str, ...]:
    listing = _git(tree, "status", "--porcelain", "-z", "--untracked-files=all")
    return tuple(sorted(entry[3:] for entry in listing.split("\0") if entry))


# ── Statements ─────────────────────────────────────────────────────────────────────────────────────


class StatementMismatch(ValueError):
    """A declared statement does not match its file the declared number of times."""


def rewritten(project: Project, tree: Path, target: Target) -> dict[str, str]:
    """The new text of every file that holds a statement. Raises before anything is written.

    Every pattern is checked against the declared number of occurrences first, so a mismatch in the
    last statement leaves the first one unwritten too.
    """
    texts: dict[str, str] = {}
    for statement in project.statements:
        path = tree / statement.path
        if not path.is_file():
            raise StatementMismatch(f"{statement.path} does not exist")
        text = texts.get(statement.path, path.read_text(encoding="utf-8"))
        found = list(statement.compiled().finditer(text))
        if len(found) != statement.occurrences:
            raise StatementMismatch(f"{statement.path}: pattern {statement.pattern!r} matches {len(found)} time(s), declared {statement.occurrences}")
        for match in reversed(found):
            start, end = match.span("release")
            text = text[:start] + statement.form.rewrite(match.group("release"), target) + text[end:]
        texts[statement.path] = text
    return texts


def rewrite_statements(project: Project, tree: Path, target: Target) -> int:
    """Rewrite every declared statement. Returns the number of files that changed."""
    changed = 0
    for name, text in rewritten(project, tree, target).items():
        path = tree / name
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            changed += 1
    return changed


# ── The branch a person edited (FR-018) ────────────────────────────────────────────────────────────


def foreign_authors(tree: Path, branch: str, base: str) -> tuple[str, ...]:
    """The authors of commits on the remote adoption branch who are not the workflow."""
    names = _git(tree, "log", f"{base}..{branch}", "--format=%an").splitlines()
    return tuple(sorted({name for name in names if name and name not in WORKFLOW_AUTHORS}))


def differing_from(tree: Path, branch: str) -> tuple[str, ...]:
    """The files where the working tree differs from the branch."""
    tracked = _git(tree, "diff", "--name-only", branch).splitlines()
    untracked = _git(tree, "ls-files", "--others", "--exclude-standard").splitlines()
    return tuple(sorted({*tracked, *untracked}))


# ── The adoption ───────────────────────────────────────────────────────────────────────────────────


def _content(step: Step) -> bool:
    return any(isinstance(member, CONTENT_KINDS) for member in members(step.verify))


def adopt(
    roster: Roster,
    project: Project,
    release: Release,
    tree: Path,
    source: Source,
    hub: Hub,
    packages: Packages,
    upstream: UpstreamRelease | None,
    runner: Runner,
    *,
    recorded: Recorded | None = None,
    remote_branch: str | None = None,
    base: str = "origin/HEAD",
) -> AdoptionResult:
    if project.lag is not None:
        return AdoptionResult(Result.LAG, project.lag.stated())
    def fresh() -> Evaluator:
        # An evaluator keeps every answer it computed. A step changes the tree, so each step is
        # judged by an evaluator created after the previous step ran.
        return Evaluator(roster, release, TreeSource(project.repository, tree, source), hub, packages, upstream)

    evaluator = fresh()
    if evaluator.carries(project, None).holds:
        return AdoptionResult(Result.NOTHING_TO_DO, f"{project.id} already carries {release.full()}", present=tuple(step.id for step in project.steps))
    base_checks = hub.checks(project.repository, None, project.checks)
    if base_checks.outcome is CheckOutcome.FAILURE:
        return AdoptionResult(Result.PRE_EXISTING, f"the default branch's checks were failing before this run: {', '.join(base_checks.failing)}")
    changed = source_changes(recorded, upstream.commit if upstream else "", consumed(upstream))
    if changed:
        return AdoptionResult(Result.SOURCE_CHANGED, "; ".join(f"{change.what}: recorded {change.recorded}, now {change.live}" for change in changed))
    try:
        rewritten(project, tree, evaluator.target)
    except StatementMismatch as mismatch:
        return AdoptionResult(Result.STATEMENT_MISMATCH, str(mismatch))

    performed, present, waiting = [], [], []
    for step in project.steps:
        judge = fresh()
        holds = _content(step) and all(judge.holds(m, project).holds for m in members(step.verify) if isinstance(m, CONTENT_KINDS))
        if holds:
            present.append(step.id)
        elif step.run is None:
            # A step with no command is verified and not performed: a library level moved by the
            # project's own bump workflow, or a judgement a person makes on the pull request.
            if any(isinstance(member, PublishedCarries) for member in members(step.verify)):
                waiting.append(step.id)
        else:
            ran = runner.run(release.substitute(step.run), tree / project.workdir)
            if ran.returncode != 0:
                return AdoptionResult(Result.STEP_FAILED, f"step {step.id} exited {ran.returncode}", tuple(performed), tuple(present), log=ran.output)
            performed.append(step.id)
    if waiting:
        return AdoptionResult(Result.WAITING, f"waiting on: {', '.join(waiting)}. The project's own bump workflow moves a library level.", tuple(performed), tuple(present), tuple(waiting))
    try:
        statements = rewrite_statements(project, tree, evaluator.target)
    except StatementMismatch as mismatch:
        return AdoptionResult(Result.STATEMENT_MISMATCH, str(mismatch), tuple(performed), tuple(present))
    files = changed_files(tree)
    pins = [(pin.glob, glob_pattern(pin.glob)) for pin in (*roster.default_pins, *project.pins)]
    statement_paths = {statement.path for statement in project.statements}
    touched = [f"{name} (pin {glob})" for name in files for glob, pattern in pins if pattern.match(name) and name not in statement_paths]
    if touched:
        return AdoptionResult(Result.PIN_TOUCHED, f"the run changed a pinned path: {', '.join(touched)}", tuple(performed), tuple(present), files_changed=files)
    if not files:
        return AdoptionResult(Result.NOTHING_TO_DO, "no file changed", tuple(performed), tuple(present))
    if project.gate:
        gate = runner.run(project.gate, tree / project.workdir)
        if gate.returncode != 0:
            return AdoptionResult(Result.GATE_FAILED, f"the gate exited {gate.returncode}", tuple(performed), tuple(present), (), statements, files, log=gate.output)
    if remote_branch is not None and (people := foreign_authors(tree, remote_branch, base)):
        differing = differing_from(tree, remote_branch)
        detail = f"the branch holds commits by {', '.join(people)} and was left as it is"
        return AdoptionResult(Result.BRANCH_EDITED, detail, tuple(performed), tuple(present), (), statements, files, differing)
    return AdoptionResult(Result.PROPOSED, f"{len(files)} file(s) changed", tuple(performed), tuple(present), (), statements, files)


# ── The pull request body (contract section 5) ─────────────────────────────────────────────────────


def body(project: Project, release: Release, result: AdoptionResult, issue: str) -> str:
    lines = [f"Adopts EnergyPlus {release.full()} in {project.id}. Tracking issue: {issue or 'not stated'}", ""]
    lines += ["| Step | Class | In this pull request |", "| --- | --- | --- |"]
    for step in project.steps:
        if step.step_class is not StepClass.AUTOMATED:
            state = "to be accepted by a person"
        elif step.id in result.performed:
            state = "performed by the run"
        elif step.id in result.present:
            state = "already present"
        else:
            state = "verified by the project's checks on this pull request"
        lines.append(f"| {step.title} | {step.step_class.value} | {state} |")
    lines += ["", f"Statements rewritten in {result.statements_rewritten} file(s). {len(result.files_changed)} file(s) changed in total."]
    review = [step for step in project.steps if step.step_class is not StepClass.AUTOMATED]
    if review:
        lines += ["", "### To inspect before approving", ""]
        for step in review:
            prepared = f" Prepared: {step.prepares}" if step.prepares else ""
            lines.append(f"- **{step.title}** ({step.step_class.value}). {step.inspect}{prepared}")
        lines += ["", "This pull request stays a draft until those steps are accepted. An approving review is the acceptance."]
    if project.merge_effect:
        lines += ["", f"**{project.merge_effect}**"]
    if project.after_merge and not project.after_merge.startswith("None"):
        lines += ["", f"After the merge, a maintainer: {project.after_merge}"]
    lines += ["", "The run never merges, publishes, releases or deploys. Regenerate by deleting the branch and dispatching the workflow again."]
    return "\n".join(lines) + "\n"


def edited_comment(release: Release, result: AdoptionResult) -> str:
    listed = "\n".join(f"- `{name}`" for name in result.differing) or "- none"
    return (
        f"The adoption of EnergyPlus {release.full()} was generated again, and {result.detail}.\n\n"
        f"Files where the fresh generation differs from this branch:\n\n{listed}\n\n"
        "The fresh generation is attached to the run as an artifact. To take it, delete this branch or revert the commits "
        "made by a person, and dispatch the workflow again."
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--roster", type=Path, required=True)
    parser.add_argument("--tree", type=Path, default=Path.cwd())
    parser.add_argument("--issue", default="", help="the URL of the tracking issue")
    parser.add_argument("--body", type=Path, required=True, help="where to write the pull request body, or the comment for an edited branch")
    arguments = parser.parse_args(argv)
    roster = Roster.load(arguments.roster)
    project = roster.project(arguments.project)
    release = Release.parse(arguments.release)
    tree = arguments.tree.resolve()
    branch = f"origin/{branch_for(release)}"
    remote = branch if _git(tree, "rev-parse", "--verify", "--quiet", branch).strip() else None
    with tempfile.TemporaryDirectory() as scratch:
        try:
            upstream = find(releases(GhReleases(roster.upstream)), release)
            result = adopt(roster, project, release, tree, GitHubArchive(Path(scratch)), GhHub(), HttpPackages(), upstream, ShellRunner(), remote_branch=remote)
        except Unreachable as error:
            print(f"::error title=Roster or project unreadable::{error}")
            return 2
    arguments.body.write_text(edited_comment(release, result) if result.result is Result.BRANCH_EDITED else body(project, release, result, arguments.issue), encoding="utf-8")
    print(f"{project.id} {release.full()}: {result.result.value}: {result.detail}")
    if result.log:
        print(result.log)
    draft = any(step.step_class is not StepClass.AUTOMATED for step in project.steps)
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"result={result.result.value}\ndraft={'true' if draft else 'false'}\n")
    return 1 if result.result.failed() else 0


if __name__ == "__main__":
    sys.exit(main())
