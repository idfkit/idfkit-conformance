"""Validate the governance artifacts and the exception register before a tag is cut.

`governance/README.md` step 2 of "Cutting a governance tag" says the repository's own CI runs
these checks on the pull request. Until feature 002 it did not: `.github/` held only CODEOWNERS,
so a malformed entry could reach an immutable tag unchallenged and turn both libraries red on
their next pin bump. This script is that check.

WHAT THIS IS NOT

It is not either library's gate. `idfkit/scripts/check_naming_register.py` and
`idfkit-js/scripts/check-naming-register.mjs` compare a library's actual public surface against
the register at a pinned tag; they answer "does the code match the record". This script never
imports either library. It answers the narrower question that has to be settled first: is the
record internally well formed, and does it say anything it contradicts elsewhere in itself.

Run it with no arguments from the repository root. Exit 0 when every rule holds, 1 otherwise.
"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

VALID_KINDS = frozenset({"aligned", "divergent", "excluded"})
REASON_REQUIRED_KINDS = frozenset({"divergent", "excluded"})
VALID_AVAILABILITY = frozenset({"complete", "partial", "absent"})
VALID_TIERS = frozenset({"tier-1", "tier-2", "tier-3", "never"})
VALID_ABSENCE_KINDS = frozenset({"not-yet", "never"})


@dataclass(frozen=True, slots=True)
class Finding:
    """One rule violation, named by the file and entry it was found in."""

    rule: str
    where: str
    message: str

    def render(self) -> str:
        return f"  [{self.rule}] {self.where}: {self.message}"


def _load(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def check_naming(register: dict) -> list[Finding]:
    """Concepts are unique, kinds are valid, and a stated divergence states a reason."""
    findings: list[Finding] = []
    seen: set[str] = set()

    for index, entry in enumerate(register.get("entry", [])):
        concept = entry.get("concept")
        where = f"naming.toml entry {index}" if not concept else f"naming.toml {concept!r}"

        if not concept:
            findings.append(Finding("required-field", where, "no concept."))
            continue
        if concept in seen:
            findings.append(Finding("unique-concept", where, "concept appears more than once."))
        seen.add(concept)

        for field in ("python", "typescript"):
            if field not in entry:
                findings.append(Finding("required-field", where, f"no {field}. The absent name is \"\", not a missing key."))

        kind = entry.get("kind")
        if kind not in VALID_KINDS:
            findings.append(Finding("kind", where, f"kind {kind!r} is not one of aligned, divergent, excluded."))
        elif kind in REASON_REQUIRED_KINDS and not (entry.get("divergence_reason") or "").strip():
            findings.append(Finding("divergence-reason", where, f"kind is {kind!r} but no divergence_reason is given."))

        # FR-006: excluded is terminal. A counterpart on the other side fails.
        if kind == "excluded":
            if entry.get("python") and entry.get("typescript"):
                findings.append(
                    Finding("excluded-counterpart", where, "an excluded entry names something on both sides.")
                )
            elif not entry.get("python") and not entry.get("typescript"):
                findings.append(Finding("excluded-counterpart", where, "an excluded entry names nothing on either side."))

        if kind == "aligned" and not (entry.get("python") and entry.get("typescript")):
            findings.append(Finding("aligned-both-sides", where, "an aligned entry must name a symbol on both sides."))

        counts = entry.get("rename_count")
        if not isinstance(counts, dict) or set(counts) != {"python", "typescript"}:
            findings.append(Finding("rename-count", where, "rename_count must be { python = <int>, typescript = <int> }."))
        elif not all(isinstance(value, int) for value in counts.values()):
            findings.append(Finding("rename-count", where, "rename_count values must be integers."))

    return findings


def check_parity(ledger: dict, concepts: set[str]) -> list[Finding]:
    """Availability, the fields each availability forces, and that every name resolves."""
    findings: list[Finding] = []
    seen: set[str] = set()

    for index, entry in enumerate(ledger.get("capability", [])):
        cap_id = entry.get("id")
        where = f"parity.toml entry {index}" if not cap_id else f"parity.toml {cap_id!r}"

        if not cap_id:
            findings.append(Finding("required-field", where, "no id."))
            continue
        if cap_id in seen:
            findings.append(Finding("unique-id", where, "id appears more than once."))
        seen.add(cap_id)

        if entry.get("tier") not in VALID_TIERS:
            findings.append(Finding("tier", where, f"tier {entry.get('tier')!r} is not a valid tier."))

        availabilities = {side: entry.get(side) for side in ("python", "typescript")}
        for side, value in availabilities.items():
            if value not in VALID_AVAILABILITY:
                findings.append(Finding("availability", where, f"{side} is {value!r}, not complete, partial or absent."))

        has_differences = bool((entry.get("differences") or "").strip())
        is_partial = "partial" in availabilities.values()

        # The rule that forces a closure to remove its differences text in the same edit.
        if is_partial and not has_differences:
            findings.append(Finding("differences", where, "a partial capability must say what differs."))
        if not is_partial and has_differences:
            findings.append(
                Finding(
                    "differences",
                    where,
                    "differences is set but neither side is partial. A closure removes the text in "
                    "the same edit that moves the state; a difference that outlives the closure "
                    "belongs on a page or in a comment.",
                )
            )

        if "absent" in availabilities.values():
            absence_kind = entry.get("absence_kind")
            if absence_kind not in VALID_ABSENCE_KINDS:
                findings.append(Finding("absence-kind", where, "an absent side requires absence_kind of not-yet or never."))
            elif absence_kind == "not-yet" and not str(entry.get("issue", "")).startswith(("http://", "https://")):
                findings.append(Finding("tracking-issue", where, "absence_kind is not-yet, so issue must be a real URL."))
            elif absence_kind == "never" and not (entry.get("note") or "").strip():
                findings.append(Finding("never-note", where, "absence_kind is never, so note must say why."))
        elif entry.get("absence_kind"):
            findings.append(Finding("absence-kind", where, "absence_kind is set but neither side is absent."))

        names = entry.get("names")
        if not isinstance(names, list):
            findings.append(Finding("names", where, "names must be a list of register concepts."))
            continue
        # An empty list is legitimate in exactly one place: a capability delivered by a package the
        # register does not govern. `browser-simulation` and `scene-rendering` are both `never` and
        # both say in a comment that registering a name would imply the shared install carries the
        # package, which is the claim those entries exist to deny. Anywhere else it is an omission.
        if not names and not (entry.get("tier") == "never" or entry.get("absence_kind") == "never"):
            findings.append(
                Finding("names", where, "names is empty but the capability is not terminally `never`.")
            )
            continue
        for name in names:
            if name not in concepts:
                findings.append(Finding("unresolved-name", where, f"names entry {name!r} is not a concept in naming.toml."))

    return findings


def check_exceptions(register: dict, case_ids: set[str]) -> list[Finding]:
    """Every accepted divergence points at a real case and a real issue."""
    findings: list[Finding] = []

    for index, entry in enumerate(register.get("divergence", [])):
        case = entry.get("case")
        where = f"known-divergence.toml entry {index}" if not case else f"known-divergence.toml {case!r}"

        if not case:
            findings.append(Finding("required-field", where, "no case."))
        elif case not in case_ids:
            findings.append(Finding("unknown-case", where, "names a case that does not exist in cases/."))

        if not str(entry.get("issue", "")).startswith(("http://", "https://")):
            findings.append(Finding("tracking-issue", where, "every accepted divergence carries a real tracking issue."))

        if entry.get("library") not in {"python", "typescript"}:
            findings.append(Finding("library", where, f"library {entry.get('library')!r} is not python or typescript."))

    return findings


def main() -> int:
    naming = _load(ROOT / "governance" / "naming.toml")
    parity = _load(ROOT / "governance" / "parity.toml")
    exceptions = _load(ROOT / "known-divergence.toml")

    concepts = {entry["concept"] for entry in naming.get("entry", []) if entry.get("concept")}
    case_ids = {path.name for path in (ROOT / "cases").iterdir() if path.is_dir()}

    findings = check_naming(naming) + check_parity(parity, concepts) + check_exceptions(exceptions, case_ids)

    print("idfkit-conformance governance validation")
    print(f"  naming.toml            {len(naming.get('entry', []))} entries")
    print(f"  parity.toml            {len(parity.get('capability', []))} capabilities")
    print(f"  known-divergence.toml  {len(exceptions.get('divergence', []))} accepted divergences")
    print(f"  cases/                 {len(case_ids)} cases")
    print()

    if not findings:
        print("PASSED: the governance artifacts are internally consistent.")
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
