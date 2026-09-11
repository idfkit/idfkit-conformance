"""Validate the consumer register before a governance tag is cut.

`governance/consumers.toml` is read by every consumer's self-check, by the sweep, by the rehearsal,
and by both libraries' downstream dispatch. A malformed entry reaches all of them at once through an
immutable tag, so this check is the only thing permitted to declare the file well formed
(contracts/consumer-register.md, "Gates"), and it runs on every pull request that touches it.

It enforces the ten invariants in specs/004-integrate-unified-releases/data-model.md, numbered as
they are there, and the rules the invariants imply for the fields feature 004 added while building
the register (the `package` and `via` of a delivery path, `out_of_scope`, and the formatting answer
feature 006 left waiting). Every finding names its rule, so a reader can find the reason.

Like `validate_governance.py`, it never imports either library and reads nothing outside this
repository: it answers whether the record is well formed, never whether a consumer matches it.
That second question is `check_consumer_self.py`, run in the consumer.

Run it with no arguments from the repository root. Exit 0 when every rule holds, 1 otherwise.
"""

from __future__ import annotations

import re
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from consumers import (  # noqa: E402
    DELIVERED_PACKAGES,
    ENTRY_POINTS,
    FORMATTING_ANSWERS,
    FORMS,
    GOVERNED_PACKAGES,
    INSTALLS,
    LAG_KINDS,
    LIBRARIES,
    MEANS,
    OUT_OF_SCOPE_PACKAGES,
    ROLES,
    SURFACE_STATUSES,
)

ROOT = Path(__file__).resolve().parent.parent

#: Invariant 6. A reason that explains a lag by the door a consumer came through is presenting a
#: supported entry point as a deficiency, which FR-037 forbids however it is phrased.
_CITES_ENTRY_POINT = re.compile(r"entry[\s_-]?point|scoped|shared[\s_-]?name|@idfkit/", re.IGNORECASE)
#: The keys an out-of-scope entry may carry. Anything else is a gate being configured on it.
_OUT_OF_SCOPE_KEYS = frozenset({"package", "path", "locator", "form", "note"})
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class Finding:
    """One rule violation, named by the entry it was found in."""

    rule: str
    where: str
    message: str

    def render(self) -> str:
        return f"  [{self.rule}] {self.where}: {self.message}"


def _is_url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def check_register(data: Mapping[str, Any], parity_ids: set[str]) -> list[Finding]:
    """Every rule, over the parsed file. *parity_ids* is `parity.toml` at the same commit."""
    findings: list[Finding] = []
    header = data.get("register")
    if not isinstance(header, Mapping):
        return [Finding("register-header", "[register]", "the file has no [register] table.")]
    libraries = header.get("libraries")
    if not isinstance(libraries, Mapping) or set(libraries) != LIBRARIES or not all(
        isinstance(v, str) and _REPOSITORY.match(v) for v in libraries.values()
    ):
        findings.append(
            Finding("register-header", "[register]", "libraries must map python and javascript to owner/name.")
        )

    consumers = [c for c in data.get("consumer", []) if isinstance(c, Mapping)]
    ids = [c.get("id") for c in consumers]

    # Invariant 1: every id unique, every depends_on resolves.
    seen: set[str] = set()
    for index, consumer_id in enumerate(ids):
        if not _text(consumer_id):
            findings.append(Finding("unique-id", f"consumer {index}", "no id."))
        elif consumer_id in seen:
            findings.append(Finding("unique-id", f"consumer {consumer_id!r}", "id appears more than once."))
        else:
            seen.add(consumer_id)

    graph: dict[str, list[str]] = {}
    for consumer in consumers:
        where = f"consumer {consumer.get('id')!r}"
        depends_on = consumer.get("depends_on", [])
        if not isinstance(depends_on, list):
            findings.append(Finding("depends-on", where, "depends_on must be a list of consumer ids."))
            depends_on = []
        for target in depends_on:
            if target not in seen:
                findings.append(Finding("depends-on", where, f"depends_on {target!r} is not a consumer id."))
        if _text(consumer.get("id")):
            graph[consumer["id"]] = [t for t in depends_on if t in seen]

    # Invariant 2: acyclic. A cycle has no wave order and would deadlock adoption.
    findings += _cycles(graph)

    for consumer in consumers:
        findings += _check_consumer(consumer, seen, parity_ids)

    findings += _check_surfaces(data.get("surface", []), seen, parity_ids)
    return findings


def _cycles(graph: Mapping[str, Sequence[str]]) -> list[Finding]:
    state: dict[str, int] = {}
    found: list[Finding] = []

    def visit(node: str, trail: list[str]) -> None:
        state[node] = 1
        for target in graph.get(node, ()):
            if state.get(target) == 1:
                cycle = trail[trail.index(target) :] + [target]
                found.append(Finding("acyclic", f"consumer {node!r}", "depends_on cycle: " + " -> ".join(cycle)))
            elif state.get(target) is None:
                visit(target, [*trail, target])
        state[node] = 2

    for node in graph:
        if node not in state:
            visit(node, [node])
    return found


def _check_consumer(consumer: Mapping[str, Any], ids: set[str], parity_ids: set[str]) -> list[Finding]:
    findings: list[Finding] = []
    where = f"consumer {consumer.get('id')!r}"
    role = consumer.get("role")
    depends_on = consumer.get("depends_on", []) if isinstance(consumer.get("depends_on"), list) else []

    if not isinstance(consumer.get("repository"), str) or not _REPOSITORY.match(consumer["repository"]):
        findings.append(Finding("repository", where, "repository must be owner/name."))
    if role not in ROLES:
        findings.append(Finding("role", where, f"role {role!r} is not one of builds, delivers, teaches."))

    resolutions = [r for r in consumer.get("libraries", []) if isinstance(r, Mapping)]
    if role != "teaches" and not resolutions:
        findings.append(Finding("libraries", where, "only a consumer that teaches may resolve no library."))
    libraries_seen: set[str] = set()
    for resolution in resolutions:
        library = resolution.get("library")
        rwhere = f"{where} library {library!r}"
        if library not in LIBRARIES:
            findings.append(Finding("library", rwhere, "library must be python or javascript, never a package name."))
            continue
        if library in libraries_seen:
            findings.append(Finding("library", rwhere, "one Resolution per library per consumer."))
        libraries_seen.add(library)
        if resolution.get("entry_point") not in ENTRY_POINTS[library]:
            findings.append(
                Finding("entry-point", rwhere, f"entry_point must be one of {sorted(ENTRY_POINTS[library])}.")
            )
        findings += _check_resolution(resolution, rwhere, library, role, depends_on, ids, parity_ids)

    findings += _check_rehearsals(consumer, where, role, libraries_seen, depends_on)
    findings += _check_out_of_scope(consumer, where)
    findings += _check_formatting(consumer, where, depends_on)
    return findings


def _check_resolution(
    resolution: Mapping[str, Any],
    where: str,
    library: str,
    role: Any,
    depends_on: Sequence[str],
    ids: set[str],
    parity_ids: set[str],
) -> list[Finding]:
    findings: list[Finding] = []
    means = resolution.get("means")
    declared_at = resolution.get("declared_at", [])
    if means not in MEANS:
        findings.append(Finding("means", where, f"means {means!r} is not a recognised means of resolution."))

    # Invariant 3: through-consumer has nothing to pin and takes its level from a provider.
    if means == "through-consumer":
        if declared_at:
            findings.append(Finding("through-consumer", where, "through-consumer declares no declared_at."))
        if not depends_on:
            findings.append(Finding("through-consumer", where, "through-consumer needs a non-empty depends_on."))
    # Invariant 4: every other means points at where its level is written.
    elif means in MEANS and not declared_at:
        findings.append(Finding("declared-at", where, f"means {means!r} must name at least one Declaration."))

    for index, declaration in enumerate(declared_at if isinstance(declared_at, list) else []):
        dwhere = f"{where} declared_at {index}"
        if not isinstance(declaration, Mapping):
            findings.append(Finding("declared-at", dwhere, "a Declaration is a table."))
            continue
        for key in ("path", "locator"):
            if not _text(declaration.get(key)):
                findings.append(Finding("declared-at", dwhere, f"no {key}."))
        if declaration.get("form") not in FORMS:
            findings.append(Finding("declared-at", dwhere, "form must be exact or range."))
        package = declaration.get("package")
        if package is not None:
            if package in OUT_OF_SCOPE_PACKAGES:
                findings.append(
                    Finding(
                        "out-of-scope",
                        dwhere,
                        f"{package} is outside the unification (FR-041). Record it under out_of_scope, "
                        "where no gate reads it, not as a Declaration every gate follows.",
                    )
                )
            elif package not in GOVERNED_PACKAGES[library] | set(DELIVERED_PACKAGES):
                findings.append(Finding("declared-at", dwhere, f"package {package!r} is neither governed nor delivered."))
        via = declaration.get("via")
        if via is not None and (via not in ids or via not in depends_on):
            findings.append(Finding("via", dwhere, f"via {via!r} must be a consumer this one depends_on (FR-018)."))
        if package in DELIVERED_PACKAGES and via is None:
            findings.append(
                Finding("via", dwhere, f"a delivery path pinning {package} names the consumer whose level it delivers.")
            )
        # A delivery path with no exact level is FR-015's gap. It may exist, but not unstated.
        if role == "delivers" and declaration.get("form") == "range" and not resolution.get("lag"):
            findings.append(
                Finding(
                    "delivery-exact",
                    dwhere,
                    "a delivery path declares a range, so what it delivers changes with time. Pin it, "
                    "or state why not in a lag (FR-015).",
                )
            )

    lag = resolution.get("lag")
    if lag is not None:
        findings += _check_lag(lag, f"{where} lag", parity_ids)
    return findings


def _check_lag(lag: Any, where: str, parity_ids: set[str]) -> list[Finding]:
    if not isinstance(lag, Mapping):
        return [Finding("lag", where, "a lag is a table.")]
    findings: list[Finding] = []
    kind = lag.get("kind")
    # Invariant 5: the two kinds carry different evidence, and never both.
    if kind not in LAG_KINDS:
        findings.append(Finding("lag", where, "kind must be not-yet or deliberate."))
    elif kind == "not-yet":
        if not _is_url(lag.get("issue")):
            findings.append(Finding("lag", where, "a not-yet lag names its tracked item as a real URL."))
        if lag.get("reason"):
            findings.append(Finding("lag", where, "a not-yet lag carries an issue, not a reason."))
    elif kind == "deliberate":
        if not _text(lag.get("reason")):
            findings.append(Finding("lag", where, "a deliberate lag carries a reason stating a policy."))
        if lag.get("issue"):
            findings.append(Finding("lag", where, "a deliberate lag carries a reason, not an issue."))
    # Invariant 6: an entry point is never a lag.
    reason = lag.get("reason") or ""
    if _CITES_ENTRY_POINT.search(reason):
        findings.append(
            Finding("entry-point-lag", where, "the reason cites an entry point, and an entry point is never a lag (FR-037).")
        )
    # R2: a reason that names a version goes stale on the next release.
    if re.search(r"\b\d+\.\d+(?:\.\d+)?", reason):
        findings.append(Finding("lag", where, "a reason states a policy, never a version (R2)."))
    # Invariant 7.
    if lag.get("parity_id") is not None and lag["parity_id"] not in parity_ids:
        findings.append(Finding("parity-id", where, f"parity_id {lag['parity_id']!r} is not in parity.toml."))
    return findings


def _check_rehearsals(
    consumer: Mapping[str, Any], where: str, role: Any, libraries: set[str], depends_on: Sequence[str]
) -> list[Finding]:
    findings: list[Finding] = []
    rehearsals = consumer.get("rehearsal", [])
    if not isinstance(rehearsals, list):
        return [Finding("rehearsal", where, "rehearsal is an array of tables, one per library.")]
    rehearsed = {r.get("library") for r in rehearsals if isinstance(r, Mapping)}
    # Invariant 8: every consumer that is not only teaching can be rehearsed, in every library.
    if role != "teaches":
        for library in sorted(libraries - rehearsed):
            findings.append(Finding("rehearsal", where, f"no rehearsal for {library}."))
    for rehearsal in rehearsals:
        if not isinstance(rehearsal, Mapping):
            continue
        rwhere = f"{where} rehearsal {rehearsal.get('library')!r}"
        if rehearsal.get("library") not in libraries:
            findings.append(Finding("rehearsal", rwhere, "rehearses a library this consumer does not resolve."))
        install = rehearsal.get("install")
        if install not in INSTALLS:
            findings.append(Finding("rehearsal", rwhere, f"install must be one of {sorted(INSTALLS)}."))
        elif install == "through-provider":
            if not depends_on:
                findings.append(Finding("rehearsal", rwhere, "through-provider needs a provider to rehearse through."))
            if rehearsal.get("typecheck") or rehearsal.get("test"):
                findings.append(
                    Finding("rehearsal", rwhere, "through-provider runs its providers' commands and names none of its own.")
                )
            continue
        elif (install == "uv-wheel") != (rehearsal.get("library") == "python"):
            findings.append(Finding("rehearsal", rwhere, "uv-wheel rehearses python, npm-tarball javascript."))
        if not _text(rehearsal.get("typecheck")):
            findings.append(Finding("rehearsal", rwhere, "every rehearsal names a typecheck, and it runs first (R4)."))
        if not _text(rehearsal.get("test")) and not _text(rehearsal.get("no_test_reason")):
            findings.append(Finding("rehearsal", rwhere, "name a test command, or say in no_test_reason why there is none."))
        prose = rehearsal.get("prose_paths", [])
        if not isinstance(prose, list) or not all(_text(p) for p in prose):
            findings.append(Finding("rehearsal", rwhere, "prose_paths is a list of repository-relative paths."))
    return findings


def _check_out_of_scope(consumer: Mapping[str, Any], where: str) -> list[Finding]:
    """FR-041's second half: recorded, and acted on by no gate (T105)."""
    findings: list[Finding] = []
    for index, entry in enumerate(consumer.get("out_of_scope", [])):
        owhere = f"{where} out_of_scope {index}"
        if not isinstance(entry, Mapping):
            findings.append(Finding("out-of-scope", owhere, "an out_of_scope entry is a table."))
            continue
        if entry.get("package") not in OUT_OF_SCOPE_PACKAGES:
            findings.append(
                Finding(
                    "out-of-scope",
                    owhere,
                    f"{entry.get('package')!r} is not outside the unification. Listing a governed package "
                    "here would take it out of every gate.",
                )
            )
        extra = set(entry) - _OUT_OF_SCOPE_KEYS
        if extra:
            findings.append(
                Finding(
                    "out-of-scope",
                    owhere,
                    f"carries {sorted(extra)}, which configures a gate on a dependency no gate may act on.",
                )
            )
        for key in ("path", "locator", "note"):
            if not _text(entry.get(key)):
                findings.append(Finding("out-of-scope", owhere, f"no {key}."))
        if entry.get("form") not in FORMS:
            findings.append(Finding("out-of-scope", owhere, "form must be exact or range."))
    return findings


def _check_formatting(consumer: Mapping[str, Any], where: str, depends_on: Sequence[str]) -> list[Finding]:
    """Feature 006's SC-009: a consumer that writes a model to disk says whether it preserves."""
    writes = consumer.get("writes_models")
    answer = consumer.get("preserves_formatting")
    if writes is None and answer is None:
        return []
    findings: list[Finding] = []
    if not isinstance(writes, bool):
        findings.append(Finding("formatting", where, "writes_models is true or false."))
    if answer not in FORMATTING_ANSWERS:
        findings.append(Finding("formatting", where, f"preserves_formatting must be one of {sorted(FORMATTING_ANSWERS)}."))
    elif writes is False and answer != "not-applicable":
        findings.append(Finding("formatting", where, "a consumer that writes no model answers not-applicable."))
    elif writes is True and answer == "not-applicable" and not _text(consumer.get("formatting_note")):
        findings.append(Finding("formatting", where, "a writer that answers not-applicable must say why."))
    if answer == "inherited" and not depends_on:
        findings.append(Finding("formatting", where, "inherited names nobody to inherit from without depends_on."))
    if not _text(consumer.get("formatting_note")):
        findings.append(Finding("formatting", where, "every answer carries a formatting_note saying what is done today."))
    return findings


def _check_surfaces(surfaces: Any, ids: set[str], parity_ids: set[str]) -> list[Finding]:
    findings: list[Finding] = []
    hosts: set[str] = set()
    for index, surface in enumerate(surfaces if isinstance(surfaces, list) else []):
        where = f"surface {surface.get('host', index)!r}" if isinstance(surface, Mapping) else f"surface {index}"
        if not isinstance(surface, Mapping) or not _text(surface.get("host")):
            findings.append(Finding("surface", where, "a surface names its host."))
            continue
        if surface["host"] in hosts:
            findings.append(Finding("surface", where, "host appears more than once."))
        hosts.add(surface["host"])
        status = surface.get("status")
        # Invariant 9: no third state, and nothing serving without saying what it describes.
        if status not in SURFACE_STATUSES:
            findings.append(Finding("surface", where, "status must be serving or retired."))
        elif status == "retired" and not _text(surface.get("redirects_to")):
            findings.append(Finding("surface", where, "a retired surface names the host it must redirect to."))
        elif status == "serving" and surface.get("states_level") is not True:
            findings.append(Finding("surface", where, "a serving surface must state the level it describes."))
        # Invariant 10: a published_by is a consumer, or else a repository.
        published_by = surface.get("published_by")
        if not _text(published_by) or (published_by not in ids and not _REPOSITORY.match(published_by)):
            findings.append(Finding("surface", where, "published_by must be a consumer id or an owner/name repository."))
        if surface.get("parity_id") is not None and surface["parity_id"] not in parity_ids:
            findings.append(Finding("parity-id", where, f"parity_id {surface['parity_id']!r} is not in parity.toml."))
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    register_path = Path(args[0]) if args else ROOT / "governance" / "consumers.toml"
    parity_path = Path(args[1]) if len(args) > 1 else ROOT / "governance" / "parity.toml"

    with register_path.open("rb") as handle:
        data = tomllib.load(handle)
    with parity_path.open("rb") as handle:
        parity_ids = {c["id"] for c in tomllib.load(handle).get("capability", []) if "id" in c}

    findings = check_register(data, parity_ids)
    print("idfkit-conformance consumer register validation")
    print(f"  consumers.toml  {len(data.get('consumer', []))} consumers, {len(data.get('surface', []))} surfaces")
    print()
    if not findings:
        print("PASSED: the consumer register is well formed.")
        return 0

    by_rule: dict[str, list[Finding]] = {}
    for finding in findings:
        by_rule.setdefault(finding.rule, []).append(finding)
    for rule in sorted(by_rule):
        print(f"{rule.upper()} ({len(by_rule[rule])})")
        for finding in by_rule[rule]:
            print(finding.render())
        print()
    print(f"FAILED: {len(findings)} finding(s) across {len(by_rule)} rule(s).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
