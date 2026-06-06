from __future__ import annotations

import io
from pathlib import Path

import pytest

from micropython_wasm import MicroPythonWasmError, RunResult
from micropython_wasm.cli import main


def fake_execute(code: str, **kwargs: object) -> RunResult:
    return RunResult(stdout=f"ran: {code}", stderr="", fuel_remaining=1)


def test_cli_c_executes_code():
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["-c", "print(1 + 1)"],
        stdout=stdout,
        stderr=stderr,
        execute=fake_execute,
    )

    assert exit_code == 0
    assert stdout.getvalue() == "ran: print(1 + 1)"
    assert stderr.getvalue() == ""


def test_cli_file_executes_file_contents(tmp_path: Path):
    source = tmp_path / "script.py"
    source.write_text("print('file')\n", encoding="utf-8")
    stdout = io.StringIO()

    exit_code = main([str(source)], stdout=stdout, execute=fake_execute)

    assert exit_code == 0
    assert stdout.getvalue() == "ran: print('file')\n"


def test_cli_missing_file_returns_error(tmp_path: Path):
    stderr = io.StringIO()

    exit_code = main([str(tmp_path / "missing.py")], stderr=stderr)

    assert exit_code == 1
    assert "micropython-wasm:" in stderr.getvalue()
    assert "missing.py" in stderr.getvalue()


def test_cli_rejects_c_with_file(tmp_path: Path):
    source = tmp_path / "script.py"
    source.write_text("print(1)\n", encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        main(["-c", "print(2)", str(source)])

    assert excinfo.value.code == 2


def test_cli_reports_execution_errors():
    def fail(code: str, **kwargs: object) -> RunResult:
        raise MicroPythonWasmError("guest exited")

    stderr = io.StringIO()

    exit_code = main(["-c", "raise ValueError"], stderr=stderr, execute=fail)

    assert exit_code == 1
    assert stderr.getvalue() == "micropython-wasm: guest exited\n"


def test_cli_c_passes_memory_and_fuel_options():
    calls: list[tuple[str, dict[str, object]]] = []

    def execute(code: str, **kwargs: object) -> RunResult:
        calls.append((code, kwargs))
        return RunResult(stdout="", stderr="", fuel_remaining=1)

    exit_code = main(
        ["--memory", "33554432", "--fuel", "1234", "-c", "print(1)"],
        execute=execute,
    )

    assert exit_code == 0
    assert calls == [
        ("print(1)", {"memory_bytes": 33_554_432, "fuel": 1_234}),
    ]


class FakeSession:
    def __init__(self) -> None:
        self.codes: list[str] = []
        self.closed = False

    def run(self, code: str) -> RunResult:
        self.codes.append(code)
        return RunResult(stdout=f"out: {code}\n", stderr="", fuel_remaining=1)

    def close(self) -> None:
        self.closed = True


def test_cli_repl_uses_session_and_wraps_expressions():
    session = FakeSession()
    stdin = io.StringIO("1 + 1\nx = 3\nquit()\n")
    stdout = io.StringIO()

    exit_code = main(
        [],
        stdin=stdin,
        stdout=stdout,
        session_factory=lambda **kwargs: session,
    )

    assert exit_code == 0
    assert session.closed
    assert session.codes == [
        "__micropython_wasm_repl_value = (1 + 1)\n"
        "if __micropython_wasm_repl_value is not None:\n"
        "    print(repr(__micropython_wasm_repl_value))",
        "x = 3",
    ]
    assert stdout.getvalue().startswith(">>> out: ")


def test_cli_repl_passes_memory_and_fuel_options():
    session = FakeSession()
    calls: list[dict[str, object]] = []

    def session_factory(**kwargs: object) -> FakeSession:
        calls.append(kwargs)
        return session

    exit_code = main(
        ["--memory", "33554432", "--fuel", "1234"],
        stdin=io.StringIO("quit()\n"),
        session_factory=session_factory,
    )

    assert exit_code == 0
    assert calls == [{"memory_bytes": 33_554_432, "fuel": 1_234}]
