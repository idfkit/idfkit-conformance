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

``--without <clause>`` removes one clause and requires that the check then fail on the fixture that
clause exists for. A check that has only ever passed is half a check, and this is the half that
asks. The clause is removed on the output rather than inside the library, because the corpus cannot
reach into either library's source and must ask the same question of both.

Four clauses, and the fourth is not like the other three. ``coordinate-system``, ``north-axis`` and
``entry-direction`` are clauses of the RESOLUTION rule, and removing one changes what the library is
taken to have returned. ``starting-vertex`` is a clause of the COMPARISON: it drops the
rotation-insensitivity of the ring comparison, which is the same thing as requiring the extractor's
first vertex to be the engine's. It is the one guard that must go on failing, and
``checks/geometry-vertices/check.md`` says at length why.

WHICH FIXTURES MAY FAIL UNDER A GUARD

Not "only the named one". A clause fires wherever the model declares the condition it reads, and two
fixtures declare a non-zero building north axis. So each guard says which models it APPLIES to,
reading the library's own declaration, and the verdict is that the named fixture must fail by at
least the recorded magnitude and no fixture the clause never touched may fail at all. A fixture the
clause did touch is allowed to fail and is reported as expected company, because that is the clause
doing its job in a second model rather than a second bug.

A guarded run reverses the verdict. It exits 0 when that holds, and 1 when the clause turned out not
to matter, which is the finding worth reporting.

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
import tempfile
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Callable, Final, Iterator, Mapping, Sequence

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
    #: The zone the library placed this surface in, empty for a surface it placed in no zone. The
    #: coordinate system guard needs it to know which origin clause one would have applied.
    zone: str = ""


@dataclass(frozen=True, slots=True)
class Extraction:
    """One model as the library resolved it: its surfaces, and what it read the model to declare.

    The declarations are here because the guards need them. Undoing a clause means knowing whether
    the clause fired, and the honest source for that is the library's own reading of the model
    rather than a second parse by the runner: a library that misreads a declaration then leaves its
    own guard a no-op, and the guard says so instead of passing.

    ``zone_origins`` is the one thing here the library's scene does not state, because a scene names
    each surface's zone and not that zone's origin. It is read from the document the library parsed,
    which is still the library's reading of the file and not the runner's own parser.
    """

    surfaces: tuple[Extracted, ...]
    entry_direction: str
    coordinate_system: str = "Relative"
    starting_vertex_position: str = "UpperLeftCorner"
    north_axis: float = 0.0
    zone_origins: Mapping[str, Vertex] = dataclass_field(default_factory=dict)


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


def index_error(resolved: Sequence[Vertex], reported: Sequence[Vertex]) -> float:
    """The same comparison with the rotation search removed: vertex one against vertex one.

    NOT THE CHECK'S COMPARISON, and never reached except under ``--without starting-vertex``. It is
    here to be the wrong answer, because requiring the extractor's first vertex to be the engine's
    is exactly what comparing by index requires, and the fixture set carries a model that proves
    what that costs.
    """
    if len(resolved) != len(reported):
        raise ValueError(f"{len(resolved)} vertices against {len(reported)}")
    if not resolved:
        raise ValueError("an empty ring has no error to report")
    return max(vertex_error(one, other) for one, other in zip(resolved, reported, strict=True))


# ---------------------------------------------------------------------------
# The guards
# ---------------------------------------------------------------------------


def _mapped(extraction: Extraction, move: Callable[[Extracted], tuple[Vertex, ...]]) -> Extraction:
    """One extraction with every ring replaced, and everything else carried through."""
    return Extraction(
        surfaces=tuple(
            Extracted(
                name=surface.name,
                vertices=move(surface),
                parent_surface=surface.parent_surface,
                zone=surface.zone,
            )
            for surface in extraction.surfaces
        ),
        entry_direction=extraction.entry_direction,
        coordinate_system=extraction.coordinate_system,
        starting_vertex_position=extraction.starting_vertex_position,
        north_axis=extraction.north_axis,
        zone_origins=extraction.zone_origins,
    )


def unchanged(extraction: Extraction) -> Extraction:
    """What a guard that removes a clause of the comparison rather than of the rule does to it."""
    return extraction


def without_entry_direction(extraction: Extraction) -> Extraction:
    """What a library that never wrote the vertex entry direction clause would have returned.

    The clause reverses a ring while holding its first vertex, so applying it twice is applying it
    never, which is what makes undoing it on the output exact rather than approximate.

    THE FIRST VERTEX STAYS WHERE IT IS. Reversing the whole list would renormalise the starting
    vertex as a side effect, and the check's ring comparison is insensitive to where a ring starts,
    so nothing downstream would ever say so.
    """
    return _mapped(extraction, lambda surface: (surface.vertices[0], *reversed(surface.vertices[1:])))


def without_coordinate_system(extraction: Extraction) -> Extraction:
    """What a library that never read ``GlobalGeometryRules``'s coordinate system would have returned.

    Clause one applies the zone's origin only under the relative system. A library that never wrote
    the condition applies it always, so on a model declaring ``World`` every surface comes out
    displaced by its zone's origin, and on one declaring ``Relative`` it comes out exactly where it
    already is. That is why this is applied only to the models that declare ``World``: on the others
    the clause has already fired and applying it again would measure a double shift, which is a
    third answer neither library would ever give.

    TWO THINGS IT DOES NOT DO, both because the fixture set does not exercise them.

    It does not apply the zone's ``direction_of_relative_north``, which clause one also governs. No
    model in the set declares ``World`` and carries a non-zero zone rotation, so a branch for it
    would be an untested path standing in for a proof.

    It does not move a surface the library placed in no zone. Twenty-one of the ninety-nine surfaces
    in ``world-nonzero-zone-origin`` are ``Shading:Zone:Detailed``, which resolve against the zone of
    the surface they are attached to and which a scene reports with no zone of their own. The guard
    therefore moves seventy-eight of them, which is enough to make the point at 201.98 m and is less
    than a library without the clause would move. Under-reaching is safe here in a way that
    over-reaching would not be: it can only make the guard harder to satisfy.
    """
    origins = extraction.zone_origins

    def moved(surface: Extracted) -> tuple[Vertex, ...]:
        origin = origins.get(surface.zone.upper()) if surface.zone else None
        if origin is None:
            return surface.vertices
        return tuple((x + origin[0], y + origin[1], z + origin[2]) for x, y, z in surface.vertices)

    return _mapped(extraction, moved)


def without_north_axis(extraction: Extraction) -> Extraction:
    """What a library that never rotated the building by its north axis would have returned.

    Clause two turns the whole resolved building about the world origin by the negation of
    ``Building.north_axis``, the negation being there because EnergyPlus measures the axis clockwise
    from true north while a rotation turns counter-clockwise. Undoing it is turning it back, and a
    rotation is exactly invertible, so this undoing is as exact as the entry direction's.

    It is the one clause of the three that is unconditional in the rule: it fires on every model
    carrying a non-zero axis, and two fixtures do. Both are therefore allowed to fail under this
    guard, and the verdict says which one it was written for.
    """
    radians = math.radians(extraction.north_axis)
    cosine, sine = math.cos(radians), math.sin(radians)
    return _mapped(
        extraction,
        lambda surface: tuple(
            (x * cosine - y * sine, x * sine + y * cosine, z) for x, y, z in surface.vertices
        ),
    )


@dataclass(frozen=True, slots=True)
class Guard:
    """One clause removed, and the fixture that must then fail.

    ``at_least_m`` is a measurement and not a threshold to clear: it is how far the fixture moved
    when the clause was first removed, recorded so that a clause quietly becoming a rounding
    difference is a failure rather than a pass.

    ``applies`` answers, from the library's own reading of a model, whether the clause had anything
    to do in it. It is what separates a second fixture failing because the clause fired there too
    from a second fixture failing because something else is wrong, and it is read from the library
    rather than from a second parse so that a library misreading its own declaration leaves its
    guard a visible no-op instead of a quiet pass.

    ``by_index`` is set by the one guard that removes a clause of the COMPARISON instead. Its
    ``remove`` is the identity, because there is nothing wrong with what the library returned.
    """

    name: str
    fails_on: str
    at_least_m: float
    applies: Callable[[Extraction], bool]
    remove: Callable[[Extraction], Extraction] = unchanged
    by_index: bool = False


#: The guards this runner implements. ``geometry-check.mjs`` names the same ones, and
#: ``checks/geometry-vertices/check.md`` records what each is worth and on which fixture.
GUARDS: Final[dict[str, Guard]] = {
    "coordinate-system": Guard(
        name="coordinate-system",
        fails_on="world-nonzero-zone-origin",
        at_least_m=201.98,
        applies=lambda extraction: extraction.coordinate_system.casefold() != "relative",
        remove=without_coordinate_system,
    ),
    "north-axis": Guard(
        name="north-axis",
        fails_on="north-axis-multizone",
        at_least_m=22.5571,
        applies=lambda extraction: extraction.north_axis != 0.0,
        remove=without_north_axis,
    ),
    "entry-direction": Guard(
        name="entry-direction",
        fails_on="clockwise-entry",
        at_least_m=4.0,
        applies=lambda extraction: extraction.entry_direction.casefold().startswith("clockwise"),
        remove=without_entry_direction,
    ),
    "starting-vertex": Guard(
        name="starting-vertex",
        fails_on="lower-left-start",
        at_least_m=17.59,
        applies=lambda extraction: extraction.starting_vertex_position.casefold() != "upperleftcorner",
        by_index=True,
    ),
}


def guard_verdict(guard: Guard, report: Report, applied_to: Sequence[str]) -> int:
    """Whether the clause is load-bearing: report it, and return the run's exit code.

    Three things have to hold, and the last two are the ones a weaker guard would skip. The named
    fixture must fail; it must fail by at least what was measured when the clause was written, so
    that a clause reduced to noise cannot pass as one that matters; and no fixture the clause never
    touched may fail, because a clause firing on a model that declares no such thing is a different
    bug wearing this one's clothes.

    ``applied_to`` names the fixtures whose declarations put the clause in scope. A fixture in that
    list failing is the clause doing its job twice and is reported as such; a fixture outside it
    failing is the finding.
    """
    failures = report.failed.get(guard.fails_on, 0)
    worst = report.worst.get(guard.fails_on, 0.0)
    in_scope = set(applied_to)
    alongside = sorted(name for name in report.failed if name != guard.fails_on and name in in_scope)
    untouched = sorted(name for name in report.failed if name not in in_scope)

    print(f"  guard       {guard.name}: the clause removed, {guard.fails_on} expected to fail")
    print(f"     in scope: {', '.join(sorted(in_scope)) or 'no fixture declares it'}")
    print(f"     {guard.fails_on}: {failures} comparisons disagree, worst {worst:.4f} m")
    for name in alongside:
        print(f"     {name}: {report.failed[name]} disagree, worst {report.worst[name]:.4f} m, and it declares it too")
    if untouched:
        print(f"     also failing, untouched by the clause: {', '.join(untouched)}")
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
    if untouched:
        print(
            f"GUARD DID NOT HOLD: removing the {guard.name} clause also fails "
            f"{', '.join(untouched)}, which declares no such thing.",
            file=sys.stderr,
        )
        return 1
    company = f", along with {', '.join(alongside)}, which declares it too" if alongside else ""
    print(
        f"GUARD HOLDS: without the {guard.name} clause, {guard.fails_on} is {worst:.4f} m from the "
        f"engine over {failures} comparisons{company}, and no fixture the clause never touched "
        "changes its verdict."
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
        doc = module.load_idf(path)
        scene = module.get_scene(doc)
        origins = zone_origins(doc)

    return Extraction(
        surfaces=tuple(
            Extracted(
                name=surface.name,
                vertices=tuple((v.x, v.y, v.z) for v in surface.polygon.vertices),
                parent_surface=surface.parent_surface or "",
                zone=surface.zone,
            )
            for surface in scene.surfaces
        ),
        entry_direction=scene.applied.vertex_entry_direction,
        coordinate_system=scene.applied.coordinate_system,
        starting_vertex_position=scene.applied.starting_vertex_position,
        north_axis=scene.applied.north_axis,
        zone_origins=origins,
    )


def zone_origins(doc) -> dict[str, Vertex]:
    """Each zone's declared origin, keyed by upper-cased name.

    Read from the document rather than from the scene because a scene names each surface's zone and
    not that zone's origin, and the coordinate system guard needs the origin. It is still the
    library's own parse of the file: the runner asks the document it was handed for three numeric
    fields and does not read the text itself.
    """
    if "Zone" not in doc:
        return {}
    found: dict[str, Vertex] = {}
    for zone in doc["Zone"]:
        found[zone.name.upper()] = (
            float(zone.data.get("x_origin") or 0.0),
            float(zone.data.get("y_origin") or 0.0),
            float(zone.data.get("z_origin") or 0.0),
        )
    return found


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


def compare_fixture(name: str, extracted: Sequence[Extracted], report: Report, by_index: bool = False) -> None:
    """Compare one model's resolved surfaces against the engine's report of the same model.

    ``by_index`` is the starting-vertex guard and nothing else. An unguarded run always compares as
    a ring, and ``check.md`` records why the option to do otherwise exists only to be shown failing.
    """
    compare = index_error if by_index else ring_error
    reported = {surface.name.upper(): surface for surface in expectation(name)}
    for surface in extracted:
        against = reported.get(surface.name.upper())
        if against is None:
            report.fail(name, f"{surface.name} was resolved and the engine reports no such surface")
            continue
        try:
            error = compare(surface.vertices, against.vertices)
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

    applied_to: list[str] = []
    for fixture in committed:
        name = fixture.name.removesuffix(".idf.gz")
        report.notes.append(f"{name}: {provenance(name).get('engine', 'engine unrecorded')}")
        extraction = extract(library, model_text(fixture))
        if guard is not None and guard.applies(extraction):
            # Only where the model declares the condition the clause reads. Removing a clause from
            # a model that never triggered it would measure a second wrong answer, not this one.
            applied_to.append(name)
            extraction = guard.remove(extraction)
        compare_fixture(name, extraction.surfaces, report, by_index=guard is not None and guard.by_index)

    shape = "vertex by vertex" if guard is not None and guard.by_index else "ring comparison"
    print(f"  vertices    {len(committed)} fixtures, {shape} within {TOLERANCE_M} m")
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
        return guard_verdict(guard, report, applied_to)

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
