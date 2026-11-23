from __future__ import annotations

import io
from pathlib import Path

import pytest

from micropython_wasm import MicroPythonWasmError, RunResult, default_wasm_path
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
    stopped = False

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


TRACEBACK = 'Traceback (most recent call last):\n  File "<stdin>", line 2\nValueError: boom\n'


def test_cli_shows_guest_output_and_traceback_when_execution_fails():
    def fail(code: str, **kwargs: object) -> RunResult:
        raise MicroPythonWasmError(
            "guest exited with code 1", stdout="before\n", stderr=TRACEBACK
        )

    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(["-c", "ignored"], stdout=stdout, stderr=stderr, execute=fail)

    assert exit_code == 1
    assert stdout.getvalue() == "before\n"
    assert stderr.getvalue() == (
        TRACEBACK + "micropython-wasm: guest exited with code 1\n"
    )


class FailingSession(FakeSession):
    def __init__(self, error: MicroPythonWasmError) -> None:
        super().__init__()
        self.error = error

    def run(self, code: str) -> RunResult:
        self.codes.append(code)
        raise self.error


def test_cli_repl_shows_guest_output_and_traceback_once():
    session = FailingSession(
        MicroPythonWasmError("ValueError: boom", stdout="before\n", stderr=TRACEBACK)
    )
    stdout = io.StringIO()
    stderr = io.StringIO()

    main(
        [],
        stdin=io.StringIO("fail()\n"),
        stdout=stdout,
        stderr=stderr,
        session_factory=lambda **kwargs: session,
    )

    assert "before\n" in stdout.getvalue()
    # The traceback already ends with the error, so it is not printed twice.
    assert stderr.getvalue() == TRACEBACK


def test_cli_repl_prints_error_that_has_no_traceback():
    session = FailingSession(MicroPythonWasmError("guest trapped: out of fuel"))
    stderr = io.StringIO()

    main(
        [],
        stdin=io.StringIO("spin()\n"),
        stdout=io.StringIO(),
        stderr=stderr,
        session_factory=lambda **kwargs: session,
    )

    assert stderr.getvalue() == "guest trapped: out of fuel\n"


def test_cli_repl_collects_multi_line_blocks_until_blank_line():
    session = FakeSession()
    stdin = io.StringIO("def f():\n    x = 1\n    return x\n\nf()\nx = (1,\n     2)\n")
    stdout = io.StringIO()

    main([], stdin=stdin, stdout=stdout, session_factory=lambda **kwargs: session)

    assert session.codes == [
        "def f():\n    x = 1\n    return x\n",
        "__micropython_wasm_repl_value = (f())\n"
        "if __micropython_wasm_repl_value is not None:\n"
        "    print(repr(__micropython_wasm_repl_value))",
        "x = (1,\n     2)",
    ]
    assert stdout.getvalue().startswith(">>> ... ... ... out: ")


def test_cli_repl_restarts_session_that_stopped():
    dead = FailingSession(
        MicroPythonWasmError(
            "MicroPythonSession stopped: guest trapped: error while executing\n"
            "    0: 0x1 - <unknown>!<wasm function 1>\n\n"
            "Caused by:\n    wasm trap: all fuel consumed by WebAssembly",
            stdout="partial\n",
        )
    )
    dead.stopped = True
    fresh = FakeSession()
    sessions = [dead, fresh]
    created: list[dict[str, object]] = []

    def session_factory(**kwargs: object) -> FakeSession:
        created.append(kwargs)
        return sessions.pop(0)

    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["--fuel", "1234"],
        stdin=io.StringIO("spin()\nx = 1\n"),
        stdout=stdout,
        stderr=stderr,
        session_factory=session_factory,
    )

    assert exit_code == 0
    assert dead.closed
    assert fresh.codes == ["x = 1"]
    assert fresh.closed
    # The replacement session gets the same options as the first one.
    assert created[0] == created[1]
    assert "partial\n" in stdout.getvalue()
    assert stderr.getvalue() == (
        "wasm trap: all fuel consumed by WebAssembly\n"
        "micropython-wasm: session restarted, variables and imports were lost\n"
    )


def test_cli_repl_keeps_session_after_ordinary_guest_exception():
    session = FailingSession(MicroPythonWasmError("ValueError: boom"))
    created: list[object] = []

    def session_factory(**kwargs: object) -> FakeSession:
        created.append(kwargs)
        return session

    stderr = io.StringIO()

    main(
        [],
        stdin=io.StringIO("fail()\nfail()\n"),
        stdout=io.StringIO(),
        stderr=stderr,
        session_factory=session_factory,
    )

    assert len(created) == 1
    assert session.codes == [
        "__micropython_wasm_repl_value = (fail())\n"
        "if __micropython_wasm_repl_value is not None:\n"
        "    print(repr(__micropython_wasm_repl_value))"
    ] * 2
    assert "restarted" not in stderr.getvalue()


def test_cli_repl_exits_with_guest_exit_code():
    session = FailingSession(
        MicroPythonWasmError(
            "MicroPythonSession stopped: guest exited with code 3",
            stdout="bye\n",
            exit_code=3,
        )
    )
    session.stopped = True
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        [],
        stdin=io.StringIO("import sys; sys.exit(3)\nprint('unreached')\n"),
        stdout=stdout,
        stderr=stderr,
        session_factory=lambda **kwargs: session,
    )

    assert exit_code == 3
    assert session.closed
    assert len(session.codes) == 1
    assert "bye\n" in stdout.getvalue()
    assert stderr.getvalue() == ""


def test_cli_c_exits_with_guest_exit_code():
    def fail(code: str, **kwargs: object) -> RunResult:
        raise MicroPythonWasmError(
            "guest exited with code 3", stdout="bye\n", exit_code=3
        )

    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(["-c", "ignored"], stdout=stdout, stderr=stderr, execute=fail)

    assert exit_code == 3
    assert stdout.getvalue() == "bye\n"
    assert stderr.getvalue() == ""


@pytest.mark.parametrize(
    ("value", "expected"), [("2.5", 2.5), ("30", 30.0), ("0", None)]
)
def test_cli_timeout_option_is_passed_to_run_and_repl(value, expected):
    calls: list[dict[str, object]] = []

    def execute(code: str, **kwargs: object) -> RunResult:
        calls.append(kwargs)
        return RunResult(stdout="", stderr="", fuel_remaining=1)

    def session_factory(**kwargs: object) -> FakeSession:
        calls.append(kwargs)
        return FakeSession()

    main(["--timeout", value, "-c", "print(1)"], execute=execute)
    main(
        ["--timeout", value],
        stdin=io.StringIO(""),
        stdout=io.StringIO(),
        session_factory=session_factory,
    )

    assert [call["wall_timeout_seconds"] for call in calls] == [expected, expected]


@pytest.mark.parametrize("value", ["-1", "soon", "nan", "inf"])
def test_cli_rejects_invalid_timeout(value):
    with pytest.raises(SystemExit) as excinfo:
        main(["--timeout", value, "-c", "print(1)"])

    assert excinfo.value.code == 2


@pytest.mark.skipif(
    not default_wasm_path().exists(),
    reason="packaged MicroPython WASI artifact is not built",
)
def test_cli_repl_end_to_end_with_real_session():
    stdin = io.StringIO(
        "def double(value):\n"
        "    result = value * 2\n"
        "    return result\n"
        "\n"
        "double(21)\n"
        "while True: pass\n"
        "\n"
        "double(1)\n"
        "import sys\n"
        "sys.exit(4)\n"
        "print('unreached')\n"
    )
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main([], stdin=stdin, stdout=stdout, stderr=stderr)

    assert exit_code == 4
    assert "42\n" in stdout.getvalue()
    assert "unreached" not in stdout.getvalue()
    # Running out of fuel restarted the session, which lost double().
    assert stderr.getvalue().splitlines() == [
        "wasm trap: all fuel consumed by WebAssembly",
        "micropython-wasm: session restarted, variables and imports were lost",
        "Traceback (most recent call last):",
        '  File "<string>", line 1, in <module>',
        "NameError: name 'double' isn't defined",
    ]
