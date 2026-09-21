"""Drive ``runners/geometry_check.py``'s guards over the shared fixture table.

``guard_fixtures.json`` is the contract between the two guards: this file asserts that the Python
removal of the vertex entry direction clause produces exactly the rings the table records, and
``test-guard.mjs`` asserts the same of ``geometry-check.mjs``. Both harnesses decode the table the
same way, so a case added there constrains both implementations.

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


def extraction(case: dict[str, Any], key: str = "surfaces") -> geometry_check.Extraction:
    """One side of a case as the runner's own shape."""
    return geometry_check.Extraction(
        surfaces=tuple(
            geometry_check.Extracted(
                name=surface["name"],
                vertices=tuple((float(x), float(y), float(z)) for x, y, z in surface["vertices"]),
                parent_surface=surface["parent_surface"],
            )
            for surface in case[key]
        ),
        entry_direction=case["entry_direction"],
    )


def ids(cases: list[dict[str, Any]]) -> list[str]:
    return [case["name"] for case in cases]


@pytest.mark.parametrize("case", TABLE["cases"], ids=ids(TABLE["cases"]))
def test_the_clause_is_removed_as_the_table_records(case: dict[str, Any]) -> None:
    found = geometry_check.without_entry_direction(extraction(case))
    assert found == extraction(case, "expected"), case.get("note", "")


@pytest.mark.parametrize("case", TABLE["cases"], ids=ids(TABLE["cases"]))
def test_removing_the_clause_twice_removes_it_never(case: dict[str, Any]) -> None:
    """The clause is its own inverse, which is what makes undoing it on the output exact.

    If it were not, the guard would be measuring the distance between two wrong answers rather than
    the distance between the right one and the one a library without the clause would give.
    """
    once = geometry_check.without_entry_direction(extraction(case))
    assert geometry_check.without_entry_direction(once) == extraction(case), case.get("note", "")


@pytest.mark.parametrize("case", TABLE["cases"], ids=ids(TABLE["cases"]))
def test_the_first_vertex_never_moves(case: dict[str, Any]) -> None:
    """FR-008's line, held on the guard side too.

    The check's ring comparison is insensitive to where a ring starts, so a guard that reversed the
    whole list would produce a different starting vertex and nothing downstream would ever say so.
    """
    found = geometry_check.without_entry_direction(extraction(case))
    for before, after in zip(extraction(case).surfaces, found.surfaces, strict=True):
        assert before.vertices[0] == after.vertices[0]


def test_the_guard_names_the_fixture_and_the_magnitude_the_table_records() -> None:
    """The recorded measurement is part of the contract, not a threshold either side may relax."""
    guard = geometry_check.GUARDS[TABLE["clause"]]
    assert guard.fails_on == TABLE["guard"]["fails_on"]
    assert guard.at_least_m == TABLE["guard"]["at_least_m"]


def test_a_guarded_run_needs_the_named_fixture_to_fail() -> None:
    """The verdict, asserted directly: a clause whose removal changes nothing has not been proven.

    This is the case a guard that only counted failures would get wrong, and it is not hypothetical:
    it is what a guard reports the day someone deletes the fixture the clause was written for.
    """
    guard = geometry_check.GUARDS[TABLE["clause"]]
    silent = geometry_check.Report(compared=8)
    assert geometry_check.guard_verdict(guard, silent) == 1

    too_small = geometry_check.Report(compared=8, failed={guard.fails_on: 8}, worst={guard.fails_on: 0.004})
    assert geometry_check.guard_verdict(guard, too_small) == 1

    elsewhere = geometry_check.Report(
        compared=8,
        failed={guard.fails_on: 8, "relative-zone-origin": 1},
        worst={guard.fails_on: 4.0, "relative-zone-origin": 4.0},
    )
    assert geometry_check.guard_verdict(guard, elsewhere) == 1

    held = geometry_check.Report(compared=8, failed={guard.fails_on: 8}, worst={guard.fails_on: 4.0})
    assert geometry_check.guard_verdict(guard, held) == 0
