"""Drive ``runners/geometry_check.py``'s guards over the shared fixture table.

``guard_fixtures.json`` is the contract between the two runners' guards: this file asserts that the
Python removal of each clause produces exactly the rings the table records, and ``test-guard.mjs``
asserts the same of ``geometry-check.mjs``. Both harnesses decode the table the same way, so a case
added there constrains both implementations.

Run it from the root of the repository::

    python -m pytest runners/tests/test_guard.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Final

RUNNERS: Final = Path(__file__).resolve().parents[1]
if str(RUNNERS) not in sys.path:
    sys.path.insert(0, str(RUNNERS))

import geometry_check  # noqa: E402
import pytest  # noqa: E402

TABLE: Final[dict[str, Any]] = json.loads(
    (Path(__file__).resolve().parent / "guard_fixtures.json").read_text(encoding="utf-8")
)
CLAUSES: Final[dict[str, Any]] = TABLE["clauses"]
TOLERANCE_M: Final[float] = float(TABLE["tolerance_m"])

#: Every case in the table, each carrying the clause it belongs to, so that one parametrisation
#: covers all four guards and a clause added to the table is picked up without a code change here.
CASES: Final[list[tuple[str, dict[str, Any]]]] = [
    (clause, case) for clause, entry in sorted(CLAUSES.items()) for case in entry["cases"]
]

#: The cases whose clause is in scope. ``remove`` is defined only where ``applies`` holds, because
#: that is the only place the runner calls it: removing a clause from a model that never triggered
#: it would apply the clause a second time rather than undo it. The out-of-scope cases are asserted
#: by :func:`test_a_model_out_of_scope_is_compared_as_the_library_returned_it` instead.
APPLYING: Final[list[tuple[str, dict[str, Any]]]] = [(c, k) for c, k in CASES if k["applies"]]


def ids(cases: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [f"{clause}: {case['name']}" for clause, case in cases]


IDS: Final[list[str]] = ids(CASES)


def extraction(case: dict[str, Any], key: str = "surfaces") -> geometry_check.Extraction:
    """One side of a case as the runner's own shape.

    A case states only the declarations its clause reads; the rest take the values a model that
    declares nothing is read as, which is what the runner's own defaults are for.
    """
    declares: dict[str, Any] = case.get("declares", {})
    return geometry_check.Extraction(
        surfaces=tuple(
            geometry_check.Extracted(
                name=surface["name"],
                vertices=tuple((float(x), float(y), float(z)) for x, y, z in surface["vertices"]),
                parent_surface=surface["parent_surface"],
                zone=surface.get("zone", ""),
            )
            for surface in case[key]
        ),
        entry_direction=declares.get("entry_direction", "Counterclockwise"),
        coordinate_system=declares.get("coordinate_system", "Relative"),
        starting_vertex_position=declares.get("starting_vertex_position", "UpperLeftCorner"),
        north_axis=float(declares.get("north_axis", 0.0)),
        zone_origins={
            name: (float(x), float(y), float(z))
            for name, (x, y, z) in case.get("zone_origins", {}).items()
        },
    )


def assert_close(found: geometry_check.Extraction, wanted: geometry_check.Extraction, note: str) -> None:
    """The same extraction within a nanometre.

    Compared rather than equated because two of the four clauses are undone by a rotation, and a
    rotation through a right angle is not exact in binary floating point. Everything about a surface
    that is not a coordinate is still equated exactly.
    """
    assert [s.name for s in found.surfaces] == [s.name for s in wanted.surfaces], note
    for one, other in zip(found.surfaces, wanted.surfaces, strict=True):
        assert one.parent_surface == other.parent_surface, note
        assert one.zone == other.zone, note
        assert len(one.vertices) == len(other.vertices), note
        for here, there in zip(one.vertices, other.vertices, strict=True):
            for a, b in zip(here, there, strict=True):
                assert abs(a - b) <= TOLERANCE_M, f"{note}: {here} against {there}"


@pytest.mark.parametrize(("clause", "case"), APPLYING, ids=ids(APPLYING))
def test_the_clause_is_removed_as_the_table_records(clause: str, case: dict[str, Any]) -> None:
    guard = geometry_check.GUARDS[clause]
    assert_close(guard.remove(extraction(case)), extraction(case, "expected"), case.get("note", ""))


@pytest.mark.parametrize(("clause", "case"), CASES, ids=IDS)
def test_the_clause_is_in_scope_only_where_the_model_declares_it(clause: str, case: dict[str, Any]) -> None:
    """The half of a guard that decides which models it touches at all.

    A guard that removed its clause everywhere would measure the distance between two wrong answers
    on every model that never declared the condition, and the fixtures that came out wrong would
    read as the clause firing where it should not.
    """
    guard = geometry_check.GUARDS[clause]
    assert guard.applies(extraction(case)) is case["applies"], case.get("note", "")


@pytest.mark.parametrize(("clause", "case"), APPLYING, ids=ids(APPLYING))
def test_a_guard_never_changes_which_surfaces_there_are(clause: str, case: dict[str, Any]) -> None:
    """A clause removed changes where surfaces are, never which ones or how many vertices each has.

    A guard that dropped or added a surface would fail the named fixture for a reason that has
    nothing to do with the clause, and the run would report the clause as load-bearing anyway.
    """
    before = extraction(case)
    after = geometry_check.GUARDS[clause].remove(before)
    assert [(s.name, len(s.vertices)) for s in after.surfaces] == [
        (s.name, len(s.vertices)) for s in before.surfaces
    ]


@pytest.mark.parametrize(
    ("clause", "case"),
    [row for row in APPLYING if CLAUSES[row[0]].get("involution")],
    ids=ids([row for row in APPLYING if CLAUSES[row[0]].get("involution")]),
)
def test_removing_the_clause_twice_removes_it_never(clause: str, case: dict[str, Any]) -> None:
    """The clause is its own inverse, which is what makes undoing it on the output exact.

    If it were not, the guard would be measuring the distance between two wrong answers rather than
    the distance between the right one and the one a library without the clause would give. Only the
    entry direction claims this: undoing a rotation twice turns the building through twice the axis.
    """
    guard = geometry_check.GUARDS[clause]
    once = guard.remove(extraction(case))
    assert_close(guard.remove(once), extraction(case), case.get("note", ""))


@pytest.mark.parametrize(
    ("clause", "case"),
    [row for row in APPLYING if CLAUSES[row[0]].get("holds_the_head")],
    ids=ids([row for row in APPLYING if CLAUSES[row[0]].get("holds_the_head")]),
)
def test_the_first_vertex_never_moves(clause: str, case: dict[str, Any]) -> None:
    """FR-008's line, held on the guard side too.

    The check's ring comparison is insensitive to where a ring starts, so a guard that reversed the
    whole list would produce a different starting vertex and nothing downstream would ever say so.
    """
    found = geometry_check.GUARDS[clause].remove(extraction(case))
    for before, after in zip(extraction(case).surfaces, found.surfaces, strict=True):
        assert before.vertices[0] == after.vertices[0]


@pytest.mark.parametrize(
    ("clause", "case"),
    [row for row in CASES if not row[1]["applies"]],
    ids=ids([row for row in CASES if not row[1]["applies"]]),
)
def test_a_model_out_of_scope_is_compared_as_the_library_returned_it(clause: str, case: dict[str, Any]) -> None:
    """A model the clause never touched reaches the comparison unchanged, and the table says so.

    Asserted on the table rather than on the guard, because the guard is never called for these:
    the runner asks ``applies`` first. What could go wrong is the table recording a mutation for a
    case the runner will never mutate, which would read as a guarantee nothing enforces.
    """
    assert case["expected"] == case["surfaces"], case.get("note", "")


@pytest.mark.parametrize("clause", sorted(CLAUSES), ids=sorted(CLAUSES))
def test_the_guard_names_the_fixture_and_the_magnitude_the_table_records(clause: str) -> None:
    """The recorded measurement is part of the contract, not a threshold either side may relax."""
    guard = geometry_check.GUARDS[clause]
    assert guard.name == clause
    assert guard.fails_on == CLAUSES[clause]["guard"]["fails_on"]
    assert guard.at_least_m == CLAUSES[clause]["guard"]["at_least_m"]


def test_the_table_and_the_runner_name_the_same_guards() -> None:
    """A guard added to one and not the other is the failure this catches."""
    assert sorted(geometry_check.GUARDS) == sorted(CLAUSES)


def test_only_the_starting_vertex_guard_changes_the_comparison() -> None:
    """The three resolution clauses are removed from the answer; the fourth from the question.

    Asserted rather than left to a reader, because a guard that quietly compared by index would make
    every other guard's magnitude a measurement of something else.
    """
    by_index = sorted(name for name, guard in geometry_check.GUARDS.items() if guard.by_index)
    assert by_index == ["starting-vertex"]


def test_a_guarded_run_needs_the_named_fixture_to_fail() -> None:
    """The verdict, asserted directly: a clause whose removal changes nothing has not been proven.

    This is the case a guard that only counted failures would get wrong, and it is not hypothetical:
    it is what a guard reports the day someone deletes the fixture the clause was written for.
    """
    guard = geometry_check.GUARDS["entry-direction"]
    scope = [guard.fails_on]

    silent = geometry_check.Report(compared=8)
    assert geometry_check.guard_verdict(guard, silent, scope) == 1

    too_small = geometry_check.Report(compared=8, failed={guard.fails_on: 8}, worst={guard.fails_on: 0.004})
    assert geometry_check.guard_verdict(guard, too_small, scope) == 1

    untouched = geometry_check.Report(
        compared=8,
        failed={guard.fails_on: 8, "relative-zone-origin": 1},
        worst={guard.fails_on: 4.0, "relative-zone-origin": 4.0},
    )
    assert geometry_check.guard_verdict(guard, untouched, scope) == 1

    held = geometry_check.Report(compared=8, failed={guard.fails_on: 8}, worst={guard.fails_on: 4.0})
    assert geometry_check.guard_verdict(guard, held, scope) == 0


def test_a_second_fixture_declaring_the_clause_may_fail_alongside_the_named_one() -> None:
    """Two fixtures declare a non-zero north axis, and both fail when the clause is removed.

    The verdict must call that the clause doing its job twice rather than a second bug, and must go
    on refusing a fixture that declares no such thing. Both halves are asserted here, because a rule
    loosened to admit the first would otherwise admit the second by accident.
    """
    guard = geometry_check.GUARDS["north-axis"]
    both = geometry_check.Report(
        compared=42,
        failed={"north-axis-multizone": 34, "clockwise-entry": 8},
        worst={"north-axis-multizone": 22.5571, "clockwise-entry": 5.71},
    )
    assert geometry_check.guard_verdict(guard, both, ["north-axis-multizone", "clockwise-entry"]) == 0
    assert geometry_check.guard_verdict(guard, both, ["north-axis-multizone"]) == 1
