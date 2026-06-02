from __future__ import annotations

import pytest

from micropython_wasm import MicroPythonWasmError, default_wasm_path, run

pytestmark = pytest.mark.skipif(
    not default_wasm_path().exists(),
    reason="packaged MicroPython WASI artifact is not built",
)


def run_stdout(code: str, **kwargs) -> str:
    result = run(code, wall_timeout_seconds=None, **kwargs)
    assert result.stderr == ""
    assert result.fuel_remaining > 0
    return result.stdout


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (
            """
print(1 + 2 * 3)
print(2 ** 10)
print(17 // 5, 17 % 5)
print(True and not False)
""",
            "7\n1024\n3 2\nTrue\n",
        ),
        (
            """
value = 12345678901234567890 * 9
print(value)
print(value // 10)
""",
            "111111110111111111010\n11111111011111111101\n",
        ),
        (
            """
text = "Hello, MicroPython"
print(text.lower())
print(text.replace("Micro", "WASI"))
print(",".join(["a", "b", "c"]))
print(b"abc".hex())
""",
            "hello, micropython\nHello, WASIPython\na,b,c\n616263\n",
        ),
        (
            """
items = [3, 1, 2]
items.append(5)
print(items)
print(tuple(reversed(items)))
print(sorted({x * x for x in range(5)}))
""",
            "[3, 1, 2, 5]\n(5, 2, 1, 3)\n[0, 1, 4, 9, 16]\n",
        ),
        (
            """
data = {"b": 2, "a": 1}
data["c"] = 3
print(sorted(data.items()))
print(sorted(set("banana")))
""",
            "[('a', 1), ('b', 2), ('c', 3)]\n['a', 'b', 'n']\n",
        ),
        (
            """
print([x * 2 for x in range(4)])
print({x: x * x for x in range(3)})
print(sum(x for x in range(6) if x % 2))
""",
            "[0, 2, 4, 6]\n{0: 0, 1: 1, 2: 4}\n9\n",
        ),
        (
            """
def describe(name, count=1, *, suffix="!"):
    return "{}:{}{}".format(name, count, suffix)

print(describe("task"))
print(describe("task", 3, suffix="?"))
print((lambda x: x + 4)(6))
""",
            "task:1!\ntask:3?\n10\n",
        ),
        (
            """
def make_counter(start):
    value = start
    def next_value():
        nonlocal value
        value += 1
        return value
    return next_value

counter = make_counter(10)
print(counter())
print(counter())
""",
            "11\n12\n",
        ),
        (
            """
def factorial(n):
    if n <= 1:
        return 1
    return n * factorial(n - 1)

print(factorial(6))
""",
            "720\n",
        ),
        (
            """
class Base:
    label = "base"

    def greet(self):
        return self.label

class Thing(Base):
    def __init__(self, value):
        self.value = value

    @property
    def doubled(self):
        return self.value * 2

item = Thing(7)
item.label = "thing"
print(item.greet())
print(item.doubled)
print(isinstance(item, Base))
""",
            "thing\n14\nTrue\n",
        ),
        (
            """
try:
    raise ValueError("boom")
except ValueError as ex:
    print(type(ex).__name__)
    print(str(ex))
finally:
    print("finally")
""",
            "ValueError\nboom\nfinally\n",
        ),
        (
            """
class Recorder:
    def __enter__(self):
        print("enter")
        return self

    def __exit__(self, exc_type, exc, tb):
        print("exit", exc_type is None)

with Recorder():
    print("inside")
""",
            "enter\ninside\nexit True\n",
        ),
        (
            """
print(list(enumerate(["a", "b"], 5)))
print(list(zip([1, 2], ["one", "two"])))
print(sorted(["aaa", "b", "cc"], key=len))
""",
            "[(5, 'a'), (6, 'b')]\n[(1, 'one'), (2, 'two')]\n['b', 'cc', 'aaa']\n",
        ),
    ],
)
def test_core_python_language_features(code, expected):
    assert run_stdout(code) == expected


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (
            """
import math
print(math.sqrt(81))
print(math.sin(0))
""",
            "9.0\n0.0\n",
        ),
        (
            """
import json
payload = json.dumps({"a": [1, 2], "ok": True})
print(payload)
print(json.loads(payload)["a"][1])
""",
            '{"a": [1, 2], "ok": true}\n2\n',
        ),
        (
            """
import re
match = re.match("h.*o", "hello")
print(match.group(0))
print(re.sub("[0-9]+", "#", "item-123"))
""",
            "hello\nitem-#\n",
        ),
        (
            """
import binascii
print(binascii.hexlify(b"hello"))
print(binascii.unhexlify("6869"))
""",
            "b'68656c6c6f'\nb'hi'\n",
        ),
        (
            """
import sys
print(sys.argv[:2])
print(sys.platform)
""",
            "['-c']\nlinux\n",
        ),
    ],
)
def test_standard_library_subset(code, expected):
    assert run_stdout(code) == expected


def test_instances_are_fresh_between_runs():
    assert run_stdout("x = 42\nprint(x)") == "42\n"
    assert run_stdout("""
try:
    print(x)
except NameError:
    print("missing")
""") == "missing\n"


def test_readonly_preopened_directory_allows_reading(tmp_path):
    (tmp_path / "hello.txt").write_text("hello from wasi\n")

    assert (
        run_stdout(
            """
import os
print(os.listdir("/input"))
print(open("/input/hello.txt").read())
""",
            readonly_dir=tmp_path,
        )
        == "['hello.txt']\nhello from wasi\n\n"
    )


def test_readonly_preopened_directory_rejects_writes(tmp_path):
    (tmp_path / "hello.txt").write_text("hello from wasi\n")

    with pytest.raises(MicroPythonWasmError, match="guest exited with code 1"):
        run(
            'open("/input/new.txt", "w").write("nope")',
            readonly_dir=tmp_path,
            wall_timeout_seconds=None,
        )


def test_low_fuel_interrupts_infinite_loop():
    with pytest.raises(MicroPythonWasmError, match="guest trapped"):
        run("while True:\n    pass", fuel=50_000, wall_timeout_seconds=None)


def test_uncaught_guest_exception_is_reported_as_guest_failure():
    with pytest.raises(
        MicroPythonWasmError, match="guest exited with code 1|guest trapped"
    ):
        run('raise ValueError("boom")', wall_timeout_seconds=None)
