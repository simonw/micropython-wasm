from __future__ import annotations

from typing import Callable, cast

import pytest

from micropython_wasm import (
    MicroPythonReplaySession,
    MicroPythonSession,
    MicroPythonWasmError,
    default_wasm_path,
    run,
)

pytestmark = pytest.mark.skipif(
    not default_wasm_path().exists(),
    reason="packaged MicroPython WASI artifact is not built",
)


def test_session_exposes_registered_python_function():
    session = MicroPythonSession(wall_timeout_seconds=None)
    try:
        session.register_function(lambda a, b: a + b, name="add")

        result = session.run("print(add(2, 3))")
    finally:
        session.close()

    assert result.stdout == "5\n"


def test_session_can_register_function_from_callable_name_without_with():
    def shout(value):
        return value.upper() + "!"

    session = MicroPythonSession(wall_timeout_seconds=None)
    try:
        session.register_function(shout)
        assert session.run("print(shout('hello'))").stdout == "HELLO!\n"
    finally:
        session.close()


def test_session_can_register_function_with_custom_name():
    def add(a, b):
        return a + b

    session = MicroPythonSession(wall_timeout_seconds=None)
    try:
        session.register_function(add, name="plus")
        assert session.run("print(plus(2, 3))").stdout == "5\n"
    finally:
        session.close()


def test_session_exposes_host_function_with_kwargs():
    def format_name(first, last, uppercase=False):
        result = f"{first} {last}"
        if uppercase:
            result = result.upper()
        return result

    session = MicroPythonSession(
        host_functions={"format_name": format_name},
        wall_timeout_seconds=None,
    )
    try:
        result = session.run(
            "print(format_name('Ada', last='Lovelace', uppercase=True))"
        )
    finally:
        session.close()

    assert result.stdout == "ADA LOVELACE\n"


def test_session_host_function_can_return_json_values():
    def describe(value):
        return {"value": value, "doubled": value * 2, "tags": ["host", "python"]}

    session = MicroPythonSession(
        host_functions={"describe": describe}, wall_timeout_seconds=None
    )
    try:
        result = session.run("""
data = describe(4)
print(data["value"])
print(data["doubled"])
print(",".join(data["tags"]))
""")
    finally:
        session.close()

    assert result.stdout == "4\n8\nhost,python\n"


def test_run_host_function_allows_256k_results_by_default():
    result = run(
        """
import host
print(len(host.call("large", '{"args": [], "kwargs": {}}')))
""",
        host_functions={"large": lambda: "x" * (128 * 1024)},
        wall_timeout_seconds=None,
    )

    assert result.stdout == "131094\n"


def test_run_host_result_bytes_can_lower_result_limit():
    result = run(
        """
import host
try:
    host.call("large", '{"args": [], "kwargs": {}}')
except ValueError as ex:
    print(str(ex))
""",
        host_functions={"large": lambda: "x" * 2048},
        host_result_bytes=1024,
        wall_timeout_seconds=None,
    )

    assert result.stdout == "host callback result too large\n"


def test_session_host_result_bytes_can_lower_result_limit():
    session = MicroPythonSession(
        host_functions={"large": lambda: "x" * 2048},
        host_result_bytes=1024,
        wall_timeout_seconds=None,
    )
    try:
        with pytest.raises(
            MicroPythonWasmError, match="ValueError: host callback result too large"
        ):
            session.run("large()")
    finally:
        session.close()


def test_session_host_function_result_can_be_used_as_session_state():
    session = MicroPythonSession(
        host_functions={"add": lambda a, b: a + b}, wall_timeout_seconds=None
    )
    try:
        assert session.run("total = add(10, 5)\nprint(total)").stdout == "15\n"
        assert session.run("print(total * 2)").stdout == "30\n"
    finally:
        session.close()


def test_session_host_function_exception_can_be_caught_in_micropython():
    def fail():
        raise ValueError("bad host value")

    session = MicroPythonSession(
        host_functions={"fail": fail}, wall_timeout_seconds=None
    )
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


def test_register_function_requires_valid_identifier():
    session = MicroPythonSession(wall_timeout_seconds=None)

    with pytest.raises(ValueError):
        session.register_function(lambda: None, name="not-valid")

    with pytest.raises(TypeError):
        session.register_function(cast(Callable[..., object], "not-callable"))


def test_raw_host_call_module_is_available_to_micropython():
    result = run(
        """
import host
print(host.call("missing", "{}"))
""",
        wall_timeout_seconds=None,
    )

    assert result.stdout == '{"ok":false,"error":"KeyError: \'missing\'"}\n'


def test_session_host_functions_survive_syntax_error_in_first_run():
    session = MicroPythonSession(host_functions={"add": lambda a, b: a + b})
    session.register_function(lambda value: value.upper(), name="shout")
    try:
        with pytest.raises(MicroPythonWasmError, match="SyntaxError"):
            session.run("x = (")

        assert session.run("print(add(1, 2), shout('hi'))").stdout == "3 HI\n"
    finally:
        session.close()


def test_session_host_function_wrappers_do_not_shift_traceback_line_numbers():
    session = MicroPythonSession(host_functions={"add": lambda a, b: a + b})
    try:
        with pytest.raises(MicroPythonWasmError, match="ValueError: boom") as info:
            session.run("total = add(1, 2)\nraise ValueError('boom')")

        assert '  File "<string>", line 2, in <module>' in info.value.stderr
        assert session.run("print(total)").stdout == "3\n"
    finally:
        session.close()


def test_session_function_registered_twice_before_start_uses_latest():
    session = MicroPythonSession(host_functions={"value": lambda: "first"})
    session.register_function(lambda: "second", name="value")
    try:
        assert session.run("print(value())").stdout == "second\n"
    finally:
        session.close()


@pytest.mark.parametrize(
    "session_class", [MicroPythonSession, MicroPythonReplaySession]
)
@pytest.mark.parametrize("name", ["class", "def", "None", "import", "__debug__"])
def test_register_function_rejects_reserved_words(session_class, name):
    session = session_class()
    try:
        with pytest.raises(ValueError, match="reserved word"):
            session.register_function(lambda: None, name=name)
        with pytest.raises(ValueError, match="reserved word"):
            session_class(host_functions={name: lambda: None})

        # The rejected name left nothing behind, so the session still works.
        assert name not in session.host_functions
        session.register_function(lambda: "fine", name="okay")
        assert session.run("print(okay())").stdout == "fine\n"
    finally:
        session.close()


def test_register_function_accepts_soft_keywords_and_builtin_names():
    session = MicroPythonSession()
    try:
        session.register_function(lambda: "m", name="match")
        session.register_function(lambda: "t", name="type")
        assert session.run("print(match(), type())").stdout == "m t\n"
    finally:
        session.close()
