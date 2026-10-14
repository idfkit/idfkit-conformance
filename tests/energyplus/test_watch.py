"""The watch, with fake issues, a fake dispatcher and the recorded upstream listing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dispatch_waves import DispatchError
from energyplus import watch as watching
from energyplus.checklist import Recorded, marker
from energyplus.evaluate import ADOPT_WORKFLOW, NOT_DISPATCHED, RAN_AND_WAITS, Evaluation, PullRequest, Run, evaluate
from energyplus.roster import Release, Roster
from energyplus.upstream import FixtureReleases, UpstreamRelease
from energyplus.watch import DISPATCHED, EXIT_FAILED, EXIT_OK, EXIT_REFUSED, EXIT_UNREAD, EXIT_WAITING, Issue, dispatchable, watch
from support import APP, FIXTURES, HEADER, LIB, NEXT, NONE, NOW, RED, ROSTER, VERIFICATIONS, FakeHub, FakePackages, FakeSource, adopted, organisation, packages

SOURCE = FixtureReleases(FIXTURES / "releases-2026-10-13.json", FIXTURES / "tag-commits-2026-10-13.json")
ROSTERED = Roster.from_text(ROSTER)
SUPPORTED = Release(26, 1, 0)


@dataclass
class FakeIssues:
    issues: dict[Release, Issue] = field(default_factory=dict)
    created: list[Release] = field(default_factory=list)
    rewritten: list[Release] = field(default_factory=list)

    def find(self, release: Release) -> Issue | None:
        return self.issues.get(release)

    def create(self, release: Release, body: str) -> Issue:
        assert release not in self.issues, "a second issue was created for one release"
        self.issues[release] = Issue(len(self.issues) + 1, f"https://github.com/o/host/issues/{len(self.issues) + 1}", "open", None, ("energyplus-adoption",), body)
        self.created.append(release)
        return self.issues[release]

    def rewrite(self, issue: Issue, body: str) -> None:
        release = next(key for key, value in self.issues.items() if value.number == issue.number)
        self.issues[release] = Issue(issue.number, issue.url, issue.state, issue.state_reason, issue.labels, body)
        self.rewritten.append(release)


@dataclass
class FakeDispatcher:
    fail: bool = False
    sent: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)

    def dispatch(self, repository: str, workflow: str, inputs: Mapping[str, str]) -> None:
        if self.fail:
            raise DispatchError("HTTP 404: workflow not found")
        self.sent.append((repository, workflow, dict(inputs)))


def _run(tmp_path: Path, issues: FakeIssues | None = None, dispatcher: FakeDispatcher | None = None, requested: Release | None = None,
         hub: FakeHub | None = None, repos=None, published: FakePackages | None = None, roster: Roster = ROSTERED, supported: Release = SUPPORTED):  # fmt: skip
    source = FakeSource(repos or organisation())

    def evaluate_release(release: Release, upstream: UpstreamRelease, first: Recorded | None) -> Evaluation:
        return evaluate(roster, release, source, hub or FakeHub(), published or packages(released=False), upstream, now=NOW, process_revision="a1b2c3d4", governance_level=None, recorded=first)

    issues = issues if issues is not None else FakeIssues()
    return watch(roster, SOURCE, supported, issues, evaluate_release, dispatcher, records=tmp_path, requested=requested, now=NOW), issues


def test_g2_one_issue_per_release_and_a_second_run_creates_none(tmp_path: Path) -> None:
    first, issues = _run(tmp_path)
    assert issues.created == [NEXT] and issues.rewritten == []
    assert issues.issues[NEXT].body.splitlines()[0] == marker(NEXT)
    second, _ = _run(tmp_path, issues)
    assert issues.created == [NEXT] and issues.rewritten == [NEXT]
    assert first.exit_code == second.exit_code == EXIT_WAITING


def test_g2_the_marker_search_covers_closed_issues(tmp_path: Path) -> None:
    closed = Issue(4, "https://github.com/o/host/issues/4", "closed", "not_planned", ("energyplus-adoption", "set-aside"), marker(NEXT))
    outcome, issues = _run(tmp_path, FakeIssues({NEXT: closed}))
    assert issues.created == [] and issues.rewritten == [] and outcome.evaluations == ()
    assert "set aside" in outcome.lines[0] and outcome.exit_code == EXIT_OK


def test_g3_a_tag_that_is_not_exactly_a_release_never_produces_an_issue(tmp_path: Path) -> None:
    _, issues = _run(tmp_path, supported=Release(25, 2, 0))
    assert set(issues.issues) == {Release(26, 1, 0), NEXT}


def test_a_candidate_as_input_is_refused_with_exit_four() -> None:
    assert watching.main(["--release", "26.2.0-RC3"]) == EXIT_REFUSED


def test_an_unpublished_release_as_input_is_refused_with_its_cause(tmp_path: Path) -> None:
    outcome, issues = _run(tmp_path, requested=Release(99, 9, 0))
    assert outcome.exit_code == EXIT_REFUSED and "no release tagged v99.9.0" in outcome.lines[0]
    assert issues.created == []


def test_fr010_a_newer_release_is_refused_while_an_older_one_is_open(tmp_path: Path) -> None:
    outcome, issues = _run(tmp_path, requested=NEXT, supported=Release(25, 2, 0))
    assert outcome.exit_code == EXIT_REFUSED and "older release 26.1.0 awaits adoption" in outcome.lines[0]
    done = Issue(1, "https://github.com/o/host/issues/1", "closed", "completed", ("energyplus-adoption",), marker(Release(26, 1, 0)))
    outcome, _ = _run(tmp_path, FakeIssues({Release(26, 1, 0): done}), requested=NEXT, supported=Release(25, 2, 0))
    assert outcome.exit_code != EXIT_REFUSED


def test_a_newer_release_is_evaluated_and_not_dispatched_while_an_older_one_is_open(tmp_path: Path) -> None:
    dispatcher = FakeDispatcher()
    repos = organisation()
    files = repos["o/lib"]
    assert files is not None
    files["versions.py"] = "ENERGYPLUS_VERSIONS = (\n    (25, 2, 0),\n)\n"
    outcome, issues = _run(tmp_path, dispatcher=dispatcher, supported=Release(25, 2, 0), repos=repos)
    assert issues.created == [Release(26, 1, 0), NEXT] and len(outcome.evaluations) == 2
    assert {inputs["release"] for _, _, inputs in dispatcher.sent} == {"26.1.0"}
    assert any("dispatch is withheld" in line for line in outcome.lines)


def test_dispatch_sends_each_open_project_once_and_marks_its_steps(tmp_path: Path) -> None:
    dispatcher = FakeDispatcher()
    outcome, issues = _run(tmp_path, dispatcher=dispatcher)
    assert dispatcher.sent == [("o/lib", ADOPT_WORKFLOW, {"release": "26.2.0"})]
    reasons = {row.reason for row in outcome.evaluations[0].of("lib")}
    assert reasons == {DISPATCHED} and NOT_DISPATCHED not in issues.issues[NEXT].body


def _dispatchable(tmp_path: Path, **more) -> tuple[str, ...]:
    outcome, _ = _run(tmp_path, **more)
    return dispatchable(more.get("roster", ROSTERED), outcome.evaluations[0])


def test_the_five_conditions_of_dispatch(tmp_path: Path) -> None:
    assert _dispatchable(tmp_path) == ("lib",)
    # Its openings do not hold.
    assert "app" not in _dispatchable(tmp_path)
    # It has a lag.
    lag = Roster.from_text(HEADER + LIB + '\n  [project.lag]\n  kind = "deliberate"\n  reason = "It adopts with its platform."\n' + APP + NONE + VERIFICATIONS)
    assert _dispatchable(tmp_path, roster=lag) == ()
    # It has an open adoption pull request.
    assert _dispatchable(tmp_path, hub=FakeHub(pulls={"o/lib": PullRequest(7, "https://github.com/o/lib/pull/7", "abc", "open")})) == ()
    # A run is in progress.
    assert _dispatchable(tmp_path, hub=FakeHub(runs={"o/lib": Run("https://ci/run/1", None)})) == ()
    # Every step is passed.
    assert _dispatchable(tmp_path, repos=adopted()) == ()
    # Its default branch was failing before any adoption.
    assert _dispatchable(tmp_path, hub=FakeHub(states={("o/lib", None): RED})) == ()


def test_a_dispatch_that_does_not_land_is_exit_one_and_never_a_skip(tmp_path: Path) -> None:
    outcome, issues = _run(tmp_path, dispatcher=FakeDispatcher(fail=True))
    assert outcome.exit_code == EXIT_FAILED
    assert any("dispatch did not land: lib: HTTP 404" in line for line in outcome.lines)
    assert NOT_DISPATCHED in issues.issues[NEXT].body


def test_g4_a_recorded_adoption_dispatches_nothing_and_writes_nothing(tmp_path: Path) -> None:
    (tmp_path / "26.2.0.toml").write_text("")
    dispatcher = FakeDispatcher()
    outcome, issues = _run(tmp_path, dispatcher=dispatcher)
    assert outcome.exit_code == EXIT_OK and "No release awaits adoption" in outcome.lines[0]
    requested, _ = _run(tmp_path, issues, dispatcher, requested=NEXT)
    assert "Nothing to do" in requested.lines[0]
    assert (dispatcher.sent, issues.created, issues.rewritten) == ([], [], [])


def test_g5_a_complete_adoption_writes_only_the_record(tmp_path: Path) -> None:
    dispatcher = FakeDispatcher()
    outcome, issues = _run(tmp_path, dispatcher=dispatcher, repos=adopted(), published=packages(released=True))
    assert outcome.exit_code == EXIT_OK and dispatcher.sent == []
    assert [path.name for path in outcome.records] == ["26.2.0.toml"] and (tmp_path / "26.2.0.toml").is_file()
    assert "**Next action**: none is owed." in issues.issues[NEXT].body


def test_a_changed_source_withholds_dispatch(tmp_path: Path) -> None:
    _, issues = _run(tmp_path)
    tampered = issues.issues[NEXT]
    issues.issues[NEXT] = Issue(tampered.number, tampered.url, "open", None, tampered.labels, tampered.body.replace("4bd7a1f26fb749a83257855aba6760e26c8a3c8f", "0" * 40))
    dispatcher = FakeDispatcher()
    outcome, _ = _run(tmp_path, issues, dispatcher)
    assert dispatcher.sent == [] and "SOURCE CHANGED" in issues.issues[NEXT].body and outcome.exit_code == EXIT_WAITING


def test_the_exit_codes_are_distinct(tmp_path: Path) -> None:
    assert len({EXIT_OK, EXIT_FAILED, EXIT_UNREAD, EXIT_WAITING, EXIT_REFUSED}) == 5
    repos = organisation()
    repos["o/app"] = None
    assert _run(tmp_path, repos=repos, dispatcher=FakeDispatcher())[0].exit_code == EXIT_UNREAD


# The watch and a project that waits do not dispatch each other without end.


def test_a_project_whose_run_proposed_nothing_is_not_dispatched_again_on_the_same_commit(tmp_path: Path) -> None:
    ran = FakeHub(runs={"o/lib": Run("https://github.com/o/lib/actions/runs/1", "success", "c0ffee")}, heads={"o/lib": "c0ffee"})
    outcome, _ = _run(tmp_path, hub=ran)
    assert dispatchable(ROSTERED, outcome.evaluations[0]) == ()
    assert RAN_AND_WAITS in {row.reason for row in outcome.evaluations[0].of("lib")}


def test_it_is_dispatched_again_once_its_default_branch_has_moved(tmp_path: Path) -> None:
    moved = FakeHub(runs={"o/lib": Run("https://github.com/o/lib/actions/runs/1", "success", "c0ffee")}, heads={"o/lib": "f00d"})
    assert _dispatchable(tmp_path, hub=moved) == ("lib",)
