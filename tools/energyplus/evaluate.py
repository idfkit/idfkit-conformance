"""Evaluate one EnergyPlus release against the roster.

The verdict of every step is computed from observable state each time. No file records that a step
passed, so an interrupted adoption has no state to recover and an automated verdict cannot be set
by hand (research R9.1). The rules are those of data-model.md section 2, in order:

    1  the project has a lag                                   not applicable
    2  the verification holds on the default branch            passed, merged or released
    3  an opening of the project does not hold                 not started
    4  the default branch's own checks fail, no adoption yet   failed, pre-existing
    5  the verification holds on the adoption branch           passed, proposed
    6  as 5 except a person's acceptance, on a reviewed or
       manual step                                             awaiting person, proposed
    7  a check on the adoption branch, or the run, failed      failed
    8  a run or a check is in progress                         not started
    9  the openings hold and nothing was dispatched            not started

A check does not establish a release. `checks-green` and `accepted` hold only on a commit on which
every content verification of the same project holds.

    python -m tools.energyplus.evaluate --release X.Y.Z --workspace .. --out evaluation.json
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from consumers import normalize
from sweep_consumers import HttpRegistry, Registry, Source, Unreachable

from . import changes as change_summary
from .changes import ChangeSummary
from .roster import (
    END_TO_END,
    RECORD_STEP,
    Accepted,
    All,
    AssetRole,
    ChecksGreen,
    HttpContains,
    ReleaseStates,
    OpeningKind,
    PathExists,
    Project,
    PublishedCarries,
    RegistryLists,
    Release,
    Roster,
    StatementsCurrent,
    Step,
    StepClass,
    TagExists,
    Target,
    Verification,
    WorkflowSucceeded,
    members,
)
from .scan import Finding, FindingKind, scan, source_for, statement_findings, supported
from .serial import encode
from .upstream import Asset, GhReleases, Releases, UpstreamError, UpstreamRelease, find, releases

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ROSTER = ROOT / "governance" / "energyplus.toml"
ADOPT_WORKFLOW = "adopt-energyplus.yml"
#: How much of a release asset is read to find the release it states.
HEAD_BYTES = 4096
PRE_EXISTING = "pre-existing"
NOT_DISPATCHED = "open; no adoption has been dispatched"
#: The adoption ran on the commit the default branch is at, succeeded, and opened no pull request.
RAN_AND_WAITS = "the adoption ran on the current commit and proposed nothing; it waits on content that a run does not produce"
SOURCE_CHANGED = "upstream source changed"
CONSUMED_ROLES = (AssetRole.LINUX_X86_64, AssetRole.LINUX_ARM64, AssetRole.CHECKSUMS)
CONTENT_KINDS = (RegistryLists, PathExists, TagExists, ReleaseStates, StatementsCurrent, PublishedCarries, HttpContains)


def branch_for(release: Release) -> str:
    return f"energyplus/{release.full()}"


# ── The result model (data-model 1.12, 1.13) ───────────────────────────────────────────────────────


class Verdict(Enum):
    PASSED = "passed"
    FAILED = "failed"
    AWAITING_PERSON = "awaiting-person"
    NOT_APPLICABLE = "not-applicable"
    NOT_STARTED = "not-started"


class Landing(Enum):
    NONE = "none"
    PROPOSED = "proposed"
    MERGED = "merged"
    RELEASED = "released"


@dataclass(frozen=True, slots=True)
class Evidence:
    holds: bool
    url: str | None
    detail: str
    accepted_by: str | None = None
    accepted_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class StepResult:
    project: str
    step: str
    title: str
    step_class: StepClass
    verdict: Verdict
    landing: Landing
    evidence: str | None = None
    detail: str | None = None
    reason: str | None = None
    accepted_by: str | None = None
    accepted_at: datetime | None = None
    inspect: str | None = None

    def resolved(self) -> bool:
        """Passed on a default branch or a package index, or not applicable. What a record requires."""
        if self.verdict is Verdict.NOT_APPLICABLE:
            return True
        return self.verdict is Verdict.PASSED and self.landing is not Landing.PROPOSED


@dataclass(frozen=True, slots=True)
class ProjectState:
    """What a section of the checklist says about one project."""

    project: str
    repository: str
    opened: bool
    waits_on: str
    lag: str | None
    pull_request: str | None
    merge_effect: str | None
    after_merge: str | None


@dataclass(frozen=True, slots=True)
class NextAction:
    text: str
    who: str
    url: str | None = None


@dataclass(frozen=True, slots=True)
class SourceChange:
    what: str
    recorded: str
    live: str


@dataclass(frozen=True, slots=True)
class Occurrence:
    project: str
    path: str
    line: int
    text: str


@dataclass(frozen=True, slots=True)
class SourceAsset:
    role: AssetRole
    name: str
    size: int
    sha256: str | None


@dataclass(frozen=True, slots=True)
class UpstreamIdentity:
    repository: str
    tag: str
    commit: str
    published_at: datetime


@dataclass(frozen=True, slots=True)
class Evaluation:
    release: Release
    evaluated_at: datetime
    process_revision: str
    governance_level: str | None
    upstream: UpstreamIdentity | None
    sources: tuple[SourceAsset, ...]
    source_changed: tuple[SourceChange, ...]
    strangers: tuple[Occurrence, ...]
    unread: tuple[str, ...]
    projects: tuple[ProjectState, ...]
    rows: tuple[StepResult, ...]
    next_action: NextAction | None
    changes: ChangeSummary | None = None

    def count(self, verdict: Verdict) -> int:
        return sum(1 for row in self.rows if row.verdict is verdict)

    def complete(self) -> bool:
        """Every step but the record itself is resolved, nothing is strange and the source is unchanged."""
        others = [row for row in self.rows if not (row.step == RECORD_STEP and row.project != END_TO_END)]
        return all(row.resolved() for row in others) and not self.strangers and not self.source_changed and not self.unread

    def of(self, project: str) -> tuple[StepResult, ...]:
        return tuple(row for row in self.rows if row.project == project)


# ── What the evaluation asks of GitHub and of the package indexes ──────────────────────────────────


class CheckOutcome(Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    PENDING = "pending"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class CheckState:
    outcome: CheckOutcome
    url: str | None = None
    failing: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PullRequest:
    number: int
    url: str
    head: str
    state: str
    """`open`, `merged` or `closed`."""
    merged_by: str | None = None
    merged_at: datetime | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Released:
    """One release of a repository whose tag matched a glob, with the start of one of its assets."""

    tag: str
    url: str
    #: The names of every asset of the release.
    assets: tuple[str, ...]
    #: The first characters of the asset that was asked for. Empty when the release has no such asset.
    head: str


@dataclass(frozen=True, slots=True)
class Run:
    url: str
    #: `None` while the run has not concluded.
    conclusion: str | None
    #: The commit the run was dispatched on. Empty where it is not known.
    head: str = ""


class Hub(Protocol):
    """GitHub facts that are not file contents on a default branch. Injectable, so the rules can be tested."""

    def read(self, repository: str, path: str, ref: str) -> str: ...

    def exists(self, repository: str, path: str, ref: str) -> bool: ...

    def checks(self, repository: str, ref: str | None, workflow: str | None) -> CheckState: ...

    def pull_request(self, repository: str, branch: str) -> PullRequest | None: ...

    def tags(self, repository: str) -> tuple[str, ...]: ...

    def released(self, repository: str, tag: str, asset: str) -> tuple[Released, ...]: ...

    def head(self, repository: str) -> str:
        """The commit the default branch is at."""
        ...

    def run(self, repository: str, workflow: str, release: Release) -> Run | None: ...

    def get(self, url: str) -> str | None: ...


class Packages(Protocol):
    """The package indexes. A member is a path inside the published artifact."""

    def versions(self, index: str, package: str) -> tuple[str, ...]: ...

    def newest(self, index: str, package: str) -> str | None: ...

    def has_member(self, index: str, package: str, version: str, member: str) -> bool: ...

    def requires(self, index: str, package: str, version: str, dependency: str) -> str | None: ...


# ── Whether a verification holds ───────────────────────────────────────────────────────────────────


def permalink(repository: str, path: str, ref: str | None, line: int = 0) -> str:
    return f"https://github.com/{repository}/blob/{ref or 'HEAD'}/{path}" + (f"#L{line}" if line else "")


class Evaluator:
    """One release, one roster, and the readers. Every `holds` answer is computed once."""

    def __init__(
        self, roster: Roster, release: Release, source: Source, hub: Hub, packages: Packages, upstream: UpstreamRelease | None
    ) -> None:
        self.roster = roster
        self.release = release
        self.source = source
        self.hub = hub
        self.packages = packages
        self.upstream = upstream
        known = set(supported(roster, source)) | {release}
        self.target = Target(max(known), len(known))
        self._held: dict[tuple[int, str, str | None], Evidence] = {}
        self._pulls: dict[str, PullRequest | None] = {}
        self._rows: dict[str, tuple[StepResult, ...]] = {}
        self._files: dict[str, frozenset[str]] = {}

    # Reading a project at its default branch, or at the head of its adoption pull request.

    def read(self, repository: str, path: str, ref: str | None) -> str:
        return self.source.read(repository, path) if ref is None else self.hub.read(repository, path, ref)

    def exists(self, repository: str, path: str, ref: str | None) -> bool:
        if ref is not None:
            return self.hub.exists(repository, path, ref)
        if repository not in self._files:
            self._files[repository] = frozenset(self.source.files(repository))
        files = self._files[repository]
        return path in files or any(name.startswith(path.rstrip("/") + "/") for name in files)

    def pull(self, project: Project) -> PullRequest | None:
        if project.id not in self._pulls:
            self._pulls[project.id] = self.hub.pull_request(project.repository, branch_for(self.release)) if project.caller else None
        return self._pulls[project.id]

    # The verification kinds.

    def holds(self, verification: Verification, project: Project | None, ref: str | None = None) -> Evidence:
        key = (id(verification), project.id if project else END_TO_END, ref)
        if key not in self._held:
            try:
                self._held[key] = self._holds(verification, project, ref)
            except Unreachable as error:
                self._held[key] = Evidence(False, None, f"could not be read: {error}")
        return self._held[key]

    def _repository(self, stated: str | None, project: Project | None) -> str:
        if stated:
            return stated
        if project is None:
            raise Unreachable("an end-to-end verification must name its repository")
        return project.repository

    def _own_ref(self, repository: str, project: Project | None, ref: str | None) -> str | None:
        """An adoption branch exists only in the project's own repository."""
        return ref if project is not None and repository == project.repository else None

    def _holds(self, verification: Verification, project: Project | None, ref: str | None) -> Evidence:
        release = self.release
        match verification:
            case RegistryLists(repository=stated, path=path, pattern=pattern):
                repository = self._repository(stated, project)
                at = self._own_ref(repository, project, ref)
                try:
                    text = self.read(repository, path, at)
                except FileNotFoundError:
                    return Evidence(False, None, f"{path} does not exist")
                wanted = verification.form.render(Target(release, 0))
                for match in re.finditer(pattern, text, re.MULTILINE):
                    found = verification.form.read(match.group("release"))
                    if found is not None and verification.form.render(Target(found, 0)) == wanted:
                        return Evidence(True, permalink(repository, path, at, text.count("\n", 0, match.start("release")) + 1), f"{path} lists {release.full()}")
                return Evidence(False, permalink(repository, path, at), f"{path} does not list {release.full()}")
            case PathExists(repository=stated, path=path):
                repository = self._repository(stated, project)
                at = self._own_ref(repository, project, ref)
                actual = release.substitute(path)
                found = self.exists(repository, actual, at)
                return Evidence(found, permalink(repository, actual, at), f"{actual} {'exists' if found else 'does not exist'}")
            case TagExists(repository=stated, tag=tag):
                repository = self._repository(stated, project)
                wanted = release.substitute(tag)
                found = [name for name in self.hub.tags(repository) if fnmatch.fnmatchcase(name, wanted)]
                url = f"https://github.com/{repository}/releases/tag/{found[0]}" if found else None
                return Evidence(bool(found), url, f"tag {wanted} {'exists' if found else 'does not exist'} in {repository}")
            case ReleaseStates(repository=stated, tag=tag, asset=asset, pattern=pattern, beside=beside):
                repository = self._repository(stated, project)
                wanted = release.substitute(tag)
                read = self.hub.released(repository, wanted, asset)
                for found in read:
                    names = [verification.form.read(match.group("release")) for match in re.finditer(pattern, found.head, re.MULTILINE)]
                    missing = [name for name in beside if name not in found.assets]
                    if release in names and not missing:
                        return Evidence(True, found.url, f"{found.tag} in {repository} was built from {release.full()}")
                    if release in names:
                        return Evidence(False, found.url, f"{found.tag} in {repository} was built from {release.full()} and lacks {', '.join(missing)}")
                held = ", ".join(found.tag for found in read) or "none"
                return Evidence(False, None, f"no release {wanted} of {repository} states {release.full()} in {asset} (releases read: {held})")
            case StatementsCurrent(project=named):
                return self._statements(named, project, ref)
            case ChecksGreen(repository=stated, workflow=workflow):
                repository = self._repository(stated, project)
                owner = self.roster.by_repository(repository)
                at = self._own_ref(repository, project, ref)
                if owner is not None and not (carried := self.carries(owner, at)).holds:
                    return Evidence(False, carried.url, f"{owner.id} does not carry {release.full()}: {carried.detail}")
                if workflow is None and owner is not None and at is None:
                    workflow = owner.checks
                state = self.hub.checks(repository, at, workflow)
                what = f"{workflow or 'checks'} on {repository}"
                if state.outcome is CheckOutcome.SUCCESS:
                    return Evidence(True, state.url, f"{what} concluded successfully")
                failing = f": {', '.join(state.failing)}" if state.failing else ""
                return Evidence(False, state.url, f"{what}: {state.outcome.value}{failing}")
            case WorkflowSucceeded(repository=stated, workflow=workflow):
                repository = self._repository(stated, project)
                run = self.hub.run(repository, workflow, release)
                if run is None:
                    return Evidence(False, None, f"no run of {workflow} names {release.full()}")
                return Evidence(run.conclusion == "success", run.url, f"{workflow}: {run.conclusion or 'in progress'}")
            case PublishedCarries():
                return self._published(verification, project, ref)
            case HttpContains(url=stated_url, pattern=pattern):
                url = release.substitute(stated_url)
                text = self.hub.get(url)
                if text is None:
                    return Evidence(False, url, f"{url} could not be read")
                wanted = verification.form.render(Target(release, 0))
                read = [verification.form.read(match.group("release")) for match in re.finditer(pattern, text, re.MULTILINE)]
                named = [release for found in read if found is not None and verification.form.render(Target(found, 0)) == wanted]
                return Evidence(release in named, url, f"{url} {'names' if release in named else 'does not name'} {release.full()}")
            case Accepted():
                return self._accepted(project, ref)
            case All(of=of):
                evidence = [self.holds(member, project, ref) for member in of]
                failed = next((item for item in evidence if not item.holds), None)
                if failed is not None:
                    return failed
                person = next((item for item in evidence if item.accepted_by), None)
                first = next((item for item in evidence if item.url), evidence[0])
                return Evidence(
                    True, first.url, "; ".join(item.detail for item in evidence), person.accepted_by if person else None, person.accepted_at if person else None
                )
        raise TypeError(f"unknown verification {verification!r}")

    def _statements(self, named: str | None, project: Project | None, ref: str | None) -> Evidence:
        if named == "*":
            projects = [candidate for candidate in self.roster.rostered() if candidate.lag is None]
        else:
            owner = self.roster.project(named) if named else project
            if owner is None:
                raise Unreachable("statements-current must name a project outside a project's own steps")
            projects = [owner]
        checked = 0
        findings: list[Finding] = []
        for candidate in projects:
            at = ref if project is not None and candidate.id == project.id else None

            def read(path: str, repository: str = candidate.repository, at: str | None = at) -> str:
                return self.read(repository, path, at)

            checked += len(candidate.statements)
            findings.extend(statement_findings(candidate, read, self.target))
        if not findings:
            shown = next((candidate for candidate in projects if candidate.statements), projects[0])
            url = permalink(shown.repository, shown.statements[0].path, None) if shown.statements else f"https://github.com/{shown.repository}"
            return Evidence(True, url, f"{checked} statement(s) in {len(projects)} project(s) state {self.target.release.full()}")
        first = findings[0]
        repository = self.roster.project(first.project).repository
        return Evidence(False, permalink(repository, first.path, None, first.line), f"{len(findings)} finding(s) across {checked} statement(s): {first.project} {first.path} {first.detail}")

    def _published(self, verification: PublishedCarries, project: Project | None, ref: str | None) -> Evidence:
        release, index, package = self.release, verification.index, verification.package
        through = verification.through
        if through is None:
            candidates = self.packages.versions(index, package) if verification.version_names_release else tuple(filter(None, [self.packages.newest(index, package)]))
            origin = "published"
        else:
            if project is None:
                raise Unreachable("a level is read through a project's own file")
            try:
                found = re.search(through.pattern, self.read(project.repository, through.path, ref), re.MULTILINE)
            except FileNotFoundError:
                return Evidence(False, None, f"{through.path} does not exist")
            if found is None:
                return Evidence(False, permalink(project.repository, through.path, ref), f"{through.path} declares no level")
            level = found.group("level")
            origin = f"pinned in {through.path}"
            if through.via is not None:
                resolved = self.packages.requires(index, through.via, normalize(level), package)
                if resolved is None:
                    return Evidence(False, None, f"{through.via} {level} declares no {package} level")
                origin = f"resolved by {through.via} {level}"
                level = resolved
            candidates = (level,)
        if not candidates:
            return Evidence(False, None, f"{package} has no published version")
        for version in candidates:
            if verification.version_names_release:
                carried = version.startswith(release.line() + ".")
            else:
                assert verification.member is not None
                carried = self.packages.has_member(index, package, self._indexed(index, version), release.substitute(verification.member))
            if carried:
                return Evidence(True, _package_url(index, package, version), f"{package} {version} ({origin}) carries {release.full()}")
        return Evidence(False, _package_url(index, package, candidates[-1]), f"{package} {candidates[-1]} ({origin}) does not carry {release.full()}")

    @staticmethod
    def _indexed(index: str, version: str) -> str:
        return normalize(version) if index == "pypi" else version

    def _accepted(self, project: Project | None, ref: str | None) -> Evidence:
        if project is None:
            raise Unreachable("an acceptance belongs to a project's pull request")
        pull = self.pull(project)
        if ref is not None:
            if pull is not None and pull.approved_by:
                return Evidence(True, pull.url, f"approved by {pull.approved_by}", pull.approved_by, pull.approved_at)
            return Evidence(False, pull.url if pull else None, "the pull request has not been approved by a person")
        if pull is not None and pull.state == "merged" and pull.merged_by:
            return Evidence(True, pull.url, f"merged by {pull.merged_by}", pull.merged_by, pull.merged_at)
        carried = self.carries(project, None)
        if carried.holds:
            return Evidence(True, f"https://github.com/{project.repository}/commits", "carried on the default branch; merged outside an adoption run")
        return Evidence(False, None, "no adoption pull request has been merged")

    def carries(self, project: Project, ref: str | None) -> Evidence:
        """Every content verification of the project holds at `ref`. What a check or an acceptance presupposes."""
        for step in project.steps:
            for member in members(step.verify):
                if isinstance(member, CONTENT_KINDS) and not (evidence := self.holds(member, project, ref)).holds:
                    return evidence
        return Evidence(True, None, f"{project.id} carries {self.release.full()}")

    def content_holds(self, step: Step, project: Project, ref: str | None) -> bool:
        """The step's verification holds apart from a person's acceptance (rule 6)."""
        return all(self.holds(member, project, ref).holds for member in members(step.verify) if not isinstance(member, Accepted)) and self.carries(project, ref).holds

    # Openings.

    def opening_holds(self, project: Project) -> tuple[bool, str]:
        waiting = []
        for opening in project.opens_on:
            match opening.kind:
                case OpeningKind.UPSTREAM_ASSETS:
                    held = self.upstream is not None and all(self.upstream.has(role, opening.ubuntu) for role in opening.roles)
                case OpeningKind.MERGED:
                    other = self.roster.project(opening.project or "")
                    held = self.holds(other.step(opening.step or "").verify, other).holds
                case OpeningKind.PUBLISHED:
                    assert opening.published is not None
                    held = self.holds(opening.published, None).holds
                case OpeningKind.PROJECT_COMPLETE:
                    held = all(row.resolved() for row in self.rows_of(self.roster.project(opening.project or "")))
            if not held:
                waiting.append(opening.stated())
        stated = "; ".join(opening.stated() for opening in project.opens_on) or "a release awaiting adoption"
        return (not waiting, "; ".join(waiting) if waiting else stated)

    # The nine rules.

    def rows_of(self, project: Project) -> tuple[StepResult, ...]:
        if project.id not in self._rows:
            self._rows[project.id] = tuple(self._row(project, step) for step in project.steps)
        return self._rows[project.id]

    def _row(self, project: Project, step: Step) -> StepResult:
        def row(verdict: Verdict, landing: Landing, evidence: Evidence | None = None, reason: str | None = None) -> StepResult:
            unresolved = step.step_class is not StepClass.AUTOMATED and verdict is not Verdict.PASSED
            return StepResult(
                project.id, step.id, step.title, step.step_class, verdict, landing,
                evidence.url if evidence else None, evidence.detail if evidence else None, reason,
                evidence.accepted_by if evidence else None, evidence.accepted_at if evidence else None,
                step.inspect if unresolved else None,
            )  # fmt: skip

        if project.lag is not None:
            return row(Verdict.NOT_APPLICABLE, Landing.NONE, None, project.lag.stated())
        default = self.holds(step.verify, project)
        if default.holds:
            released = any(isinstance(member, PublishedCarries) for member in members(step.verify))
            return row(Verdict.PASSED, Landing.RELEASED if released else Landing.MERGED, default)
        opened, waiting = self.opening_holds(project)
        if not opened:
            return row(Verdict.NOT_STARTED, Landing.NONE, None, f"waiting on {waiting}")
        if not project.caller:
            return row(Verdict.NOT_STARTED, Landing.NONE, default, "written when every other step is resolved")
        pull = self.pull(project)
        if pull is None or pull.state != "open":
            base = self.hub.checks(project.repository, None, project.checks)
            if base.outcome is CheckOutcome.FAILURE:
                if pull is not None and pull.state == "merged":
                    after = f"failing after the adoption merged: {', '.join(base.failing)}"
                    return row(Verdict.FAILED, Landing.NONE, Evidence(False, base.url or pull.url, after), "the default branch's checks fail after the adoption merged")
                return row(Verdict.FAILED, Landing.NONE, Evidence(False, base.url, f"failing before any adoption: {', '.join(base.failing)}"), PRE_EXISTING)
            run = self.hub.run(project.repository, ADOPT_WORKFLOW, self.release)
            if run is not None and run.conclusion is None:
                return row(Verdict.NOT_STARTED, Landing.NONE, Evidence(False, run.url, "the adoption run is in progress"), "the adoption run is in progress")
            if run is not None and run.conclusion != "success":
                return row(Verdict.FAILED, Landing.NONE, Evidence(False, run.url, f"the adoption run concluded {run.conclusion}"), f"the adoption run concluded {run.conclusion}")
            # A run that succeeded and opened no pull request found nothing it could propose: it
            # stopped at a step whose content it has no command for. Run again on the same commit it
            # would stop at the same step, so the project is dispatched again only when its default
            # branch has moved. Without this the watch and the project dispatch each other without end.
            if step.run is None and step.step_class is StepClass.AUTOMATED and any(isinstance(m, PublishedCarries) for m in members(step.verify)):
                return row(Verdict.NOT_STARTED, Landing.NONE, default, "waiting on the project's own library bump")
            if run is not None and run.head and run.head == self.hub.head(project.repository):
                return row(Verdict.NOT_STARTED, Landing.NONE, Evidence(False, run.url, default.detail), RAN_AND_WAITS)
            return row(Verdict.NOT_STARTED, Landing.NONE, default, NOT_DISPATCHED)
        head = self.holds(step.verify, project, pull.head)
        proposed = replace(head, url=head.url or pull.url)
        if head.holds:
            return row(Verdict.PASSED, Landing.PROPOSED, proposed)
        if step.step_class is not StepClass.AUTOMATED and self.content_holds(step, project, pull.head):
            return row(Verdict.AWAITING_PERSON, Landing.PROPOSED, replace(proposed, url=pull.url), "awaiting a person's review of the pull request")
        checks = self.hub.checks(project.repository, pull.head, None)
        if checks.outcome is CheckOutcome.FAILURE:
            return row(Verdict.FAILED, Landing.PROPOSED, Evidence(False, checks.url or pull.url, head.detail), f"failing: {', '.join(checks.failing)}")
        if checks.outcome is CheckOutcome.PENDING:
            return row(Verdict.NOT_STARTED, Landing.PROPOSED, proposed, "checks on the pull request are in progress")
        if step.run is None and any(isinstance(m, PublishedCarries) for m in members(step.verify)):
            return row(Verdict.NOT_STARTED, Landing.PROPOSED, proposed, "waiting on the project's own library bump")
        if step.run is not None and any(isinstance(m, (TagExists, ReleaseStates)) for m in members(step.verify)):
            # The step's result is produced outside the branch, by a build its command dispatched.
            return row(Verdict.NOT_STARTED, Landing.PROPOSED, proposed, "waiting on a build the adoption dispatched; dispatch the adoption again when the build has completed")
        return row(Verdict.FAILED, Landing.PROPOSED, proposed, "the adoption branch does not carry this step")

    def end_to_end(self) -> tuple[StepResult, ...]:
        rows = []
        for step in self.roster.verifications:
            evidence = self.holds(step.verify, None)
            verdict = Verdict.PASSED if evidence.holds else Verdict.NOT_STARTED
            landing = Landing.MERGED if evidence.holds else Landing.NONE
            reason = None if evidence.holds else evidence.detail
            rows.append(StepResult(END_TO_END, step.id, step.title, step.step_class, verdict, landing, evidence.url, evidence.detail, reason))
        return tuple(rows)

    def state_of(self, project: Project) -> ProjectState:
        opened, waiting = self.opening_holds(project)
        pull = self.pull(project) if project.lag is None else None
        return ProjectState(
            project.id, project.repository, opened, waiting, project.lag.stated() if project.lag else None,
            pull.url if pull and pull.state == "open" else None, project.merge_effect, project.after_merge,
        )  # fmt: skip


# ── The next action ────────────────────────────────────────────────────────────────────────────────


def next_action(roster: Roster, rows: Sequence[StepResult], states: Sequence[ProjectState]) -> NextAction | None:
    """The one action owed by a person, or `None` when nothing is owed (rule C6)."""
    for row in rows:
        if row.verdict is Verdict.AWAITING_PERSON:
            return NextAction(f"review {row.project}: {row.title}", f"a maintainer of {row.project}", row.evidence)
        if row.verdict is Verdict.FAILED:
            cause = f" ({row.reason})" if row.reason else ""
            return NextAction(f"resolve the failure in {row.project}: {row.title}{cause}", f"a maintainer of {row.project}", row.evidence)
    for state in states:
        rows_of = [row for row in rows if row.project == state.project]
        if state.pull_request and rows_of and all(row.verdict is Verdict.PASSED for row in rows_of if row.landing is Landing.PROPOSED):
            if any(row.landing is Landing.PROPOSED for row in rows_of):
                effect = f" {state.merge_effect}" if state.merge_effect else ""
                return NextAction(f"merge the {state.project} adoption pull request.{effect}", f"a maintainer of {state.project}", state.pull_request)
    undispatched = [state.project for state in states if any(row.reason == NOT_DISPATCHED for row in rows if row.project == state.project)]
    if undispatched:
        return NextAction(f"start the adoption in {', '.join(undispatched)}: dispatch its adoption workflow, or perform its steps by hand.", "a maintainer")
    for project in roster.rostered():
        if project.lag is not None:
            continue
        state = next(item for item in states if item.project == project.id)
        for opening in project.opens_on:
            if opening.kind is not OpeningKind.PUBLISHED or opening.published is None or state.opened or opening.stated() not in state.waits_on:
                continue
            package = opening.published.package
            owner = next((candidate for candidate in roster.rostered() if package in candidate.publishes), None)
            if owner is not None and not all(row.resolved() for row in rows if row.project == owner.id):
                continue
            waiting = sum(1 for item in states if not item.opened and opening.stated() in item.waits_on)
            who = f"a maintainer of {owner.id}" if owner else "a maintainer"
            return NextAction(f"publish {package} with the release. {waiting} project(s) are waiting on it.", who)
    return None


# ── The evaluation ─────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Recorded:
    """The source identity recorded at the first evaluation, read back from the tracking issue."""

    commit: str
    assets: tuple[SourceAsset, ...]


def source_changes(recorded: Recorded | None, commit: str, sources: Sequence[SourceAsset]) -> tuple[SourceChange, ...]:
    """Every difference between the recorded source and the live one (FR-019)."""
    if recorded is None:
        return ()
    found = []
    if recorded.commit and commit and recorded.commit != commit:
        found.append(SourceChange("the commit the tag points to", recorded.commit, commit))
    live = {asset.role: asset for asset in sources}
    for asset in recorded.assets:
        now = live.get(asset.role)
        if now is None:
            found.append(SourceChange(f"asset {asset.name}", asset.sha256 or "", "no longer published"))
        elif (now.name, now.size, now.sha256) != (asset.name, asset.size, asset.sha256):
            found.append(SourceChange(f"asset {asset.name}", f"{asset.size} bytes, sha256 {asset.sha256}", f"{now.name}, {now.size} bytes, sha256 {now.sha256}"))
    return tuple(found)


def consumed(upstream: UpstreamRelease | None, ubuntu: Mapping[AssetRole, str] | None = None) -> tuple[SourceAsset, ...]:
    """The assets the suite consumes. `ubuntu` is the roster's stated build per role (`Roster.ubuntu_builds`)."""
    if upstream is None:
        return ()
    found = []
    for role in CONSUMED_ROLES:
        try:
            asset: Asset = upstream.asset(role, (ubuntu or {}).get(role))
        except RuntimeError:
            continue
        found.append(SourceAsset(role, asset.name, asset.size, asset.sha256))
    return tuple(found)


def evaluate(
    roster: Roster,
    release: Release,
    source: Source,
    hub: Hub,
    packages: Packages,
    upstream: UpstreamRelease | None,
    *,
    now: datetime,
    process_revision: str,
    governance_level: str | None,
    recorded: Recorded | None = None,
    with_scan: bool = True,
    changes: ChangeSummary | None = None,
) -> Evaluation:
    evaluator = Evaluator(roster, release, source, hub, packages, upstream)
    rows: list[StepResult] = []
    states = []
    for project in roster.rostered():
        rows.extend(evaluator.rows_of(project))
        states.append(evaluator.state_of(project))
    rows.extend(evaluator.end_to_end())
    sources = consumed(upstream, roster.ubuntu_builds())
    changed = source_changes(recorded, upstream.commit if upstream else "", sources)
    if changed:
        rows.insert(0, StepResult(END_TO_END, "source", SOURCE_CHANGED.capitalize(), StepClass.AUTOMATED, Verdict.FAILED, Landing.NONE, None, changed[0].what, SOURCE_CHANGED))
    report = scan(roster, source) if with_scan else None
    strangers = tuple(Occurrence(f.project, f.path, f.line, f.text) for f in report.of(FindingKind.STRANGER)) if report else ()
    identity = UpstreamIdentity(roster.upstream, upstream.tag, upstream.commit, upstream.published_at) if upstream else None
    return Evaluation(
        release=release,
        evaluated_at=now,
        process_revision=process_revision,
        governance_level=governance_level,
        upstream=identity,
        sources=sources,
        source_changed=changed,
        strangers=strangers,
        unread=tuple(f"{unread.project}: {unread.cause}" for unread in report.unread) if report else (),
        projects=tuple(states),
        rows=tuple(rows),
        next_action=next_action(roster, rows, states),
        changes=changes,
    )


# ── GitHub and the package indexes, for a real run ─────────────────────────────────────────────────

_BOT = re.compile(r"\[bot\]$|^github-actions$")

#: The review states that replace a reviewer's earlier verdict. A comment does not.
_VERDICTS = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")


@dataclass(frozen=True, slots=True)
class Approval:
    by: str
    at: datetime | None


def approval(reviews: Sequence[Mapping[str, Any]], head: str) -> Approval | None:
    """The newest standing approval of `head` by a person, from a pull request's reviews in order.

    An approval stands only for the commit it was given on: one given before the branch was
    regenerated accepts content its reviewer did not see. A reviewer's later request for changes,
    or a dismissal, replaces their approval.
    """
    latest: dict[str, Mapping[str, Any]] = {}
    for review in reviews:
        login = (review.get("user") or {}).get("login")
        if login and not _BOT.search(login) and review.get("state") in _VERDICTS:
            latest[login] = review
    standing = [(login, review) for login, review in latest.items() if review["state"] == "APPROVED" and review.get("commit_id") == head]
    if not standing:
        return None
    login, review = max(standing, key=lambda pair: pair[1].get("submitted_at") or "")
    return Approval(login, _when(review.get("submitted_at")))
_BAD = {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}


def _when(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text.replace("Z", "+00:00")) if text else None


class GhHub:
    """GitHub, through `gh api`. A request that fails for a reason other than absence is `Unreachable`."""

    def __init__(self) -> None:
        self._default: dict[str, str] = {}
        self._tags: dict[str, tuple[str, ...]] = {}
        self._releases: dict[str, list[Mapping[str, Any]]] = {}
        self._heads: dict[tuple[str, int, str], str] = {}
        self._head_commits: dict[str, str] = {}

    def _api(self, *args: str, missing: Any = None) -> Any:
        result = subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=False)  # noqa: S603, S607
        if result.returncode != 0:
            if "404" in result.stderr or "Not Found" in result.stdout:
                return missing
            raise Unreachable(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "gh api failed")
        return json.loads(result.stdout) if result.stdout.strip() else missing

    def _default_branch(self, repository: str) -> str:
        if repository not in self._default:
            payload = self._api(f"repos/{repository}")
            if payload is None:
                raise Unreachable(f"{repository}: not found")
            self._default[repository] = payload["default_branch"]
        return self._default[repository]

    def read(self, repository: str, path: str, ref: str) -> str:
        result = subprocess.run(  # noqa: S603
            ["gh", "api", f"repos/{repository}/contents/{path}?ref={ref}", "-H", "Accept: application/vnd.github.raw"],  # noqa: S607
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        if result.returncode != 0:
            if "404" in result.stderr:
                raise FileNotFoundError(path)
            raise Unreachable(result.stderr.strip() or "gh api failed")
        return result.stdout

    def exists(self, repository: str, path: str, ref: str) -> bool:
        return self._api(f"repos/{repository}/contents/{path.rstrip('/')}?ref={ref}", "--jq", "{}") is not None

    def checks(self, repository: str, ref: str | None, workflow: str | None) -> CheckState:
        if workflow is not None:
            query = f"head_sha={ref}" if ref else f"branch={self._default_branch(repository)}&exclude_pull_requests=true"
            payload = self._api(f"repos/{repository}/actions/workflows/{workflow}/runs?per_page=1&{query}", missing={})
            runs = payload.get("workflow_runs") or []
            if not runs:
                return CheckState(CheckOutcome.NONE)
            run = runs[0]
            if run["status"] != "completed":
                return CheckState(CheckOutcome.PENDING, run["html_url"])
            if run["conclusion"] == "success":
                return CheckState(CheckOutcome.SUCCESS, run["html_url"])
            return CheckState(CheckOutcome.FAILURE, run["html_url"], (workflow,))
        at = ref or self._default_branch(repository)
        payload = self._api(f"repos/{repository}/commits/{at}/check-runs?per_page=100", missing={})
        runs = payload.get("check_runs") or []
        url = f"https://github.com/{repository}/commit/{at}/checks"
        if not runs:
            return CheckState(CheckOutcome.NONE, url)
        failing = tuple(sorted({run["name"] for run in runs if run.get("conclusion") in _BAD}))
        if failing:
            return CheckState(CheckOutcome.FAILURE, url, failing)
        if any(run["status"] != "completed" for run in runs):
            return CheckState(CheckOutcome.PENDING, url)
        return CheckState(CheckOutcome.SUCCESS, url)

    def pull_request(self, repository: str, branch: str) -> PullRequest | None:
        owner = repository.split("/", 1)[0]
        listing = self._api(f"repos/{repository}/pulls?head={owner}:{branch}&state=all&per_page=5", missing=[])
        if not listing:
            return None
        number = listing[0]["number"]
        pull = self._api(f"repos/{repository}/pulls/{number}")
        reviews = self._api(f"repos/{repository}/pulls/{number}/reviews?per_page=100", missing=[])
        approved = approval(reviews, pull["head"]["sha"])
        merged_by = (pull.get("merged_by") or {}).get("login")
        return PullRequest(
            number=number,
            url=pull["html_url"],
            head=pull["head"]["sha"],
            state="merged" if pull.get("merged_at") else pull["state"],
            merged_by=merged_by if merged_by and not _BOT.search(merged_by) else None,
            merged_at=_when(pull.get("merged_at")),
            approved_by=approved.by if approved else None,
            approved_at=approved.at if approved else None,
        )

    def tags(self, repository: str) -> tuple[str, ...]:
        if repository not in self._tags:
            refs = self._api("--paginate", f"repos/{repository}/git/matching-refs/tags", missing=[])
            self._tags[repository] = tuple(ref["ref"].removeprefix("refs/tags/") for ref in refs)
        return self._tags[repository]

    def released(self, repository: str, tag: str, asset: str) -> tuple[Released, ...]:
        if repository not in self._releases:
            self._releases[repository] = self._api(f"repos/{repository}/releases?per_page=100", missing=[])
        found = []
        for entry in self._releases[repository]:
            if not fnmatch.fnmatchcase(entry["tag_name"], tag):
                continue
            wanted = next((item for item in entry.get("assets", ()) if item["name"] == asset), None)
            found.append(Released(entry["tag_name"], entry["html_url"], tuple(item["name"] for item in entry.get("assets", ())), self._head(repository, wanted)))
        return tuple(found)

    def _head(self, repository: str, asset: Mapping[str, Any] | None) -> str:
        """The first characters of a release asset. An asset is read once for each time it was uploaded."""
        if asset is None:
            return ""
        key = (repository, asset["id"], asset.get("updated_at", ""))
        if key not in self._heads:
            done = subprocess.run(  # noqa: S603
                ["gh", "api", "-H", "Accept: application/octet-stream", f"repos/{repository}/releases/assets/{asset['id']}"],  # noqa: S607
                capture_output=True, check=False,
            )  # fmt: skip
            if done.returncode != 0:
                raise Unreachable(f"{repository}: the asset {asset['name']} could not be read")
            self._heads[key] = done.stdout[:HEAD_BYTES].decode("utf-8", errors="replace")
        return self._heads[key]

    def run(self, repository: str, workflow: str, release: Release) -> Run | None:
        payload = self._api(f"repos/{repository}/actions/workflows/{workflow}/runs?per_page=30&event=workflow_dispatch", missing={})
        for run in payload.get("workflow_runs") or []:
            # The release is matched as a whole number. Looked for as a substring, 6.1.0 would be
            # found in the title of a run for 26.1.0.
            if re.search(rf"(?<![\d.]){re.escape(release.full())}(?![\d.])", run.get("display_title") or ""):
                return Run(run["html_url"], run["conclusion"] if run["status"] == "completed" else None, run.get("head_sha") or "")
        return None

    def head(self, repository: str) -> str:
        if repository not in self._head_commits:
            self._head_commits[repository] = (self._api(f"repos/{repository}/commits/{self._default_branch(repository)}", "--jq", "{sha: .sha}") or {}).get("sha", "")
        return self._head_commits[repository]

    def get(self, url: str) -> str | None:
        try:
            with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
                return response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError):
            return None


def _package_url(index: str, package: str, version: str) -> str:
    if index == "pypi":
        return f"https://pypi.org/project/{package}/{normalize(version)}/"
    return f"https://www.npmjs.com/package/{package}/v/{version}"


class HttpPackages:
    """PyPI and npm. An artifact is downloaded once and its member names are kept."""

    def __init__(self, registry: Registry | None = None) -> None:
        self.registry = registry or HttpRegistry()
        self._names: dict[tuple[str, str, str], tuple[str, ...]] = {}

    def _published(self, index: str, package: str) -> list[tuple[str, str]]:
        """Every published version with its publication time."""
        if index == "pypi":
            document = self.registry.pypi(package)
            return [(version, max(f["upload_time_iso_8601"] for f in files)) for version, files in (document.get("releases") or {}).items() if files]
        document = self.registry.npm(package)
        return [(version, when) for version, when in (document.get("time") or {}).items() if version in (document.get("versions") or {})]

    def versions(self, index: str, package: str) -> tuple[str, ...]:
        return tuple(version for version, _ in sorted(self._published(index, package), key=lambda item: item[1]))

    def newest(self, index: str, package: str) -> str | None:
        found = self.versions(index, package)
        return found[-1] if found else None

    def _download(self, url: str) -> bytes:
        with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310
            return response.read()

    def _members(self, index: str, package: str, version: str) -> tuple[str, ...]:
        key = (index, package, version)
        if key not in self._names:
            names: tuple[str, ...] = ()
            if index == "pypi":
                files = self.registry.pypi(f"{package}/{version}").get("urls") or []
                wheel = next((f["url"] for f in files if f.get("packagetype") == "bdist_wheel"), None)
                if wheel:
                    with zipfile.ZipFile(io.BytesIO(self._download(wheel))) as archive:
                        names = tuple(archive.namelist())
            else:
                tarball = (((self.registry.npm(package).get("versions") or {}).get(version) or {}).get("dist") or {}).get("tarball")
                if tarball:
                    with tarfile.open(fileobj=io.BytesIO(self._download(tarball)), mode="r:gz") as archive:
                        names = tuple(archive.getnames())
            self._names[key] = names
        return self._names[key]

    def has_member(self, index: str, package: str, version: str, member: str) -> bool:
        return any(fnmatch.fnmatchcase(name, member) for name in self._members(index, package, version))

    def requires(self, index: str, package: str, version: str, dependency: str) -> str | None:
        for requirement in (self.registry.pypi(f"{package}/{version}").get("info") or {}).get("requires_dist") or []:
            found = re.match(rf"{re.escape(dependency)}\s*==\s*([\w.+-]+)", requirement.split(";", 1)[0].strip())
            if found:
                return found.group(1)
        return None


# ── The command ────────────────────────────────────────────────────────────────────────────────────


def revision(root: Path = ROOT) -> str:
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)  # noqa: S603, S607
    return result.stdout.strip() or "unknown"


def summarised(roster: Roster, source: Source, upstream: UpstreamRelease | None, previous: Release | None) -> ChangeSummary | None:
    """The change summary from the previous supported release, or `None` when there is nothing to compare."""
    if upstream is None or previous is None or previous >= upstream.release:
        return None
    repository = roster.supported_at.repository
    directory = str(Path(roster.supported_at.path).parent / "schemas")
    before = change_summary.bundled_schema(source, repository, directory, previous)
    after = change_summary.bundled_schema(source, repository, directory, upstream.release)
    return change_summary.summarise(previous, upstream, before, after, change_summary.HttpFetch(), roster.upstream)


def main(argv: Sequence[str] | None = None, *, upstream_source: Releases | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--release", required=True, help="X.Y.Z, with or without a leading v")
    parser.add_argument("--roster", type=Path, default=DEFAULT_ROSTER)
    parser.add_argument("--workspace", type=Path, help="a directory of sibling checkouts, each read at origin/HEAD; otherwise the organisation through gh")
    parser.add_argument("--working-tree", action="append", default=[], metavar="REPOSITORY", help="with --workspace: read this checkout as it is on disk")
    parser.add_argument("--no-changes", action="store_true", help="omit the change summary, which may download the release archive")
    parser.add_argument("--out", type=Path, required=True)
    arguments = parser.parse_args(argv)
    roster = Roster.load(arguments.roster)
    release = Release.parse(arguments.release)
    with tempfile.TemporaryDirectory() as scratch:
        source = source_for(arguments.workspace, Path(scratch), arguments.working_tree)
        try:
            upstream = find(releases(upstream_source or GhReleases(roster.upstream)), release)
            known = [candidate for candidate in supported(roster, source) if candidate < release]
            changes = None if arguments.no_changes else summarised(roster, source, upstream, known[-1] if known else None)
            evaluation = evaluate(
                roster, release, source, GhHub(), HttpPackages(), upstream,
                now=datetime.now(timezone.utc), process_revision=revision(), governance_level=None, changes=changes,
            )  # fmt: skip
        except (Unreachable, UpstreamError) as error:
            print(f"the evaluation could not be completed: {error}", file=sys.stderr)
            return 2
    arguments.out.write_text(encode(evaluation), encoding="utf-8")
    counts = ", ".join(f"{evaluation.count(verdict)} {verdict.value}" for verdict in Verdict)
    print(f"EnergyPlus {release.full()}: {len(evaluation.rows)} steps: {counts}; {len(evaluation.strangers)} stranger(s)")
    print(f"next action: {evaluation.next_action.text if evaluation.next_action else 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
