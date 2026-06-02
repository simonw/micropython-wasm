from __future__ import annotations

import pytest

from micropython_wasm import (
    MicroPythonReplaySession,
    MicroPythonSessionClosed,
    MicroPythonWasmError,
    default_wasm_path,
)

pytestmark = pytest.mark.skipif(
    not default_wasm_path().exists(),
    reason="packaged MicroPython WASI artifact is not built",
)


def test_session_keeps_variables_between_runs():
    session = MicroPythonReplaySession(wall_timeout_seconds=None)

    first = session.run("x = 10\nprint(x)")
    second = session.run("x += 5\nprint(x)")
    third = session.run("print(x * 2)")

    assert first.stdout == "10\n"
    assert second.stdout == "15\n"
    assert third.stdout == "30\n"


def test_session_keeps_functions_classes_and_imports():
    session = MicroPythonReplaySession(wall_timeout_seconds=None)

    session.run("""
import math

def scale(value):
    return math.sqrt(value) * 10

class Box:
    def __init__(self, value):
        self.value = value

    def scaled(self):
        return scale(self.value)
""")

    result = session.run("box = Box(81)\nprint(box.scaled())")

    assert result.stdout == "90.0\n"


def test_session_returns_only_output_from_current_run():
    session = MicroPythonReplaySession(wall_timeout_seconds=None)

    first = session.run("print('first')\nvalue = 3")
    second = session.run("print('second')\nprint(value)")

    assert first.stdout == "first\n"
    assert second.stdout == "second\n3\n"


def test_session_does_not_save_failed_snippet():
    session = MicroPythonReplaySession(wall_timeout_seconds=None)
    session.run("x = 1")

    with pytest.raises(MicroPythonWasmError):
        session.run("x = 2\nraise ValueError('boom')")

    assert session.run("print(x)").stdout == "1\n"


def test_session_supports_readonly_directory(tmp_path):
    (tmp_path / "message.txt").write_text("hello\n")
    session = MicroPythonReplaySession(readonly_dir=tmp_path, wall_timeout_seconds=None)

    result = session.run(
        "contents = open('/input/message.txt').read()\nprint(contents)"
    )
    later = session.run("print(contents.upper())")

    assert result.stdout == "hello\n\n"
    assert later.stdout == "HELLO\n\n"


def test_session_close_clears_state_and_rejects_more_runs():
    session = MicroPythonReplaySession(wall_timeout_seconds=None)
    session.run("x = 1")

    session.close()

    assert session.closed
    assert session.snippets == ()
    with pytest.raises(MicroPythonSessionClosed):
        session.run("print(x)")


def test_session_context_manager_closes_session():
    with MicroPythonReplaySession(wall_timeout_seconds=None) as session:
        assert session.run("x = 4\nprint(x)").stdout == "4\n"

    assert session.closed
    with pytest.raises(MicroPythonSessionClosed):
        session.run("print(x)")
