from __future__ import annotations

import threading
import time

import pytest

from micropython_wasm import (
    DEFAULT_SESSION_WALL_TIMEOUT_SECONDS,
    MicroPythonSessionClosed,
    MicroPythonSession,
    MicroPythonWasmError,
    default_wasm_path,
)

pytestmark = pytest.mark.skipif(
    not default_wasm_path().exists(),
    reason="packaged MicroPython WASI artifact is not built",
)


def run_with_deadline(session, code, seconds=30):
    """Run code in a session, failing instead of hanging if it never returns."""

    outcome = {}

    def target():
        try:
            outcome["result"] = session.run(code)
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), f"session.run() did not return in {seconds}s"
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


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


def test_persistent_session_readonly_directory_rejects_writes(tmp_path):
    (tmp_path / "hello.txt").write_text("hello\n", newline="\n")
    session = MicroPythonSession(readonly_dir=tmp_path)
    try:
        assert session.run("print(open('/input/hello.txt').read())").stdout == (
            "hello\n\n"
        )
        with pytest.raises(MicroPythonWasmError, match="OSError"):
            session.run('open("/input/new.txt", "w").write("nope")')
    finally:
        session.close()

    assert [path.name for path in tmp_path.iterdir()] == ["hello.txt"]


def test_persistent_session_survives_garbage_collection():
    # Regression test: every snippet runs inside the bootstrap loop's try
    # block, so the first collection used to corrupt the resident VM.
    #
    # Each run leaves 20 KB of garbage, so 400 of them fill the default heap
    # more than twice over and force automatic collections. Those are paid for
    # out of the fuel of whichever run triggers them, so this also checks that
    # the default heap is small enough for the default fuel budget.
    session = MicroPythonSession()
    try:
        session.run("keep = [str(i) * 10 for i in range(200)]")
        for _ in range(400):
            session.run("data = [0] * 5000")
        session.run("import gc\ngc.collect()")
        result = session.run(
            "print(all(keep[i] == str(i) * 10 for i in range(200)), len(data))"
        )
    finally:
        session.close()

    assert result.stdout == "True 5000\n"


def test_persistent_session_guest_exception_does_not_poison_state():
    session = MicroPythonSession()
    try:
        session.run("x = 1")
        with pytest.raises(MicroPythonWasmError, match="ValueError: boom"):
            session.run("x = 2\nraise ValueError('boom')")

        assert session.run("print(x)").stdout == "2\n"
    finally:
        session.close()


@pytest.mark.parametrize(
    ("code", "exit_code"),
    [
        ("import sys\nsys.exit()", 0),
        ("raise SystemExit", 0),
        ("raise SystemExit(0)", 0),
        ("import sys\nsys.exit(3)", 3),
    ],
)
def test_persistent_session_guest_exit_does_not_hang_run(code, exit_code):
    session = MicroPythonSession()
    try:
        assert session.run("x = 1\nprint(x)").stdout == "1\n"
        with pytest.raises(
            MicroPythonWasmError, match=f"stopped: guest exited with code {exit_code}"
        ):
            run_with_deadline(session, code)

        # The VM is gone, so later runs fail promptly as well.
        with pytest.raises(
            MicroPythonWasmError, match=f"stopped: guest exited with code {exit_code}"
        ):
            run_with_deadline(session, "print(x)")
    finally:
        session.close()

    assert session._thread is not None
    assert not session._thread.is_alive()


def test_persistent_session_waiting_run_is_released_when_session_closes():
    # A run() that slipped past the closed check, and queued its request after
    # the guest loop was told to close, must not wait for a reply that can
    # never come.
    session = MicroPythonSession()
    session.run("x = 1")
    session.close()

    outcome = {}

    def target():
        try:
            session._wait_for_result("never-answered")
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(30)

    assert not thread.is_alive()
    assert isinstance(outcome["error"], MicroPythonSessionClosed)


# Guest code that calls the session's own control function directly, the way
# the bootstrap loop does, and returns what the host sent back.
CALL_SESSION_CONTROL = """
import host as _host, json as _json

def _session_control(payload):
    return _json.loads(_host.call("__session_exchange__", payload))
"""

REPORT_SUCCESS = """'{"args": [{"ok": true}], "kwargs": {}}'"""


def test_guest_calling_session_control_cannot_stall_or_desynchronise_run():
    session = MicroPythonSession()
    try:
        # The guest ends its own request early and swallows the next one.
        first = run_with_deadline(
            session,
            CALL_SESSION_CONTROL
            + f"""
print("before")
_message = _session_control({REPORT_SUCCESS})["value"]
print("after", _message["op"], sorted(_message))
""",
        )
        assert first.stdout == "before\n"

        # Everything the guest prints while this request is in flight belongs
        # to it. Nothing printed during the first request leaks in, and the
        # guest was not given a request id to forge a reply with.
        second = run_with_deadline(session, "print('never executed')")
        assert second.stdout == "after run ['code', 'op']\n"

        # The bootstrap loop is back in step afterwards.
        third = run_with_deadline(session, "print('third')")
        assert third.stdout == "third\n"
    finally:
        session.close()


@pytest.mark.parametrize(
    "payload",
    [
        "'{}'",
        """'{"args": [], "kwargs": {}}'""",
        """'{"args": ["not a result"], "kwargs": {}}'""",
        """'{"args": [{"ok": 1}], "kwargs": {}}'""",
    ],
)
def test_guest_calling_session_control_without_a_result_fails_that_run(payload):
    session = MicroPythonSession()
    try:
        with pytest.raises(
            MicroPythonWasmError, match="ended the request without reporting a result"
        ):
            run_with_deadline(
                session, CALL_SESSION_CONTROL + f"_session_control({payload})"
            )
    finally:
        session.close()


def test_guest_calling_session_control_with_bad_arguments_changes_nothing():
    session = MicroPythonSession()
    try:
        result = run_with_deadline(
            session,
            CALL_SESSION_CONTROL
            + """
_response = _session_control('{"args": [1, 2, 3], "kwargs": {}}')
print(_response["ok"], _response["error"].split(":")[0])
""",
        )
        assert result.stdout == "False TypeError\n"
        assert run_with_deadline(session, "print('next')").stdout == "next\n"
    finally:
        session.close()


def test_guest_calling_session_control_stays_inside_resource_limits():
    session = MicroPythonSession()
    try:
        # The guest takes over the next request and then spins. The fuel budget
        # restarts when that request is delivered, so it is still enforced.
        run_with_deadline(
            session,
            CALL_SESSION_CONTROL
            + f"""
_session_control({REPORT_SUCCESS})
while True:
    pass
""",
        )
        with pytest.raises(MicroPythonWasmError, match="all fuel consumed"):
            run_with_deadline(session, "print('never executed')")
    finally:
        session.close()


def test_guest_calling_session_control_cannot_block_close():
    session = MicroPythonSession()
    # The guest swallows the close message and then asks for more work.
    run_with_deadline(
        session,
        CALL_SESSION_CONTROL
        + f"""
print(_session_control({REPORT_SUCCESS})["value"]["op"])
_session_control({REPORT_SUCCESS})
print("still running")
""",
    )

    closer = threading.Thread(target=session.close, daemon=True)
    closer.start()
    closer.join(30)

    assert not closer.is_alive(), "session.close() did not return"
    assert session._thread is not None
    assert not session._thread.is_alive()


def test_session_control_function_name_is_reserved():
    session = MicroPythonSession()
    try:
        with pytest.raises(ValueError, match="reserved"):
            session.register_function(lambda: None, name="__session_exchange__")
        with pytest.raises(ValueError, match="reserved"):
            MicroPythonSession(host_functions={"__session_exchange__": lambda: None})

        # The loop still works, and is still driven by the real control function.
        assert session.run("print(1)").stdout == "1\n"
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
        assert session._thread_error is not None
        assert session._thread_error.__traceback__ is None
        assert session._thread_error.__cause__ is None
        assert session._thread_error.__context__ is None
    finally:
        session.close()


def test_persistent_session_has_a_wall_timeout_by_default():
    session = MicroPythonSession()
    try:
        assert session.wall_timeout_seconds == DEFAULT_SESSION_WALL_TIMEOUT_SECONDS
        assert DEFAULT_SESSION_WALL_TIMEOUT_SECONDS > 0
        assert session.run("print(1)").stdout == "1\n"
    finally:
        session.close()


def test_persistent_session_wall_timeout_interrupts_sleeping_guest():
    # See test_wall_timeout_interrupts_blocking_guest for why this is 30.
    session = MicroPythonSession(wall_timeout_seconds=0.5)
    started = time.monotonic()
    try:
        with pytest.raises(
            MicroPythonWasmError, match="guest trapped: wall-clock timeout exceeded"
        ):
            session.run("import time\ntime.sleep(30)")
    finally:
        session.close()

    assert time.monotonic() - started < 15


def test_persistent_session_sleep_within_wall_timeout_keeps_state():
    session = MicroPythonSession(wall_timeout_seconds=30)
    try:
        session.run("import time\nx = 7\ntime.sleep(0.2)")
        # Each run gets a fresh deadline, so earlier sleeps do not count.
        session.run("time.sleep(0.2)")
        assert session.run("print(x + 1)").stdout == "8\n"
    finally:
        session.close()


def test_persistent_session_close_wakes_sleeping_guest():
    session = MicroPythonSession(wall_timeout_seconds=None)
    session.run("import time")
    outcome = {}

    def target():
        try:
            session.run("time.sleep(30)")
        except BaseException as exc:
            outcome["error"] = exc

    runner = threading.Thread(target=target, daemon=True)
    runner.start()
    time.sleep(0.5)

    started = time.monotonic()
    session.close()
    runner.join(15)

    assert time.monotonic() - started < 15
    assert not runner.is_alive()
    assert isinstance(outcome["error"], MicroPythonWasmError)
    assert session._thread is not None
    assert not session._thread.is_alive()


def test_persistent_session_guest_exception_keeps_output_and_traceback():
    session = MicroPythonSession()
    try:
        session.run("def fail():\n    raise ValueError('boom')")
        with pytest.raises(MicroPythonWasmError, match="^ValueError: boom$") as info:
            session.run("print('before')\nfail()")

        assert info.value.stdout == "before\n"
        # The frame of the session's own bootstrap loop is left out.
        assert info.value.stderr.splitlines() == [
            "Traceback (most recent call last):",
            '  File "<string>", line 2, in <module>',
            '  File "<string>", line 2, in fail',
            "ValueError: boom",
        ]

        # The next run starts with clean output.
        result = session.run("print('after')")
        assert (result.stdout, result.stderr) == ("after\n", "")
    finally:
        session.close()


def test_persistent_session_trap_keeps_output_written_before_the_trap():
    session = MicroPythonSession()
    try:
        with pytest.raises(MicroPythonWasmError, match="guest trapped") as info:
            session.run("print('before')\nwhile True:\n    pass")

        assert info.value.stdout == "before\n"
    finally:
        session.close()


def test_persistent_session_stopped_property_follows_the_vm():
    session = MicroPythonSession()
    assert not session.stopped  # not started yet
    try:
        session.run("x = 1")
        assert not session.stopped

        with pytest.raises(MicroPythonWasmError, match="ValueError") as info:
            session.run("raise ValueError('still alive')")
        assert info.value.exit_code is None
        assert not session.stopped

        with pytest.raises(MicroPythonWasmError, match="guest trapped") as info:
            session.run("while True:\n    pass")
        assert info.value.exit_code is None
        assert session.stopped
        assert not session.closed
    finally:
        session.close()

    assert session.stopped


def test_persistent_session_guest_exit_reports_exit_code():
    session = MicroPythonSession()
    try:
        with pytest.raises(MicroPythonWasmError) as info:
            run_with_deadline(session, "print('bye')\nimport sys\nsys.exit(3)")
        assert info.value.exit_code == 3
        assert info.value.stdout == "bye\n"
        assert session.stopped

        with pytest.raises(MicroPythonWasmError) as info:
            run_with_deadline(session, "print(1)")
        assert info.value.exit_code == 3
    finally:
        session.close()


def test_persistent_session_heap_is_sized_from_memory_bytes():
    heap_size = "import gc\nprint(gc.mem_free() + gc.mem_alloc())"
    for megabytes in (2, 16):
        memory_bytes = megabytes * 1024 * 1024
        session = MicroPythonSession(memory_bytes=memory_bytes, fuel=2_000_000_000)
        try:
            heap = int(session.run(heap_size).stdout)
        finally:
            session.close()

        assert 0.95 * (memory_bytes - 400 * 1024) < heap < memory_bytes
