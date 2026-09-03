#!/usr/bin/env python3
"""The Python conformance runner: six shipping assertions against one exit contract.

Run it from the root of this repository, pointing ``--library`` at a checkout of the Python
library. The flag takes a **path**, never a language word, because the runner file already fixes
the language: ``run.py`` drives Python and ``run.mjs`` drives JavaScript.

    python runners/run.py --library /path/to/idfkit
    python runners/run.py --library /path/to/idfkit --case naming-blank-vs-absent
    python runners/run.py --library /path/to/idfkit --tag extensible

What it checks, per case, in this order:

1. ``parse-outcome``  the input parses, or fails to parse, exactly as ``case.toml`` declares.
2. ``epjson``         the library's canonical epJSON equals the committed ``expected.epJSON``.
3. ``round-trip``     re-parsing the library's own IDF output deep-equals the original document.
4. ``diagnostics``    accepted and reported as skipped. Assertion 4 is deferred to phase two, and a
                      case may declare it today so that landing it later changes no case file.
5. ``validation``     the library's validation findings equal ``expected.validation.json``.
6. ``introspection``  the library's type descriptions equal ``expected.introspection.json``.
7. ``docs-url``       the library's documentation addresses equal ``expected.docs-url.json``.
8. ``type-lookup``   naming an object type through the library's own collection accessor returns
                      what ``expected.type-lookup.json`` says, for canonical, mis-cased and unknown
                      names alike, and leaves the document unchanged.

Assertions 5 to 7 have no oracle behind them: ``ConvertInputFormat`` converts a file, and does not
validate one, describe a type, or build a documentation address. What rules them is written down
instead, in ``validate.md`` for assertion 5 and in the epJSON schema itself for assertions 6 and 7,
so a Tier 1 failure is read against that rule before it is read as a port bug. Their expectations
are also serialized in the corpus's own snake_case vocabulary rather than either library's, which
is why this file spells out every key it emits instead of handing a library object to the
comparator: ``tools/seed_tier1.py`` writes the same shapes, and the two must not drift.

Assertion 3 is the only place IDF field order is compared, which is rule 4 of ``compare.md``. That
rule is a constraint on what this runner hands to the comparator rather than on the comparator
itself: :func:`document_snapshot` puts each object's field sequence in a JSON **array**, because
rule 7 makes every array ordered and compared index by index. Flattening the sequence into an
object would throw the order away before the comparator ever saw it, and nothing else in the suite
catches a round trip that emits the right values in the wrong slots.

The snapshot shape, so a reader of a failure path can decode it:

    {"<ObjectType>": [{"name": "<object name>",
                       "fields": [{"name": "<field>", "value": <value>}, ...]}, ...]}

so ``/Zone/0/fields/4/name`` is the name of the fifth field of the first ``Zone``, and
``/Zone/0/fields/4/value`` is its value. A field that moved shows up as a ``value`` difference on
the ``name`` path, which is what a positional bug looks like.

Three behaviours that are requirements rather than conveniences:

* **Outstanding exceptions are printed in the normal output**, never behind a verbose flag. A
  silent allowlist is how a temporary exception becomes permanent, so every entry in
  ``known-divergence.toml`` that is still failing is listed with its issue link on every run.
* **A stale exception fails the run.** An entry whose case now passes exits 1 and says to remove
  it, because a stale exception is removed by the change that fixes the bug.
* **An empty corpus is a clean pass**, not a crash. ``cases/`` is populated by curation from a
  bootstrap sweep, and the runner must be usable, and testable, before that lands. No case id
  appears anywhere in this file.

Exit codes: 0 when everything is green or every failure is allowlisted, 1 for any blocking
failure, stale entry, or corpus fault, and 2 when the run could not start at all, for instance
because ``--library`` names no importable checkout.

Python version: 3.10 or newer, matching ``model.py``.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Final

RUNNERS_DIR: Final = Path(__file__).resolve().parent
REPO_ROOT: Final = RUNNERS_DIR.parent

# ``compare`` and ``model`` are siblings of this file, not an installed package. Put their
# directory on the path before importing them, exactly as ``compare.py`` documents. This runs in a
# spawned worker too, which re-imports this module under ``__mp_main__``.
if str(RUNNERS_DIR) not in sys.path:
    sys.path.insert(0, str(RUNNERS_DIR))

from compare import (  # noqa: E402
    AssertionReport,
    Comparison,
    compare_documents,
    compare_epjson,
    compare_outcome,
    compare_unordered,
    compare_values,
    json_pointer,
)
from model import (  # noqa: E402
    CASES_DIR,
    CORPUS_LEVEL_PATTERN,
    DIVERGENCE_FILE,
    EXPECTED_DOCS_URL,
    EXPECTED_EPJSON,
    EXPECTED_INTROSPECTION,
    EXPECTED_TYPE_LOOKUP,
    EXPECTED_VALIDATION,
    MANIFEST_FILE,
    Assertion,
    Corpus,
    CorpusError,
    Divergence,
    InputFile,
    Library,
    Manifest,
    ParseOutcome,
    Tag,
    Truth,
    load_corpus,
    load_manifest,
)

__all__ = [
    "AssertionOutcome",
    "CaseJob",
    "CaseOutcome",
    "Level",
    "LibraryUnderTest",
    "RunReport",
    "RunnerError",
    "Status",
    "build_jobs",
    "detect_level",
    "document_snapshot",
    "import_library",
    "main",
    "run_case",
]

# This file drives the Python library and only the Python library. ``run.mjs`` is its mirror.
LIBRARY: Final = Library.PYTHON

# Rule 6 of compare.md. Every IDF this runner reads or writes is latin-1, which never fails on any
# byte sequence, so a library that garbles a high byte reports it as a value difference rather than
# crashing the comparison.
IDF_ENCODING: Final = "latin-1"

# The assertion order used in every report, independent of the order a case.toml happens to list.
ASSERTION_ORDER: Final = (
    Assertion.PARSE_OUTCOME,
    Assertion.EPJSON,
    Assertion.ROUND_TRIP,
    Assertion.DIAGNOSTICS,
    Assertion.VALIDATION,
    Assertion.INTROSPECTION,
    Assertion.DOCS_URL,
    Assertion.TYPE_LOOKUP,
)

# The members of one field description, in the order ``compare.md`` writes them. Spelled out here
# rather than read off the library object because the corpus vocabulary is snake_case and belongs
# to neither library: Python's attributes already read this way and TypeScript's are camelCase, so
# a file written from whichever object happened to be handed over would be a transcript of that
# library. Naming the keys also makes rule 3 do its work, since a key the library stopped
# populating is then a ``missing`` difference rather than a silently shorter description.
# ``tools/seed_tier1.py`` holds the same tuple, and the two must agree or the runner and the
# seeder disagree about what the corpus records.
INTROSPECTION_FIELD_KEYS: Final = (
    "name",
    "field_type",
    "required",
    "default",
    "units",
    "enum_values",
    "minimum",
    "maximum",
    "exclusive_minimum",
    "exclusive_maximum",
    "note",
    "is_reference",
    "object_list",
)

DEFAULT_DIFFERENCE_LIMIT: Final = 20
MAX_POOL_WORKERS: Final = 8
POOL_THRESHOLD: Final = 8  # Below this many cases a pool costs more than it saves.

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_UNUSABLE: Final = 2

DIAGNOSTICS_DEFERRED: Final = (
    "assertion 4 is deferred to phase two and is not compared yet. The case may declare it today "
    "so that landing it later changes no case file"
)


class RunnerError(Exception):
    """The run cannot start: a bad flag, or a ``--library`` path with nothing importable in it."""


class Status(str, Enum):
    """What one assertion did.

    ``FAILED`` is the only status the exception register can accept. ``ERRORED`` means the runner
    could not evaluate the assertion at all, which is a corpus or environment problem rather than a
    library disagreement, so it always blocks and is never allowlistable.
    """

    PASSED = "pass"
    FAILED = "fail"
    SKIPPED = "skip"
    ERRORED = "error"


# ---------------------------------------------------------------------------
# The shapes carried through the run
# ---------------------------------------------------------------------------
#
# None of these use ``slots=True``. They cross a process boundary when ``--jobs`` opens a pool, and
# a frozen slotted dataclass does not pickle on every interpreter this runner supports.


@dataclass(frozen=True)
class LibraryUnderTest:
    """The checkout ``--library`` named, once it has been imported out of that path."""

    root: Path
    import_root: Path
    module_file: Path
    version: str

    def describe(self) -> str:
        """One line for the header: which implementation, which build, and where it came from."""
        return f"{LIBRARY.value}, idfkit {self.version} from {self.module_file}"


@dataclass(frozen=True)
class Level:
    """The corpus level this run is reporting against, and where the value came from."""

    value: str | None
    source: str

    def describe(self) -> str:
        """One line for the header. An unpinned corpus says so rather than inventing a level."""
        if self.value is None:
            return f"unpinned ({self.source})"
        return f"{self.value} ({self.source})"


@dataclass(frozen=True)
class CaseJob:
    """Everything one case needs, flattened so a worker process needs no corpus of its own."""

    case_id: str
    case_dir: Path
    input_file: InputFile
    expected_parse_outcome: ParseOutcome
    truth: Truth
    assertions: tuple[Assertion, ...]

    @property
    def input_path(self) -> Path:
        """The case input, whose extension decides which reader the runner dispatches to."""
        return self.case_dir / self.input_file.value

    @property
    def expected_epjson_path(self) -> Path:
        """The committed expectation. Present only when ``truth = oracle``."""
        return self.case_dir / EXPECTED_EPJSON


@dataclass(frozen=True)
class AssertionOutcome:
    """One assertion's verdict for one case, with everything the exit contract has to print.

    ``lines`` is the finished printable block. For a failed comparison it comes straight from
    :meth:`compare.AssertionReport.render`, which is the exit contract's own layout, so this runner
    never writes a second rendering of it. ``reason`` is the one-line summary the digest views use,
    and ``first_difference`` is the single most informative line, quoted beside an accepted
    exception so a reader sees what the library does today without scrolling.

    Everything is rendered in the worker, which keeps every value that crosses a process boundary a
    plain string.
    """

    case_id: str
    assertion: Assertion
    status: Status
    reason: str = ""
    lines: tuple[str, ...] = ()
    first_difference: str = ""

    @property
    def blocks(self) -> bool:
        """Whether this outcome fails the run unless the register accepts it."""
        return self.status in (Status.FAILED, Status.ERRORED)

    def render(self) -> tuple[str, ...]:
        """The report block for this outcome."""
        return self.lines or (_head(self.case_id, self.assertion, self.reason),)


@dataclass(frozen=True)
class CaseOutcome:
    """Every assertion a case declared, plus what the case cost."""

    case_id: str
    seconds: float
    outcomes: tuple[AssertionOutcome, ...] = ()

    def outcome_for(self, assertion: Assertion) -> AssertionOutcome | None:
        """This case's verdict on one assertion, or ``None`` when the case never declared it."""
        for outcome in self.outcomes:
            if outcome.assertion is assertion:
                return outcome
        return None


@dataclass
class RunReport:
    """What the run found, sorted into the buckets the exit contract distinguishes.

    Mutable on purpose: it is filled in as results arrive, then read once to print and once to
    decide the exit code.
    """

    library: LibraryUnderTest
    level: Level
    corpus_root: Path
    register_note: str
    selected: int
    total: int
    cases: list[CaseOutcome] = field(default_factory=list)
    blocking: list[AssertionOutcome] = field(default_factory=list)
    outstanding: list[tuple[Divergence, AssertionOutcome]] = field(default_factory=list)
    stale: list[Divergence] = field(default_factory=list)
    unexercisable: list[tuple[Divergence, str]] = field(default_factory=list)
    deferred_exceptions: list[Divergence] = field(default_factory=list)
    unselected_exceptions: int = 0
    seconds: float = 0.0

    def counts(self) -> dict[Status, int]:
        """How many assertions landed in each status, across every case that ran."""
        tally = dict.fromkeys(Status, 0)
        for case in self.cases:
            for outcome in case.outcomes:
                tally[outcome.status] += 1
        return tally

    @property
    def assertion_count(self) -> int:
        """How many assertions ran, which is not the case count: a case declares several."""
        return sum(len(case.outcomes) for case in self.cases)

    @property
    def green(self) -> bool:
        """Whether the run passes: nothing blocking, nothing stale, no corpus fault."""
        return not (self.blocking or self.stale or self.unexercisable)


# ---------------------------------------------------------------------------
# Importing the library out of the path ``--library`` named
# ---------------------------------------------------------------------------


def import_library(root: Path) -> LibraryUnderTest:
    """Import ``idfkit`` out of the checkout at ``root``, never from site-packages.

    The suite tests a checkout, so an installed copy shadowing it would silently test the wrong
    build. The import is verified to have come from under ``root``, and a mismatch is an error
    rather than a warning.
    """
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise RunnerError(f"--library {root} is not a directory")

    candidates = [root / "src", root]
    if root.name == "idfkit" and (root / "__init__.py").is_file():
        candidates.append(root.parent)
    import_root = next((c for c in candidates if (c / "idfkit" / "__init__.py").is_file()), None)
    if import_root is None:
        raise RunnerError(
            f"no importable 'idfkit' package under {root}. Looked for "
            f"{', '.join(str(c / 'idfkit' / '__init__.py') for c in candidates)}. "
            f"run.py drives the Python library; use 'node runners/run.mjs --library <path>' for idfkit-js"
        )

    text = str(import_root)
    if sys.path[:1] != [text]:
        while text in sys.path:
            sys.path.remove(text)
        sys.path.insert(0, text)
    # A previously imported idfkit from somewhere else would win over the path entry.
    for name in [name for name in sys.modules if name == "idfkit" or name.startswith("idfkit.")]:
        module_file = getattr(sys.modules[name], "__file__", None)
        if module_file is None or import_root not in Path(module_file).resolve().parents:
            del sys.modules[name]

    module = importlib.import_module("idfkit")
    module_file = getattr(module, "__file__", None)
    if module_file is None:
        raise RunnerError(f"imported 'idfkit' has no __file__, so it cannot be traced back to {root}")
    resolved = Path(module_file).resolve()
    if import_root not in resolved.parents:
        raise RunnerError(
            f"imported 'idfkit' from {resolved}, which is not under {import_root}. "
            f"An installed copy is shadowing the checkout, so the run would test the wrong build"
        )
    return LibraryUnderTest(
        root=root,
        import_root=import_root,
        module_file=resolved,
        version=str(getattr(module, "__version__", "unknown")),
    )


# ---------------------------------------------------------------------------
# The level: an explicit flag, else the repository's git tag, else the manifest
# ---------------------------------------------------------------------------


def detect_level(root: Path, manifest: Manifest, explicit: str | None) -> Level:
    """Resolve the corpus level. A level is an immutable git tag of the form ``conformance-YYYY.N``."""
    if explicit is not None:
        if not CORPUS_LEVEL_PATTERN.match(explicit):
            raise RunnerError(f"--level must match conformance-YYYY.N, got {explicit!r}")
        return Level(explicit, "--level")

    exact = _git_tag(root, exact=True)
    if exact is not None:
        return Level(exact, "git tag")
    nearest = _git_tag(root, exact=False)
    if nearest is not None:
        return Level(nearest, "nearest git tag, HEAD is ahead of it")
    if manifest.corpus_level is not None:
        return Level(manifest.corpus_level, "manifest.json")
    return Level(None, "no conformance-* git tag, and manifest.json sets corpus_level to null")


def _git_tag(root: Path, *, exact: bool) -> str | None:
    """The level tag on HEAD, or the most recent one, or ``None``. Never raises."""
    command = ["git", "-C", str(root), "describe", "--tags", "--match", "conformance-*"]
    command.append("--exact-match" if exact else "--abbrev=0")
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    tag = completed.stdout.strip()
    return tag if CORPUS_LEVEL_PATTERN.match(tag) else None


# ---------------------------------------------------------------------------
# Selecting cases
# ---------------------------------------------------------------------------


def build_jobs(corpus: Corpus, case_ids: Sequence[str], tags: Sequence[Tag]) -> tuple[CaseJob, ...]:
    """The cases to run, in manifest order, narrowed by ``--case`` and ``--tag``.

    Both filters may be repeated. Given both, a case must match an id **and** carry a tag, so
    ``--case x --tag numeric`` asks whether that one case pins that hazard.

    A filter that selects nothing raises, which exits 2, symmetric with an unknown ``--case`` id
    and an unknown ``--tag`` value. A selection matching no case runs no assertion and would
    otherwise report a green PASS proving nothing, and a green gate that proves nothing is worse
    than a red one: wiring one into CI is exactly the mistake this refusal prevents. An empty
    corpus is a different thing, and stays a clean pass with its own message, because ``cases/`` is
    populated by curation and the runner must be usable before that lands.
    """
    known = {entry.id for entry in corpus.manifest.entries()}
    unknown = [case_id for case_id in case_ids if case_id not in known]
    if unknown:
        raise RunnerError(
            f"--case named {', '.join(repr(case_id) for case_id in unknown)}, which is not in the manifest. "
            f"The corpus holds {len(known)} case(s)"
        )

    wanted_ids = set(case_ids)
    wanted_tags = set(tags)
    jobs: list[CaseJob] = []
    for entry in corpus.manifest.entries():
        if wanted_ids and entry.id not in wanted_ids:
            continue
        if wanted_tags and not wanted_tags.intersection(entry.tags):
            continue
        jobs.append(
            CaseJob(
                case_id=entry.id,
                case_dir=corpus.case_dir(entry.id),
                input_file=entry.input,
                expected_parse_outcome=entry.parse_outcome,
                truth=entry.truth,
                assertions=tuple(a for a in ASSERTION_ORDER if a in entry.assertions),
            )
        )

    if not jobs and (case_ids or tags):
        given = ", ".join(
            (*(f"--case {case_id}" for case_id in case_ids), *(f"--tag {tag.value}" for tag in tags)),
        )
        raise RunnerError(
            f"{given} matched no case, so nothing would run and the result would be a green PASS that "
            f"checked nothing. The corpus holds {len(known)} case(s). Widen the selection, or drop the "
            f"filter that no case satisfies"
        )
    return tuple(jobs)


# ---------------------------------------------------------------------------
# The shipping assertions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Parse:
    """What reading the input did: the outcome, the document if there is one, and the error if not."""

    outcome: ParseOutcome
    document: Any = None
    error: str = ""


def _parse_input(job: CaseJob) -> _Parse:
    """Read the case input with the reader its extension dispatches to. A crash here is a finding."""
    import idfkit

    try:
        if job.input_file is InputFile.IDF:
            document = idfkit.parse_idf(job.input_path, encoding=IDF_ENCODING)
        else:
            document = idfkit.parse_epjson(job.input_path)
    except Exception as error:  # any failure to read is the observed outcome, not a crash
        return _Parse(ParseOutcome.FAILURE, None, f"{type(error).__name__}: {error}")
    return _Parse(ParseOutcome.SUCCESS, document)


def document_snapshot(document: Any) -> dict[str, Any]:
    """A JSON-safe view of a parsed document in which the field sequence is an array.

    Rule 4 of ``compare.md`` compares IDF field order in assertion 3 and nowhere else, and rule 7
    makes arrays ordered. Putting each object's fields in an array is therefore what makes the
    order observable to the comparator, including trailing unset fields and the boundaries of
    extensible groups: an extensible wrapper is a field whose value is a list, and that list stays
    ordered too.
    """
    snapshot: dict[str, Any] = {}
    for obj_type, collection in document.items():
        snapshot[obj_type] = [
            {
                "name": obj.name,
                "fields": [{"name": key, "value": _jsonable(value)} for key, value in obj.data.items()],
            }
            for obj in collection
        ]
    return snapshot


def _jsonable(value: Any) -> Any:
    """Pass a parsed field value through unchanged, or fail loudly on a type JSON cannot carry.

    Never coerce. A silent ``str()`` here would turn a library returning the wrong type into a
    passing case, which is the one outcome the suite exists to prevent.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    raise RunnerError(f"field value of type {type(value).__name__!r} is not a JSON value: {value!r}")


def _round_trip_snapshot(document: Any) -> dict[str, Any]:
    """Write the document as IDF, read it back, and snapshot the result.

    Rule 6: the IDF is written and read as latin-1, never as UTF-8 and never at a platform default.
    ``preserve_formatting=False`` is explicit because the lossless path echoes the source text back,
    which would make the assertion trivially true and test nothing.
    """
    import idfkit

    text = idfkit.write_idf(document, preserve_formatting=False)
    with tempfile.TemporaryDirectory(prefix="idfkit-conformance-") as scratch:
        path = Path(scratch) / "round-trip.idf"
        path.write_text(text, encoding=IDF_ENCODING)
        reparsed = idfkit.parse_idf(path, encoding=IDF_ENCODING)
    return document_snapshot(reparsed)


def run_case(job: CaseJob, limit: int) -> CaseOutcome:
    """Run every assertion the case declares, in :data:`ASSERTION_ORDER`, and never raise."""
    started = time.perf_counter()
    parse = _parse_input(job)
    outcomes: list[AssertionOutcome] = []
    for assertion in job.assertions:
        try:
            outcomes.append(_run_assertion(job, assertion, parse, limit))
        except Exception as error:  # a runner crash is reported against the case, not propagated
            outcomes.append(
                _errored(
                    job.case_id,
                    assertion,
                    f"the runner could not evaluate this assertion: {type(error).__name__}: {error}",
                )
            )
    return CaseOutcome(case_id=job.case_id, seconds=time.perf_counter() - started, outcomes=tuple(outcomes))


def _run_assertion(job: CaseJob, assertion: Assertion, parse: _Parse, limit: int) -> AssertionOutcome:
    """Dispatch one assertion. Assertion 4 is accepted and skipped."""
    if assertion is Assertion.DIAGNOSTICS:
        return AssertionOutcome(job.case_id, assertion, Status.SKIPPED, DIAGNOSTICS_DEFERRED)
    if assertion is Assertion.PARSE_OUTCOME:
        return _assert_parse_outcome(job, parse, limit)
    if parse.document is None:
        return _no_document(job, assertion, parse)
    if assertion is Assertion.EPJSON:
        return _assert_epjson(job, parse.document, limit)
    if assertion is Assertion.ROUND_TRIP:
        return _assert_round_trip(job, parse.document, limit)
    if assertion is Assertion.VALIDATION:
        return _assert_validation(job, parse.document, limit)
    if assertion is Assertion.INTROSPECTION:
        return _assert_introspection(job, parse.document, limit)
    if assertion is Assertion.DOCS_URL:
        return _assert_docs_url(job, parse.document, limit)
    if assertion is Assertion.TYPE_LOOKUP:
        return _assert_type_lookup(job, parse.document, limit)
    # A further assertion added to model.py without a runner change lands here. Saying so beats
    # falling through to whichever branch happened to be last.
    return _errored(job.case_id, assertion, f"this runner has no implementation for {assertion.value!r}")


def _errored(case_id: str, assertion: Assertion, reason: str) -> AssertionOutcome:
    """An assertion the runner could not evaluate. Always blocking, never allowlistable."""
    return AssertionOutcome(case_id, assertion, Status.ERRORED, reason, first_difference=reason)


def _head(case_id: str, assertion: Assertion, reason: str) -> str:
    """The first line of every report block, matching ``compare.AssertionReport``'s own head."""
    return f"{case_id}: {LIBRARY.value}: {assertion.value}: {reason}"


def _block(case_id: str, assertion: Assertion, reason: str, detail: Sequence[str] = ()) -> tuple[str, ...]:
    """A report block for an outcome no comparison produced: a head, then indented detail."""
    return (_head(case_id, assertion, reason), *(f"  {line}" for line in detail))


def _no_document(job: CaseJob, assertion: Assertion, parse: _Parse) -> AssertionOutcome:
    """What to say about assertion 2 or 3 when the input never parsed.

    A case that declares failure and fails has nothing left to compare, and saying so is honest. A
    case that declares success and failed has genuinely not satisfied the assertion, so it fails
    here as well as on assertion 1: the register keys on case, library and assertion, so each is
    accepted or blocked on its own terms.
    """
    if job.expected_parse_outcome is ParseOutcome.FAILURE:
        return AssertionOutcome(
            job.case_id,
            assertion,
            Status.SKIPPED,
            f"the input is declared to fail parsing, so there is no document to compare. "
            f"Drop this assertion from {job.case_id}/case.toml if it was not meant to apply",
        )
    reason = "the input did not parse, so the library produced nothing to compare"
    return AssertionOutcome(
        job.case_id,
        assertion,
        Status.FAILED,
        reason,
        _block(job.case_id, assertion, reason, (parse.error,)),
        parse.error,
    )


def _assert_parse_outcome(job: CaseJob, parse: _Parse, limit: int) -> AssertionOutcome:
    """Assertion 1: the observed outcome against the declared one."""
    comparison = compare_outcome(parse.outcome, job.expected_parse_outcome)
    extra = (f"parse error: {parse.error}",) if parse.error else ()
    return _from_comparison(job.case_id, Assertion.PARSE_OUTCOME, comparison, limit, extra)


def _assert_epjson(job: CaseJob, document: Any, limit: int) -> AssertionOutcome:
    """Assertion 2: canonical epJSON against the committed expectation.

    ``truth = convention`` forbids an expectation file, so a convention case declaring this
    assertion has nothing to be compared against. That is a corpus fault, reported as an error
    rather than passed over, because an assertion nobody can evaluate reads as a green tick.
    """
    if job.truth is not Truth.ORACLE:
        return _errored(
            job.case_id,
            Assertion.EPJSON,
            f"the case sits in the convention section, which forbids {EXPECTED_EPJSON}, so there is no "
            f"expectation to compare. Move the case to the oracle section or drop the 'epjson' assertion",
        )
    if not job.expected_epjson_path.is_file():
        return _errored(
            job.case_id,
            Assertion.EPJSON,
            f"{EXPECTED_EPJSON} is missing while truth = oracle. Generate it with tools/regenerate.sh and commit it",
        )

    import idfkit

    produced = json.loads(idfkit.write_epjson(document, preserve_formatting=False))
    expected = json.loads(job.expected_epjson_path.read_text(encoding="utf-8"))
    return _from_comparison(job.case_id, Assertion.EPJSON, compare_epjson(produced, expected), limit)


def _assert_round_trip(job: CaseJob, document: Any, limit: int) -> AssertionOutcome:
    """Assertion 3: re-parsing the library's own IDF output against the original document."""
    original = document_snapshot(document)
    reparsed = _round_trip_snapshot(document)
    return _from_comparison(job.case_id, Assertion.ROUND_TRIP, compare_documents(reparsed, original), limit)


def _missing_expectation(job: CaseJob, assertion: Assertion, path: Path) -> AssertionOutcome | None:
    """The error to report when a Tier 1 expectation file is absent, or ``None`` when it is there.

    The same shape :func:`_assert_epjson` uses for a missing ``expected.epJSON``: an assertion with
    nothing to compare against is a corpus fault, and reporting it as an error rather than passing
    over it is what stops an assertion nobody can evaluate from reading as a green tick. The advice
    differs because a Tier 1 expectation has no oracle to regenerate it from.
    """
    if path.is_file():
        return None
    return _errored(
        job.case_id,
        assertion,
        f"{path.name} is missing while the case declares the {assertion.value!r} assertion. Draft it with "
        f"tools/seed_tier1.py, read the draft against the rule that governs it, and commit it",
    )


def _validation_findings(document: Any) -> list[dict[str, Any]]:
    """Every finding the library reports for the document, in the corpus vocabulary.

    ``validate_document`` is called with every default in place, so what the corpus records is what
    an ordinary caller gets rather than a configuration only the suite asks for. The three severity
    arrays are concatenated into one list because rule 7 compares them as a single unordered
    multiset: neither library promises an order within an array, and a library that visited object
    types in a different order would otherwise fail every validation case for a property nobody
    claims.

    ``message`` is never serialized, for the reason assertion 4 gives about diagnostics: wording is
    a presentation choice each library is free to improve. The two are already known to differ,
    because one runtime distinguishes an int from a float and the other has no way to, and both
    renderings are correct.
    """
    from idfkit.validation import validate_document

    result = validate_document(document)
    return [
        {
            "object_type": issue.obj_type,
            "object_name": issue.obj_name,
            "field": issue.field,
            "code": issue.code,
            "severity": issue.severity.value,
        }
        for issue in (*result.errors, *result.warnings, *result.info)
    ]


def _introspection_snapshot(document: Any) -> dict[str, Any]:
    """One type description per object type the document holds, keyed by type name.

    The document decides which types are described, so a case pins exactly the types its input
    names and adding an object type to the input is what widens the assertion. Field entries stay
    in schema order, which rule 7 compares index by index: the order is the order a caller is shown
    the fields in, and a library that reordered them would be showing a different type.
    """
    from idfkit.introspection import describe_object_type

    described: dict[str, Any] = {}
    for obj_type in sorted(document.collections):
        description = describe_object_type(document.schema, obj_type)
        described[obj_type] = {
            "obj_type": description.obj_type,
            "memo": _jsonable(description.memo),
            "has_name": description.has_name,
            "is_extensible": description.is_extensible,
            "extensible_size": _jsonable(description.extensible_size),
            "required_fields": list(description.required_fields),
            "fields": [
                {key: _jsonable(getattr(described_field, key)) for key in INTROSPECTION_FIELD_KEYS}
                for described_field in description.fields
            ],
        }
    return {"object_types": described}


def _docs_url_snapshot(document: Any) -> dict[str, Any]:
    """The best documentation address per object type the document holds, or ``null`` for none.

    The version segment comes from the document's own version and never from a constant, so a
    library that hardcoded a release fails here the next time one ships. ``None`` is written as
    JSON ``null`` rather than omitted, because rule 3 makes absent and ``null`` different: a type
    the library has no address for is a fact the corpus records, and a type it dropped entirely is
    a difference.
    """
    from idfkit.docs import docs_url_for_object

    addresses: dict[str, Any] = {}
    for obj_type in sorted(document.collections):
        address = docs_url_for_object(obj_type, document.version, document.schema)
        addresses[obj_type] = (
            None
            if address is None
            else {"url": address.url, "doc_set": address.doc_set, "version": address.version, "label": address.label}
        )
    return {"object_types": addresses}


def _type_lookup_snapshot(document: Any, queries: Mapping[str, Any]) -> dict[str, Any]:
    """What the library returns when a caller names an object type, and what that read left behind.

    The queried names are the keys of the expectation's ``lookups`` object, so the case file alone
    decides which spellings are exercised and the runner never invents one. Each query records the
    object names the collection accessor returned, in order, and what the membership test said, so
    that a library whose ``in`` disagrees with its ``[]`` fails here rather than agreeing by
    halves.

    ``object_types_after`` is taken after every query has run, and it is what makes a read that
    mutates the document a finding. Both libraries used to file an empty collection under whatever
    name was asked for, so probing three misspellings left three keys behind, visible to every
    later iteration over the document. It is sorted because the order of a document's type list is
    already compared by assertions 2 and 3; what this assertion adds is the *set*.
    """
    lookups: dict[str, Any] = {}
    for written in queries:
        collection = document[written]
        lookups[written] = {
            "names": [obj.name for obj in collection],
            "present": written in document,
        }
    return {"lookups": lookups, "object_types_after": sorted(document.collections)}


def _assert_validation(job: CaseJob, document: Any, limit: int) -> AssertionOutcome:
    """Assertion 5: validation findings against ``expected.validation.json``, as a multiset."""
    path = job.case_dir / EXPECTED_VALIDATION
    missing = _missing_expectation(job, Assertion.VALIDATION, path)
    if missing is not None:
        return missing

    expected = json.loads(path.read_text(encoding="utf-8"))
    findings = _validation_findings(document)
    comparison = compare_unordered(findings, expected["findings"], path=json_pointer("findings"))
    return _from_comparison(job.case_id, Assertion.VALIDATION, comparison, limit)


def _assert_introspection(job: CaseJob, document: Any, limit: int) -> AssertionOutcome:
    """Assertion 6: type descriptions against ``expected.introspection.json``."""
    path = job.case_dir / EXPECTED_INTROSPECTION
    missing = _missing_expectation(job, Assertion.INTROSPECTION, path)
    if missing is not None:
        return missing

    expected = json.loads(path.read_text(encoding="utf-8"))
    comparison = compare_values(_introspection_snapshot(document), expected)
    return _from_comparison(job.case_id, Assertion.INTROSPECTION, comparison, limit)


def _assert_docs_url(job: CaseJob, document: Any, limit: int) -> AssertionOutcome:
    """Assertion 7: documentation addresses against ``expected.docs-url.json``."""
    path = job.case_dir / EXPECTED_DOCS_URL
    missing = _missing_expectation(job, Assertion.DOCS_URL, path)
    if missing is not None:
        return missing

    expected = json.loads(path.read_text(encoding="utf-8"))
    comparison = compare_values(_docs_url_snapshot(document), expected)
    return _from_comparison(job.case_id, Assertion.DOCS_URL, comparison, limit)


def _assert_type_lookup(job: CaseJob, document: Any, limit: int) -> AssertionOutcome:
    """Assertion 8: collection lookup by type name against ``expected.type-lookup.json``."""
    path = job.case_dir / EXPECTED_TYPE_LOOKUP
    missing = _missing_expectation(job, Assertion.TYPE_LOOKUP, path)
    if missing is not None:
        return missing

    expected = json.loads(path.read_text(encoding="utf-8"))
    comparison = compare_values(_type_lookup_snapshot(document, expected["lookups"]), expected)
    return _from_comparison(job.case_id, Assertion.TYPE_LOOKUP, comparison, limit)


def _from_comparison(
    case_id: str,
    assertion: Assertion,
    comparison: Comparison,
    limit: int,
    extra: tuple[str, ...] = (),
) -> AssertionOutcome:
    """Turn a comparison into an outcome, rendered eagerly through ``compare.py``'s own layout.

    :class:`compare.AssertionReport` already prints the exit contract: the case id, the library,
    the assertion, the differing value, and the path to both sides. The runner takes that block
    verbatim rather than composing a second one, so the two can never drift apart.
    """
    report = AssertionReport(case_id=case_id, library=LIBRARY, assertion=assertion, comparison=comparison)
    if report.passed:
        return AssertionOutcome(case_id, assertion, Status.PASSED)
    first = comparison.first
    return AssertionOutcome(
        case_id,
        assertion,
        Status.FAILED,
        f"{comparison.count} difference(s), left is {LIBRARY.value}",
        (*report.render(limit=limit or None), *(f"  {line}" for line in extra)),
        first.render() if first is not None else "",
    )


# ---------------------------------------------------------------------------
# Execution, serial or pooled
# ---------------------------------------------------------------------------

_WORKER_LIBRARY: LibraryUnderTest | None = None
_WORKER_LIMIT: int = DEFAULT_DIFFERENCE_LIMIT


def _worker_init(library_root: str, limit: int) -> None:
    """Import the library once per worker process. Schema loading is cached per process."""
    global _WORKER_LIBRARY, _WORKER_LIMIT  # a pool initializer has no other channel into the worker
    _WORKER_LIBRARY = import_library(Path(library_root))
    _WORKER_LIMIT = limit


def _worker_run(job: CaseJob) -> CaseOutcome:
    """The pool entry point. Module level so it pickles."""
    return run_case(job, _WORKER_LIMIT)


def _worker_count(requested: int, case_count: int) -> int:
    """How many processes to use. Zero means decide from the corpus size."""
    if requested > 0:
        return min(requested, max(case_count, 1))
    if case_count < POOL_THRESHOLD:
        return 1
    return max(1, min(os.cpu_count() or 1, MAX_POOL_WORKERS, case_count))


def execute(jobs: Sequence[CaseJob], library: LibraryUnderTest, limit: int, jobs_requested: int) -> list[CaseOutcome]:
    """Run every job, in manifest order in the report whatever order they finish in."""
    workers = _worker_count(jobs_requested, len(jobs))
    if workers <= 1 or len(jobs) <= 1:
        return [run_case(job, limit) for job in jobs]

    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_worker_init,
        initargs=(str(library.root), limit),
    ) as pool:
        results = list(pool.map(_worker_run, jobs))
    return results


# ---------------------------------------------------------------------------
# The exception register: outstanding, stale, and unexercisable entries
# ---------------------------------------------------------------------------


def reconcile(report: RunReport, divergences: Sequence[Divergence], selected: Iterable[str]) -> None:
    """Sort every failure and every register entry into the buckets the exit contract distinguishes.

    An outstanding exception is reported and does not block. A stale one blocks. An entry naming an
    assertion its case never declares can never be exercised, so it blocks too: it looks like
    coverage and is not.
    """
    selected_ids = set(selected)
    by_case = {case.case_id: case for case in report.cases}
    accepted: set[tuple[str, Assertion]] = set()

    for divergence in divergences:
        if divergence.case not in selected_ids:
            report.unselected_exceptions += 1
            continue
        case = by_case.get(divergence.case)
        outcome = case.outcome_for(divergence.assertion) if case else None
        if outcome is None:
            report.unexercisable.append(
                (
                    divergence,
                    f"case {divergence.case!r} does not declare the {divergence.assertion.value!r} assertion, "
                    f"so this entry can never be exercised",
                )
            )
            continue
        if outcome.status is Status.SKIPPED:
            report.deferred_exceptions.append(divergence)
            continue
        if outcome.status is Status.PASSED:
            report.stale.append(divergence)
            continue
        if outcome.status is Status.FAILED:
            accepted.add((divergence.case, divergence.assertion))
            report.outstanding.append((divergence, outcome))

    for case in report.cases:
        for outcome in case.outcomes:
            if outcome.blocks and (outcome.case_id, outcome.assertion) not in accepted:
                report.blocking.append(outcome)


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------


def _print_block(title: str, body: Iterable[str]) -> None:
    lines = list(body)
    if not lines:
        return
    print()
    print(title)
    for line in lines:
        print(f"  {line}")


def print_report(report: RunReport) -> None:
    """Print the whole run, in the order a reader needs it: what ran, what broke, what is accepted."""
    print("idfkit conformance runner")
    print(f"  corpus     {report.corpus_root}")
    print(f"  level      {report.level.describe()}")
    print(f"  library    {report.library.describe()}")
    print(f"  register   {report.register_note}")
    print(f"  selection  {report.selected} of {report.total} case(s)")

    if report.total == 0:
        print()
        print(f"no cases in the corpus: {report.corpus_root / CASES_DIR} holds none yet, so there is nothing to check")
    elif report.selected == 0:
        print()
        print("no case matched the selection, so nothing ran")

    _print_block(
        "failures not in known-divergence.toml (exit 1)",
        (line for outcome in report.blocking if outcome.status is Status.FAILED for line in outcome.render()),
    )
    # An errored assertion is a corpus or environment problem, not a library disagreement, so it is
    # never allowlistable and is kept out of the failure block where a reader would look for one.
    _print_block(
        "assertions the runner could not evaluate (exit 1)",
        (line for outcome in report.blocking if outcome.status is Status.ERRORED for line in outcome.render()),
    )
    _print_block(
        "stale exceptions: these cases now pass, so remove their entries (exit 1)",
        (
            line
            for divergence in report.stale
            for line in (
                f"{divergence.case}: {divergence.library.value}: {divergence.assertion.value} now passes",
                f"    remove this entry from {DIVERGENCE_FILE}. A stale exception is removed by the change "
                f"that fixes the bug",
                f"    issue: {divergence.issue}",
            )
        ),
    )
    _print_block(
        "exceptions that can never be exercised, so remove them (exit 1)",
        (
            line
            for divergence, why in report.unexercisable
            for line in (f"{divergence.case}: {divergence.assertion.value}: {why}", f"    issue: {divergence.issue}")
        ),
    )
    # T038: outstanding exceptions are normal output, never behind a flag. A silent allowlist is how
    # a temporary exception becomes permanent.
    _print_block(
        "outstanding exceptions: accepted, tracked, still failing",
        (
            line
            for divergence, outcome in report.outstanding
            for line in (
                f"{divergence.case}: {divergence.library.value}: {divergence.assertion.value}",
                f"    issue:    {divergence.issue}",
                f"    observed: {divergence.observed}",
                f"    expected: {divergence.expected}",
                *((f"    now:      {outcome.first_difference}",) if outcome.first_difference else ()),
            )
        ),
    )
    _print_block(
        "exceptions not evaluated: their assertion is skipped",
        (
            f"{divergence.case}: {divergence.assertion.value}: {divergence.issue}"
            for divergence in report.deferred_exceptions
        ),
    )
    _print_block(
        "skipped assertions",
        (
            f"{outcome.case_id}: {outcome.assertion.value}: {outcome.reason}"
            for case in report.cases
            for outcome in case.outcomes
            if outcome.status is Status.SKIPPED
        ),
    )

    if report.unselected_exceptions:
        print()
        print(
            f"{report.unselected_exceptions} exception(s) were not evaluated because --case or --tag "
            f"excluded their case. Run without a filter before trusting a green result"
        )

    counts = report.counts()
    print()
    print(
        f"{report.selected} case(s), {report.assertion_count} assertion(s): "
        f"{counts[Status.PASSED]} passed, {counts[Status.FAILED]} failed, "
        f"{counts[Status.ERRORED]} errored, {counts[Status.SKIPPED]} skipped; "
        f"{len(report.outstanding)} outstanding exception(s); "
        f"level {report.level.value or 'unpinned'}; {report.seconds:.2f}s"
    )
    print("PASS" if report.green else "FAIL")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The CLI. ``--library`` takes a path, never a language word: the runner file fixes the language."""
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Run the idfkit conformance corpus against a checkout of the Python library.",
        epilog=(
            "Exit 0 when every case passes or every failure is listed in known-divergence.toml, "
            "1 for a blocking failure, a stale exception, or a corpus fault, and 2 when the run "
            "could not start."
        ),
    )
    parser.add_argument(
        "--library",
        required=True,
        type=Path,
        metavar="PATH",
        help="path to the idfkit checkout to test, for example ../idfkit",
    )
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        metavar="ID",
        help="run only this case. Repeatable",
    )
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        metavar="TAG",
        help=f"run only cases carrying this hazard tag ({', '.join(t.value for t in Tag)}). Repeatable",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=REPO_ROOT,
        metavar="PATH",
        help="root of the conformance corpus (default: the repository this runner lives in)",
    )
    parser.add_argument(
        "--level",
        metavar="conformance-YYYY.N",
        help="report against this level instead of the repository's git tag",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=0,
        metavar="N",
        help="worker processes. 0 decides from the corpus size (default)",
    )
    parser.add_argument(
        "--max-differences",
        type=int,
        default=DEFAULT_DIFFERENCE_LIMIT,
        metavar="N",
        help=f"differences printed per failing assertion, 0 for all (default: {DEFAULT_DIFFERENCE_LIMIT}). "
        f"The total is always printed",
    )
    return parser


def _parse_tags(values: Sequence[str]) -> tuple[Tag, ...]:
    tags: list[Tag] = []
    for value in values:
        try:
            tags.append(Tag(value))
        except ValueError as error:
            raise RunnerError(f"--tag {value!r} is not in the taxonomy: {', '.join(t.value for t in Tag)}") from error
    return tuple(tags)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the corpus and return the exit code. Never raises out of a corpus or library problem."""
    args = build_parser().parse_args(argv)
    started = time.perf_counter()

    try:
        tags = _parse_tags(args.tag)
        library = import_library(args.library)
    except RunnerError as error:
        print(f"run.py: {error}", file=sys.stderr)
        return EXIT_UNUSABLE

    corpus_root = args.corpus.expanduser().resolve()

    # Preflight, so the exit contract's dedicated row for a missing expectation prints the case id
    # rather than a loader traceback.
    try:
        manifest = load_manifest(corpus_root / MANIFEST_FILE)
    except CorpusError as error:
        print(f"run.py: {error}", file=sys.stderr)
        return EXIT_FAILED

    missing = [
        entry.id
        for entry in manifest.entries()
        if entry.truth is Truth.ORACLE and not (corpus_root / CASES_DIR / entry.id / EXPECTED_EPJSON).is_file()
    ]
    if missing:
        for case_id in missing:
            print(
                f"run.py: case {case_id}: {EXPECTED_EPJSON} is missing while truth = oracle. "
                f"Generate it with tools/regenerate.sh and commit it",
                file=sys.stderr,
            )
        return EXIT_FAILED

    register_path = corpus_root / DIVERGENCE_FILE
    try:
        corpus = load_corpus(corpus_root, require_divergences=False)
    except CorpusError as error:
        print(f"run.py: {error}", file=sys.stderr)
        return EXIT_FAILED

    try:
        level = detect_level(corpus_root, manifest, args.level)
        jobs = build_jobs(corpus, args.case, tags)
    except RunnerError as error:
        print(f"run.py: {error}", file=sys.stderr)
        return EXIT_UNUSABLE

    register_note = (
        f"{len(corpus.divergences.entries)} entry(ies) in {DIVERGENCE_FILE}"
        if register_path.is_file()
        else f"{DIVERGENCE_FILE} is absent, so no failure is accepted"
    )
    report = RunReport(
        library=library,
        level=level,
        corpus_root=corpus_root,
        register_note=register_note,
        selected=len(jobs),
        total=len(corpus.manifest.oracle) + len(corpus.manifest.convention),
    )
    limit = max(args.max_differences, 0)
    report.cases = execute(jobs, library, limit, args.jobs)
    reconcile(report, corpus.divergences.for_library(LIBRARY), (job.case_id for job in jobs))
    report.seconds = time.perf_counter() - started

    print_report(report)
    return EXIT_OK if report.green else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
