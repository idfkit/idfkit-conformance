"""Record in the register what an adoption changed: a lag that closed, or a level declined.

Most adoptions change nothing here, because the register points at a level rather than holding one
(R2): a consumer that bumps from one version to the next touches no register file. Two outcomes do
change it, and FR-023 requires that they land with the adoption rather than afterwards:

    close-lag   the consumer reached the current level, so its `not-yet` lag has nothing left to say
    decline     the consumer will not take the level, and says why as a `deliberate` lag (FR-024)

The register lives in this repository and the adoption in the consumer's, so "the same change" is
realised as a paired pull request opened by the same run: `.github/workflows/record-adoption.yml`
is dispatched by the consumer's bump workflow and calls this.

EDITS ARE TEXT SURGERY, AND VERIFIED. There is no TOML writer in the standard library, and a
round-trip through one would destroy every comment in the file, which are most of its value. So this
edits the lines of one lag table in place, re-parses the result, and runs the schema check over it;
a result that does not pass is never written.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_consumers import check_register  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
_TABLE = re.compile(r"^\s*\[\[?(?P<name>[\w.]+)\]\]?\s*(?:#.*)?$")


class RecordError(ValueError):
    """The edit could not be made, or would have left the register malformed."""


def _resolution_span(lines: list[str], consumer_id: str, library: str) -> tuple[int, int]:
    """The line range of one consumer's Resolution for one library, header to last line."""
    in_consumer = False
    start: int | None = None
    for index, line in enumerate(lines):
        table = _TABLE.match(line)
        if table and table.group("name") == "consumer":
            if start is not None:
                return start, index
            in_consumer = False
        if re.match(rf'^id\s*=\s*"{re.escape(consumer_id)}"\s*$', line.strip()):
            in_consumer = True
        if not in_consumer or not table:
            continue
        name = table.group("name")
        if start is not None and name in {"consumer.libraries", "consumer.rehearsal", "consumer.out_of_scope", "surface"}:
            return start, _trim_comments(lines, start, index)
        if name == "consumer.libraries" and start is None:
            following = lines[index + 1 : index + 4]
            if any(re.match(rf'^\s*library\s*=\s*"{library}"', f) for f in following):
                start = index
    if start is not None:
        return start, len(lines)
    raise RecordError(f"no {library} Resolution for consumer {consumer_id!r} in the register")


def _trim_comments(lines: list[str], start: int, end: int) -> int:
    """Step back over the comment block that introduces the next table, which belongs to it."""
    while end - 1 > start and (lines[end - 1].strip().startswith("#") or not lines[end - 1].strip()):
        end -= 1
    return end


def _lag_span(lines: list[str], start: int, end: int) -> tuple[int, int] | None:
    for index in range(start, end):
        table = _TABLE.match(lines[index])
        if table and table.group("name") == "consumer.libraries.lag":
            stop = index + 1
            while stop < end and lines[stop].strip() and not _TABLE.match(lines[stop]) and not lines[stop].strip().startswith("#"):
                stop += 1
            first = index
            while first - 1 > start and lines[first - 1].strip().startswith("#"):
                first -= 1
            return first, stop
    return None


def _verify(text: str, parity_ids: set[str]) -> None:
    data = tomllib.loads(text)
    findings = check_register(data, parity_ids)
    if findings:
        raise RecordError("the edit would leave the register malformed:\n" + "\n".join(f.render() for f in findings))


def close_lag(text: str, consumer_id: str, library: str, parity_ids: set[str]) -> str:
    lines = text.splitlines(keepends=True)
    start, end = _resolution_span(lines, consumer_id, library)
    span = _lag_span(lines, start, end)
    if span is None:
        return text
    edited = "".join(lines[: span[0]] + lines[span[1] :])
    _verify(edited, parity_ids)
    return edited


def decline(text: str, consumer_id: str, library: str, reason: str, parity_ids: set[str]) -> str:
    lines = text.splitlines(keepends=True)
    start, end = _resolution_span(lines, consumer_id, library)
    span = _lag_span(lines, start, end)
    if span is not None:
        lines = lines[: span[0]] + lines[span[1] :]
        start, end = _resolution_span(lines, consumer_id, library)
    indent = "    "
    escaped = reason.replace("\\", "\\\\").replace('"', '\\"')
    block = [
        f"{indent}[consumer.libraries.lag]\n",
        f'{indent}kind = "deliberate"\n',
        f'{indent}reason = "{escaped}"\n',
    ]
    edited = "".join(lines[:end] + block + lines[end:])
    _verify(edited, parity_ids)
    return edited


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record an adoption's effect on the consumer register.")
    parser.add_argument("outcome", choices=["close-lag", "decline"])
    parser.add_argument("--consumer", required=True)
    parser.add_argument("--library", choices=["python", "javascript"], required=True)
    parser.add_argument("--reason", help="decline only: a policy, never a version")
    parser.add_argument("--register", type=Path, default=ROOT / "governance" / "consumers.toml")
    parser.add_argument("--parity", type=Path, default=ROOT / "governance" / "parity.toml")
    args = parser.parse_args(argv)

    with args.parity.open("rb") as handle:
        parity_ids = {c["id"] for c in tomllib.load(handle).get("capability", []) if "id" in c}
    text = args.register.read_text(encoding="utf-8")
    try:
        if args.outcome == "close-lag":
            edited = close_lag(text, args.consumer, args.library, parity_ids)
        else:
            if not args.reason:
                parser.error("decline needs --reason, stating a policy")
            edited = decline(text, args.consumer, args.library, args.reason, parity_ids)
    except RecordError as error:
        print(f"NOT RECORDED: {error}", file=sys.stderr)
        return 1
    if edited == text:
        print(f"{args.consumer} ({args.library}): nothing to record; the register holds no lag for it.")
        return 0
    args.register.write_text(edited, encoding="utf-8")
    print(f"{args.consumer} ({args.library}): {args.outcome} recorded in {args.register}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
