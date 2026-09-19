#!/usr/bin/env python3
"""Regenerate the committed expectations for ``checks/geometry-vertices``.

Run it from the root of this repository with EnergyPlus installed. CI never runs it: the
expectations it writes are committed, which is the corpus's habit everywhere, so a check needs no
external tool to run and cannot change its verdict because a tool was upgraded underneath it.

    python checks/geometry-vertices/regenerate.py
    python checks/geometry-vertices/regenerate.py --energyplus /path/to/energyplus

WHAT THE ORACLE IS

``Output:Surfaces:List`` with the vertex report, read from ``eplusout.eio``. EnergyPlus writes it
after applying the coordinate system, the zone origin, the zone's relative north and the building's
north axis, which is the computation under test, and it states its own frame in the header it
emits::

    ! <Zone Surfaces>,Zone Name,# Surfaces, Vertices are shown starting at
      Upper-Left-Corner => Counter-Clockwise => World Coordinates

The object is appended here rather than required of the fixture, so that each fixture stays a
byte-for-byte copy of the example file it came from and a reader can diff it against the original.

WHY ``-x -D``

``-x`` expands ``HVACTemplate:*``, without which such a model stops before it reports. ``-D`` runs
design days only: the vertex report is written during input processing, so a full annual run would
produce identical rows for a great deal more time.

THE ENGINE'S OWN SURFACES ARE DROPPED, AND EVERY COUNT IS RECORDED

The report describes the surfaces EnergyPlus ended up with, which is not the set the model states.
Three kinds of row are the engine's rather than the model's, and no extractor should produce any of
them:

``Mir-<name>``
    A mirrored twin of a detailed shading surface, created so that it shades from both sides and
    reported beside the original.

``iz-<name>``
    The reciprocal of a surface whose outside boundary condition is ``Zone`` rather than
    ``Surface``. The model names the adjacent zone and the engine synthesises the other side of the
    wall, with the same vertices wound the other way. ``SmOffPSZ-MultiModeDX`` declares two such
    walls and the engine reports four.

a row with no vertices
    ``InternalMass`` states a surface area and a construction and no geometry at all. The engine
    reports it as a heat transfer surface with ``#Sides`` of zero. It says nothing about where
    anything is, which is the only claim this check makes, and leaving it in would hand the runner
    an empty ring to compare.

Each count is written into the expectation's header rather than left silent, so every exclusion is
auditable from the expectation alone. The prefixes are the engine's own naming conventions rather
than a guess: no fixture declares a surface under either of them, and this generator deliberately
does not parse the model to find out. Deciding what is in the model with a library under test is
how an oracle stops being one.

Exit codes: 0 when every fixture regenerated, 1 when any did not.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Iterator

CHECK_DIR: Final = Path(__file__).resolve().parent
FIXTURES_DIR: Final = CHECK_DIR / "fixtures"
EXPECTED_DIR: Final = CHECK_DIR / "expected"

#: Appended to each fixture before the run. ``DetailsWithVertices`` rather than ``Vertices``
#: because the surface class and the base surface travel in the same row, and the base surface is
#: how the oracle states a window's parent.
REPORT_REQUEST: Final = """

!- Appended by checks/geometry-vertices/regenerate.py. Not part of the fixture.
Output:Surfaces:List,
  DetailsWithVertices;     !- Report Type
"""

#: The two row kinds the report emits for a surface, both under the ``<HeatTransfer Surface>``
#: column header. ``Shading Surface`` rows carry the same columns; only the leading word differs.
ROW_KINDS: Final = ("HeatTransfer Surface", "Shading Surface")

#: Where ``#Sides`` sits in a report row, counting the leading kind word as field zero. The
#: twenty-five columns before it are the surface's properties, which this check does not compare;
#: the vertices follow, three fields each.
SIDES_FIELD: Final = 26

#: The engine's own prefixes for surfaces it created rather than read. Matched without regard to
#: case, because the report echoes neither the model's casing nor a consistent one of its own.
GENERATED_PREFIXES: Final = ("mir-", "iz-")


class Unusable(Exception):
    """The run could not start, or produced nothing to read."""


@dataclass(frozen=True, slots=True)
class ReportedSurface:
    """One surface as the engine reported it."""

    kind: str
    name: str
    surface_class: str
    base_surface: str
    vertices: tuple[tuple[float, float, float], ...]


def fixtures() -> list[Path]:
    """Every committed fixture, in name order so the run is reproducible."""
    found = sorted(FIXTURES_DIR.glob("*.idf.gz"))
    if not found:
        raise Unusable(f"no fixtures under {FIXTURES_DIR}")
    return found


def model_text(fixture: Path) -> str:
    """Decompress one fixture. EnergyPlus input is written latin-1."""
    with gzip.open(fixture, "rt", encoding="latin-1") as handle:
        return handle.read()


def run_energyplus(energyplus: str, model: str, into: Path) -> Path:
    """Run one model design-day-only and return the path to its ``eio`` file."""
    idf = into / "in.idf"
    idf.write_text(model + REPORT_REQUEST, encoding="latin-1")
    outcome = subprocess.run(
        [energyplus, "-x", "-D", "-d", str(into / "out"), str(idf)],
        capture_output=True,
        text=True,
    )
    eio = into / "out" / "eplusout.eio"
    if not eio.is_file():
        tail = (outcome.stdout or outcome.stderr or "").strip().splitlines()[-5:]
        raise Unusable(f"no eio file; energyplus exited {outcome.returncode}: {' / '.join(tail)}")
    return eio


def engine_version(eio_lines: list[str]) -> str:
    """The engine's own statement of what it is, recorded in every expectation per FR-025."""
    for line in eio_lines:
        if line.startswith("Program Version,"):
            # ``Program Version,EnergyPlus, Version 26.1.0-6f2e40d102, YMD=2026.09.18 15:57``.
            # The timestamp is dropped: it would make every regeneration a diff.
            fields = [field.strip() for field in line.split(",")]
            return ", ".join(fields[1:3])
    raise Unusable("the eio file states no program version")


def frame_statement(eio_lines: list[str]) -> str:
    """The report's own statement of the frame it writes in, carried into the expectation.

    Copied rather than paraphrased. It is the sentence that makes this an oracle rather than a
    second opinion, and a reader of the expectation should meet it in the engine's words.
    """
    for line in eio_lines:
        if line.startswith("! <Zone Surfaces>"):
            _, _, tail = line.partition("# Surfaces,")
            return tail.strip()
    raise Unusable("the eio file states no vertex frame")


def reported_surfaces(eio_lines: list[str]) -> Iterator[ReportedSurface]:
    """Every surface row the report emitted, mirrors included; the caller drops those."""
    for line in eio_lines:
        kind, _, rest = line.partition(",")
        if kind not in ROW_KINDS:
            continue
        fields = [field.strip() for field in rest.split(",")]
        # ``fields`` is the row without its kind word, so ``#Sides`` shifts down by one.
        sides = int(fields[SIDES_FIELD - 1])
        flat = fields[SIDES_FIELD : SIDES_FIELD + sides * 3]
        if len(flat) != sides * 3:
            raise Unusable(f"{fields[0]} declares {sides} vertices and carries {len(flat) // 3}")
        # ``sides`` of zero is InternalMass and is legitimate here; the caller drops it.
        yield ReportedSurface(
            kind=kind,
            name=fields[0],
            surface_class=fields[1],
            base_surface=fields[2],
            vertices=tuple(
                (float(flat[at]), float(flat[at + 1]), float(flat[at + 2]))
                for at in range(0, len(flat), 3)
            ),
        )


def write_expectation(
    name: str,
    surfaces: list[ReportedSurface],
    dropped: dict[str, int],
    version: str,
    frame: str,
) -> Path:
    """Write one expectation as plain text, committed uncompressed so a reviewer can read it."""
    path = EXPECTED_DIR / f"{name}.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(f"# fixture: {name}\n")
        handle.write(f"# engine: {version}\n")
        handle.write(f"# frame: {frame}\n")
        for label, count in dropped.items():
            handle.write(f"# {label} dropped: {count}\n")
        handle.write("# generated by checks/geometry-vertices/regenerate.py; do not hand-edit\n")
        handle.write("# rows are ragged by design: three columns per vertex, vertex_count of them\n")
        # The header is written directly rather than through the writer, because the last column
        # stands for every vertex column after it and a quoted cell would misdescribe the shape.
        handle.write("kind,name,surface_class,base_surface,vertex_count,vertices\n")
        writer = csv.writer(handle, lineterminator="\n")
        for surface in surfaces:
            writer.writerow(
                [
                    surface.kind,
                    surface.name,
                    surface.surface_class,
                    surface.base_surface,
                    len(surface.vertices),
                    *(f"{axis:.2f}" for vertex in surface.vertices for axis in vertex),
                ]
            )
    return path


def regenerate(fixture: Path, energyplus: str) -> str:
    """Regenerate one expectation and report what it holds."""
    name = fixture.name.removesuffix(".idf.gz")
    with tempfile.TemporaryDirectory(prefix=f"geometry-vertices-{name}-") as scratch:
        eio = run_energyplus(energyplus, model_text(fixture), Path(scratch))
        lines = eio.read_text(encoding="latin-1").splitlines()
    every = list(reported_surfaces(lines))
    kept: list[ReportedSurface] = []
    dropped = {"mirrored shading surfaces": 0, "engine-created interzone surfaces": 0, "surfaces with no vertices": 0}
    for surface in every:
        lowered = surface.name.casefold()
        if lowered.startswith("mir-"):
            dropped["mirrored shading surfaces"] += 1
        elif lowered.startswith("iz-"):
            dropped["engine-created interzone surfaces"] += 1
        elif not surface.vertices:
            dropped["surfaces with no vertices"] += 1
        else:
            kept.append(surface)
    write_expectation(name, kept, dropped, engine_version(lines), frame_statement(lines))
    summary = ", ".join(f"{count} {label}" for label, count in dropped.items() if count)
    return f"{name}: {len(kept)} surfaces" + (f", dropped {summary}" if summary else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--energyplus",
        default=shutil.which("energyplus") or "energyplus",
        help="the engine to generate expectations with (default: the one on PATH)",
    )
    arguments = parser.parse_args(argv)

    EXPECTED_DIR.mkdir(exist_ok=True)
    failures = 0
    for fixture in fixtures():
        try:
            print(regenerate(fixture, arguments.energyplus))
        except (Unusable, OSError, ValueError) as reason:
            print(f"{fixture.name}: {reason}", file=sys.stderr)
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
