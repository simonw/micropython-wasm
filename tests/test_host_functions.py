from __future__ import annotations

from typing import Callable, cast

import pytest

from micropython_wasm import (
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
