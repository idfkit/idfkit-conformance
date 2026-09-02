#!/usr/bin/env python3
"""
Three-way bootstrap sweep that seeds the conformance corpus (FR-022, research R5).

For every EnergyPlus example file this produces three canonical epJSON documents:

1. ``idfkit`` (Python) parses the IDF and serialises it,
2. ``@idfkit/core`` (JavaScript) parses the same IDF and serialises it,
3. ``ConvertInputFormat -f epJSON``, which ships with EnergyPlus and is the oracle,
   the only one of the three that is external truth.

Every disagreement is recorded, grouped by *shape* rather than by file, into
``sweep-report.json``. Grouping by shape is the whole point: the next task
curates one minimal reproducer per distinct disagreement, not one case per file.
A single mis-handled positional rule shows up in four hundred files and is one
case.

Nothing here is a test. It runs once, offline, on a machine with EnergyPlus
installed, and its output is read by a human.

Usage::

    python3 tools/bootstrap_sweep.py --limit 5
    python3 tools/bootstrap_sweep.py --workers 8

Robustness: a file that crashes one side is a finding, not a reason to stop.
``ConvertInputFormat`` validates before it converts, so example files that fail
validation are recorded as an oracle failure rather than dropped.
"""

from __future__ import annotations

import argparse
import atexit
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

# --------------------------------------------------------------------------------------
# Defaults. Every one of these is overridable so the script is not machine-specific.
# --------------------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = REPO_ROOT.parent

DEFAULT_ENERGYPLUS_ROOT = Path("/Applications/EnergyPlus-26-1-0")
DEFAULT_OUTPUT = REPO_ROOT / "sweep-report.json"
DEFAULT_IDFKIT_SRC = WORKSPACE_ROOT / "idfkit" / "src"
DEFAULT_JS_CORE = WORKSPACE_ROOT / "idfkit-js" / "packages" / "core"
DEFAULT_JS_DRIVER = REPO_ROOT / "tools" / "sweep_js.mjs"

PYTHON = "python"
JAVASCRIPT = "javascript"
ORACLE = "oracle"
SIDES = (PYTHON, JAVASCRIPT, ORACLE)

REPORT_SCHEMA_VERSION = 1

# --------------------------------------------------------------------------------------
# Comparison rules.
#
# TODO(T033): delete everything between here and the end of the comparison section and
# import the shared comparator from ``runners/compare.py`` once it lands. The rules below
# are the ones written down in ``runners/compare.md``; they are duplicated here only
# because this script predates that module. Two copies of a comparison rule is exactly
# the drift this repository exists to catch.
#
#   - Values, not text. Compare parsed JSON, never JSON text.
#   - Numbers: relative tolerance 1e-12. Integer against float is not a difference.
#   - Object key order: not compared.
#   - Strings: case-sensitive.
#   - Encoding: latin-1.
#   - Unordered collections compared unordered. In epJSON the only unordered collection
#     is the object map itself, which is a JSON object and therefore already covered by
#     the key-order rule. Extensible groups are ordered arrays and stay ordered.
# --------------------------------------------------------------------------------------

RELATIVE_TOLERANCE = 1e-12
ENCODING = "latin-1"


class _Absent:
    """Sentinel for a value one side does not have at all."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<absent>"


ABSENT = _Absent()


def json_type(value: Any) -> str:
    """The JSON type name of a decoded value, or ``absent``."""
    if value is ABSENT:
        return "absent"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def numbers_equal(left: float, right: float) -> bool:
    """Relative tolerance 1e-12. epJSON has one number type, so 3 and 3.0 are equal."""
    a = float(left)
    b = float(right)
    if a == b:
        return True
    if math.isnan(a) and math.isnan(b):
        return True
    if math.isinf(a) or math.isinf(b):
        return False
    return abs(a - b) <= RELATIVE_TOLERANCE * max(abs(a), abs(b))


def values_equal(left: Any, right: Any) -> bool:
    """Deep equality under the comparison rules above."""
    left_type = json_type(left)
    right_type = json_type(right)
    if left_type != right_type:
        return False
    if left_type == "absent":
        return True
    if left_type == "number":
        return numbers_equal(left, right)
    if left_type == "array":
        return len(left) == len(right) and all(values_equal(a, b) for a, b in zip(left, right))
    if left_type == "object":
        # Key order is not compared: dict equality of key sets, then value by value.
        return left.keys() == right.keys() and all(values_equal(left[k], right[k]) for k in left)
    return bool(left == right)


# --------------------------------------------------------------------------------------
# Record shapes. Dataclasses throughout, never bare dicts.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SweepConfig:
    """Everything the sweep needs to know about this machine."""

    energyplus_root: Path
    example_dir: Path
    convert_input_format: Path
    output: Path
    idfkit_src: Path
    js_core: Path
    js_driver: Path
    node: str
    energyplus_version: str
    pattern: str
    workers: int
    limit: int | None
    oracle_timeout: float
    js_timeout: float
    max_differences_per_file: int
    max_value_chars: int
    max_files_per_group: int
    keep_temp: bool


@dataclass(frozen=True)
class Observation:
    """What one side had at one location."""

    side: str
    present: bool
    value_type: str
    value: str


@dataclass(frozen=True)
class Difference:
    """One disagreement at one location in one file."""

    kind: str  # value | type | absent | length
    pointer: str  # JSON pointer into the epJSON document
    pointer_pattern: str  # pointer with the file-specific parts erased
    partition: str  # e.g. "javascript=oracle | python"
    differing_sides: tuple[str, ...]
    value_types: str  # e.g. "python=string,javascript=number,oracle=number"
    shape: str
    observations: tuple[Observation, ...]


@dataclass(frozen=True)
class SideOutcome:
    """Whether one side produced an epJSON document, and how long it took."""

    side: str
    ok: bool
    seconds: float
    error: str = ""
    error_pattern: str = ""


@dataclass(frozen=True)
class FileResult:
    """The full three-way result for a single example file."""

    path: str
    outcomes: tuple[SideOutcome, ...]
    differences: tuple[Difference, ...]
    compared_sides: tuple[str, ...]
    differences_truncated: bool


@dataclass
class DisagreementGroup:
    """Every occurrence of one distinct disagreement shape, across all files."""

    shape: str
    kind: str
    partition: str
    value_types: str
    differing_sides: list[str]
    compared_sides: list[str]
    pointer_pattern: str
    occurrences: int
    file_count: int
    files: list[str]
    example_file: str
    example_pointer: str
    example_observations: list[Observation]


@dataclass
class ShapeFamily:
    """
    Shapes that differ only in which field they landed on.

    One hazard, say a library lower-casing ``Autosize``, produces a separate shape per
    field name and would otherwise read as three hundred findings. The family is the
    curator's entry point; the shapes under it are the evidence.
    """

    family: str
    kind: str
    partition: str
    value_types: str
    shape_count: int
    occurrences: int
    file_count: int
    pointer_patterns: list[str]


@dataclass
class OutcomeGroup:
    """Every file on which one side failed the same way."""

    side: str
    error_pattern: str
    file_count: int
    files: list[str]
    example_file: str
    example_error: str


@dataclass
class SweepTotals:
    files_seen: int
    files_swept: int
    files_all_three_ok: int
    files_with_differences: int
    files_clean: int
    differences_total: int
    distinct_shapes: int
    distinct_families: int
    failures_by_side: dict[str, int]


@dataclass
class SweepReport:
    schema_version: int
    generated_at: str
    elapsed_seconds: float
    energyplus_version: str
    config: SweepConfig
    totals: SweepTotals
    outcome_groups: list[OutcomeGroup]
    families: list[ShapeFamily]
    disagreements: list[DisagreementGroup]
    truncated_files: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# JSON pointers and shape keys.
# --------------------------------------------------------------------------------------


def escape_token(token: str) -> str:
    """RFC 6901 escaping. Object names legitimately contain slashes."""
    return token.replace("~", "~0").replace("/", "~1")


def child_pointer(pointer: str, token: str) -> str:
    return f"{pointer}/{escape_token(token)}"


_DIGITS = re.compile(r"^\d+$")


def pointer_pattern(pointer: str) -> str:
    """
    Erase the file-specific parts of a pointer so two files can share a shape.

    In epJSON the top level is ``/<Type>/<object name>/<field>``. The object name is
    file-specific and becomes ``*``; array indices become ``#``. The type and the field
    name are what the disagreement is actually about, so they stay.
    """
    if pointer == "":
        return "/"
    tokens = pointer.split("/")[1:]
    out: list[str] = []
    for index, token in enumerate(tokens):
        if index == 1:
            out.append("*")
        elif _DIGITS.match(token):
            out.append("#")
        else:
            out.append(token)
    return "/" + "/".join(out)


_HEX_OR_DIGITS = re.compile(r"\d+")
_PATHS = re.compile(r"(/[^\s'\"]+)+")


def error_pattern(message: str) -> str:
    """
    Collapse an error message to its shape so identical failures group together.

    Absolute paths and line numbers are what make two copies of the same failure look
    different, so both are erased.
    """
    first_line = message.strip().splitlines()[0] if message.strip() else "<no message>"
    without_paths = _PATHS.sub("<path>", first_line)
    return _HEX_OR_DIGITS.sub("#", without_paths).strip()[:300]


def render_value(value: Any, max_chars: int) -> str:
    """A short JSON rendering of an observed value, for the report."""
    if value is ABSENT:
        return "<absent>"
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = repr(value)
    if len(text) > max_chars:
        return text[:max_chars] + "..."
    return text


# --------------------------------------------------------------------------------------
# The three-way diff.
# --------------------------------------------------------------------------------------


@dataclass
class _SideValue:
    side: str
    value: Any


@dataclass
class _DiffBudget:
    remaining: int
    exhausted: bool = False

    def take(self) -> bool:
        if self.remaining <= 0:
            self.exhausted = True
            return False
        self.remaining -= 1
        return True


def partition_sides(entries: Sequence[_SideValue]) -> list[list[str]]:
    """
    Group sides that agree.

    Greedy against the first member of each group. With a 1e-12 tolerance the equality
    relation is near enough transitive that this is stable, and any case where it is not
    is itself worth looking at by hand.
    """
    groups: list[list[str]] = []
    representatives: list[Any] = []
    for entry in entries:
        for index, representative in enumerate(representatives):
            if values_equal(representative, entry.value):
                groups[index].append(entry.side)
                break
        else:
            groups.append([entry.side])
            representatives.append(entry.value)
    return groups


def partition_label(groups: Sequence[Sequence[str]]) -> str:
    """``javascript=oracle | python``: who agrees with whom, in a stable order."""
    ordered = sorted(
        (sorted(group, key=SIDES.index) for group in groups),
        key=lambda group: SIDES.index(group[0]),
    )
    return " | ".join("=".join(group) for group in ordered)


def _record(
    entries: Sequence[_SideValue],
    groups: Sequence[Sequence[str]],
    kind: str,
    pointer: str,
    config: SweepConfig,
) -> Difference:
    observations = tuple(
        Observation(
            side=entry.side,
            present=entry.value is not ABSENT,
            value_type=json_type(entry.value),
            value=render_value(entry.value, config.max_value_chars),
        )
        for entry in entries
    )
    label = partition_label(groups)
    pattern = pointer_pattern(pointer)
    # A side is "differing" when it sits alone; when the split is even, every side is.
    largest = max(len(group) for group in groups)
    differing = tuple(
        side
        for group in sorted(groups, key=lambda g: SIDES.index(g[0]))
        for side in sorted(group, key=SIDES.index)
        if len(group) < largest or largest == 1
    )
    types = ",".join(f"{observation.side}={observation.value_type}" for observation in observations)
    shape = f"{kind}::{label}::{pattern}::{types}"
    return Difference(
        kind=kind,
        pointer=pointer,
        pointer_pattern=pattern,
        partition=label,
        differing_sides=differing,
        value_types=types,
        shape=shape,
        observations=observations,
    )


def diff_sides(
    entries: Sequence[_SideValue],
    pointer: str,
    config: SweepConfig,
    budget: _DiffBudget,
    out: list[Difference],
) -> None:
    """Walk all sides at once, recording every location where they do not agree."""
    groups = partition_sides(entries)
    if len(groups) == 1:
        return

    types = {json_type(entry.value) for entry in entries}

    if types == {"object"}:
        keys: list[str] = []
        seen: set[str] = set()
        for entry in entries:
            for key in entry.value:
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
        for key in keys:
            diff_sides(
                [_SideValue(entry.side, entry.value.get(key, ABSENT)) for entry in entries],
                child_pointer(pointer, key),
                config,
                budget,
                out,
            )
            if budget.exhausted:
                return
        return

    if types == {"array"}:
        lengths = {len(entry.value) for entry in entries}
        if len(lengths) > 1:
            length_entries = [_SideValue(entry.side, len(entry.value)) for entry in entries]
            if not budget.take():
                return
            out.append(
                _record(length_entries, partition_sides(length_entries), "length", pointer, config)
            )
        shortest = min(len(entry.value) for entry in entries)
        for index in range(shortest):
            diff_sides(
                [_SideValue(entry.side, entry.value[index]) for entry in entries],
                child_pointer(pointer, str(index)),
                config,
                budget,
                out,
            )
            if budget.exhausted:
                return
        return

    if "absent" in types:
        kind = "absent"
    elif len(types) > 1:
        kind = "type"
    else:
        kind = "value"

    if not budget.take():
        return
    out.append(_record(entries, groups, kind, pointer, config))


# --------------------------------------------------------------------------------------
# The three sides.
# --------------------------------------------------------------------------------------


def _python_epjson(path: Path, config: SweepConfig) -> tuple[SideOutcome, Any]:
    started = time.perf_counter()
    try:
        import idfkit

        document = idfkit.parse_idf(path, encoding=ENCODING)
        parsed = json.loads(idfkit.write_epjson(document))
    except BaseException as error:  # noqa: BLE001 - a crash on one side is a finding
        message = f"{type(error).__name__}: {error}"
        return (
            SideOutcome(
                side=PYTHON,
                ok=False,
                seconds=time.perf_counter() - started,
                error=message[:2000],
                error_pattern=error_pattern(message),
            ),
            ABSENT,
        )
    return SideOutcome(side=PYTHON, ok=True, seconds=time.perf_counter() - started), parsed


@dataclass
class JsRunner:
    """A long-lived ``sweep_js.mjs`` process, one per worker."""

    config: SweepConfig
    scratch: Path
    process: subprocess.Popen[str] | None = None
    stderr_path: Path | None = None

    def start(self) -> None:
        self.stderr_path = self.scratch / "sweep_js.stderr"
        with open(self.stderr_path, "w", encoding="utf-8") as handle:
            self.process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                [self.config.node, str(self.config.js_driver), str(self.config.js_core)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=handle,
                text=True,
                encoding="utf-8",
            )

    def stop(self) -> None:
        if self.process is None:
            return
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
            self.process.wait(timeout=5)
        except Exception:  # noqa: BLE001 - shutdown is best effort
            self.process.kill()
        finally:
            self.process = None

    def _stderr_tail(self) -> str:
        if self.stderr_path is None or not self.stderr_path.exists():
            return ""
        return self.stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]

    def convert(self, path: Path, destination: Path) -> tuple[SideOutcome, Any]:
        started = time.perf_counter()
        if self.process is None or self.process.poll() is not None:
            self.stop()
            self.start()
        assert self.process is not None
        assert self.process.stdin is not None
        assert self.process.stdout is not None

        request = json.dumps({"input": str(path), "output": str(destination)})
        try:
            self.process.stdin.write(request + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, ValueError):
            return self._failed(started, f"driver is not running: {self._stderr_tail()}"), ABSENT

        line = self._read_line(self.config.js_timeout)
        if line is None:
            # A hang would otherwise take the whole sweep with it. Kill and move on.
            self.process.kill()
            self.stop()
            return self._failed(started, f"timed out after {self.config.js_timeout:g}s"), ABSENT
        if line == "":
            return self._failed(started, f"driver exited: {self._stderr_tail()}"), ABSENT

        reply = json.loads(line)
        if not reply.get("ok"):
            return self._failed(started, str(reply.get("error", "unknown error"))), ABSENT
        try:
            parsed = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            return self._failed(started, f"unreadable output: {error}"), ABSENT
        finally:
            destination.unlink(missing_ok=True)
        return SideOutcome(side=JAVASCRIPT, ok=True, seconds=time.perf_counter() - started), parsed

    def _read_line(self, timeout: float) -> str | None:
        assert self.process is not None
        assert self.process.stdout is not None
        stdout = self.process.stdout
        box: list[str] = []

        def reader() -> None:
            try:
                box.append(stdout.readline())
            except Exception:  # noqa: BLE001 - the killed-pipe case
                box.append("")

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            return None
        return box[0] if box else ""

    def _failed(self, started: float, message: str) -> SideOutcome:
        return SideOutcome(
            side=JAVASCRIPT,
            ok=False,
            seconds=time.perf_counter() - started,
            error=message[:2000],
            error_pattern=error_pattern(message),
        )


def _oracle_epjson(path: Path, scratch: Path, config: SweepConfig) -> tuple[SideOutcome, Any]:
    started = time.perf_counter()
    destination = scratch / "oracle"
    destination.mkdir(parents=True, exist_ok=True)
    produced = destination / f"{path.stem}.epJSON"
    produced.unlink(missing_ok=True)
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [str(config.convert_input_format), "-f", "epJSON", "-o", str(destination), str(path)],
            capture_output=True,
            text=True,
            encoding=ENCODING,
            timeout=config.oracle_timeout,
            check=False,
            cwd=str(destination),
        )
    except subprocess.TimeoutExpired:
        return _oracle_failure(started, f"timed out after {config.oracle_timeout:g}s"), ABSENT

    if not produced.exists():
        # ConvertInputFormat validates before it converts, so this is a legitimate and
        # expected outcome for some example files. Record it; never drop the file.
        detail = (completed.stdout or "").strip() or (completed.stderr or "").strip()
        return _oracle_failure(started, detail or f"exit code {completed.returncode}"), ABSENT

    try:
        parsed = json.loads(produced.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return _oracle_failure(started, f"unreadable output: {error}"), ABSENT
    finally:
        produced.unlink(missing_ok=True)
    return SideOutcome(side=ORACLE, ok=True, seconds=time.perf_counter() - started), parsed


def _oracle_failure(started: float, message: str) -> SideOutcome:
    return SideOutcome(
        side=ORACLE,
        ok=False,
        seconds=time.perf_counter() - started,
        error=message[:2000],
        error_pattern=error_pattern(message),
    )


# --------------------------------------------------------------------------------------
# Worker plumbing.
# --------------------------------------------------------------------------------------

_CONFIG: SweepConfig | None = None
_SCRATCH: Path | None = None
_JS: JsRunner | None = None


def worker_init(config: SweepConfig) -> None:
    global _CONFIG, _SCRATCH, _JS
    _CONFIG = config
    _SCRATCH = Path(tempfile.mkdtemp(prefix="idfkit-sweep-"))
    ensure_idfkit_importable(config)
    _JS = JsRunner(config=config, scratch=_SCRATCH)
    atexit.register(worker_shutdown)


def worker_shutdown() -> None:
    global _JS, _SCRATCH
    if _JS is not None:
        _JS.stop()
        _JS = None
    if _SCRATCH is not None and _CONFIG is not None and not _CONFIG.keep_temp:
        shutil.rmtree(_SCRATCH, ignore_errors=True)
    _SCRATCH = None


def ensure_idfkit_importable(config: SweepConfig) -> None:
    """Import ``idfkit`` from a checkout when it is not installed in this interpreter."""
    src = str(config.idfkit_src)
    if src not in sys.path:
        sys.path.insert(0, src)


def sweep_file(path_text: str) -> FileResult:
    assert _CONFIG is not None and _SCRATCH is not None and _JS is not None
    config, scratch, runner = _CONFIG, _SCRATCH, _JS
    path = Path(path_text)

    python_outcome, python_value = _python_epjson(path, config)
    js_outcome, js_value = runner.convert(path, scratch / "js.epJSON")
    oracle_outcome, oracle_value = _oracle_epjson(path, scratch, config)

    outcomes = (python_outcome, js_outcome, oracle_outcome)
    entries = [
        _SideValue(PYTHON, python_value),
        _SideValue(JAVASCRIPT, js_value),
        _SideValue(ORACLE, oracle_value),
    ]
    available = [entry for entry in entries if entry.value is not ABSENT]

    differences: list[Difference] = []
    budget = _DiffBudget(remaining=config.max_differences_per_file)
    if len(available) >= 2:
        # Two working sides still say something worth recording, even when the oracle
        # refused the file.
        diff_sides(available, "", config, budget, differences)

    return FileResult(
        path=str(path),
        outcomes=outcomes,
        differences=tuple(differences),
        compared_sides=tuple(entry.side for entry in available),
        differences_truncated=budget.exhausted,
    )


# --------------------------------------------------------------------------------------
# Aggregation.
# --------------------------------------------------------------------------------------


def aggregate(
    results: Iterable[FileResult], config: SweepConfig
) -> tuple[
    list[DisagreementGroup],
    list[ShapeFamily],
    list[OutcomeGroup],
    SweepTotals,
    list[str],
]:
    groups: dict[str, DisagreementGroup] = {}
    group_files: dict[str, set[str]] = {}
    outcome_groups: dict[tuple[str, str], OutcomeGroup] = {}
    outcome_files: dict[tuple[str, str], set[str]] = {}
    failures_by_side = {side: 0 for side in SIDES}
    files_seen = 0
    files_all_three_ok = 0
    files_with_differences = 0
    differences_total = 0
    truncated: list[str] = []

    for result in results:
        files_seen += 1
        name = Path(result.path).name
        if all(outcome.ok for outcome in result.outcomes):
            files_all_three_ok += 1
        for outcome in result.outcomes:
            if outcome.ok:
                continue
            failures_by_side[outcome.side] += 1
            key = (outcome.side, outcome.error_pattern)
            outcome_group = outcome_groups.get(key)
            if outcome_group is None:
                outcome_groups[key] = OutcomeGroup(
                    side=outcome.side,
                    error_pattern=outcome.error_pattern,
                    file_count=1,
                    files=[name],
                    example_file=name,
                    example_error=outcome.error,
                )
                outcome_files[key] = {name}
            elif name not in outcome_files[key]:
                outcome_files[key].add(name)
                outcome_group.file_count += 1
                if len(outcome_group.files) < config.max_files_per_group:
                    outcome_group.files.append(name)

        if result.differences:
            files_with_differences += 1
        if result.differences_truncated:
            truncated.append(name)

        for difference in result.differences:
            differences_total += 1
            group = groups.get(difference.shape)
            if group is None:
                groups[difference.shape] = DisagreementGroup(
                    shape=difference.shape,
                    kind=difference.kind,
                    partition=difference.partition,
                    value_types=difference.value_types,
                    differing_sides=list(difference.differing_sides),
                    compared_sides=list(result.compared_sides),
                    pointer_pattern=difference.pointer_pattern,
                    occurrences=1,
                    file_count=1,
                    files=[name],
                    example_file=name,
                    example_pointer=difference.pointer,
                    example_observations=list(difference.observations),
                )
                group_files[difference.shape] = {name}
            else:
                group.occurrences += 1
                if name not in group_files[difference.shape]:
                    group_files[difference.shape].add(name)
                    group.file_count += 1
                    if len(group.files) < config.max_files_per_group:
                        group.files.append(name)

    ordered_groups = sorted(groups.values(), key=lambda g: (-g.occurrences, g.shape))
    ordered_outcomes = sorted(outcome_groups.values(), key=lambda g: (g.side, -g.file_count))
    families = build_families(ordered_groups, group_files, config)
    totals = SweepTotals(
        files_seen=files_seen,
        files_swept=files_seen,
        files_all_three_ok=files_all_three_ok,
        files_with_differences=files_with_differences,
        files_clean=files_seen - files_with_differences,
        differences_total=differences_total,
        distinct_shapes=len(ordered_groups),
        distinct_families=len(families),
        failures_by_side=failures_by_side,
    )
    return ordered_groups, families, ordered_outcomes, totals, truncated


def build_families(
    groups: Sequence[DisagreementGroup],
    group_files: dict[str, set[str]],
    config: SweepConfig,
) -> list[ShapeFamily]:
    """Roll shapes up by everything except the field they landed on."""
    families: dict[str, ShapeFamily] = {}
    family_files: dict[str, set[str]] = {}
    for group in groups:
        key = f"{group.kind}::{group.partition}::{group.value_types}"
        family = families.get(key)
        if family is None:
            families[key] = ShapeFamily(
                family=key,
                kind=group.kind,
                partition=group.partition,
                value_types=group.value_types,
                shape_count=1,
                occurrences=group.occurrences,
                file_count=0,
                pointer_patterns=[group.pointer_pattern],
            )
            family_files[key] = set(group_files.get(group.shape, set()))
        else:
            family.shape_count += 1
            family.occurrences += group.occurrences
            if len(family.pointer_patterns) < config.max_files_per_group:
                family.pointer_patterns.append(group.pointer_pattern)
            family_files[key] |= group_files.get(group.shape, set())
    for key, family in families.items():
        family.file_count = len(family_files[key])
    return sorted(families.values(), key=lambda f: (-f.occurrences, f.family))


# --------------------------------------------------------------------------------------
# Entry point.
# --------------------------------------------------------------------------------------


def version_from_root(root: Path) -> str:
    match = re.search(r"(\d+)[-.](\d+)[-.](\d+)", root.name)
    if match:
        return ".".join(match.groups())
    return "unknown"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Three-way sweep of idfkit, @idfkit/core and ConvertInputFormat.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--energyplus-root", type=Path, default=DEFAULT_ENERGYPLUS_ROOT)
    parser.add_argument(
        "--example-dir",
        type=Path,
        default=None,
        help="defaults to ExampleFiles under the EnergyPlus root",
    )
    parser.add_argument(
        "--convert-input-format",
        type=Path,
        default=None,
        help="defaults to ConvertInputFormat under the EnergyPlus root",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--idfkit-src", type=Path, default=DEFAULT_IDFKIT_SRC)
    parser.add_argument("--js-core", type=Path, default=DEFAULT_JS_CORE)
    parser.add_argument("--js-driver", type=Path, default=DEFAULT_JS_DRIVER)
    parser.add_argument("--node", default="node")
    parser.add_argument("--energyplus-version", default=None)
    parser.add_argument("--pattern", default="*.idf")
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) // 2))
    parser.add_argument("--limit", type=int, default=None, help="sweep only the first N files")
    parser.add_argument("--oracle-timeout", type=float, default=120.0)
    parser.add_argument("--js-timeout", type=float, default=180.0)
    parser.add_argument("--max-differences-per-file", type=int, default=500)
    parser.add_argument("--max-value-chars", type=int, default=200)
    parser.add_argument("--max-files-per-group", type=int, default=50)
    parser.add_argument("--keep-temp", action="store_true")
    return parser


def config_from_args(args: argparse.Namespace) -> SweepConfig:
    root = args.energyplus_root.expanduser().resolve()
    example_dir = (args.example_dir or root / "ExampleFiles").expanduser()
    convert = (args.convert_input_format or root / "ConvertInputFormat").expanduser()
    return SweepConfig(
        energyplus_root=root,
        example_dir=example_dir.resolve(),
        convert_input_format=convert.resolve(),
        output=args.output.expanduser().resolve(),
        idfkit_src=args.idfkit_src.expanduser().resolve(),
        js_core=args.js_core.expanduser().resolve(),
        js_driver=args.js_driver.expanduser().resolve(),
        node=args.node,
        energyplus_version=args.energyplus_version or version_from_root(root),
        pattern=args.pattern,
        workers=max(1, args.workers),
        limit=args.limit,
        oracle_timeout=args.oracle_timeout,
        js_timeout=args.js_timeout,
        max_differences_per_file=args.max_differences_per_file,
        max_value_chars=args.max_value_chars,
        max_files_per_group=args.max_files_per_group,
        keep_temp=args.keep_temp,
    )


def collect_inputs(config: SweepConfig) -> list[Path]:
    files = sorted(config.example_dir.glob(config.pattern))
    if config.limit is not None:
        files = files[: config.limit]
    return files


def run_sweep(config: SweepConfig, files: Sequence[Path]) -> list[FileResult]:
    results: list[FileResult] = []
    total = len(files)

    if config.workers == 1:
        worker_init(config)
        try:
            for index, path in enumerate(files, start=1):
                results.append(sweep_file(str(path)))
                _progress(index, total, path)
        finally:
            worker_shutdown()
        return results

    with ProcessPoolExecutor(
        max_workers=config.workers, initializer=worker_init, initargs=(config,)
    ) as pool:
        futures = {pool.submit(sweep_file, str(path)): path for path in files}
        for index, future in enumerate(as_completed(futures), start=1):
            path = futures[future]
            try:
                results.append(future.result())
            except Exception as error:  # noqa: BLE001 - one bad file must not stop the sweep
                message = f"{type(error).__name__}: {error}"
                results.append(
                    FileResult(
                        path=str(path),
                        outcomes=tuple(
                            SideOutcome(
                                side=side,
                                ok=False,
                                seconds=0.0,
                                error=f"worker crashed: {message}"[:2000],
                                error_pattern=error_pattern(f"worker crashed: {message}"),
                            )
                            for side in SIDES
                        ),
                        differences=(),
                        compared_sides=(),
                        differences_truncated=False,
                    )
                )
            _progress(index, total, path)
    results.sort(key=lambda result: result.path)
    return results


def _progress(index: int, total: int, path: Path) -> None:
    print(f"[{index}/{total}] {path.name}", file=sys.stderr, flush=True)


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def print_summary(report: SweepReport) -> None:
    totals = report.totals
    print(
        f"\nswept {totals.files_swept} file(s) in {report.elapsed_seconds:.1f}s "
        f"against EnergyPlus {report.energyplus_version}",
        file=sys.stderr,
    )
    print(
        f"  all three sides parsed: {totals.files_all_three_ok}"
        f"   files with disagreement: {totals.files_with_differences}",
        file=sys.stderr,
    )
    for side in SIDES:
        print(f"  {side} failures: {totals.failures_by_side[side]}", file=sys.stderr)
    print(
        f"  {totals.differences_total} difference(s) in {totals.distinct_shapes} distinct "
        f"shape(s), {totals.distinct_families} family(ies)",
        file=sys.stderr,
    )
    for family in report.families[:15]:
        first = family.pointer_patterns[0]
        more = f" (+{family.shape_count - 1} more field(s))" if family.shape_count > 1 else ""
        print(
            f"    {family.occurrences:>6} x [{family.partition}] {family.kind} "
            f"{family.value_types}\n           e.g. {first}{more}",
            file=sys.stderr,
        )
    if len(report.families) > 15:
        print(f"    ... {len(report.families) - 15} more family(ies)", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = config_from_args(args)

    if not config.example_dir.is_dir():
        print(f"no example directory at {config.example_dir}", file=sys.stderr)
        return 2
    if not config.convert_input_format.exists():
        print(f"no ConvertInputFormat at {config.convert_input_format}", file=sys.stderr)
        return 2
    if not config.js_driver.exists():
        print(f"no JavaScript driver at {config.js_driver}", file=sys.stderr)
        return 2

    files = collect_inputs(config)
    if not files:
        print(f"no files matching {config.pattern} in {config.example_dir}", file=sys.stderr)
        return 2

    started = time.perf_counter()
    results = run_sweep(config, files)
    elapsed = time.perf_counter() - started

    disagreements, families, outcome_groups, totals, truncated = aggregate(results, config)
    report = SweepReport(
        schema_version=REPORT_SCHEMA_VERSION,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        elapsed_seconds=round(elapsed, 3),
        energyplus_version=config.energyplus_version,
        config=config,
        totals=totals,
        outcome_groups=outcome_groups,
        families=families,
        disagreements=disagreements,
        truncated_files=truncated,
    )

    config.output.parent.mkdir(parents=True, exist_ok=True)
    config.output.write_text(
        json.dumps(asdict(report), indent=2, ensure_ascii=False, default=_json_default) + "\n",
        encoding="utf-8",
    )
    print_summary(report)
    print(f"\nwrote {config.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
