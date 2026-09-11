"""Build and test one consumer against a candidate build of one library, before it is published.

Implements contracts/rehearsal.md. Run by `.github/workflows/rehearse.yml`, and by hand from a
workspace, which is how its two acceptance runs were made.

THE FIVE HARD RULES, and where each is enforced here:

  1. No committed manifest or lockfile changes (FR-010). The first language's candidate wheel is
     installed into the synced environment with `uv pip`, which reads and writes neither
     `pyproject.toml` nor `uv.lock`, and every command then runs with `UV_NO_SYNC=1` so that `uv
     run` cannot quietly re-resolve the candidate away. The second language's tarballs install into a
     scratch copy with `--no-save`. Either way the consumer's working tree is compared before and
     after, and a difference fails the run with exit 2 whatever else it found.
  2. The type check runs first (R4). It is the instrument that sees a call whose name survived.
  3. No candidate, no result (FR-012). An empty or missing candidate exits 3 and reports nothing.
  4. Every failure is attributed (FR-013). Each command runs twice, at the consumer's declared level
     and then at the candidate. Red at both is the consumer's, and pre-existing; green then red is
     the candidate's. That is the only attribution that does not need anyone's opinion.
  5. One library at a time (FR-014). The other stays at whatever the consumer declares.

Exit codes: 0 the candidate broke nothing, 1 the candidate broke something, 2 the rehearsal broke its
own contract, 3 there was no candidate. A 1 is a finding against the LIBRARY; this workflow is
dispatched on demand and never runs on a consumer's pull requests, so it cannot block their work.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from consumers import Register, Rehearsal  # noqa: E402
from rename_scan import Appearance, at_risk, scan  # noqa: E402

EXIT_BROKEN = 1
EXIT_CONTRACT = 2
EXIT_NO_CANDIDATE = 3
_TAIL = 40


@dataclass(frozen=True, slots=True)
class Candidate:
    """What was rehearsed. A result that cannot name its candidate is not evidence."""

    library: str
    files: tuple[str, ...]
    sha: str
    version: str | None


@dataclass(frozen=True, slots=True)
class Run:
    command: str
    exit_code: int
    tail: str
    #: Every line of the output that reports an error, normalised so the same error reads the same
    #: at both levels. Compared, not just counted: see `_attribute`.
    errors: tuple[str, ...] = ()


#: Lines that report a failure in the four instruments the register names: pyright, tsc, pytest and
#: vitest. Anything else in the output is context, not a verdict.
_ERROR_LINE = re.compile(r"( - error: |error TS\d+|^FAILED |^\s*(?:FAIL|×) )")
_NOISE = re.compile(r"(\d+(\.\d+)?m?s\b|0x[0-9a-f]+|/(?:private/)?(?:tmp|var)/\S+?/(?=[^/\s]+\.(?:py|ts|tsx|js)))")


@dataclass(frozen=True, slots=True)
class Check:
    """One command, at the declared level and at the candidate, and whose failure it is."""

    step: str
    command: str
    declared: Run | None
    candidate: Run
    #: ok, library, or consumer.
    attribution: str


@dataclass
class Result:
    consumer: str
    library: str
    candidate: Candidate | None
    declared_level: str = ""
    checks: list[Check] = field(default_factory=list)
    renames: list[Appearance] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    contract: list[str] = field(default_factory=list)

    @property
    def broken(self) -> bool:
        return any(check.attribution == "library" for check in self.checks)


def find_candidate(path: Path, library: str, sha: str) -> Candidate | None:
    suffix = ".whl" if library == "python" else ".tgz"
    files = sorted(p for p in ([path] if path.is_file() else path.glob(f"*{suffix}")) if p.name.endswith(suffix))
    if library == "python":
        files = [p for p in files if p.name.startswith("idfkit-")]
        if len(files) != 1:
            return None
        version = files[0].name.split("-")[1]
    else:
        files = [p for p in files if p.name.startswith(("idfkit-", "idfkit-core", "idfkit-schemas"))]
        if not files:
            return None
        core = next((p for p in files if p.name.startswith("idfkit-core-")), files[0])
        version = core.name.removesuffix(".tgz").split("-", 2)[-1]
    return Candidate(library, tuple(str(p.resolve()) for p in files), sha, version)


def _run(command: str, cwd: Path, env: dict[str, str]) -> Run:
    process = subprocess.run(command, shell=True, cwd=cwd, env=env, capture_output=True, text=True, check=False)  # noqa: S602
    output = (process.stdout + process.stderr).strip().splitlines()
    errors = tuple(sorted({_NOISE.sub("", line.strip()) for line in output if _ERROR_LINE.search(line)}))
    return Run(command, process.returncode, "\n".join(output[-_TAIL:]), errors)


def _status(root: Path) -> str:
    return subprocess.run(["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True, check=False).stdout  # noqa: S603, S607


def _attribute(declared: Run | None, candidate: Run) -> str:
    """Whose failure this is: the candidate's (`library`), the consumer's own (`consumer`), or none.

    Exit codes alone are not enough, and the first acceptance run showed why: idfkit-docs' type
    check is red at its declared level for three reasons that have nothing to do with idfkit, and a
    command that is already red would hide every new error a candidate caused. So a candidate that
    fails where the declared level also failed is still the library's when it reports an error line
    the declared level did not.
    """
    if candidate.exit_code == 0:
        return "ok" if declared is None or declared.exit_code == 0 else "consumer"
    if declared is None or declared.exit_code == 0:
        return "library"
    return "library" if set(candidate.errors) - set(declared.errors) else "consumer"


def _commands(rehearsal: Rehearsal) -> list[tuple[str, str]]:
    commands = [("typecheck", rehearsal.typecheck or "")]
    if rehearsal.test:
        commands.append(("test", rehearsal.test))
    return commands


def rehearse_python(rehearsal: Rehearsal, root: Path, candidate: Candidate, baseline: bool) -> list[Check]:
    workdir = root / rehearsal.workdir
    env = {**os.environ, "UV_NO_SYNC": "1"}
    sync = _run("uv sync --frozen --all-extras --all-groups", workdir, {**os.environ})
    if sync.exit_code != 0:
        return [Check("setup", sync.command, None, sync, "consumer")]
    declared = {step: _run(cmd, workdir, env) for step, cmd in _commands(rehearsal)} if baseline else {}
    python = workdir / ".venv" / "bin" / "python"
    install = _run(f"uv pip install --python {python} --no-deps --reinstall {candidate.files[0]}", workdir, {**os.environ})
    if install.exit_code != 0:
        return [Check("install", install.command, None, install, "library")]
    checks = []
    for step, cmd in _commands(rehearsal):
        after = _run(cmd, workdir, env)
        checks.append(Check(step, cmd, declared.get(step), after, _attribute(declared.get(step), after)))
    return checks


def _package_of_tarball(filename: str) -> str:
    """`idfkit-core-0.0.0.tgz` is `@idfkit/core`; `idfkit-0.0.0.tgz` is the shared name."""
    stem = re.sub(r"-\d+\.\d+\.\d+[^/]*\.tgz$", "", Path(filename).name)
    return "idfkit" if stem == "idfkit" else "@idfkit/" + stem.removeprefix("idfkit-")


def _tarball_dependencies(filename: str) -> set[str]:
    """The runtime dependencies a packed tarball declares, read from its own package.json."""
    with tarfile.open(filename, "r:gz") as archive:
        member = archive.extractfile("package/package.json")
        if member is None:
            return set()
        return set((json.loads(member.read()).get("dependencies") or {}).keys())


def _tarballs_for(manifest: Path, files: Sequence[str]) -> list[str]:
    """The candidate's packages this consumer needs: what it depends on, and what those depend on.

    The closure matters because a candidate's packages pin each other at the candidate's own
    version, which exists nowhere but in these tarballs. Installing `@idfkit/core` alone sends npm to
    the registry for the `@idfkit/schemas` it pins, and the second acceptance run failed exactly
    that way on the lsp model server, which depends on core but not directly on schemas. Packages
    nothing reaches (the type packages, the facade for a scoped consumer) stay out.
    """
    by_package = {_package_of_tarball(f): f for f in files}
    wanted = set((json.loads(manifest.read_text(encoding="utf-8")).get("dependencies") or {}).keys()) & set(by_package)
    frontier = list(wanted)
    while frontier:
        for dependency in _tarball_dependencies(by_package[frontier.pop()]) & set(by_package):
            if dependency not in wanted:
                wanted.add(dependency)
                frontier.append(dependency)
    return [by_package[p] for p in sorted(wanted)]


def rehearse_javascript(rehearsal: Rehearsal, root: Path, candidate: Candidate, baseline: bool) -> list[Check]:
    # A scratch copy, so that even an npm that ignored --no-save could not touch the consumer.
    with tempfile.TemporaryDirectory(prefix="rehearsal-") as scratch:
        copy = Path(scratch) / "consumer"
        # Not the first language's environment: a consumer of both languages may be rehearsing its
        # other half in the same checkout, and a copy of a half-written .venv is a copy of nothing.
        shutil.copytree(root, copy, ignore=shutil.ignore_patterns("node_modules", ".git", "dist", ".venv"), symlinks=True)
        workdir = copy / rehearsal.workdir
        env = {**os.environ}
        install = _run("npm ci --no-audit --no-fund", workdir, env)
        if install.exit_code != 0:
            return [Check("setup", install.command, None, install, "consumer")]
        declared = {step: _run(cmd, workdir, env) for step, cmd in _commands(rehearsal)} if baseline else {}
        tarballs = " ".join(_tarballs_for(workdir / "package.json", candidate.files))
        if not tarballs:
            return [Check("install", "(none)", None, Run("(none)", 1, "the candidate carries no package this consumer depends on"), "library")]
        overlay = _run(f"npm install --no-save --no-audit --no-fund {tarballs}", workdir, env)
        if overlay.exit_code != 0:
            return [Check("install", overlay.command, None, overlay, "library")]
        checks = []
        for step, cmd in _commands(rehearsal):
            after = _run(cmd, workdir, env)
            checks.append(Check(step, cmd, declared.get(step), after, _attribute(declared.get(step), after)))
        for manifest in ("package.json", "package-lock.json"):
            original, scratched = root / rehearsal.workdir / manifest, workdir / manifest
            if original.is_file() and original.read_bytes() != scratched.read_bytes():
                checks.append(
                    Check("contract", manifest, None, Run(manifest, 1, f"{manifest} changed in the scratch copy"), "library")
                )
        return checks


def rehearse(
    register: Register,
    consumer_id: str,
    library: str,
    candidate: Candidate | None,
    root: Path,
    naming: dict,
    *,
    baseline: bool = True,
) -> Result:
    consumer = register.consumer(consumer_id)
    rehearsal = consumer.rehearsal(library)
    result = Result(consumer_id, library, candidate)
    if rehearsal is None:
        result.contract.append(f"{consumer_id} has no {library} rehearsal in the register.")
        return result
    if rehearsal.install == "through-provider":
        result.providers = list(consumer.depends_on)
        return result
    if candidate is None:
        return result

    before = _status(root)
    if library == "python":
        result.checks = rehearse_python(rehearsal, root, candidate, baseline)
    else:
        result.checks = rehearse_javascript(rehearsal, root, candidate, baseline)
    result.renames = scan(root, library, at_risk(naming, library), rehearsal.prose_paths)
    after = _status(root)
    if after != before:
        result.contract.append(f"the consumer's working tree changed during the rehearsal (FR-010):\n{after}")
    return result


def render(result: Result) -> str:
    lines = [f"## Rehearsal: {result.consumer} against a {result.library} candidate", ""]
    if result.candidate is None and not result.providers:
        lines.append("**No candidate.** Nothing was rehearsed, and nothing passed (FR-012).")
        return "\n".join(lines)
    if result.providers:
        lines.append(
            f"This consumer holds no code that calls the library. A candidate reaches it only inside a release of "
            f"{', '.join(result.providers)}, so its rehearsal is theirs."
        )
        return "\n".join(lines)
    assert result.candidate is not None
    lines.append(f"Candidate: `{', '.join(Path(f).name for f in result.candidate.files)}` at `{result.candidate.sha}`")
    lines.append(f"Declared level, unchanged: `{result.declared_level or 'as committed'}`")
    lines.append("")
    lines.append("| Step | Declared | Candidate | Whose |")
    lines.append("| --- | --- | --- | --- |")
    for check in result.checks:
        declared = "not run" if check.declared is None else ("pass" if check.declared.exit_code == 0 else "FAIL")
        candidate = "pass" if check.candidate.exit_code == 0 else "FAIL"
        lines.append(f"| {check.step} | {declared} | {candidate} | {check.attribution} |")
    lines.append("")
    lines.append(f"Rename scan: {len(result.renames)} appearance(s) of a name that survived with a different meaning.")
    for appearance in result.renames:
        lines.append(f"- `{appearance.path}:{appearance.line}` ({appearance.kind}) `{appearance.text}`")
    for check in result.checks:
        if check.attribution == "ok":
            continue
        lines += ["", f"### {check.step}, {check.attribution}"]
        new = sorted(set(check.candidate.errors) - set(check.declared.errors if check.declared else ()))
        if new:
            lines += ["", "Errors the candidate introduced:", "```", *new, "```"]
        lines += ["", "Output:", "```", check.candidate.tail, "```"]
    for breach in result.contract:
        lines += ["", f"**Contract breach:** {breach}"]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rehearse one consumer against an unpublished candidate.")
    parser.add_argument("--register", type=Path, required=True)
    parser.add_argument("--naming", type=Path, required=True)
    parser.add_argument("--consumer", required=True)
    parser.add_argument("--library", choices=["python", "javascript"], required=True)
    parser.add_argument("--candidate", type=Path, help="a wheel, or a directory of wheels or tarballs")
    parser.add_argument("--candidate-sha", default="(unstated)")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="the consumer's checkout")
    parser.add_argument("--report", type=Path, help="write the result here as JSON")
    parser.add_argument("--no-baseline", action="store_true", help="skip the run at the declared level")
    args = parser.parse_args(argv)

    register = Register.load(args.register)
    with args.naming.open("rb") as handle:
        naming = tomllib.load(handle)
    candidate = find_candidate(args.candidate, args.library, args.candidate_sha) if args.candidate and args.candidate.exists() else None

    consumer = register.consumer(args.consumer)
    rehearsal = consumer.rehearsal(args.library)
    if candidate is None and not (rehearsal and rehearsal.install == "through-provider"):
        print(render(Result(args.consumer, args.library, None)))
        print(f"\nNo {args.library} candidate at {args.candidate}. A rehearsal with nothing to rehearse is not a pass.", file=sys.stderr)
        return EXIT_NO_CANDIDATE

    result = rehearse(register, args.consumer, args.library, candidate, args.root, naming, baseline=not args.no_baseline)
    summary = render(result)
    print(summary)
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(step_summary, "a", encoding="utf-8") as handle:
            handle.write(summary + "\n")
    if args.report:
        args.report.write_text(json.dumps(asdict(result), indent=2, default=str) + "\n", encoding="utf-8")
    if result.contract:
        return EXIT_CONTRACT
    return EXIT_BROKEN if result.broken else 0


if __name__ == "__main__":
    sys.exit(main())
