#!/usr/bin/env python3
"""The Python entry point for ``checks/geometry-vertices``.

Run it from the root of this repository, pointing ``--library`` at a checkout of the Python
library. The flag takes a **path**, never a language word, exactly as ``run.py`` and
``weather_check.py`` do: the runner file already fixes the language, and ``geometry-check.mjs``
drives JavaScript.

    python runners/geometry_check.py --library /path/to/idfkit

WHY THIS IS A SEPARATE ENTRY POINT AND NOT A FLAG ON ``run.py``

``run.py`` runs cases. A case is an input file, a parsed document, and an assertion about what the
library made of it against an expectation ``ConvertInputFormat`` produced. ``ConvertInputFormat``
never resolves a coordinate: handed a relative-coordinate model it returns the authored numbers in
the other format. The claim here is about what a library computes from a document rather than about
what the document contains, which is the criterion ``checks/README.md`` states for this directory.

WHAT IT CHECKS, which is ``checks/geometry-vertices/check.md`` in code:

Per surface, the polygon the library resolved against the polygon EnergyPlus reported, **as a ring**
and with **orientation preserved**, within 0.005 m. Fenestration additionally compares the parent
surface the library named against the base surface column of the report.

THE RING COMPARISON, AND WHY IT IS NOT AN INDEX COMPARISON

The engine renormalises every reported surface to begin at its upper-left corner. A faithful
extractor preserves the author's order. Comparing index by index therefore fails the models that
declare a different starting corner for a reason that has nothing to do with resolution, and
``lower-left-start`` is in the fixture set to keep that fact in front of whoever reads this next.

The comparison takes the smallest maximum vertex error over the **cyclic rotations** of the ring. It
never tries the reversal, because a reversed ring is a real difference and it is the difference the
clockwise clause exists to produce. That asymmetry is the whole point of this function and is the
thing a later reader will try to simplify away; ``runners/tests/test_ring.py`` asserts both halves of
it.

NO NETWORK, and no dependency. The fixtures are committed gzipped and decompressed here with the
standard library ``gzip``, which is the only compression both standard libraries hold.

This file is a section-by-section mirror of ``geometry-check.mjs``: the same sections, the same
statuses, and the same report strings apart from the library name and the paths.

Exit codes: 0 when every comparison is green, 1 for any failure, and 2 when the run could not start
at all, which is what a library without the capability reports rather than a failure.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import importlib
import math
import sys
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Final, Iterator, Sequence

RUNNERS_DIR: Final = Path(__file__).resolve().parent
REPO_ROOT: Final = RUNNERS_DIR.parent
CHECK_DIR: Final = REPO_ROOT / "checks" / "geometry-vertices"

#: EnergyPlus input is written latin-1, and latin-1 never fails on any byte sequence.
ENCODING: Final = "latin-1"

#: Derived, not chosen. The report prints two decimals, so half the last printed place is the
#: finest agreement the authority can express. Tightening it asks the oracle for a digit it does
#: not state; ``check.md`` carries this derivation so nobody tries.
TOLERANCE_M: Final = 0.005

Vertex = tuple[float, float, float]


class Unusable(Exception):
    """The run could not start. Distinct from a failure on purpose."""


@dataclass(frozen=True, slots=True)
class Reported:
    """One surface as the engine reported it, read from a committed expectation."""

    kind: str
    name: str
    surface_class: str
    base_surface: str
    vertices: tuple[Vertex, ...]


@dataclass(frozen=True, slots=True)
class Extracted:
    """One surface as the library resolved it.

    Deliberately not the library's own type. The runner compares a shape it owns, so that the two
    runners compare the same thing and neither is written around one library's spelling.
    """

    name: str
    vertices: tuple[Vertex, ...]
    parent_surface: str


@dataclass(slots=True)
class Report:
    """What the run has to say when it ends."""

    compared: int = 0
    failures: list[str] = dataclass_field(default_factory=list)
    unresolved: list[str] = dataclass_field(default_factory=list)
    notes: list[str] = dataclass_field(default_factory=list)


# ---------------------------------------------------------------------------
# The ring comparison
# ---------------------------------------------------------------------------


def distance(one: Vertex, other: Vertex) -> float:
    """Euclidean distance between two vertices."""
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(one, other, strict=True)))


def ring_error(resolved: Sequence[Vertex], reported: Sequence[Vertex]) -> float:
    """The smallest maximum vertex error over the cyclic rotations of one ring against another.

    Rotation-insensitive and **orientation-sensitive**: the reversal is never tried. Raises when the
    two rings differ in length, because that is a disagreement about the surface rather than about
    where it is, and averaging over a shorter ring would hide it.
    """
    if len(resolved) != len(reported):
        raise ValueError(f"{len(resolved)} vertices against {len(reported)}")
    if not resolved:
        raise ValueError("an empty ring has no error to report")
    count = len(resolved)
    return min(
        max(distance(resolved[(at + shift) % count], reported[at]) for at in range(count))
        for shift in range(count)
    )


# ---------------------------------------------------------------------------
# The corpus side
# ---------------------------------------------------------------------------


def fixtures() -> list[Path]:
    """Every committed fixture, in name order so two runs report in the same order."""
    found = sorted((CHECK_DIR / "fixtures").glob("*.idf.gz"))
    if not found:
        raise Unusable(f"no fixtures under {CHECK_DIR / 'fixtures'}")
    return found


def model_text(fixture: Path) -> str:
    """Decompress one committed fixture into text."""
    with gzip.open(fixture, "rt", encoding=ENCODING) as handle:
        return handle.read()


def expectation(name: str) -> list[Reported]:
    """Read one committed expectation.

    The comment lines carrying the engine version and the frame are skipped here and read by
    ``provenance``; they are in the file so that a reader of the expectation meets them, not so that
    this function does.
    """
    path = CHECK_DIR / "expected" / f"{name}.csv"
    if not path.is_file():
        raise Unusable(f"no expectation at {path}")
    rows: list[Reported] = []
    with path.open(encoding="utf-8", newline="") as handle:
        body = (line for line in handle if not line.startswith("#"))
        for fields in csv.reader(body):
            if not fields or fields[0] == "kind":
                continue
            count = int(fields[4])
            flat = [float(value) for value in fields[5 : 5 + count * 3]]
            rows.append(
                Reported(
                    kind=fields[0],
                    name=fields[1],
                    surface_class=fields[2],
                    base_surface=fields[3],
                    vertices=tuple(
                        (flat[at], flat[at + 1], flat[at + 2]) for at in range(0, len(flat), 3)
                    ),
                )
            )
    return rows


def provenance(name: str) -> dict[str, str]:
    """The engine version and frame the expectation carries, for ``--verbose``."""
    path = CHECK_DIR / "expected" / f"{name}.csv"
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("#"):
            break
        key, _, value = line.removeprefix("#").strip().partition(":")
        found[key.strip()] = value.strip()
    return found


# ---------------------------------------------------------------------------
# The library side
# ---------------------------------------------------------------------------


def library_module(library: Path):
    """Import the library from the checkout ``--library`` names, never from the environment."""
    source = library / "src"
    if not source.is_dir():
        raise Unusable(f"{library} holds no src/ directory")
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    try:
        return importlib.import_module("idfkit")
    except ImportError as reason:
        raise Unusable(f"could not import the library from {source}: {reason}") from reason


def extract(module, model: str) -> list[Extracted]:
    """Resolve one model's geometry with the library under test.

    UNIMPLEMENTED ON PURPOSE, and loudly.

    The capability this check exists for does not ship yet in either language. Writing the
    comparison first means the rule is established against committed evidence rather than against
    whatever the first implementation happens to produce, which is the order the corpus already uses
    for cases. What is missing here is only the adapter from the library's own scene type to
    ``Extracted``; everything above and below this function is complete and under test.

    It raises rather than returning an empty list. An empty list would make every fixture compare
    nothing and the run report success, which is the failure mode a check must never have.
    """
    raise Unusable(
        "the library's scene extraction is not wired into this runner yet. "
        "This runner is complete apart from this call; see checks/geometry-vertices/check.md"
    )


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


def compare_fixture(name: str, extracted: Sequence[Extracted], report: Report) -> None:
    """Compare one model's resolved surfaces against the engine's report of the same model."""
    reported = {surface.name.upper(): surface for surface in expectation(name)}
    for surface in extracted:
        against = reported.get(surface.name.upper())
        if against is None:
            report.failures.append(f"{name}: {surface.name} was resolved and the engine reports no such surface")
            continue
        try:
            error = ring_error(surface.vertices, against.vertices)
        except ValueError as reason:
            report.failures.append(f"{name}: {surface.name} {reason}")
            continue
        report.compared += 1
        if error > TOLERANCE_M:
            report.failures.append(
                f"{name}: {surface.name} is {error:.4f} m from the engine, tolerance {TOLERANCE_M} m"
            )
        if surface.parent_surface and surface.parent_surface.upper() != against.base_surface.upper():
            report.failures.append(
                f"{name}: {surface.name} names parent {surface.parent_surface!r} "
                f"and the engine reports {against.base_surface!r}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run checks/geometry-vertices against the Python library.")
    parser.add_argument("--library", type=Path, required=True, help="path to an idfkit checkout")
    parser.add_argument("--verbose", action="store_true", help="name the engine each expectation came from")
    args = parser.parse_args(argv)

    library = library_module(args.library.expanduser().resolve())

    if not CHECK_DIR.is_dir():
        raise Unusable(f"{CHECK_DIR}: missing")
    committed = fixtures()

    report = Report()

    print("idfkit geometry-vertices check: Python")
    print(f"  library     {args.library.expanduser().resolve()}")
    print(f"  check       {CHECK_DIR}")
    print("")

    for fixture in committed:
        name = fixture.name.removesuffix(".idf.gz")
        report.notes.append(f"{name}: {provenance(name).get('engine', 'engine unrecorded')}")
        compare_fixture(name, extract(library, model_text(fixture)), report)

    print(f"  vertices    {len(committed)} fixtures, ring comparison within {TOLERANCE_M} m")
    if args.verbose:
        for note in report.notes:
            print(f"     {note}")
    print("")

    for line in report.unresolved:
        print(f"  UNRESOLVED {line}")
    for line in report.failures:
        print(f"  FAIL       {line}", file=sys.stderr)
    print("")

    # A check that compared nothing has proven nothing, and must not report success for it. This is
    # not a defensive line: an adapter that returns an empty list on every fixture is the cheapest
    # way for this check to go quietly green while proving nothing at all.
    if report.compared == 0:
        print("FAIL: no surface was compared, so a green run would prove nothing.", file=sys.stderr)
        return 1

    if report.failures:
        print(
            f"FAIL: {len(report.failures)} of {report.compared} comparisons disagree "
            f"({len(report.unresolved)} unresolved).",
            file=sys.stderr,
        )
        return 1
    print(
        f"PASS: {report.compared} surfaces against the engine's own vertex report "
        f"({len(report.unresolved)} unresolved)."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Unusable as error:
        print(f"The check could not run: {error}", file=sys.stderr)
        sys.exit(2)
