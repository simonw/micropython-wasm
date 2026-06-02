from __future__ import annotations

import pytest

from micropython_wasm import (
    MicroPythonSessionClosed,
    MicroPythonSession,
    MicroPythonWasmError,
    default_wasm_path,
)

pytestmark = pytest.mark.skipif(
    not default_wasm_path().exists(),
    reason="packaged MicroPython WASI artifact is not built",
)


def test_persistent_session_keeps_real_resident_state_without_replay():
    calls: list[str] = []

    def record(value):
        calls.append(value)
        return len(calls)

    session = MicroPythonSession(host_functions={"record": record})
    try:
        assert session.run("x = 10\nprint(x)").stdout == "10\n"
        assert session.run("x += 5\nprint(x)").stdout == "15\n"
        assert session.run("print(record('once'))").stdout == "1\n"
        assert session.run("print(x)").stdout == "15\n"
    finally:
        session.close()

    assert calls == ["once"]


def test_persistent_session_keeps_functions_classes_and_imports():
    session = MicroPythonSession()
    try:
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

        assert session.run("box = Box(81)\nprint(box.scaled())").stdout == "90.0\n"
    finally:
        session.close()


def test_persistent_session_does_not_replay_file_side_effects(tmp_path):
    session = MicroPythonSession(readonly_dir=tmp_path)
    counter = {"count": 0}

    def increment():
        counter["count"] += 1
        return counter["count"]

    session.register_function(increment)
    try:
        assert session.run("value = increment()\nprint(value)").stdout == "1\n"
        assert session.run("print(value)").stdout == "1\n"
    finally:
        session.close()

    assert counter["count"] == 1


def test_persistent_session_guest_exception_does_not_poison_state():
    session = MicroPythonSession()
    try:
        session.run("x = 1")
        with pytest.raises(MicroPythonWasmError, match="ValueError: boom"):
            session.run("x = 2\nraise ValueError('boom')")

        assert session.run("print(x)").stdout == "2\n"
    finally:
        session.close()


def test_persistent_session_host_function_exception_can_be_caught():
    def fail():
        raise ValueError("bad host value")

    session = MicroPythonSession(host_functions={"fail": fail})
    try:
        result = session.run("""
try:
    fail()
except RuntimeError as ex:
    print(str(ex))
""")
    finally:
        session.close()

    assert result.stdout == "ValueError: bad host value\n"


def test_persistent_session_context_manager_closes_session():
    with MicroPythonSession() as session:
        assert session.run("x = 4\nprint(x)").stdout == "4\n"

    assert session.closed
    with pytest.raises(MicroPythonSessionClosed):
        session.run("print(x)")


def test_persistent_session_close_is_idempotent():
    session = MicroPythonSession()
    session.close()
    session.close()

    with pytest.raises(MicroPythonSessionClosed):
        session.run("print(1)")


def test_persistent_session_close_releases_thread_resources():
    session = MicroPythonSession()
    session.run("print(1)")

    session.close()

    assert session._thread is not None
    assert not session._thread.is_alive()
    assert session._store is None
    assert session._thread_host_functions is None


def test_persistent_session_wall_timeout_allows_successful_runs():
    session = MicroPythonSession(wall_timeout_seconds=0.5)
    try:
        assert session.run("print(1)").stdout == "1\n"
        assert session.run("print(2)").stdout == "2\n"
    finally:
        session.close()


def test_persistent_session_wall_timeout_interrupts_infinite_loop():
    session = MicroPythonSession(wall_timeout_seconds=0.05)
    try:
        with pytest.raises(MicroPythonWasmError, match="guest trapped"):
            session.run("while True:\n    pass")
    finally:
        session.close()
