#!/usr/bin/env python3
"""The Python entry point for ``checks/weather-monthly``.

Run it from the root of this repository, pointing ``--library`` at a checkout of the Python
library. The flag takes a **path**, never a language word, exactly as ``run.py`` does: the runner
file already fixes the language, and ``weather-check.mjs`` drives JavaScript.

    python runners/weather_check.py --library /path/to/idfkit

WHY THIS IS A SEPARATE ENTRY POINT AND NOT A FLAG ON ``run.py``

``run.py`` runs cases. A case is an input file, a parsed document, and an assertion about what the
library made of it against an expectation ``ConvertInputFormat`` produced. This check has no model,
no epJSON, and an oracle that is not ``ConvertInputFormat``. Bending the case runner around it
would put a second shape inside the loop every case shares, and ``checks/README.md`` exists
precisely because the two shapes do not fit each other.

WHAT IT CHECKS, which is ``checks/weather-monthly/check.md`` in code:

A. **The aggregates.** For each summary-bearing station, the library's monthly means of dry bulb,
   dew point, relative humidity and wind speed against ``expected/<station>.json``, within the
   tolerance that file carries. 288 comparisons over six stations.
B. **The absent values.** For the sentinel-bearing file, every value ``sentinels.toml`` calls
   missing reads absent, and every value it calls an observation does not. Checked against the
   table rather than against an external aggregate, because an absence has no external aggregate.

A station whose expectation marks a field ``"covered": false`` is reported as uncovered for that
field rather than passing silently or failing as a defect.

NO NETWORK, and no dependency. The fixtures are committed gzipped and decompressed here with the
standard library ``gzip``, which is the only compression both standard libraries hold: Node has no
xz and Python has no brotli.

This file is a section-by-section mirror of ``weather-check.mjs``: the same sections, the same
statuses, and the same report strings apart from the library name and the paths. A reader who diffs
the two transcripts sees only the genuine disagreements.

Exit codes: 0 when every comparison is green, 1 for any failure, and 2 when the run could not start
at all.
"""

from __future__ import annotations

import argparse
import gzip
import importlib
import json
import math
import sys
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Final, Sequence

if sys.version_info >= (3, 11):
    import tomllib
else:  # Python 3.10 has no tomllib. tomli is the same parser, pre-standardisation.
    import tomli as tomllib

RUNNERS_DIR: Final = Path(__file__).resolve().parent
REPO_ROOT: Final = RUNNERS_DIR.parent
CHECK_DIR: Final = REPO_ROOT / "checks" / "weather-monthly"

#: EPW is written latin-1, and latin-1 never fails on any byte sequence.
ENCODING: Final = "latin-1"

#: The four fields the summary aggregates, and what each library calls the column.
#:
#: The mapping is the runner's, not the corpus's: the expectation names the quantity and each
#: library names its own column, which is what an aligned register entry over two idioms means.
FIELDS: Final = {
    "dry_bulb": "dry_bulb_temperature",
    "dew_point": "dew_point_temperature",
    "relative_humidity": "relative_humidity",
    "wind_speed": "wind_speed",
}


class Unusable(Exception):
    """The run could not start. Distinct from a failure on purpose."""


@dataclass(slots=True)
class Report:
    """What the run has to say when it ends."""

    compared: int = 0
    failures: list[str] = dataclass_field(default_factory=list)
    uncovered: list[str] = dataclass_field(default_factory=list)
    notes: list[str] = dataclass_field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Reserved:
    """One field's reserved values, split by what they mean."""

    name: str
    missing: tuple[float, ...]
    observation: tuple[float, ...]


# ---------------------------------------------------------------------------
# The corpus side
# ---------------------------------------------------------------------------


def fixture_text(name: str) -> str:
    """Decompress one committed fixture into text."""
    with gzip.open(CHECK_DIR / "fixtures" / name, "rt", encoding=ENCODING) as handle:
        return handle.read()


def sentinel_fixture(fixtures: Sequence[str]) -> str:
    """The sentinel-bearing fixture: the one EPW with no summary beside it.

    Derived rather than named, so adding a seventh archive cannot leave a stale constant behind.
    The division of labour is ``check.md``'s: the summary-bearing archives carry the aggregate
    claim and this one carries the absent-value claim, which is why it has no ``.stat``.
    """
    without_summary = [
        name
        for name in fixtures
        if name.endswith(".epw.gz") and name[: -len(".epw.gz")] + ".stat.gz" not in fixtures
    ]
    if len(without_summary) != 1:
        raise Unusable(
            f"checks/weather-monthly/fixtures holds {len(without_summary)} EPW files with no summary "
            f"beside them and the check is written for exactly one: {', '.join(without_summary) or '(none)'}"
        )
    return without_summary[0]


def reserved_values() -> dict[int, Reserved]:
    """``sentinels.toml``, keyed by the field's position in the hourly row."""
    path = CHECK_DIR / "sentinels.toml"
    if not path.is_file():
        raise Unusable(f"{path}: missing")
    with path.open("rb") as handle:
        table = tomllib.load(handle)
    by_position: dict[int, Reserved] = {}
    for entry in table.get("field", []):
        values = entry.get("values", [])
        by_position[entry["position"]] = Reserved(
            name=entry["field"],
            missing=tuple(v["value"] for v in values if v["kind"] == "missing"),
            observation=tuple(v["value"] for v in values if v["kind"] == "observation"),
        )
    return by_position


# ---------------------------------------------------------------------------
# The library side
# ---------------------------------------------------------------------------


def import_library(root: Path) -> Any:
    """Import ``idfkit`` out of the checkout at ``root``, never from site-packages.

    The suite tests a checkout, so an installed copy shadowing it would silently test the wrong
    build. The import is verified to have come from under ``root``.
    """
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise Unusable(f"--library {root} is not a directory")

    candidates = [root / "src", root]
    import_root = next((c for c in candidates if (c / "idfkit" / "__init__.py").is_file()), None)
    if import_root is None:
        raise Unusable(
            f"no importable 'idfkit' package under {root}. "
            f"weather_check.py drives the Python library; use "
            f"'node runners/weather-check.mjs --library <path>' for idfkit-js"
        )

    text = str(import_root)
    if sys.path[:1] != [text]:
        while text in sys.path:
            sys.path.remove(text)
        sys.path.insert(0, text)
    for name in [n for n in sys.modules if n == "idfkit" or n.startswith("idfkit.")]:
        module_file = getattr(sys.modules[name], "__file__", None)
        if module_file is None or import_root not in Path(module_file).resolve().parents:
            del sys.modules[name]

    module = importlib.import_module("idfkit.weather")
    for name in ("parse_epw", "monthly_means"):
        if not callable(getattr(module, name, None)):
            raise Unusable(f"idfkit.weather at {import_root} exports no {name}")
    return module


# ---------------------------------------------------------------------------
# The two assertions
# ---------------------------------------------------------------------------


def check_aggregates(library: Any, station: str, expectation: dict[str, Any], report: Report) -> None:
    """Assertion A, for one station."""
    epw = library.parse_epw(fixture_text(f"{station}.epw.gz"))

    for key, column in FIELDS.items():
        field = expectation.get("fields", {}).get(key)
        if field is None:
            report.uncovered.append(f"{station}/{key}: the expectation names no such field")
            continue
        if field.get("covered") is False:
            reason = field.get("reason", "the summary has no such section")
            report.uncovered.append(f"{station}/{key}: {reason}")
            continue
        means = library.monthly_means(epw, column)
        for month in range(12):
            expected = field["monthly"][month]
            actual = means[month].mean
            report.compared += 1
            if actual is None or math.isnan(actual):
                report.failures.append(
                    f"{station}/{key} month {month + 1}: the library reports no value, "
                    f"and the summary says {expected}"
                )
                continue
            if abs(actual - expected) > field["tolerance"]:
                report.failures.append(
                    f"{station}/{key} month {month + 1}: library {actual:.4f}, summary {expected}, "
                    f"difference {abs(actual - expected):.4f} over a tolerance of {field['tolerance']} "
                    f"(the summary prints {field['decimal_places']} decimal place(s))"
                )


#: The numeric columns of a weather file, by their position in the row.
#:
#: The library exposes columns by NAME, which is the whole point of the reader, and this check has
#: to speak positions because the reserved-value table is keyed on them. The bridge is the one place
#: this runner knows the field order, and it is written out rather than derived so that a library
#: that renamed a column fails here loudly rather than silently skipping it.
POSITION_TO_COLUMN: Final = {
    6: "dry_bulb_temperature",
    7: "dew_point_temperature",
    8: "relative_humidity",
    9: "atmospheric_station_pressure",
    10: "extraterrestrial_horizontal_radiation",
    11: "extraterrestrial_direct_normal_radiation",
    12: "horizontal_infrared_radiation_intensity_from_sky",
    13: "global_horizontal_radiation",
    14: "direct_normal_radiation",
    15: "diffuse_horizontal_radiation",
    16: "global_horizontal_illuminance",
    17: "direct_normal_illuminance",
    18: "diffuse_horizontal_illuminance",
    19: "zenith_luminance",
    20: "wind_direction",
    21: "wind_speed",
    22: "total_sky_cover",
    23: "opaque_sky_cover",
    24: "visibility",
    25: "ceiling_height",
    26: "present_weather_observation",
    28: "precipitable_water",
    29: "aerosol_optical_depth",
    30: "snow_depth",
    31: "days_since_last_snowfall",
    32: "albedo",
    33: "liquid_precipitation_depth",
    34: "liquid_precipitation_quantity",
}


def columns_by_position(epw: Any) -> dict[int, Sequence[float]]:
    columns: dict[int, Sequence[float]] = {}
    for position, name in POSITION_TO_COLUMN.items():
        column = getattr(epw.hours, name, None)
        if column is None:
            raise Unusable(f"the library's hourly table has no column named {name}")
        columns[position] = column
    return columns


def check_absent_values(library: Any, fixture: str, reserved: dict[int, Reserved], report: Report) -> None:
    """Assertion B.

    The rows are split here rather than asked of the library, because the claim is about what the
    library did with a value the FILE holds, so the check has to know the file's own text.
    """
    text = fixture_text(fixture)
    rows = [line.split(",") for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")[8:] if line.strip()]
    epw = library.parse_epw(text)
    columns = columns_by_position(epw)

    for position, entry in reserved.items():
        column = columns.get(position)
        if column is None:  # A text column reserves nothing.
            continue

        for kind, values in (("missing", entry.missing), ("observation", entry.observation)):
            for value in values:
                rows_at_value = [row for row, fields in enumerate(rows) if float(fields[position]) == value]
                if not rows_at_value:
                    continue

                report.compared += 1
                if kind == "missing":
                    wrong = [row for row in rows_at_value if not math.isnan(column[row])]
                else:
                    wrong = [row for row in rows_at_value if math.isnan(column[row])]
                if wrong:
                    if kind == "missing":
                        report.failures.append(
                            f"{entry.name}: {value} means the measurement was not made, and the library read "
                            f"it as a number in {len(wrong)} of {len(rows_at_value)} rows, "
                            f"first at row {wrong[0] + 1}"
                        )
                    else:
                        report.failures.append(
                            f"{entry.name}: {value} is an observation and not an absence, and the library "
                            f"blanked it in {len(wrong)} of {len(rows_at_value)} rows, "
                            f"first at row {wrong[0] + 1}"
                        )
                else:
                    read_as = "absent" if kind == "missing" else "a value"
                    report.notes.append(
                        f"{entry.name} {value} ({kind}): {len(rows_at_value)} rows, all read as {read_as}"
                    )


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="weather_check.py",
        description="Run checks/weather-monthly against a Python library checkout.",
    )
    parser.add_argument(
        "--library",
        required=True,
        type=Path,
        help="Path to an idfkit checkout. A path, never a language word: this file fixes the language.",
    )
    parser.add_argument("--verbose", action="store_true", help="List each reserved value the check saw.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    library = import_library(args.library)

    if not CHECK_DIR.is_dir():
        raise Unusable(f"{CHECK_DIR}: missing")
    fixtures = sorted(p.name for p in (CHECK_DIR / "fixtures").iterdir())
    expectations = sorted(p for p in (CHECK_DIR / "expected").iterdir() if p.suffix == ".json")
    if not expectations:
        raise Unusable(
            "checks/weather-monthly/expected holds no expectation, so a green run would prove nothing"
        )

    report = Report()

    print("idfkit weather-monthly check: Python")
    print(f"  library     {args.library.expanduser().resolve()}")
    print(f"  check       {CHECK_DIR}")
    print("")

    for path in expectations:
        expectation = json.loads(path.read_text(encoding="utf-8"))
        station = path.stem
        if f"{station}.epw.gz" not in fixtures:
            raise Unusable(f"{path.name} expects a fixture {station}.epw.gz, which is not committed")
        check_aggregates(library, station, expectation, report)

    sentinel = sentinel_fixture(fixtures)
    check_absent_values(library, sentinel, reserved_values(), report)

    print(f"  A. aggregates   {len(expectations)} stations, {len(FIELDS)} fields")
    print(f"  B. absent values {sentinel}")
    if args.verbose:
        for note in report.notes:
            print(f"     {note}")
    print("")

    for line in report.uncovered:
        print(f"  UNCOVERED  {line}")
    for line in report.failures:
        print(f"  FAIL       {line}", file=sys.stderr)
    print("")

    if report.failures:
        print(
            f"FAIL: {len(report.failures)} of {report.compared} comparisons disagree "
            f"({len(report.uncovered)} uncovered).",
            file=sys.stderr,
        )
        return 1
    print(
        f"PASS: {report.compared} comparisons against an EnergyPlus artifact and the reserved-value "
        f"table ({len(report.uncovered)} uncovered)."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Unusable as error:
        print(f"The check could not run: {error}", file=sys.stderr)
        sys.exit(2)
