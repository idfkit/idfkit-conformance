"""Rules C1 to C9 of contracts/adoption-record.md, as properties of the rendered checklist."""

from __future__ import annotations

import re
from datetime import timedelta

from energyplus.checklist import marked_release, marker, recorded_source, render
from energyplus.evaluate import Evaluation, PullRequest, Recorded, Verdict, consumed, evaluate
from energyplus.roster import Roster
from support import APP, HEADER, LIB, NEXT, NONE, NOW, RED, ROSTER, UPSTREAM, VERIFICATIONS, FakeHub, FakeSource, adopted, organisation, packages
from test_evaluate import HEAD, OPEN, _branch

DEPLOYS = APP.replace('gate = "npm test"', 'gate = "npm test"\nmerge_effect = "Merging deploys the hosted service."')
LAGGING = Roster.from_text(HEADER + LIB + DEPLOYS + '\n  [project.lag]\n  kind = "deliberate"\n  reason = "It adopts with its platform."\n' + NONE + VERIFICATIONS)


def _evaluation(roster: Roster | None = None, hub: FakeHub | None = None, repos=None, released: bool = False) -> Evaluation:
    roster = roster or Roster.from_text(ROSTER)
    hub = hub or FakeHub(branches=_branch(), pulls={"o/lib": OPEN})
    return evaluate(roster, NEXT, FakeSource(repos or organisation()), hub, packages(released=released), UPSTREAM, now=NOW, process_revision="a1b2c3d4e5", governance_level=None)


def _table_rows(text: str) -> list[list[str]]:
    rows = [line for line in text.splitlines() if line.startswith("| ") and not line.startswith(("| ---", "| Project", "| Passed", "| Role"))]
    return [[cell.strip() for cell in line.strip("|").split(" | ")] for line in rows if not re.match(r"\| \d+ \|", line) and "`EnergyPlus-" not in line and "sha256sums" not in line]


def test_the_marker_is_the_first_line() -> None:
    text = render(_evaluation(), NOW)
    assert text.splitlines()[0] == "<!-- energyplus-adoption: 26.2.0 -->" == marker(NEXT)
    assert text.splitlines()[1] == "# Adopt EnergyPlus 26.2.0"
    assert marked_release(text) == NEXT and marked_release("# Adopt EnergyPlus 26.2.0") is None


def test_c1_every_step_appears_exactly_once() -> None:
    for roster in (None, LAGGING):
        evaluation = _evaluation(roster)
        text = render(evaluation, NOW)
        for row in evaluation.rows:
            assert len(re.findall(rf"^\| {re.escape(row.project)} \| {re.escape(row.title)}[ |]", text, re.MULTILINE)) == 1, (row.project, row.title)


def test_c2_the_verdict_column_takes_only_the_five_values() -> None:
    allowed = {verdict.value.replace("-", " ") for verdict in Verdict}
    rows = [cells for cells in _table_rows(render(_evaluation(), NOW)) if len(cells) == 7]
    assert len(rows) == 13 and {cells[3] for cells in rows} <= allowed


def test_c3_every_passed_row_has_an_evidence_link() -> None:
    for evaluation in (_evaluation(), _evaluation(repos=adopted(), released=True)):
        passed = [cells for cells in _table_rows(render(evaluation, NOW)) if len(cells) == 7 and cells[3] == "passed"]
        assert passed and all(re.match(r"\[[^\]]+\]\(https://[^)]+\)", cells[5]) for cells in passed)


def test_c4_an_unresolved_reviewed_or_manual_row_is_followed_by_what_to_inspect() -> None:
    text = render(_evaluation(), NOW)
    assert "> **lib, Add the sweep row.** Inspect: The findings count in the new row." in text
    assert "> **app, Verify by hand.** Inspect: The model on disk." in text
    assert "Inspect:" not in render(_evaluation(repos=adopted(), released=True), NOW)


def test_c5_every_not_applicable_row_has_a_reason() -> None:
    text = render(_evaluation(LAGGING), NOW)
    section = text.split("## Not applicable")[1].split("##")[0]
    assert section.count("Deliberate lag: It adopts with its platform.") == 2


def test_c6_the_line_after_the_title_names_one_next_action_and_who_takes_it() -> None:
    line = render(_evaluation(), NOW).splitlines()[3]
    assert line == f"**Next action**: review lib: Add the sweep row [#7]({OPEN.url}) (a maintainer of lib)"
    assert render(_evaluation(repos=adopted(), released=True), NOW).splitlines()[3] == "**Next action**: none is owed."


def test_c7_the_evaluation_time_is_stated_and_an_old_one_is_stale() -> None:
    evaluation = _evaluation()
    stated = "Evaluated 2026-10-14 05:23 UTC at idfkit-conformance `a1b2c3d`, roster at the working tree."
    assert stated in render(evaluation, NOW + timedelta(days=2)).splitlines()
    assert "STALE: " + stated in render(evaluation, NOW + timedelta(days=4)).splitlines()


def test_c8_a_step_whose_merge_deploys_says_so_in_its_row() -> None:
    text = render(_evaluation(LAGGING), NOW)
    rows = [line for line in text.splitlines() if line.startswith("| app |")]
    assert len(rows) == 2 and all("Merging deploys the hosted service." in line for line in rows)
    assert all("Merging deploys" not in line for line in text.splitlines() if line.startswith("| lib |"))


def test_c9_sections_follow_the_order_in_which_projects_open() -> None:
    headings = [line for line in render(_evaluation(), NOW).splitlines() if line.startswith("## ")]
    assert headings == ["## Opened: upstream assets present", "## Waiting: a release of lib that carries the release", "## End-to-end verifications", "## Source"]


def test_a_failed_row_states_its_cause() -> None:
    hub = FakeHub(branches=_branch(), pulls={"o/lib": OPEN}, states={("o/lib", HEAD): RED})
    assert "| failed | proposed | [evidence](https://ci/red) failing: lint |" in render(_evaluation(hub=hub), NOW)


def test_an_acceptance_is_stated_with_who_and_when() -> None:
    approved = PullRequest(7, OPEN.url, HEAD, "open", approved_by="reviewer", approved_at=NOW)
    text = render(_evaluation(hub=FakeHub(branches=_branch(), pulls={"o/lib": approved})), NOW)
    assert "| @reviewer, 2026-10-14 |" in text


def test_the_source_table_is_read_back_as_the_recorded_identity() -> None:
    assert UPSTREAM is not None
    text = render(_evaluation(), NOW)
    assert "| linux-x86_64 | `EnergyPlus-26.2.0-4bd7a1f26f-Linux-Ubuntu24.04-x86_64.tar.gz` | 242428036 | `b848da8236482a2171a67d7eab00c82162105b653ffab3ae3b6c2217c433a8a1` |" in text
    assert recorded_source(text) == Recorded(UPSTREAM.commit, consumed(UPSTREAM))
    assert recorded_source("# no table") is None
    assert "Source unchanged since first evaluation." in text
