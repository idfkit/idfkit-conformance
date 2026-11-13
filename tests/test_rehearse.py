"""The rehearsal's own rules, without running anyone's build: attribution, candidates, and no-candidate."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
from consumers import Rehearsal
from rehearse import (
    EXIT_NO_CANDIDATE,
    Candidate,
    Run,
    _attribute,
    _package_of_tarball,
    find_candidate,
    main,
    rehearse_python,
)

ROOT = Path(__file__).resolve().parent.parent


def _run(code: int, *errors: str) -> Run:
    return Run("cmd", code, "", tuple(errors))


def test_green_then_red_is_the_library() -> None:
    assert _attribute(_run(0), _run(1, "a.py:3 - error: x")) == "library"


def test_red_at_both_with_the_same_errors_is_the_consumer() -> None:
    assert _attribute(_run(1, "lexer.py:25 - error: y"), _run(1, "lexer.py:25 - error: y")) == "consumer"


def test_a_new_error_behind_an_old_one_is_still_the_library() -> None:
    # idfkit-docs is red at its declared level for reasons unrelated to idfkit. A candidate that
    # breaks a call there must not hide behind those.
    declared = _run(1, "lexer.py:25 - error: y")
    candidate = _run(1, "lexer.py:25 - error: y", "schema_utils.py:18 - error: unknown import")
    assert _attribute(declared, candidate) == "library"


def test_red_then_green_is_the_consumer_and_not_a_pass() -> None:
    assert _attribute(_run(1, "x"), _run(0)) == "consumer"


def test_tarball_names_map_to_packages() -> None:
    assert _package_of_tarball("/tmp/idfkit-core-0.0.0.tgz") == "@idfkit/core"
    assert _package_of_tarball("idfkit-language-0.3.0-rc.3.tgz") == "@idfkit/language"
    # npm packs `@idfkit/idfkit` as `idfkit-idfkit-<version>.tgz`, the scope folded into the name.
    assert _package_of_tarball("idfkit-idfkit-0.0.0.tgz") == "@idfkit/idfkit"


def _pack(directory: Path, filename: str, dependencies: dict[str, str]) -> str:
    import io
    import json
    import tarfile

    path = directory / filename
    body = json.dumps({"dependencies": dependencies}).encode()
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(body)
        archive.addfile(info, io.BytesIO(body))
    return str(path)


def test_a_candidate_installs_its_own_closure_and_nothing_unreached(tmp_path: Path) -> None:
    import json

    from rehearse import _tarballs_for

    files = [
        _pack(tmp_path, "idfkit-core-0.0.0.tgz", {"@idfkit/schemas": "0.0.0"}),
        _pack(tmp_path, "idfkit-schemas-0.0.0.tgz", {}),
        _pack(tmp_path, "idfkit-language-0.0.0.tgz", {}),
        _pack(tmp_path, "idfkit-types-v26-1-0.0.0.tgz", {}),
        _pack(tmp_path, "idfkit-idfkit-0.0.0.tgz", {"@idfkit/core": "0.0.0", "@idfkit/schemas": "0.0.0"}),
    ]
    manifest = tmp_path / "package.json"
    manifest.write_text(json.dumps({"dependencies": {"@idfkit/core": "0.2.0", "@idfkit/language": "0.2.0"}}))
    names = sorted(Path(f).name for f in _tarballs_for(manifest, files))
    assert names == ["idfkit-core-0.0.0.tgz", "idfkit-language-0.0.0.tgz", "idfkit-schemas-0.0.0.tgz"]


def test_a_facade_consumer_installs_the_facade_and_what_it_pins(tmp_path: Path) -> None:
    import json

    from rehearse import _tarballs_for

    files = [
        _pack(tmp_path, "idfkit-core-0.0.0.tgz", {"@idfkit/schemas": "0.0.0"}),
        _pack(tmp_path, "idfkit-schemas-0.0.0.tgz", {}),
        _pack(tmp_path, "idfkit-language-0.0.0.tgz", {}),
        _pack(tmp_path, "idfkit-idfkit-0.0.0.tgz", {"@idfkit/core": "0.0.0", "@idfkit/schemas": "0.0.0"}),
    ]
    manifest = tmp_path / "package.json"
    manifest.write_text(json.dumps({"dependencies": {"@idfkit/idfkit": "0.2.0"}}))
    names = sorted(Path(f).name for f in _tarballs_for(manifest, files))
    assert names == ["idfkit-core-0.0.0.tgz", "idfkit-idfkit-0.0.0.tgz", "idfkit-schemas-0.0.0.tgz"]


def test_a_wheel_candidate_is_found_and_named(tmp_path: Path) -> None:
    (tmp_path / "idfkit-1.0.0rc4-py3-none-any.whl").write_bytes(b"")
    candidate = find_candidate(tmp_path, "python", "f709abdf")
    assert candidate is not None and candidate.version == "1.0.0rc4" and candidate.sha == "f709abdf"


def test_an_empty_candidate_directory_is_no_candidate(tmp_path: Path) -> None:
    assert find_candidate(tmp_path, "python", "x") is None
    assert find_candidate(tmp_path, "javascript", "x") is None


def test_no_candidate_fails_and_never_passes(tmp_path: Path) -> None:
    code = main(
        [
            "--register", str(ROOT / "governance" / "consumers.toml"),
            "--naming", str(ROOT / "governance" / "naming.toml"),
            "--consumer", "idfkit-mcp",
            "--library", "python",
            "--candidate", str(tmp_path / "missing"),
            "--root", str(tmp_path),
        ]
    )
    assert code == EXIT_NO_CANDIDATE


def test_a_through_provider_consumer_rehearses_through_its_providers(tmp_path: Path) -> None:
    code = main(
        [
            "--register", str(ROOT / "governance" / "consumers.toml"),
            "--naming", str(ROOT / "governance" / "naming.toml"),
            "--consumer", "idfkit-plugin",
            "--library", "python",
            "--root", str(tmp_path),
        ]
    )
    assert code == 0


def test_a_relative_root_installs_into_the_consumer_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The workflow passes `--root consumer`, and every command runs with the consumer as its cwd.

    A `--python` path relative to the repository root is then resolved a second time from inside the
    consumer, names a `consumer/consumer/.venv` that does not exist, and the install fails before the
    candidate is ever rehearsed. Every python rehearsal in CI failed this way until the path was absolute.
    """
    (tmp_path / "consumer" / "server").mkdir(parents=True)
    # A space in the path, as a local checkout may have, must survive the shell.
    candidate_file = "/wheels with space/idfkit-9.9.9-py3-none-any.whl"
    monkeypatch.chdir(tmp_path)
    calls: list[tuple[str, Path]] = []

    def record(command: str, cwd: Path, env: dict[str, str]) -> Run:
        calls.append((command, cwd))
        return Run(command, 0, "", ())

    monkeypatch.setattr("rehearse._run", record)
    rehearsal = Rehearsal(library="python", install="wheel", typecheck="pyright", test=None, workdir="server")
    candidate = Candidate("python", (candidate_file,), "abc", "9.9.9")

    rehearse_python(rehearsal, Path("consumer"), candidate, baseline=False)

    installs = [shlex.split(command) for command, _ in calls if command.startswith("uv pip install")]
    assert len(installs) == 1, calls
    interpreter = Path(installs[0][installs[0].index("--python") + 1])
    assert interpreter == tmp_path.resolve() / "consumer" / "server" / ".venv" / "bin" / "python"
    assert installs[0][-1] == candidate_file
    assert all(cwd.is_absolute() for _, cwd in calls)
