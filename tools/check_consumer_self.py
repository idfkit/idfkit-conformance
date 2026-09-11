"""One consumer's self-check: does this repository still match what the register says about it?

Run inside a consumer's checkout by `.github/workflows/check-consumer.yml`, which every consumer
calls at a pinned governance tag (research R11). It asserts the four rules in
contracts/consumer-register.md, "Gates":

  1. This repository appears in the roster.
  2. Every Declaration the roster names for it exists, and its locator resolves.
  3. Where a Resolution has more than one Declaration, they agree (FR-007).
  4. Its shape matches the roster: the same entry point, the same means, and every place this
     checkout declares a governed package is a place the roster points at (FR-006).

RULE 4 IS DELIBERATELY NOT ABOUT THE LEVEL. A bump changes nothing the register holds (R2), so
requiring a register change for one would be a rule nobody could satisfy. What must not drift
silently is shape: a level moving to a file the roster does not name, a consumer changing door,
a delivery path appearing where a manifest dependency used to be.

It does not compare against the current published level. A consumer is allowed to be behind and is
not allowed to be undescribed; the comparison is the sweep's.

Exit codes are distinguishable on purpose (T010): 0 matches, 1 does not match, 2 the register could
not be read at all. A 2 is never a pass and never falls back to another source.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from consumers import (  # noqa: E402
    GOVERNED_PACKAGES,
    OUT_OF_SCOPE_PACKAGES,
    Consumer,
    Detected,
    LocatorError,
    Register,
    agreement_key,
    detect,
    detected_entry_point,
    normalize,
    read_declaration,
)

EXIT_MISMATCH = 1
EXIT_UNREADABLE = 2

#: The file a detection was found in decides the means it evidences.
_MEANS_BY_FILE = {"pyproject.toml": "direct", "package.json": "direct", ".mcp.json": "runtime-fetch", ".lsp.json": "runtime-fetch"}


@dataclass(frozen=True, slots=True)
class Finding:
    rule: int
    message: str

    def render(self) -> str:
        return f"  rule {self.rule}: {self.message}"


def check_self(register: Register, repository: str, root: Path) -> list[Finding]:
    consumer = register.by_repository(repository)
    if consumer is None:
        return [
            Finding(
                1,
                f"{repository} is not in the consumer register. A project that resolves either library is a "
                "consumer; add it to governance/consumers.toml in idfkit-conformance.",
            )
        ]
    findings = _declarations(consumer, root)
    findings += _shape(consumer, detect(root))
    return findings


def _declarations(consumer: Consumer, root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for resolution in consumer.libraries:
        groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for declaration in resolution.declared_at:
            label = f"{declaration.path} at {declaration.locator}"
            try:
                observed = read_declaration(root, declaration, resolution.library)
            except (LocatorError, tomllib.TOMLDecodeError, ValueError) as error:
                findings.append(Finding(2, f"{resolution.library}: {label} does not resolve: {error}"))
                continue
            if declaration.form == "exact" and not observed.is_exact:
                findings.append(
                    Finding(2, f"{resolution.library}: {label} is recorded as exact and reads {observed.raw!r}.")
                )
            if observed.level is not None:
                groups[agreement_key(declaration, resolution.library)].append((label, normalize(observed.level)))
        for key, values in groups.items():
            if len({level for _, level in values}) > 1:
                listing = "; ".join(f"{label} = {level}" for label, level in values)
                findings.append(
                    Finding(3, f"{resolution.library}: the declarations of {key} disagree ({listing}) (FR-007).")
                )
    return findings


def _shape(consumer: Consumer, detections: list[Detected]) -> list[Finding]:
    findings: list[Finding] = []
    governed = [d for d in detections if d.package not in OUT_OF_SCOPE_PACKAGES]
    recorded_paths = {d.path for r in consumer.libraries for d in r.declared_at}
    recorded_paths |= {o.path for o in consumer.out_of_scope}

    # A manifest dependency is the evidence for `direct` and `runtime-fetch`. An image or a prose
    # resolution declares its level somewhere no manifest scan reads, and rule 2 has already
    # followed that declaration, so its absence from the scan is not evidence of anything.
    unscanned = {"through-consumer", "image", "prose"}
    if consumer.role != "teaches" and not governed and not any(r.means in unscanned for r in consumer.libraries):
        findings.append(
            Finding(4, "the register records this repository as a consumer, and it declares no dependency on either library.")
        )

    for detection in governed:
        if detection.path not in recorded_paths and detection.section != "peerDependencies":
            findings.append(
                Finding(
                    4,
                    f"{detection.path} declares {detection.package}, and the register points at no Declaration "
                    "in that file. A level moved, or a new one appeared, without the register following (FR-006).",
                )
            )

    for detection in detections:
        if detection.package in OUT_OF_SCOPE_PACKAGES and detection.path not in {o.path for o in consumer.out_of_scope}:
            findings.append(
                Finding(
                    4,
                    f"{detection.path} declares {detection.package}, which is outside the unification and must be "
                    "recorded under out_of_scope so a coordinated bump can see it (FR-041).",
                )
            )

    for resolution in consumer.libraries:
        paths = {d.path for d in resolution.declared_at}
        mine = [d for d in governed if d.path in paths]
        if resolution.library == "javascript":
            door = detected_entry_point([d for d in mine if d.package in GOVERNED_PACKAGES["javascript"]])
            if door is not None and door != resolution.entry_point:
                findings.append(
                    Finding(
                        4,
                        f"javascript: the register records entry point {resolution.entry_point!r} and this "
                        f"repository comes through {door!r}. Both are supported; the register must say which (FR-036).",
                    )
                )
        for detection in mine:
            evidenced = _MEANS_BY_FILE.get(detection.path.rsplit("/", 1)[-1])
            if evidenced and resolution.means not in {evidenced, "image"}:
                findings.append(
                    Finding(
                        4,
                        f"{resolution.library}: {detection.path} is a {evidenced} declaration and the register "
                        f"records means {resolution.means!r}.",
                    )
                )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--register", type=Path, required=True, help="consumers.toml, checked out at the pinned tag")
    parser.add_argument("--repository", required=True, help="owner/name of the repository being checked")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="the consumer's checkout")
    parser.add_argument("--governance-level", default="(unstated)", help="the tag the register was read at")
    args = parser.parse_args(argv)

    try:
        register = Register.load(args.register)
    except (OSError, tomllib.TOMLDecodeError, KeyError) as error:
        print(
            f"CANNOT READ THE REGISTER at {args.governance_level}: {error}\n"
            "This is not a pass and not a mismatch. Nothing was checked, and nothing falls back to another "
            "copy of the register (contracts/consumer-register.md, failure mode).",
            file=sys.stderr,
        )
        return EXIT_UNREADABLE

    findings = check_self(register, args.repository, args.root)
    print(f"consumer self-check: {args.repository} against governance/consumers.toml at {args.governance_level}")
    if not findings:
        print("PASSED: this repository matches its register entry.")
        return 0
    for finding in findings:
        print(finding.render())
    print(f"FAILED: {len(findings)} finding(s). Correct whichever is wrong, the register or this repository.")
    return EXIT_MISMATCH


if __name__ == "__main__":
    sys.exit(main())
