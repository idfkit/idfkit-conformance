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

from .checklist import recorded_source
from .evaluate import CONTENT_KINDS, CheckOutcome, Evaluator, GhHub, HttpPackages, Hub, Packages, Recorded, branch_for, consumed, source_changes
from .roster import Project, Release, Roster, StatementsCurrent, StepClass, Target, members
from .scan import GitHubArchive, glob_pattern
from .upstream import GhReleases, UpstreamError, UpstreamRelease, find, releases

#: The identities whose commits on an adoption branch are the workflow's own.
WORKFLOW_AUTHORS = ("github-actions[bot]", "idfkit-bot[bot]")


class Result(Enum):
    PROPOSED = "proposed"
    NOTHING_TO_DO = "nothing-to-do"
    WAITING = "waiting"
    LAG = "lag"
    BRANCH_EDITED = "branch-edited"
    NOT_OPEN = "not-open"
    NOT_NEWEST = "not-newest"
    PRE_EXISTING = "pre-existing"
    SOURCE_CHANGED = "source-changed"
    STATEMENT_MISMATCH = "statement-mismatch"
    STEP_FAILED = "step-failed"
    PIN_TOUCHED = "pin-touched"
    GATE_FAILED = "gate-failed"

    def failed(self) -> bool:
        return self in (Result.NOT_NEWEST, Result.PRE_EXISTING, Result.SOURCE_CHANGED, Result.STATEMENT_MISMATCH, Result.STEP_FAILED, Result.PIN_TOUCHED, Result.GATE_FAILED)


@dataclass(frozen=True, slots=True)
class Prepared:
    """The output of the command of a step that a person accepts."""

    step: str
    output: str


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
    #: What the commands of reviewed and manual steps printed, for the pull request body.
    prepared: tuple[Prepared, ...] = ()


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
    # The watch dispatches a project only when these two hold. A person who dispatches by hand is
    # held to the same conditions, so that a run cannot propose a release out of order.
    if release < evaluator.target.release:
        newest = evaluator.target.release.full()
        return AdoptionResult(Result.NOT_NEWEST, f"{release.full()} is older than {newest}, the newest supported release, and {project.id} does not carry it. An adoption adds the newest release only.")
    opened, awaited = evaluator.opening_holds(project)
    if not opened:
        return AdoptionResult(Result.NOT_OPEN, f"{project.id} does not open yet: waiting on {awaited}")
    base_checks = hub.checks(project.repository, None, project.checks)
    if base_checks.outcome is CheckOutcome.FAILURE:
        return AdoptionResult(Result.PRE_EXISTING, f"the default branch's checks were failing before this run: {', '.join(base_checks.failing)}")
    changed = source_changes(recorded, upstream.commit if upstream else "", consumed(upstream, roster.ubuntu_builds()))
    if changed:
        return AdoptionResult(Result.SOURCE_CHANGED, "; ".join(f"{change.what}: recorded {change.recorded}, now {change.live}" for change in changed))
    # Statements are written before any command runs, because a command may read a file that holds
    # one: a generator whose templates state the release would otherwise bake the previous release.
    try:
        statements = rewrite_statements(project, tree, evaluator.target)
    except StatementMismatch as mismatch:
        return AdoptionResult(Result.STATEMENT_MISMATCH, str(mismatch))

    performed, present, waiting, prepared = [], [], [], []
    for step in project.steps:
        judge = fresh()
        content = [member for member in members(step.verify) if isinstance(member, CONTENT_KINDS)]
        holds = bool(content) and all(judge.holds(member, project).holds for member in content)
        if holds:
            written = statements > 0 and any(isinstance(member, StatementsCurrent) for member in content)
            (performed if written else present).append(step.id)
        elif step.run is None:
            if content:
                # Content that this run has no command for is moved elsewhere: a library level by the
                # project's own bump workflow, a vendored artifact by the workflow that vendors it.
                # The run stops here. A later command would run on a tree that lacks what it needs,
                # and its failure would hide that the project is only waiting.
                waiting.append(step.id)
                break
            # No content and no command: a judgement a person makes on the pull request.
        else:
            ran = runner.run(release.substitute(step.run), tree / project.workdir)
            if ran.returncode != 0:
                return AdoptionResult(Result.STEP_FAILED, f"step {step.id} exited {ran.returncode}", tuple(performed), tuple(present), log=ran.output)
            performed.append(step.id)
            if step.step_class is not StepClass.AUTOMATED and ran.output.strip():
                prepared.append(Prepared(step.id, ran.output))
    if waiting:
        detail = f"waiting on: {', '.join(waiting)}. No command of the adoption produces it: another workflow of the project, or a person, moves it. No later step was run."
        return AdoptionResult(Result.WAITING, detail, tuple(performed), tuple(present), tuple(waiting))
    try:
        # A command may have regenerated a file that holds a statement.
        statements += rewrite_statements(project, tree, evaluator.target)
    except StatementMismatch as mismatch:
        return AdoptionResult(Result.STATEMENT_MISMATCH, str(mismatch), tuple(performed), tuple(present))
    files = changed_files(tree)
    pins = [(pin.glob, glob_pattern(pin.glob)) for pin in (*roster.default_pins, *project.pins)]
    # A statement and a generated file are declared to change at an adoption. Where a pin's glob also
    # covers one, the narrower declaration decides.
    declared = [glob_pattern(generated.glob) for generated in project.generated]
    statement_paths = {statement.path for statement in project.statements}
    touched = [
        f"{name} (pin {glob})"
        for name in files
        for glob, pattern in pins
        if pattern.match(name) and name not in statement_paths and not any(allowed.match(name) for allowed in declared)
    ]
    if touched:
        return AdoptionResult(Result.PIN_TOUCHED, f"the run changed a pinned path: {', '.join(touched)}", tuple(performed), tuple(present), files_changed=files)
    if not files:
        return AdoptionResult(Result.NOTHING_TO_DO, "no file changed", tuple(performed), tuple(present))
    if project.gate:
        # The gate judges the tree as the commit will hold it. A check that compares generated files
        # with `git diff --exit-code` reads the index, and fails on any tree that is not staged.
        _git(tree, "add", "--all")
        gate = runner.run(project.gate, tree / project.workdir)
        if gate.returncode != 0:
            return AdoptionResult(Result.GATE_FAILED, f"the gate exited {gate.returncode}", tuple(performed), tuple(present), (), statements, files, log=gate.output)
    if remote_branch is not None and (people := foreign_authors(tree, remote_branch, base)):
        differing = differing_from(tree, remote_branch)
        detail = f"the branch holds commits by {', '.join(people)} and was left as it is"
        return AdoptionResult(Result.BRANCH_EDITED, detail, tuple(performed), tuple(present), (), statements, files, differing, prepared=tuple(prepared))
    return AdoptionResult(Result.PROPOSED, f"{len(files)} file(s) changed", tuple(performed), tuple(present), (), statements, files, prepared=tuple(prepared))


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
        titles = {step.id: step.title for step in project.steps}
        for item in result.prepared:
            lines += ["", f"<details><summary>What the run printed for: {titles[item.step]}</summary>", "", "```text", item.output.strip().replace("```", "'''"), "```", "", "</details>"]
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


def recorded_at(issue: str) -> Recorded | None:
    """The source identity the tracking issue recorded at the first evaluation, or `None` without an issue."""
    if not issue:
        return None
    done = subprocess.run(["gh", "issue", "view", issue, "--json", "body", "--jq", ".body"], capture_output=True, text=True, check=False)  # noqa: S603, S607
    if done.returncode != 0:
        raise Unreachable(f"the tracking issue {issue} could not be read: {done.stderr.strip() or 'gh issue view failed'}")
    return recorded_source(done.stdout)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--roster", type=Path, required=True)
    parser.add_argument("--tree", type=Path, default=Path.cwd())
    parser.add_argument("--issue", default="", help="the URL of the tracking issue")
    parser.add_argument("--body", type=Path, help="where to write the pull request body, or the comment for an edited branch")
    parser.add_argument("--describe", action="store_true", help="state what the project's commands need, and adopt nothing")
    arguments = parser.parse_args(argv)
    roster = Roster.load(arguments.roster)
    project = roster.project(arguments.project)
    if arguments.describe:
        # Read by the workflow before it mints a token, so that a token which can dispatch is
        # given only to a project whose roster entry says its commands dispatch.
        _output(f"dispatches={'true' if project.dispatches else 'false'}")
        print(f"{project.id}: its commands {'dispatch' if project.dispatches else 'do not dispatch'} workflows of its own repository")
        return 0
    if arguments.body is None:
        parser.error("--body is required to adopt")
    release = Release.parse(arguments.release)
    tree = arguments.tree.resolve()
    branch = f"origin/{branch_for(release)}"
    remote = branch if _git(tree, "rev-parse", "--verify", "--quiet", branch).strip() else None
    with tempfile.TemporaryDirectory() as scratch:
        try:
            # A project with a recorded lag adopts nothing, whatever upstream has published.
            upstream = None if project.lag is not None else find(releases(GhReleases(roster.upstream)), release)
            result = adopt(
                roster, project, release, tree, GitHubArchive(Path(scratch)), GhHub(), HttpPackages(), upstream, ShellRunner(),
                recorded=recorded_at(arguments.issue), remote_branch=remote,
            )  # fmt: skip
        except (Unreachable, UpstreamError) as error:
            print(f"::error title=Roster or project unreadable::{error}")
            return 2
    arguments.body.write_text(edited_comment(release, result) if result.result is Result.BRANCH_EDITED else body(project, release, result, arguments.issue), encoding="utf-8")
    print(f"{project.id} {release.full()}: {result.result.value}: {result.detail}")
    if result.log:
        print(result.log)
    draft = any(step.step_class is not StepClass.AUTOMATED for step in project.steps)
    _output(f"result={result.result.value}", f"draft={'true' if draft else 'false'}")
    return 1 if result.result.failed() else 0


def _output(*lines: str) -> None:
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write("".join(f"{line}\n" for line in lines))


if __name__ == "__main__":
    sys.exit(main())
