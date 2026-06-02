## Bottom line

**Use `wasmtime-py`, but do not use MicroPython’s official browser/Node WebAssembly artifact directly.** The official MicroPython WebAssembly port builds with Emscripten and produces a `micropython.mjs` JavaScript wrapper plus `micropython.wasm`; it is designed around JS glue, Emscripten FS, and browser/Node APIs, not as a clean WASI module for direct `wasmtime-py` execution. ([GitHub][1])

The architecture I would pursue is:

> **Python host → `wasmtime` pip package → custom/pinned MicroPython WASI `.wasm` → fresh instance per execution**

That gets you clean `pip install wasmtime` runtime installation, memory/fuel/epoch controls, deny-by-default files/network, and a clean path to later shipping your own wheel containing the MicroPython WASI bundle. `wasmtime-py` is the official Python embedding for Wasmtime and is installed with `pip install wasmtime`. ([PyPI][2])

## What I found about MicroPython’s current WASM story

MicroPython itself is a compact Python 3 implementation for constrained environments. It implements Python 3.4 syntax plus selected later features like `async`/`await`, includes common built-ins and modules, and supports scripts as `.py` or precompiled `.mpy`, but it is not full CPython compatibility. ([GitHub][3])

MicroPython’s official `ports/webassembly` target is real and current. Its README says the build outputs `micropython.mjs` and `micropython.wasm`, and it documents Node/browser use through the JS wrapper, including `runPython`, `runPythonAsync`, `pyimport`, `registerJsModule`, `FS`, and a browser-side example that imports `js.fetch`. ([GitHub][1]) The Makefile for that port uses `emcc` and Emscripten JS flags, which is the key reason I would not treat that `.wasm` as a normal WASI module. ([GitHub][4])

There is also an official-ish package on npm/CDN, `@micropython/micropython-webassembly-pyscript`, currently showing a MicroPython WebAssembly PyScript variant with `micropython.mjs`, `micropython.wasm`, and related variants such as `settrace` and `ulab`. ([UNPKG][5]) That package is useful evidence that the artifact is small and shippable, but it is still an Emscripten/JS-wrapper artifact, not the clean server-side WASI artifact you want.

The promising route is MicroPython’s Unix/WASI work. There is an open MicroPython PR titled “Exprimental WASI support for ports/unix,” with discussion around building a `VARIANT=wasi`; later discussion indicates the toolchain patch blocker had been resolved upstream. ([GitHub][6]) I would treat that PR/branch as the starting point for a pinned experimental build, or wait for/track upstream if it lands.

## Recommended design

### 1. Build or obtain a WASI MicroPython artifact

You want a `.wasm` that imports WASI, not Emscripten JS glue.

Good target shape:

```text
micropython-wasi.wasm
  imports: wasi_snapshot_preview1 or newer WASI interfaces
  exports: _start, or a custom run_code(...) export
  no Emscripten JS runtime dependency
```

There are two practical execution modes:

**Simpler first version:** run MicroPython as a WASI command each time, passing code via `argv`, stdin, or a temporary preopened file. For example, conceptually:

```text
micropython -c "print(1 + 1)"
```

This is easy to sandbox because every execution gets a fresh Wasmtime `Store` and instance.

**Better service version:** add a tiny C shim to the MicroPython build that exports something like:

```c
int run_code(const char *code, size_t len);
```

Then the Python host writes code into guest memory and calls that export. This is more work, but faster if you need lots of executions.

For untrusted code, I would still default to **fresh instance per execution**. It avoids leaked globals, polluted imports, and retained heap state.

### 2. Use Wasmtime for host-side controls

Wasmtime gives you the resource controls you asked for:

| Requirement                                | Wasmtime/MicroPython approach                                                                                                                                                                                                                   |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Execute safely inside MicroPython via WASM | Run MicroPython as WASI Wasm inside Wasmtime, with no host imports except WASI.                                                                                                                                                                 |
| Control memory                             | Use `Store.set_limits(memory_size=...)`; this caps WebAssembly linear memory and makes instantiation or `memory.grow` fail when the threshold is exceeded. ([Bytecode Alliance][7])                                                             |
| Control CPU                                | Enable `Config.consume_fuel`, then call `Store.set_fuel(...)`; Wasmtime traps when fuel runs out. ([Bytecode Alliance][7])                                                                                                                      |
| Wall-clock timeout                         | Enable epoch interruption and call `Store.set_epoch_deadline(...)`; increment the engine epoch from the host. Wasmtime documents fuel and epochs as its two interruption mechanisms. ([Wasmtime][8])                                            |
| No files                                   | Do not call `WasiConfig.preopen_dir`; do not inherit host stdin/env/argv except what you explicitly set. WASI directory access is granted through preopened directories. ([Bytecode Alliance][7])                                               |
| Selective files                            | Preopen a temp directory as read-only or read-write with `DirPerms`/`FilePerms`. ([Bytecode Alliance][7])                                                                                                                                       |
| No network                                 | Do not expose socket/network host imports; do not use the browser/JS `fetch` bridge; do not call `set_wasi_http`. Wasmtime’s WASI HTTP support is explicit and must be configured before components using `wasi:http`. ([Bytecode Alliance][7]) |
| Selective network                          | Add a narrow host function or WASI component HTTP layer with allowlists, max bytes, and timeouts.                                                                                                                                               |

Important caveat: `memory_size` limits guest linear memory, not total host RSS. Wasmtime JIT/runtime memory, compiled code cache, Python process overhead, and host callbacks are outside that number. For hostile multi-tenant use, I would also run each execution in a worker process with OS-level RSS/time limits.

## Minimal host skeleton

This assumes you have a **WASI MicroPython** module, not the official Emscripten `micropython.wasm`.

```python
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from wasmtime import (
    Config,
    Engine,
    ExitTrap,
    Linker,
    Module,
    Store,
    WasiConfig,
    WasmtimeError,
    Trap,
    DirPerms,
    FilePerms,
)


class MicroPythonWasiSandboxError(RuntimeError):
    pass


def run_micropython_wasi(
    code: str,
    wasm_path: str | Path,
    *,
    memory_bytes: int = 16 * 1024 * 1024,
    fuel: int = 5_000_000,
    wall_timeout_seconds: Optional[float] = 1.0,
    readonly_dir: Optional[str | Path] = None,
) -> tuple[str, str, int]:
    """
    Run code in a fresh MicroPython WASI instance.

    Assumptions:
      - wasm_path points to a WASI MicroPython command module.
      - The module supports argv like: micropython -c <code>.
      - The module exports _start.

    Returns:
      (stdout, stderr, fuel_remaining)
    """

    cfg = Config()
    cfg.consume_fuel = True
    cfg.epoch_interruption = True
    cfg.max_wasm_stack = 512 * 1024

    engine = Engine(cfg)
    store = Store(engine)

    # Limits WebAssembly resources in this store.
    store.set_limits(
        memory_size=memory_bytes,
        instances=1,
        memories=1,
        tables=8,
        table_elements=10_000,
    )
    store.set_fuel(fuel)

    if wall_timeout_seconds is not None:
        # Trap after the engine epoch advances by 1.
        store.set_epoch_deadline(1)

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []

    def capture_stdout(data: bytes) -> None:
        stdout_parts.append(bytes(data).decode("utf-8", "replace"))

    def capture_stderr(data: bytes) -> None:
        stderr_parts.append(bytes(data).decode("utf-8", "replace"))

    wasi = WasiConfig()
    wasi.argv = ["micropython", "-c", code]
    wasi.env = []  # Do not inherit host environment.
    wasi.stdout_custom = capture_stdout
    wasi.stderr_custom = capture_stderr

    # Optional read-only file capability.
    # Do not preopen anything for a no-files sandbox.
    if readonly_dir is not None:
        wasi.preopen_dir(
            str(readonly_dir),
            "/input",
            dir_perms=DirPerms.READ_ONLY,
            file_perms=FilePerms.READ_ONLY,
        )

    store.set_wasi(wasi)

    linker = Linker(engine)
    linker.define_wasi()

    module = Module.from_file(engine, str(wasm_path))

    timeout_timer: Optional[threading.Timer] = None
    if wall_timeout_seconds is not None:
        timeout_timer = threading.Timer(wall_timeout_seconds, engine.increment_epoch)
        timeout_timer.daemon = True
        timeout_timer.start()

    try:
        instance = linker.instantiate(store, module)
        start = instance.exports(store).get("_start")
        if start is None:
            raise MicroPythonWasiSandboxError("WASI module does not export _start")
        start(store)

    except ExitTrap as exc:
        # WASI programs often terminate via proc_exit, surfaced as ExitTrap.
        if getattr(exc, "code", 0) not in (0, None):
            raise MicroPythonWasiSandboxError(f"guest exited with code {exc.code}") from exc

    except Trap as exc:
        raise MicroPythonWasiSandboxError(f"guest trapped: {exc}") from exc

    except WasmtimeError as exc:
        raise MicroPythonWasiSandboxError(f"wasmtime error: {exc}") from exc

    finally:
        if timeout_timer is not None:
            timeout_timer.cancel()

    return "".join(stdout_parts), "".join(stderr_parts), store.get_fuel()
```

For file-like input, the safest first implementation is: create a temporary directory, write only the files you want the guest to see, and preopen that directory at `/input` as read-only. Never preopen `/`, your project root, `$HOME`, or a shared temp directory.

## Network capability design

For the first secure version, I would build MicroPython with no socket capability and expose no network imports. The official browser WebAssembly examples use the JS bridge to call `fetch`, but that is exactly the kind of host capability you should avoid in a server sandbox unless you explicitly mediate it. ([GitHub][1])

For selective network later, prefer a custom host API such as:

```python
sandbox_http_get(url: str) -> bytes
```

and enforce all policy in the host:

```text
allowed schemes: https only
allowed hosts: explicit allowlist
max response bytes: e.g. 256 KiB
timeout: e.g. 1 s
redirects: disabled or tightly bounded
headers: stripped/controlled
no cookies, no ambient credentials
```

I would not expose raw sockets. A narrow HTTP function is easier to audit, meter, and log.

## Engine comparison

**Wasmtime is the best fit.** It has an actively maintained `wasmtime` Python package, WASI support, fuel, epoch interruption, memory limits, stdio capture, explicit preopened-directory permissions, and explicit WASI HTTP configuration. ([PyPI][2])

**Wasmer Python is possible but less attractive here.** The Python package exists and is pip-installable with a separate compiler package, but I would not choose it first because the Wasmtime Python API exposes the exact resource-control knobs you need more directly. ([PyPI][9])

**WasmEdge Python does not look like a good fit for this project right now.** The PyPI package I found is an old placeholder-style package, and the WasmEdge Python SDK docs are marked work-in-progress. ([PyPI][10])

## Packaging plan

At runtime, your install can be clean:

```toml
# pyproject.toml
[project]
name = "micropython-wasi-sandbox"
dependencies = [
  "wasmtime>=30",  # pin more tightly once tested
]

[tool.setuptools.package-data]
micropython_wasi_sandbox = ["wasm/micropython-wasi.wasm"]
```

Package layout:

```text
micropython_wasi_sandbox/
  __init__.py
  runner.py
  wasm/
    micropython-wasi.wasm
```

Load the bundled artifact with `importlib.resources`:

```python
from importlib.resources import files

wasm_path = files("micropython_wasi_sandbox").joinpath("wasm/micropython-wasi.wasm")
```

The build toolchain for MicroPython/WASI does not need to be pip-installable if you only ship the resulting `.wasm` in your wheel. Your runtime story remains:

```bash
pip install micropython-wasi-sandbox
```

or:

```bash
pip install wasmtime your-micropython-wasm-bundle
```

## Test matrix I would use before trusting it

Run these as automated tests against the exact `.wasm` you plan to ship:

```python
print(1 + 1)
```

Expected: works.

```python
while True:
    pass
```

Expected: traps by fuel or epoch deadline.

```python
x = bytearray(10**9)
```

Expected: fails/traps without exceeding the configured Wasm memory cap or host RSS policy.

```python
open("/etc/passwd").read()
```

Expected: fails. With no preopens, there should be no host filesystem namespace.

```python
open("/input/example.txt").read()
```

Expected: works only when you deliberately preopen a temp directory at `/input`.

```python
import socket
```

Expected: fails unless you intentionally built/exposed a network capability.

## My recommendation

Proceed with **Wasmtime + a MicroPython WASI build**, not the official Emscripten WebAssembly bundle. The official bundle is useful as a reference and maybe as a quick Node-based prototype, but it does not match your “Python tools, pip-installable, resource-controlled, deny-files/network-by-default” goals.

The main engineering work is getting a pinned MicroPython WASI artifact. Once you have that, the sandbox host is straightforward: fresh `Store` per run, fuel enabled, memory limits set, epoch timeout, no inherited env, no preopened dirs by default, no network imports, and narrowly designed host capabilities only when explicitly requested.

[1]: https://github.com/micropython/micropython/blob/master/ports/webassembly/README.md "micropython/ports/webassembly/README.md at master · micropython/micropython · GitHub"
[2]: https://pypi.org/project/wasmtime/ "wasmtime · PyPI"
[3]: https://github.com/micropython/micropython "GitHub - micropython/micropython: MicroPython - a lean and efficient Python implementation for microcontrollers and constrained systems · GitHub"
[4]: https://raw.githubusercontent.com/micropython/micropython/master/ports/webassembly/Makefile "raw.githubusercontent.com"
[5]: https://app.unpkg.com/%40micropython/micropython-webassembly-pyscript%401.28.0-6 "UNPKG"
[6]: https://github.com/micropython/micropython/pull/13676 "Exprimental WASI support for ports/unix by yamt · Pull Request #13676 · micropython/micropython · GitHub"
[7]: https://bytecodealliance.github.io/wasmtime-py/ "wasmtime API documentation"
[8]: https://docs.wasmtime.dev/examples-interrupting-wasm.html "Interrupting Execution - Wasmtime"
[9]: https://pypi.org/project/wasmer/ "wasmer · PyPI"
[10]: https://pypi.org/project/wasmedge/ "wasmedge · PyPI"

