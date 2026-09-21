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

THE GUARDS

``--without <clause>`` removes one clause of the resolution rule and requires that the check then
fail on the fixture that clause exists for. A check that has only ever passed is half a check, and
this is the half that asks. The clause is removed on the output rather than inside the library,
because the corpus cannot reach into either library's source and must ask the same question of both.
For the entry direction that undoing is exact: the clause reverses a ring while holding its head,
which is its own inverse.

A guarded run reverses the verdict. It exits 0 when the named fixture fails by at least the recorded
magnitude and every other fixture still passes, and 1 when the clause turned out not to matter, which
is the finding worth reporting.

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
import sys
import tempfile
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Callable, Final, Iterator, Sequence

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


@dataclass(frozen=True, slots=True)
class Extraction:
    """One model as the library resolved it: its surfaces, and what it read the model to declare.

    The declaration is here because a guard needs it. Undoing a clause means knowing whether the
    clause fired, and the honest source for that is the library's own reading of the model rather
    than a second parse by the runner: a library that misreads the declaration then leaves its own
    guard a no-op, and the guard says so instead of passing.
    """

    surfaces: tuple[Extracted, ...]
    entry_direction: str


@dataclass(slots=True)
class Report:
    """What the run has to say when it ends."""

    compared: int = 0
    failures: list[str] = dataclass_field(default_factory=list)
    unresolved: list[str] = dataclass_field(default_factory=list)
    notes: list[str] = dataclass_field(default_factory=list)
    #: Per fixture, how many comparisons disagreed and the worst disagreement seen. Kept per
    #: fixture because a guard names one fixture and must not be satisfied by another one failing.
    failed: dict[str, int] = dataclass_field(default_factory=dict)
    worst: dict[str, float] = dataclass_field(default_factory=dict)

    def fail(self, fixture: str, message: str) -> None:
        """Record one disagreement against the fixture it was found in."""
        self.failures.append(f"{fixture}: {message}")
        self.failed[fixture] = self.failed.get(fixture, 0) + 1

    def measured(self, fixture: str, error: float) -> None:
        """Record one comparison's error, whether or not it was within tolerance."""
        self.worst[fixture] = max(self.worst.get(fixture, 0.0), error)


# ---------------------------------------------------------------------------
# The ring comparison
# ---------------------------------------------------------------------------


def vertex_error(one: Vertex, other: Vertex) -> float:
    """The largest disagreement on any one coordinate, not the distance between the two points.

    PER COORDINATE, BECAUSE THAT IS WHAT THE ORACLE STATES.

    The report prints each coordinate to two decimals, so the tolerance below is half the last
    printed place of ONE NUMBER. Euclidean distance mixes three independently rounded numbers into
    one figure, and three coordinates each a legal 0.005 out give a distance of 0.00866: over the
    tolerance without a single coordinate disagreeing by more than the report can express.

    Measured rather than reasoned. Comparing by distance failed 44 of 234 surfaces in the fixture
    set, every one of them between 0.0054 and 0.0073 m, which is inside that bound and outside the
    tolerance. Comparing per coordinate passes all 234. The rule under test was never what those
    failures were about.
    """
    return max(abs(a - b) for a, b in zip(one, other, strict=True))


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
        max(vertex_error(resolved[(at + shift) % count], reported[at]) for at in range(count))
        for shift in range(count)
    )


# ---------------------------------------------------------------------------
# The guards
# ---------------------------------------------------------------------------


def without_entry_direction(extraction: Extraction) -> Extraction:
    """What a library that never wrote the vertex entry direction clause would have returned.

    The clause reverses a ring while holding its first vertex, so applying it twice is applying it
    never. On a model that does not declare clockwise entry this is the identity, which is the
    point: a clause that fires where it was not declared would show up here as a second fixture
    failing, and the guard fails the run when one does.
    """
    if not extraction.entry_direction.casefold().startswith("clockwise"):
        return extraction
    return Extraction(
        surfaces=tuple(
            Extracted(
                name=surface.name,
                vertices=(surface.vertices[0], *reversed(surface.vertices[1:])),
                parent_surface=surface.parent_surface,
            )
            for surface in extraction.surfaces
        ),
        entry_direction=extraction.entry_direction,
    )


@dataclass(frozen=True, slots=True)
class Guard:
    """One clause removed from the resolution rule, and the fixture that must then fail.

    ``at_least_m`` is a measurement and not a threshold to clear: it is how far the fixture moved
    when the clause was first removed, recorded so that a clause quietly becoming a rounding
    difference is a failure rather than a pass.
    """

    name: str
    fails_on: str
    at_least_m: float
    remove: Callable[[Extraction], Extraction]


#: The guards this runner implements. ``geometry-check.mjs`` names the same ones, and
#: ``checks/geometry-vertices/check.md`` records what each is worth.
GUARDS: Final[dict[str, Guard]] = {
    "entry-direction": Guard("entry-direction", "clockwise-entry", 4.0, without_entry_direction),
}


def guard_verdict(guard: Guard, report: Report) -> int:
    """Whether the clause is load-bearing: report it, and return the run's exit code.

    Three things have to hold, and the last two are the ones a weaker guard would skip. The named
    fixture must fail; it must fail by at least what was measured when the clause was written, so
    that a clause reduced to noise cannot pass as one that matters; and no other fixture may fail,
    because a clause that fires on a model that did not declare it is a different bug wearing this
    one's clothes.
    """
    failures = report.failed.get(guard.fails_on, 0)
    worst = report.worst.get(guard.fails_on, 0.0)
    elsewhere = sorted(name for name in report.failed if name != guard.fails_on)

    print(f"  guard       {guard.name}: the clause removed, {guard.fails_on} expected to fail")
    print(f"     {guard.fails_on}: {failures} comparisons disagree, worst {worst:.4f} m")
    if elsewhere:
        print(f"     also failing: {', '.join(elsewhere)}")
    print("")

    if failures == 0:
        print(
            f"GUARD DID NOT HOLD: {guard.fails_on} passes without the {guard.name} clause, "
            "so nothing here proves the clause is doing anything.",
            file=sys.stderr,
        )
        return 1
    if worst < guard.at_least_m:
        print(
            f"GUARD DID NOT HOLD: removing the {guard.name} clause moves {guard.fails_on} by "
            f"{worst:.4f} m, and it was worth at least {guard.at_least_m} m when it was written.",
            file=sys.stderr,
        )
        return 1
    if elsewhere:
        print(
            f"GUARD DID NOT HOLD: removing the {guard.name} clause also fails "
            f"{', '.join(elsewhere)}, which declares no such thing.",
            file=sys.stderr,
        )
        return 1
    print(
        f"GUARD HOLDS: without the {guard.name} clause, {guard.fails_on} is {worst:.4f} m from the "
        f"engine over {failures} comparisons, and no other fixture changes its verdict."
    )
    return 0


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


def extract(module, model: str) -> Extraction:
    """Resolve one model's geometry with the library under test.

    The adapter, and only the adapter. Everything the comparison needs is read out of the library's
    own scene type here, so that the two runners compare a shape the corpus owns rather than one
    library's spelling.

    The library reads a document from a path, so the committed fixture is written to a temporary
    file. That is not this check exercising the library's file reading, which the corpus tracks as
    a separate gap; it is the shortest way to hand it the text.
    """
    with tempfile.TemporaryDirectory(prefix="geometry-vertices-") as scratch:
        path = Path(scratch) / "model.idf"
        path.write_text(model, encoding=ENCODING)
        scene = module.get_scene(module.load_idf(path))

    return Extraction(
        surfaces=tuple(
            Extracted(
                name=surface.name,
                vertices=tuple((v.x, v.y, v.z) for v in surface.polygon.vertices),
                parent_surface=surface.parent_surface or "",
            )
            for surface in scene.surfaces
        ),
        entry_direction=scene.applied.vertex_entry_direction,
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
            report.fail(name, f"{surface.name} was resolved and the engine reports no such surface")
            continue
        try:
            error = ring_error(surface.vertices, against.vertices)
        except ValueError as reason:
            report.fail(name, f"{surface.name} {reason}")
            continue
        report.compared += 1
        report.measured(name, error)
        if error > TOLERANCE_M:
            report.fail(name, f"{surface.name} is {error:.4f} m from the engine, tolerance {TOLERANCE_M} m")
        if surface.parent_surface and surface.parent_surface.upper() != against.base_surface.upper():
            report.fail(
                name,
                f"{surface.name} names parent {surface.parent_surface!r} "
                f"and the engine reports {against.base_surface!r}",
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run checks/geometry-vertices against the Python library.")
    parser.add_argument("--library", type=Path, required=True, help="path to an idfkit checkout")
    parser.add_argument("--verbose", action="store_true", help="name the engine each expectation came from")
    parser.add_argument(
        "--without",
        choices=sorted(GUARDS),
        help="remove one clause of the rule and require the fixture it exists for to fail",
    )
    args = parser.parse_args(argv)
    guard = GUARDS[args.without] if args.without else None

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
        extraction = extract(library, model_text(fixture))
        if guard is not None:
            extraction = guard.remove(extraction)
        compare_fixture(name, extraction.surfaces, report)

    print(f"  vertices    {len(committed)} fixtures, ring comparison within {TOLERANCE_M} m")
    if args.verbose:
        for note in report.notes:
            print(f"     {note}")
    print("")

    for line in report.unresolved:
        print(f"  UNRESOLVED {line}")
    for line in report.failures:
        # Under a guard these are the expected finding rather than the bad news, so they are not
        # written to the error stream and are not called failures.
        if guard is None:
            print(f"  FAIL       {line}", file=sys.stderr)
        else:
            print(f"  WOULD FAIL {line}")
    print("")

    # A check that compared nothing has proven nothing, and must not report success for it. This is
    # not a defensive line: an adapter that returns an empty list on every fixture is the cheapest
    # way for this check to go quietly green while proving nothing at all.
    if report.compared == 0:
        print("FAIL: no surface was compared, so a green run would prove nothing.", file=sys.stderr)
        return 1

    if guard is not None:
        return guard_verdict(guard, report)

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
