"""Detect upstream releases, keep one tracking issue for each, and advance every open adoption.

contracts/watch-workflow.md. One run, in order:

    detect    list upstream releases and keep those that are releases (research R1)
    select    a release awaits adoption when it is newer than the newest supported release and has
              no adoption record; a requested release is selected even when it is not newer
    refuse    a requested release that upstream did not publish as a release, or that would skip an
              older release whose issue is neither completed nor set aside
    ensure    one issue per release, found by its marker among open and closed issues
    evaluate  every step of the roster (data-model.md section 2)
    dispatch  each project whose openings hold and that has not been dispatched
    render    the evaluation into the issue body, in full
    complete  write the adoption record when every step is resolved

The schedule is renewed by the workflow itself, before this program runs, because that call uses the
repository's own token and this program uses the application's.

Exit codes: 0 nothing is owed; 1 a dispatch did not land or an evaluation could not be completed;
2 a project could not be read; 3 at least one adoption waits on a person; 4 a requested release was
refused.

    python -m tools.energyplus.watch [--release X.Y.Z] [--reason TEXT] [--dispatch]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from dispatch_waves import DispatchError, Dispatcher, GhDispatcher
from sweep_consumers import Source, Unreachable

from . import checklist, record
from .evaluate import (
    ADOPT_WORKFLOW,
    NOT_DISPATCHED,
    Evaluation,
    GhHub,
    HttpPackages,
    Hub,
    Packages,
    evaluate,
    next_action,
    revision,
    summarised,
)
from .roster import Release, Roster
from .scan import source_for, supported
from .serial import encode
from .upstream import GhReleases, Releases, UpstreamRelease, find, refusal, releases

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ROSTER = ROOT / "governance" / "energyplus.toml"
RECORDS = ROOT / "energyplus" / "adoptions"
LABEL = "energyplus-adoption"
SET_ASIDE = "set-aside"
DISPATCHED = "dispatched by this evaluation"

EXIT_OK, EXIT_FAILED, EXIT_UNREAD, EXIT_WAITING, EXIT_REFUSED = 0, 1, 2, 3, 4


@dataclass(frozen=True, slots=True)
class Issue:
    number: int
    url: str
    #: `open` or `closed`.
    state: str
    #: `completed`, `not_planned`, or `None` while open.
    state_reason: str | None
    labels: tuple[str, ...]
    body: str

    def settled(self) -> bool:
        """Closed as completed, or set aside by a maintainer. An older release in this state blocks nothing."""
        return self.state == "closed" and (self.state_reason == "completed" or SET_ASIDE in self.labels)

    def set_aside(self) -> bool:
        return SET_ASIDE in self.labels


class Issues(Protocol):
    """The tracking issues of the host repository."""

    def find(self, release: Release) -> Issue | None: ...

    def create(self, release: Release, body: str) -> Issue: ...

    def rewrite(self, issue: Issue, body: str) -> None: ...


class GhIssues:
    """Issues through `gh api`. An issue is found by its marker, among open and closed issues with the label.

    The listing is used and not the search index, because the index lags a newly created issue and a
    second run inside that lag would create a duplicate.
    """

    def __init__(self, repository: str) -> None:
        self.repository = repository

    def _api(self, *args: str) -> str:
        result = subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=False)  # noqa: S603, S607
        if result.returncode != 0:
            raise Unreachable(result.stderr.strip() or "gh api failed")
        return result.stdout

    @staticmethod
    def _issue(raw: dict[str, object]) -> Issue:
        labels = tuple(str(label["name"]) for label in raw.get("labels", ()))  # type: ignore[index,union-attr]
        reason = raw.get("state_reason")
        return Issue(int(raw["number"]), str(raw["html_url"]), str(raw["state"]), str(reason) if reason else None, labels, str(raw.get("body") or ""))  # type: ignore[call-overload]

    def find(self, release: Release) -> Issue | None:
        listing = json.loads(self._api("--paginate", "--slurp", f"repos/{self.repository}/issues?labels={LABEL}&state=all&per_page=100"))
        for raw in (item for page in listing for item in page):
            if "pull_request" not in raw and checklist.marked_release(str(raw.get("body") or "")) == release:
                return self._issue(raw)
        return None

    def create(self, release: Release, body: str) -> Issue:
        created = self._api(
            "-X", "POST", f"repos/{self.repository}/issues",
            "-f", f"title=Adopt EnergyPlus {release.full()}", "-f", f"body={body}", "-f", f"labels[]={LABEL}",
        )  # fmt: skip
        return self._issue(json.loads(created))

    def rewrite(self, issue: Issue, body: str) -> None:
        self._api("-X", "PATCH", f"repos/{self.repository}/issues/{issue.number}", "-f", f"body={body}")


# ── Selection and refusal ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Refusal:
    release: Release
    cause: str


def recorded(release: Release, records: Path) -> bool:
    return (records / f"{release.full()}.toml").is_file()


def select(found: Sequence[UpstreamRelease], newest_supported: Release, records: Path, requested: Release | None) -> tuple[UpstreamRelease, ...]:
    """The releases this run processes, ascending."""
    waiting = [upstream for upstream in found if upstream.release > newest_supported and not recorded(upstream.release, records)]
    if requested is not None and all(upstream.release != requested for upstream in waiting):
        asked = find(tuple(found), requested)
        if asked is not None:
            waiting.append(asked)
    return tuple(sorted(waiting, key=lambda upstream: upstream.release))


def refuse(requested: Release, source: Releases, selected: Sequence[UpstreamRelease], issues: Issues) -> Refusal | None:
    """Why a requested release is not started, or `None` when it may be (contract step 5)."""
    cause = refusal(source, requested)
    if cause is not None:
        return Refusal(requested, cause)
    for older in selected:
        if older.release >= requested:
            break
        issue = issues.find(older.release)
        if issue is None or not issue.settled():
            state = "has no tracking issue yet" if issue is None else f"is tracked by {issue.url}, which is neither completed nor set aside"
            return Refusal(requested, f"the older release {older.release.full()} awaits adoption and {state}")
    return None


# ── Dispatch ───────────────────────────────────────────────────────────────────────────────────────


def dispatchable(roster: Roster, evaluation: Evaluation) -> tuple[str, ...]:
    """The projects to dispatch: openings hold, no lag, no open pull request, no run in progress, a step not passed.

    The five conditions reduce to one observation. The evaluation gives the reason `NOT_DISPATCHED`
    to a step only when all of them hold, so a project is dispatched when it has such a step.
    """
    states = {state.project: state for state in evaluation.projects}
    found = []
    for project in roster.rostered():
        state = states[project.id]
        if not project.caller or project.lag is not None or not state.opened or state.pull_request is not None:
            continue
        if any(row.reason == NOT_DISPATCHED for row in evaluation.of(project.id)):
            found.append(project.id)
    return tuple(found)


def dispatch(roster: Roster, evaluation: Evaluation, dispatcher: Dispatcher) -> tuple[Evaluation, tuple[str, ...]]:
    """Dispatch every dispatchable project. Returns the evaluation with those steps marked, and what failed."""
    failures = []
    sent = []
    for project_id in dispatchable(roster, evaluation):
        try:
            dispatcher.dispatch(roster.project(project_id).repository, ADOPT_WORKFLOW, {"release": evaluation.release.full()})
            sent.append(project_id)
        except DispatchError as error:
            failures.append(f"{project_id}: {error}")
    rows = tuple(replace(row, reason=DISPATCHED) if row.project in sent and row.reason == NOT_DISPATCHED else row for row in evaluation.rows)
    return replace(evaluation, rows=rows, next_action=next_action(roster, rows, evaluation.projects, evaluation.strangers)), tuple(failures)


# ── One run ────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Outcome:
    exit_code: int
    lines: tuple[str, ...]
    evaluations: tuple[Evaluation, ...] = ()
    records: tuple[Path, ...] = ()


Evaluate = Callable[[Release, UpstreamRelease, checklist.Recorded | None], Evaluation]


def watch(
    roster: Roster,
    upstream: Releases,
    newest_supported: Release,
    issues: Issues,
    evaluate_release: Evaluate,
    dispatcher: Dispatcher | None,
    *,
    records: Path,
    requested: Release | None = None,
    now: datetime,
) -> Outcome:
    """Everything after the roster and the supported set are read. `dispatcher` is `None` to withhold dispatch."""
    found = releases(upstream)
    selected = select(found, newest_supported, records, requested)
    lines: list[str] = []
    if requested is not None:
        refused = refuse(requested, upstream, selected, issues)
        if refused is not None:
            return Outcome(EXIT_REFUSED, (f"EnergyPlus {requested.full()} was refused: {refused.cause}.",))
    if not selected:
        return Outcome(EXIT_OK, (f"No release awaits adoption. The newest supported release is {newest_supported.full()}.",))
    exit_code = EXIT_OK
    evaluations = []
    written = []
    older_open = False
    for candidate in selected:
        release = candidate.release
        if recorded(release, records):
            lines.append(f"EnergyPlus {release.full()}: the adoption is recorded. Nothing to do.")
            continue
        issue = issues.find(release)
        if issue is not None and issue.state == "closed":
            how = "set aside" if issue.set_aside() else "closed"
            lines.append(f"EnergyPlus {release.full()}: {issue.url} is {how}. Nothing was evaluated or written.")
            older_open = older_open or not issue.settled()
            continue
        evaluation = evaluate_release(release, candidate, checklist.recorded_source(issue.body) if issue else None)
        failures: tuple[str, ...] = ()
        if dispatcher is not None and not older_open and not evaluation.source_changed:
            evaluation, failures = dispatch(roster, evaluation, dispatcher)
        elif older_open:
            lines.append(f"EnergyPlus {release.full()}: dispatch is withheld while an older release is open.")
        body = checklist.render(evaluation, now)
        if issue is None:
            issue = issues.create(release, body)
            lines.append(f"EnergyPlus {release.full()}: created {issue.url}.")
        else:
            issues.rewrite(issue, body)
            lines.append(f"EnergyPlus {release.full()}: rewrote {issue.url}.")
        evaluations.append(evaluation)
        lines += [f"EnergyPlus {release.full()}: dispatch did not land: {failure}" for failure in failures]
        causes = record.refusals(evaluation)
        if not causes:
            adoption = record.from_evaluation(evaluation, now, issue.url)
            target = adoption.path(records)
            target.write_text(record.to_toml(adoption), encoding="utf-8")
            written.append(target)
            lines.append(f"EnergyPlus {release.full()}: complete. The record was written to {target.name} for the record pull request.")
        else:
            older_open = True
            if evaluation.complete():
                # Every step is resolved and the record still cannot be written. That is owed by a person.
                lines.append(f"EnergyPlus {release.full()}: every step is resolved, but the record was refused: {'; '.join(causes)}.")
                exit_code = EXIT_FAILED
        owed = evaluation.next_action
        lines.append(f"EnergyPlus {release.full()}: next action: {owed.text if owed else 'none owed by a person'}")
        if failures:
            exit_code = EXIT_FAILED
        elif evaluation.unread and exit_code != EXIT_FAILED:
            exit_code = EXIT_UNREAD
        elif owed is not None and exit_code == EXIT_OK:
            exit_code = EXIT_WAITING
    return Outcome(exit_code, tuple(lines), tuple(evaluations), tuple(written))


# ── The command ────────────────────────────────────────────────────────────────────────────────────


def _evaluator(roster: Roster, source: Source, hub: Hub, packages: Packages, known: Sequence[Release], with_changes: bool) -> Evaluate:
    def evaluate_release(release: Release, upstream: UpstreamRelease, first: checklist.Recorded | None) -> Evaluation:
        previous = [candidate for candidate in known if candidate < release]
        changes = summarised(roster, source, upstream, previous[-1] if previous else None) if with_changes else None
        return evaluate(
            roster, release, source, hub, packages, upstream,
            now=datetime.now(timezone.utc), process_revision=revision(), governance_level=None, recorded=first, changes=changes,
        )  # fmt: skip

    return evaluate_release


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--release", default="", help="the release to start or advance, as X.Y.Z with or without a leading v")
    parser.add_argument("--reason", default="", help="why this run was requested; logged and not interpreted")
    parser.add_argument("--roster", type=Path, default=DEFAULT_ROSTER)
    parser.add_argument("--repository", default="idfkit/idfkit-conformance", help="the host repository, which holds the tracking issues")
    parser.add_argument("--workspace", type=Path, help="read sibling checkouts at origin/HEAD; otherwise the organisation through gh")
    parser.add_argument("--dispatch", action="store_true", help="dispatch each project's adoption workflow; without it the run only evaluates")
    parser.add_argument("--no-changes", action="store_true", help="omit the change summary")
    parser.add_argument("--out", type=Path, help="a directory to write each evaluation to, as evaluation-X.Y.Z.json")
    arguments = parser.parse_args(argv)
    if arguments.reason:
        print(f"requested because: {arguments.reason}")
    try:
        requested = Release.parse(arguments.release) if arguments.release.strip() else None
    except ValueError as error:
        print(f"EnergyPlus {arguments.release} was refused: {error}.", file=sys.stderr)
        return EXIT_REFUSED
    roster = Roster.load(arguments.roster)
    with tempfile.TemporaryDirectory() as scratch:
        source = source_for(arguments.workspace, Path(scratch))
        try:
            known = supported(roster, source)
            outcome = watch(
                roster, GhReleases(roster.upstream), known[-1], GhIssues(arguments.repository),
                _evaluator(roster, source, GhHub(), HttpPackages(), known, not arguments.no_changes),
                GhDispatcher() if arguments.dispatch else None,
                records=RECORDS, requested=requested, now=datetime.now(timezone.utc),
            )  # fmt: skip
        except Unreachable as error:
            print(f"the run could not read what it needs: {error}", file=sys.stderr)
            return EXIT_UNREAD
        except RuntimeError as error:
            print(f"the run could not be completed: {error}", file=sys.stderr)
            return EXIT_FAILED
    if arguments.out is not None:
        arguments.out.mkdir(parents=True, exist_ok=True)
        for evaluation in outcome.evaluations:
            (arguments.out / f"evaluation-{evaluation.release.full()}.json").write_text(encode(evaluation), encoding="utf-8")
    print("\n".join(outcome.lines))
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
