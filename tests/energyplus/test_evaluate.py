"""The nine verdict rules of data-model section 2, each from fake GitHub facts."""

from __future__ import annotations

from energyplus.evaluate import (
    PRE_EXISTING,
    approval,
    CheckOutcome,
    CheckState,
    Evaluation,
    Landing,
    PullRequest,
    Recorded,
    Released,
    Run,
    SourceAsset,
    StepResult,
    Verdict,
    consumed,
    evaluate,
)
from energyplus.roster import AssetRole, Release, Roster
from energyplus.serial import decode, encode
from support import APP, HEADER, LIB, NEXT, NONE, NOW, RED, ROSTER, UPSTREAM, VERIFICATIONS, FakeHub, FakePackages, FakeSource, adopted, organisation, packages

ROSTERED = Roster.from_text(ROSTER)
HEAD = "abc123"
OPEN = PullRequest(7, "https://github.com/o/lib/pull/7", HEAD, "open")


def _evaluate(repos=None, hub: FakeHub | None = None, published: FakePackages | None = None, roster: Roster = ROSTERED, release: Release = NEXT, **more) -> Evaluation:
    return evaluate(
        roster, release, FakeSource(repos or organisation()), hub or FakeHub(), published or packages(released=False), UPSTREAM,
        now=NOW, process_revision="a1b2c3d4", governance_level=None, **more,
    )  # fmt: skip


def _row(evaluation: Evaluation, project: str, step: str) -> StepResult:
    return next(row for row in evaluation.rows if (row.project, row.step) == (project, step))


def _branch(**changes: str) -> dict[tuple[str, str], dict[str, str]]:
    files = dict(adopted()["o/lib"] or {})
    files.update(changes)
    return {("o/lib", HEAD): files}


def test_every_step_of_the_roster_is_a_row_exactly_once() -> None:
    evaluation = _evaluate()
    assert len(evaluation.rows) == 13
    assert len({(row.project, row.step) for row in evaluation.rows}) == 13
    assert all(isinstance(row.verdict, Verdict) for row in evaluation.rows)


def test_rule_1_a_lag_makes_every_step_not_applicable_with_its_reason() -> None:
    text = HEADER + LIB + APP + '\n  [project.lag]\n  kind = "deliberate"\n  reason = "It adopts with its platform."\n' + NONE + VERIFICATIONS
    evaluation = _evaluate(roster=Roster.from_text(text))
    rows = evaluation.of("app")
    assert [row.verdict for row in rows] == [Verdict.NOT_APPLICABLE] * 2
    assert all(row.reason == "Deliberate lag: It adopts with its platform." and row.landing is Landing.NONE for row in rows)


def test_rule_2_what_holds_on_the_default_branch_is_passed() -> None:
    evaluation = _evaluate(adopted(), published=packages(released=True))
    assert {row.verdict for row in evaluation.rows} == {Verdict.PASSED}
    assert _row(evaluation, "lib", "registry").landing is Landing.MERGED
    assert _row(evaluation, "app", "level").landing is Landing.RELEASED
    assert all(row.evidence for row in evaluation.rows)
    assert evaluation.next_action is None and evaluation.complete()


def test_rule_2_wins_over_an_open_pull_request() -> None:
    hub = FakeHub(pulls={"o/lib": OPEN}, states={("o/lib", HEAD): RED})
    evaluation = _evaluate(adopted(), hub, packages(released=True))
    assert _row(evaluation, "lib", "gate").verdict is Verdict.PASSED
    assert _row(evaluation, "lib", "gate").landing is Landing.MERGED


def test_rule_2_counts_a_step_done_by_hand_outside_a_run() -> None:
    accepted = _row(_evaluate(adopted(), published=packages(released=True)), "lib", "sweep-row")
    assert accepted.verdict is Verdict.PASSED and "outside an adoption run" in (accepted.detail or "")


def test_rule_3_a_project_whose_opening_does_not_hold_is_not_started() -> None:
    rows = _evaluate().of("app")
    assert [row.verdict for row in rows] == [Verdict.NOT_STARTED] * 2
    assert all("waiting on a release of lib" in (row.reason or "") for row in rows)


def test_rule_4_a_failing_default_branch_is_failed_as_pre_existing() -> None:
    evaluation = _evaluate(hub=FakeHub(states={("o/lib", None): RED}))
    assert {(row.verdict, row.reason) for row in evaluation.of("lib")} == {(Verdict.FAILED, PRE_EXISTING)}
    assert "lint" in (_row(evaluation, "lib", "registry").detail or "")


def test_rule_5_an_automated_step_that_holds_on_the_adoption_branch_is_passed_and_proposed() -> None:
    evaluation = _evaluate(hub=FakeHub(branches=_branch(), pulls={"o/lib": OPEN}))
    for step in ("registry", "statements", "gate"):
        assert (_row(evaluation, "lib", step).verdict, _row(evaluation, "lib", step).landing) == (Verdict.PASSED, Landing.PROPOSED)


def test_rule_6_a_reviewed_step_on_the_adoption_branch_awaits_a_person() -> None:
    evaluation = _evaluate(hub=FakeHub(branches=_branch(), pulls={"o/lib": OPEN}))
    row = _row(evaluation, "lib", "sweep-row")
    assert (row.verdict, row.landing, row.evidence) == (Verdict.AWAITING_PERSON, Landing.PROPOSED, OPEN.url)
    assert row.inspect == "The findings count in the new row."
    assert evaluation.next_action is not None and evaluation.next_action.text == "review lib: Add the sweep row"


def test_an_approval_by_a_person_passes_a_reviewed_step() -> None:
    approved = PullRequest(7, OPEN.url, HEAD, "open", approved_by="reviewer", approved_at=NOW)
    row = _row(_evaluate(hub=FakeHub(branches=_branch(), pulls={"o/lib": approved})), "lib", "sweep-row")
    assert (row.verdict, row.accepted_by, row.accepted_at) == (Verdict.PASSED, "reviewer", NOW)


def test_rule_7_a_failed_check_on_the_adoption_branch_is_failed_and_names_the_check() -> None:
    hub = FakeHub(branches=_branch(), pulls={"o/lib": OPEN}, states={("o/lib", HEAD): RED})
    row = _row(_evaluate(hub=hub), "lib", "gate")
    assert (row.verdict, row.landing, row.reason) == (Verdict.FAILED, Landing.PROPOSED, "failing: lint")


def test_rule_7_a_failed_adoption_run_is_failed() -> None:
    hub = FakeHub(runs={"o/lib": Run("https://ci/run/1", "failure")})
    row = _row(_evaluate(hub=hub), "lib", "registry")
    assert row.verdict is Verdict.FAILED and row.evidence == "https://ci/run/1"


def test_rule_8_a_run_or_a_check_in_progress_is_not_started() -> None:
    running = _row(_evaluate(hub=FakeHub(runs={"o/lib": Run("https://ci/run/1", None)})), "lib", "registry")
    assert running.verdict is Verdict.NOT_STARTED and "in progress" in (running.reason or "")
    pending = FakeHub(branches=_branch(), pulls={"o/lib": OPEN}, states={("o/lib", HEAD): CheckState(CheckOutcome.PENDING)})
    assert _row(_evaluate(hub=pending), "lib", "gate").verdict is Verdict.NOT_STARTED


def test_rule_9_an_open_project_with_nothing_dispatched_is_not_started() -> None:
    row = _row(_evaluate(), "lib", "registry")
    assert (row.verdict, row.landing, row.reason) == (Verdict.NOT_STARTED, Landing.NONE, "open; no adoption has been dispatched")


def test_a_green_branch_that_does_not_carry_the_release_passes_nothing() -> None:
    assert _row(_evaluate(), "lib", "gate").verdict is Verdict.NOT_STARTED
    stale = _branch(**{"README.md": "Supports v8.9 through v26.1 (2 versions).\n"})
    evaluation = _evaluate(hub=FakeHub(branches=stale, pulls={"o/lib": OPEN}))
    assert _row(evaluation, "lib", "statements").verdict is Verdict.FAILED
    assert _row(evaluation, "lib", "gate").verdict is Verdict.FAILED


def test_a_pinned_level_is_followed_to_the_published_artifact() -> None:
    repos = adopted()
    repos["o/app"] = {"pyproject.toml": 'dependencies = ["lib==1.0.0"]\n'}
    row = _row(_evaluate(repos, published=packages(released=True)), "app", "level")
    assert row.verdict is Verdict.NOT_STARTED and "library bump" in (row.reason or "")
    assert "lib 1.0.0 (pinned in pyproject.toml) does not carry 26.2.0" in (row.detail or "")


def test_the_next_action_names_the_publication_that_is_owed() -> None:
    repos = adopted()
    repos["o/app"] = {"pyproject.toml": 'dependencies = ["lib==1.0.0"]\n'}
    action = _evaluate(repos).next_action
    assert action is not None and action.text == "publish lib with the release. 1 project(s) are waiting on it."


def test_the_next_action_is_the_first_failed_or_awaiting_row_in_roster_order() -> None:
    hub = FakeHub(branches=_branch(), pulls={"o/lib": OPEN}, states={("o/lib", HEAD): RED})
    action = _evaluate(hub=hub).next_action
    assert action is not None and action.text.startswith("review lib: Add the sweep row")


def test_a_passed_pull_request_that_nobody_merged_is_the_next_action_and_is_not_complete() -> None:
    approved = PullRequest(7, OPEN.url, HEAD, "open", approved_by="reviewer", approved_at=NOW)
    evaluation = _evaluate(hub=FakeHub(branches=_branch(), pulls={"o/lib": approved}), published=packages(released=True))
    assert evaluation.next_action is not None and evaluation.next_action.text.startswith("merge the lib adoption pull request")
    assert not evaluation.complete()


def test_the_supported_release_evaluates_to_nothing_to_do() -> None:
    evaluation = _evaluate(release=Release(26, 1, 0), published=FakePackages({("pypi", "lib"): {"1.0.0": ("lib/schemas/V26-1-0/schema.gz",)}}))
    assert {row.verdict for row in evaluation.rows} == {Verdict.PASSED} and evaluation.next_action is None


def test_consumed_assets_are_recorded_with_their_digests() -> None:
    sources = consumed(UPSTREAM)
    assert [asset.role for asset in sources] == [AssetRole.LINUX_X86_64, AssetRole.LINUX_ARM64, AssetRole.CHECKSUMS]
    assert sources[0].sha256 == "b848da8236482a2171a67d7eab00c82162105b653ffab3ae3b6c2217c433a8a1"


def test_a_changed_upstream_source_is_a_failed_row_and_blocks_completion() -> None:
    sources = consumed(UPSTREAM)
    assert UPSTREAM is not None
    same = _evaluate(adopted(), published=packages(released=True), recorded=Recorded(UPSTREAM.commit, sources))
    assert same.source_changed == () and same.complete()
    moved = Recorded("0" * 40, (SourceAsset(sources[0].role, sources[0].name, sources[0].size, "f" * 64), *sources[1:]))
    evaluation = _evaluate(adopted(), published=packages(released=True), recorded=moved)
    assert len(evaluation.source_changed) == 2 and not evaluation.complete()
    assert (evaluation.rows[0].verdict, evaluation.rows[0].reason) == (Verdict.FAILED, "upstream source changed")
    assert evaluation.next_action is not None and "upstream source changed" in evaluation.next_action.text


def test_an_evaluation_survives_the_file_between_two_commands() -> None:
    hub = FakeHub(branches=_branch(), pulls={"o/lib": PullRequest(7, OPEN.url, HEAD, "open", approved_by="reviewer", approved_at=NOW)})
    evaluation = _evaluate(hub=hub)
    assert decode(Evaluation, encode(evaluation)) == evaluation


def test_strangers_are_carried_into_the_evaluation() -> None:
    repos = organisation()
    files = repos["o/lib"]
    assert files is not None
    files["notes.md"] = "The latest is 26.1.0.\n"
    evaluation = _evaluate(repos)
    assert [(o.project, o.path, o.line) for o in evaluation.strangers] == [("lib", "notes.md", 1)]
    assert not evaluation.complete()


# An approval stands for the commit it was given on.


def _review(login: str, state: str, commit: str, at: str) -> dict[str, object]:
    return {"user": {"login": login}, "state": state, "commit_id": commit, "submitted_at": at}


def test_an_approval_of_the_head_commit_stands() -> None:
    found = approval([_review("ada", "APPROVED", "h2", "2026-10-02T00:00:00Z")], "h2")
    assert found is not None and found.by == "ada"


def test_an_approval_of_an_earlier_generation_does_not_stand() -> None:
    assert approval([_review("ada", "APPROVED", "h1", "2026-10-01T00:00:00Z")], "h2") is None


def test_a_later_request_for_changes_replaces_an_approval() -> None:
    reviews = [_review("ada", "APPROVED", "h2", "2026-10-01T00:00:00Z"), _review("ada", "CHANGES_REQUESTED", "h2", "2026-10-02T00:00:00Z")]
    assert approval(reviews, "h2") is None


def test_a_comment_does_not_replace_an_approval_and_a_bot_does_not_approve() -> None:
    reviews = [
        _review("ada", "APPROVED", "h2", "2026-10-01T00:00:00Z"),
        _review("ada", "COMMENTED", "h2", "2026-10-02T00:00:00Z"),
        _review("idfkit-bot[bot]", "APPROVED", "h2", "2026-10-03T00:00:00Z"),
    ]
    found = approval(reviews, "h2")
    assert found is not None and found.by == "ada"


# A release is identified by what it holds, where its tag counts something else.

BUILT = LIB.replace(
    '  [[project.step]]\n  id = "statements"',
    """  [[project.step]]
  id = "build"
  title = "Build the engine"
  class = "automated"
  run = "true"
    [project.step.verify]
    kind = "release-states"
    tag = "wasm-v{line}.*"
    asset = "Energy+.idd"
    pattern = '^!IDD_Version (?P<release>\\d+\\.\\d+\\.\\d+)'
    form = "full"
    beside = ["engine.wasm"]

  [[project.step]]
  id = "statements\"""",
)
BUILDS = Roster.from_text(HEADER + BUILT + NONE + VERIFICATIONS)


def _built(*found: Released) -> StepResult:
    return _row(_evaluate(roster=BUILDS, hub=FakeHub(releases={"o/lib": found})), "lib", "build")


def test_a_rebuild_tagged_with_the_next_patch_does_not_stand_for_that_release() -> None:
    rebuild = Released("wasm-v26.2.0", "u", ("Energy+.idd", "engine.wasm"), "!IDD_Version 26.1.0\n")
    assert _built(rebuild).verdict is not Verdict.PASSED


def test_a_release_that_states_the_adopted_release_holds_whatever_its_patch() -> None:
    assert _built(Released("wasm-v26.2.3", "u", ("Energy+.idd", "engine.wasm"), "!IDD_Version 26.2.0\n")).verdict is Verdict.PASSED


def test_a_release_without_the_assets_named_beside_does_not_hold() -> None:
    row = _built(Released("wasm-v26.2.0", "u", ("Energy+.idd",), "!IDD_Version 26.2.0\n"))
    assert row.verdict is not Verdict.PASSED and "lacks engine.wasm" in (row.detail or "")
