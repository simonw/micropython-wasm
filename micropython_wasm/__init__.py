from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

__all__ = [
    "MicroPythonWasmError",
    "MicroPythonWasmArtifactNotFound",
    "MicroPythonSession",
    "MicroPythonSessionClosed",
    "RunResult",
    "default_wasm_path",
    "run",
    "run_micropython_wasi",
]


class MicroPythonWasmError(RuntimeError):
    """Base error raised by micropython-wasm."""


class MicroPythonWasmArtifactNotFound(MicroPythonWasmError):
    """Raised when the MicroPython WASI artifact cannot be found."""


class MicroPythonSessionClosed(MicroPythonWasmError):
    """Raised when code is run after a session has been closed."""


@dataclass(frozen=True)
class RunResult:
    stdout: str
    stderr: str
    fuel_remaining: int


def default_wasm_path() -> Path:
    """Return the package location expected to contain micropython-wasi.wasm."""

    return Path(__file__).parent / "artifacts" / "micropython-wasi.wasm"


class MicroPythonSession:
    """
    Transcript-backed MicroPython session.

    The current WASI artifact exposes a command-style ``_start`` entry point,
    not an incremental eval function. This class preserves state by replaying
    previous successful snippets before each new snippet and returning only the
    output produced by the newest snippet.
    """

    def __init__(
        self,
        wasm_path: str | Path | None = None,
        *,
        memory_bytes: int = 16 * 1024 * 1024,
        fuel: int = 5_000_000,
        wall_timeout_seconds: Optional[float] = 1.0,
        readonly_dir: str | Path | None = None,
        host_functions: Mapping[str, Callable[..., object]] | None = None,
    ) -> None:
        self.wasm_path = Path(wasm_path) if wasm_path is not None else default_wasm_path()
        self.memory_bytes = memory_bytes
        self.fuel = fuel
        self.wall_timeout_seconds = wall_timeout_seconds
        self.readonly_dir = readonly_dir
        self._snippets: list[str] = []
        self._preamble: list[str] = []
        self._host_functions: dict[str, Callable[..., object]] = {}
        self._closed = False
        for name, func in (host_functions or {}).items():
            self.register_function(name, func)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def snippets(self) -> tuple[str, ...]:
        return tuple(self._snippets)

    @property
    def host_functions(self) -> Mapping[str, Callable[..., object]]:
        return dict(self._host_functions)

    def register_function(
        self,
        name_or_func: str | Callable[..., object],
        func: Callable[..., object] | None = None,
    ) -> None:
        if self._closed:
            raise MicroPythonSessionClosed("MicroPythonSession is closed")

        if func is None:
            if not callable(name_or_func):
                raise TypeError("register_function() expected a callable")
            func = name_or_func
            name = getattr(func, "__name__", "")
        else:
            name = str(name_or_func)

        if not name.isidentifier():
            raise ValueError(f"host function name is not a valid Python identifier: {name!r}")

        self._host_functions[name] = func
        wrapper = _host_function_wrapper_code(name)
        self._preamble = [
            preamble for preamble in self._preamble if not preamble.startswith(f"# host:{name}\n")
        ]
        self._preamble.append(wrapper)

    def run(self, code: str) -> RunResult:
        if self._closed:
            raise MicroPythonSessionClosed("MicroPythonSession is closed")

        marker = f"__micropython_wasm_session_{uuid.uuid4().hex}__"
        marker_line = _marker_code(marker)
        transcript = "\n\n".join([*self._preamble, *self._snippets, marker_line, code])
        result = run_micropython_wasi(
            transcript,
            self.wasm_path,
            memory_bytes=self.memory_bytes,
            fuel=self.fuel,
            wall_timeout_seconds=self.wall_timeout_seconds,
            readonly_dir=self.readonly_dir,
            host_functions=self._host_functions,
        )
        marker_with_newline = marker + "\n"
        if marker_with_newline not in result.stdout:
            raise MicroPythonWasmError("session marker was not found in guest stdout")

        self._snippets.append(code)
        return RunResult(
            stdout=result.stdout.split(marker_with_newline, 1)[1],
            stderr=result.stderr,
            fuel_remaining=result.fuel_remaining,
        )

    def close(self) -> None:
        self._snippets.clear()
        self._closed = True

    def __enter__(self) -> MicroPythonSession:
        if self._closed:
            raise MicroPythonSessionClosed("MicroPythonSession is closed")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def _marker_code(marker: str) -> str:
    return (
        "import sys as __micropython_wasm_sys\n"
        f"__micropython_wasm_sys.stdout.write({marker!r} + '\\n')"
    )


def _host_function_wrapper_code(name: str) -> str:
    return f"""# host:{name}
import json as __micropython_wasm_json
import host as __micropython_wasm_host

def {name}(*args, **kwargs):
    __micropython_wasm_payload = __micropython_wasm_json.dumps({{"args": args, "kwargs": kwargs}})
    __micropython_wasm_response = __micropython_wasm_json.loads(
        __micropython_wasm_host.call({name!r}, __micropython_wasm_payload)
    )
    if not __micropython_wasm_response.get("ok"):
        raise RuntimeError(__micropython_wasm_response.get("error", "host function failed"))
    return __micropython_wasm_response.get("value")
"""


def run(
    code: str,
    wasm_path: str | Path | None = None,
    *,
    memory_bytes: int = 16 * 1024 * 1024,
    fuel: int = 5_000_000,
    wall_timeout_seconds: Optional[float] = 1.0,
    readonly_dir: str | Path | None = None,
    host_functions: Mapping[str, Callable[..., object]] | None = None,
) -> RunResult:
    """Run MicroPython code in a fresh WASI WebAssembly instance."""

    return run_micropython_wasi(
        code,
        wasm_path or default_wasm_path(),
        memory_bytes=memory_bytes,
        fuel=fuel,
        wall_timeout_seconds=wall_timeout_seconds,
        readonly_dir=readonly_dir,
        host_functions=host_functions,
    )


def run_micropython_wasi(
    code: str,
    wasm_path: str | Path,
    *,
    memory_bytes: int = 16 * 1024 * 1024,
    fuel: int = 5_000_000,
    wall_timeout_seconds: Optional[float] = 1.0,
    readonly_dir: str | Path | None = None,
    host_functions: Mapping[str, Callable[..., object]] | None = None,
) -> RunResult:
    """
    Run code through a WASI MicroPython command module.

    The wasm module is expected to behave like ``micropython -c <code>`` and
    export ``_start``. Each call creates a fresh Wasmtime engine, store, WASI
    config, and module instance.
    """

    wasm_path = Path(wasm_path)
    if not wasm_path.exists():
        raise MicroPythonWasmArtifactNotFound(
            f"MicroPython WASI artifact not found: {wasm_path}. "
            "Build it with scripts/build_micropython_wasi.py or pass wasm_path."
        )
    if not wasm_path.is_file():
        raise MicroPythonWasmError(f"MicroPython WASI artifact is not a file: {wasm_path}")
    if memory_bytes <= 0:
        raise ValueError("memory_bytes must be greater than zero")
    if fuel <= 0:
        raise ValueError("fuel must be greater than zero")
    if wall_timeout_seconds is not None and wall_timeout_seconds <= 0:
        raise ValueError("wall_timeout_seconds must be greater than zero or None")

    try:
        from wasmtime import Config, Engine, ExitTrap, Func, FuncType, Linker, Module, Store
        from wasmtime import Trap, ValType, WasiConfig, WasmtimeError
    except ImportError as exc:  # pragma: no cover - dependency metadata should install it
        raise MicroPythonWasmError(
            "The wasmtime package is required. Install micropython-wasm with dependencies."
        ) from exc

    cfg = Config()
    cfg.consume_fuel = True
    cfg.epoch_interruption = wall_timeout_seconds is not None
    cfg.wasm_exceptions = True
    cfg.max_wasm_stack = 512 * 1024

    engine = Engine(cfg)
    store = Store(engine)
    store.set_limits(
        memory_size=memory_bytes,
        instances=1,
        memories=1,
        tables=8,
        table_elements=10_000,
    )
    store.set_fuel(fuel)

    if wall_timeout_seconds is not None:
        store.set_epoch_deadline(1)

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []

    def capture_stdout(data: bytes) -> None:
        stdout_parts.append(bytes(data).decode("utf-8", "replace"))

    def capture_stderr(data: bytes) -> None:
        stderr_parts.append(bytes(data).decode("utf-8", "replace"))

    wasi = WasiConfig()
    wasi.argv = ["micropython", "-c", code]
    wasi.env = []
    wasi.stdout_custom = capture_stdout
    wasi.stderr_custom = capture_stderr

    if readonly_dir is not None:
        readonly_path = Path(readonly_dir)
        if not readonly_path.is_dir():
            raise MicroPythonWasmError(f"readonly_dir is not a directory: {readonly_path}")
        try:
            from wasmtime import DirPerms, FilePerms
        except ImportError:
            wasi.preopen_dir(str(readonly_path), "/input")
        else:
            wasi.preopen_dir(
                str(readonly_path),
                "/input",
                dir_perms=DirPerms.READ_ONLY,
                file_perms=FilePerms.READ_ONLY,
            )

    store.set_wasi(wasi)

    linker = Linker(engine)
    linker.define_wasi()
    _define_host_call(linker, store, dict(host_functions or {}), Func, FuncType, ValType)

    timer: threading.Timer | None = None
    if wall_timeout_seconds is not None:
        timer = threading.Timer(wall_timeout_seconds, engine.increment_epoch)
        timer.daemon = True
        timer.start()

    try:
        module = Module.from_file(engine, str(wasm_path))
        instance = linker.instantiate(store, module)
        start = instance.exports(store).get("_start")
        if start is None:
            raise MicroPythonWasmError("WASI module does not export _start")
        start(store)
    except ExitTrap as exc:
        if getattr(exc, "code", 0) not in (0, None):
            raise MicroPythonWasmError(f"guest exited with code {exc.code}") from exc
    except Trap as exc:
        raise MicroPythonWasmError(f"guest trapped: {exc}") from exc
    except WasmtimeError as exc:
        raise MicroPythonWasmError(f"wasmtime error: {exc}") from exc
    finally:
        if timer is not None:
            timer.cancel()

    return RunResult(
        stdout="".join(stdout_parts),
        stderr="".join(stderr_parts),
        fuel_remaining=store.get_fuel(),
    )


def _define_host_call(linker, store, host_functions, func_cls, func_type_cls, val_type_cls) -> None:
    def host_call(
        caller,
        name_ptr: int,
        name_len: int,
        payload_ptr: int,
        payload_len: int,
        result_ptr: int,
        result_cap: int,
    ) -> int:
        memory = caller.get("memory")
        if memory is None:
            return -1

        try:
            name = bytes(memory.read(caller, name_ptr, name_ptr + name_len)).decode("utf-8")
            payload = bytes(memory.read(caller, payload_ptr, payload_ptr + payload_len)).decode(
                "utf-8"
            )
            request = json.loads(payload)
            args = request.get("args", [])
            kwargs = request.get("kwargs", {})
            func = host_functions[name]
            value = func(*args, **kwargs)
            response = {"ok": True, "value": value}
        except Exception as exc:
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        try:
            response_bytes = json.dumps(response, separators=(",", ":")).encode("utf-8")
        except Exception as exc:
            response_bytes = json.dumps(
                {
                    "ok": False,
                    "error": f"{type(exc).__name__}: host result is not JSON serializable",
                },
                separators=(",", ":"),
            ).encode("utf-8")

        if len(response_bytes) > result_cap:
            return len(response_bytes)
        memory.write(caller, response_bytes, result_ptr)
        return len(response_bytes)

    ty = func_type_cls(
        [
            val_type_cls.i32(),
            val_type_cls.i32(),
            val_type_cls.i32(),
            val_type_cls.i32(),
            val_type_cls.i32(),
            val_type_cls.i32(),
        ],
        [val_type_cls.i32()],
    )
    linker.define(
        store,
        "micropython_wasm",
        "host_call",
        func_cls(store, ty, host_call, access_caller=True),
    )
