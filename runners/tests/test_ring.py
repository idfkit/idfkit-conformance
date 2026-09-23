"""Drive ``runners/geometry_check.py``'s ring comparison over the shared fixture table.

``ring_fixtures.json`` is the contract between the two comparators: this file asserts that the
Python one returns exactly the errors the table records, and ``test-ring.mjs`` asserts the same of
``geometry-check.mjs``. Both harnesses decode the table the same way, so a case added there
constrains both implementations.

Run it from the root of the repository::

    python -m pytest runners/tests/test_ring.py
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any, Final

RUNNERS: Final = Path(__file__).resolve().parents[1]
if str(RUNNERS) not in sys.path:
    sys.path.insert(0, str(RUNNERS))

import geometry_check  # noqa: E402
import pytest  # noqa: E402

TABLE: Final[dict[str, Any]] = json.loads(
    (Path(__file__).resolve().parent / "ring_fixtures.json").read_text(encoding="utf-8")
)


def vertices(raw: list[list[float]]) -> tuple[tuple[float, float, float], ...]:
    return tuple((float(x), float(y), float(z)) for x, y, z in raw)


def ids(cases: list[dict[str, Any]]) -> list[str]:
    return [case["name"] for case in cases]


@pytest.mark.parametrize("case", TABLE["cases"], ids=ids(TABLE["cases"]))
def test_error_is_what_the_table_records(case: dict[str, Any]) -> None:
    found = geometry_check.ring_error(vertices(case["resolved"]), vertices(case["reported"]))
    assert math.isclose(found, case["error"], abs_tol=1e-9), case.get("note", "")


@pytest.mark.parametrize("case", TABLE["cases"], ids=ids(TABLE["cases"]))
def test_the_verdict_follows_the_tolerance(case: dict[str, Any]) -> None:
    found = geometry_check.ring_error(vertices(case["resolved"]), vertices(case["reported"]))
    assert (found <= geometry_check.TOLERANCE_M) is case["within_tolerance"], case.get("note", "")


@pytest.mark.parametrize("case", TABLE["rejected"], ids=ids(TABLE["rejected"]))
def test_the_table_s_rejections_raise(case: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        geometry_check.ring_error(vertices(case["resolved"]), vertices(case["reported"]))


def test_the_tolerance_matches_the_table() -> None:
    """The table states the tolerance it was written against, so a change to one fails the other."""
    assert geometry_check.TOLERANCE_M == TABLE["tolerance_m"]


def test_a_rotation_passes_and_its_reversal_does_not() -> None:
    """The asymmetry, asserted directly rather than only through the table.

    This is the property a later reader will try to simplify away, by minimising over the reversal
    as well as over the rotations. It reads like a generalisation and it is a hole: it would pass a
    model resolved inside out, which is exactly what the vertex entry direction clause exists to
    prevent. The table covers it case by case; this test says it in one sentence so that whoever
    breaks it reads why.
    """
    ring = vertices(TABLE["cases"][0]["reported"])
    rotated = ring[1:] + ring[:1]
    reversed_ring = tuple(reversed(ring))

    assert geometry_check.ring_error(rotated, ring) == pytest.approx(0.0)
    assert geometry_check.ring_error(reversed_ring, ring) > geometry_check.TOLERANCE_M


def test_the_index_comparison_sees_a_rotation_the_ring_comparison_does_not() -> None:
    """What ``--without starting-vertex`` rests on, asserted here rather than only on a fixture.

    The ring comparison exists because the engine renormalises every surface it reports to begin at
    its upper-left corner while a faithful extractor keeps the author's order, so the two agree on
    the polygon and differ on where it starts. Comparing by index is the same thing as demanding the
    extractor reproduce the engine's starting vertex, and this is what that demand costs on one
    four-metre wall. On the committed model that declares a lower-left start it is 17.59 m.
    """
    ring = vertices(TABLE["cases"][0]["reported"])
    rotated = ring[1:] + ring[:1]

    assert geometry_check.ring_error(rotated, ring) == pytest.approx(0.0)
    assert geometry_check.index_error(rotated, ring) > geometry_check.TOLERANCE_M
    assert geometry_check.index_error(ring, ring) == pytest.approx(0.0)
