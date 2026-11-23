from __future__ import annotations

import time

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
    (tmp_path / "hello.txt").write_text("hello from wasi\n", newline="\n")

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
    (tmp_path / "hello.txt").write_text("hello from wasi\n", newline="\n")

    with pytest.raises(MicroPythonWasmError, match="guest exited with code 1"):
        run(
            'open("/input/new.txt", "w").write("nope")',
            readonly_dir=tmp_path,
            wall_timeout_seconds=None,
        )


@pytest.mark.parametrize(
    "code",
    [
        'open("/input/new.txt", "w").write("nope")',
        'open("/input/hello.txt", "a").write("more")',
        'import os\nos.remove("/input/hello.txt")',
        'import os\nos.rename("/input/hello.txt", "/input/renamed.txt")',
        'import os\nos.mkdir("/input/subdir")',
    ],
)
def test_readonly_preopened_directory_is_not_modified(tmp_path, code):
    (tmp_path / "hello.txt").write_text("hello from wasi\n", newline="\n")

    with pytest.raises(MicroPythonWasmError, match="guest exited with code 1"):
        run(code, readonly_dir=tmp_path, wall_timeout_seconds=None)

    assert [path.name for path in tmp_path.iterdir()] == ["hello.txt"]
    assert (tmp_path / "hello.txt").read_bytes() == b"hello from wasi\n"


GC_PRESSURE_FUEL = 2_000_000_000


@pytest.mark.parametrize(
    "code",
    [
        # Module-level try block around ~3 MB of short-lived allocations, which
        # forces several collections of the ~1 MB MicroPython heap.
        """
keep = [str(i) * 10 for i in range(200)]
try:
    for i in range(3000):
        x = bytearray(1000)
except Exception as ex:
    print("unexpected", ex)
print(all(keep[i] == str(i) * 10 for i in range(200)))
""",
        # The allocating code runs in a callee of the frame holding the try.
        """
keep = [str(i) * 10 for i in range(200)]

def work():
    for i in range(3000):
        x = bytearray(1000)

try:
    work()
except Exception as ex:
    print("unexpected", ex)
print(all(keep[i] == str(i) * 10 for i in range(200)))
""",
        # Nested try/finally frames above the allocating code.
        """
keep = [str(i) * 10 for i in range(200)]

def work(depth):
    if depth:
        try:
            return work(depth - 1)
        finally:
            pass
    for i in range(3000):
        x = bytearray(1000)

work(3)
print(all(keep[i] == str(i) * 10 for i in range(200)))
""",
    ],
)
def test_garbage_collection_inside_try_block_does_not_corrupt_vm(code):
    # Regression test: an artifact built without "wasm-opt --spill-pointers"
    # hides pointers held in WebAssembly locals from the garbage collector, so
    # a collection inside a try block freed objects that were still in use.
    result = run(code, fuel=GC_PRESSURE_FUEL, wall_timeout_seconds=None)

    assert result.stdout == "True\n"
    assert result.stderr == ""


# The guest asks to block for 30 seconds. If blocking calls stopped honouring
# the wall timeout these tests would take that long and then fail, rather than
# hang the test run.
@pytest.mark.parametrize(
    "code",
    [
        "import time\ntime.sleep(30)",
        "import select\nselect.poll().poll(30000)",
    ],
)
def test_wall_timeout_interrupts_blocking_guest(code):
    started = time.monotonic()
    with pytest.raises(
        MicroPythonWasmError, match="guest trapped: wall-clock timeout exceeded"
    ):
        run(code, wall_timeout_seconds=0.5)

    assert time.monotonic() - started < 15


def test_sleep_within_wall_timeout_completes():
    result = run(
        """
import time
start = time.ticks_ms()
time.sleep(0.2)
time.sleep_ms(100)
time.sleep(0)
print(time.ticks_diff(time.ticks_ms(), start) >= 290)
""",
        wall_timeout_seconds=30,
    )

    assert result.stdout == "True\n"


def test_sleep_without_wall_timeout_completes():
    result = run(
        "import time\ntime.sleep(0.2)\nprint('awake')", wall_timeout_seconds=None
    )

    assert result.stdout == "awake\n"


def test_poll_reports_stdio_ready_without_blocking():
    started = time.monotonic()
    result = run(
        """
import select, sys
poller = select.poll()
poller.register(sys.stdin, select.POLLIN)
poller.register(sys.stdout, select.POLLOUT)
print(sorted(event for _, event in poller.poll(30000)))
print(repr(sys.stdin.read()))
print(select.poll().poll(50))
""",
        wall_timeout_seconds=None,
    )

    assert result.stdout == "[1, 4]\n''\n[]\n"
    assert time.monotonic() - started < 15


def test_low_fuel_interrupts_infinite_loop():
    with pytest.raises(MicroPythonWasmError, match="guest trapped"):
        run("while True:\n    pass", fuel=50_000, wall_timeout_seconds=None)


def test_uncaught_guest_exception_is_reported_as_guest_failure():
    with pytest.raises(
        MicroPythonWasmError, match="guest exited with code 1|guest trapped"
    ):
        run('raise ValueError("boom")', wall_timeout_seconds=None)


def test_uncaught_guest_exception_keeps_output_and_traceback():
    with pytest.raises(MicroPythonWasmError, match="guest exited with code 1") as info:
        run(
            """
print("before")

def fail():
    raise ValueError("boom")

fail()
""",
            wall_timeout_seconds=None,
        )

    assert info.value.stdout == "before\n"
    assert info.value.stderr.splitlines() == [
        "Traceback (most recent call last):",
        '  File "<stdin>", line 7, in <module>',
        '  File "<stdin>", line 5, in fail',
        "ValueError: boom",
    ]


def test_trapped_guest_keeps_output_written_before_the_trap():
    with pytest.raises(MicroPythonWasmError, match="guest trapped") as info:
        run("print('before')\nwhile True:\n    pass", wall_timeout_seconds=None)

    assert info.value.stdout == "before\n"
    assert info.value.stderr == ""


def test_errors_raised_before_guest_code_runs_have_empty_output(tmp_path):
    with pytest.raises(MicroPythonWasmError) as info:
        run("print(1)", wasm_path=tmp_path / "missing.wasm")

    assert info.value.stdout == ""
    assert info.value.stderr == ""
    assert MicroPythonWasmError("plain").stdout == ""


def test_error_exit_code_reports_how_the_guest_exited():
    with pytest.raises(MicroPythonWasmError) as info:
        run("import sys\nsys.exit(3)", wall_timeout_seconds=None)
    assert info.value.exit_code == 3

    with pytest.raises(MicroPythonWasmError) as info:
        run("raise ValueError('boom')", wall_timeout_seconds=None)
    assert info.value.exit_code == 1

    with pytest.raises(MicroPythonWasmError) as info:
        run("while True:\n    pass", fuel=200_000, wall_timeout_seconds=None)
    assert info.value.exit_code is None
