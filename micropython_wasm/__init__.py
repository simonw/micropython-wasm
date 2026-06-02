from __future__ import annotations

import json
import queue
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

DEFAULT_HOST_RESULT_BYTES = 256 * 1024
DEFAULT_FUEL = 20_000_000

__all__ = [
    "MicroPythonWasmError",
    "MicroPythonWasmArtifactNotFound",
    "MicroPythonSession",
    "MicroPythonReplaySession",
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


_THREAD_BOOTSTRAP = r"""
import host as __micropython_wasm_host
import json as __micropython_wasm_json

def __micropython_wasm_call(name, *args, **kwargs):
    payload = __micropython_wasm_json.dumps({"args": args, "kwargs": kwargs})
    response = __micropython_wasm_json.loads(__micropython_wasm_host.call(name, payload))
    if not response.get("ok"):
        raise RuntimeError(response.get("error", "host function failed"))
    return response.get("value")

while True:
    __micropython_wasm_message = __micropython_wasm_call("__session_next__")
    if __micropython_wasm_message.get("op") == "close":
        break

    __micropython_wasm_request_id = __micropython_wasm_message["id"]
    __micropython_wasm_code = __micropython_wasm_message["code"]
    try:
        exec(__micropython_wasm_code, globals())
        __micropython_wasm_call(
            "__session_result__",
            {"id": __micropython_wasm_request_id, "ok": True},
        )
    except Exception as __micropython_wasm_ex:
        __micropython_wasm_call(
            "__session_result__",
            {
                "id": __micropython_wasm_request_id,
                "ok": False,
                "error": type(__micropython_wasm_ex).__name__ + ": " + str(__micropython_wasm_ex),
            },
        )
"""


class MicroPythonReplaySession:
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
        fuel: int = DEFAULT_FUEL,
        wall_timeout_seconds: Optional[float] = 1.0,
        readonly_dir: str | Path | None = None,
        host_functions: Mapping[str, Callable[..., object]] | None = None,
        host_result_bytes: int = DEFAULT_HOST_RESULT_BYTES,
    ) -> None:
        self.wasm_path = (
            Path(wasm_path) if wasm_path is not None else default_wasm_path()
        )
        self.memory_bytes = memory_bytes
        self.fuel = fuel
        self.wall_timeout_seconds = wall_timeout_seconds
        self.readonly_dir = readonly_dir
        self.host_result_bytes = host_result_bytes
        self._snippets: list[str] = []
        self._preamble: list[str] = []
        self._host_functions: dict[str, Callable[..., object]] = {}
        self._closed = False
        for name, func in (host_functions or {}).items():
            self.register_function(func, name=name)

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
        func: Callable[..., object],
        *,
        name: str | None = None,
    ) -> None:
        if self._closed:
            raise MicroPythonSessionClosed("MicroPythonReplaySession is closed")

        if not callable(func):
            raise TypeError("register_function() expected a callable")
        name = name or getattr(func, "__name__", "")

        if not name.isidentifier():
            raise ValueError(
                f"host function name is not a valid Python identifier: {name!r}"
            )

        self._host_functions[name] = func
        wrapper = _host_function_wrapper_code(name)
        self._preamble = [
            preamble
            for preamble in self._preamble
            if not preamble.startswith(f"# host:{name}\n")
        ]
        self._preamble.append(wrapper)

    def run(self, code: str) -> RunResult:
        if self._closed:
            raise MicroPythonSessionClosed("MicroPythonReplaySession is closed")

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
            host_result_bytes=self.host_result_bytes,
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

    def __enter__(self) -> MicroPythonReplaySession:
        if self._closed:
            raise MicroPythonSessionClosed("MicroPythonReplaySession is closed")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class MicroPythonSession:
    """
    Persistent MicroPython session backed by a background Wasmtime thread.

    A bootstrap loop runs inside one MicroPython VM, blocks on a host callback
    for the next code snippet, and executes snippets in the same globals dict.
    """

    def __init__(
        self,
        wasm_path: str | Path | None = None,
        *,
        memory_bytes: int = 16 * 1024 * 1024,
        fuel: int = DEFAULT_FUEL,
        wall_timeout_seconds: Optional[float] = None,
        readonly_dir: str | Path | None = None,
        host_functions: Mapping[str, Callable[..., object]] | None = None,
        host_result_bytes: int = DEFAULT_HOST_RESULT_BYTES,
    ) -> None:
        self.wasm_path = (
            Path(wasm_path) if wasm_path is not None else default_wasm_path()
        )
        self.memory_bytes = memory_bytes
        self.fuel = fuel
        self.wall_timeout_seconds = wall_timeout_seconds
        self.readonly_dir = readonly_dir
        self.host_result_bytes = host_result_bytes
        self._host_functions: dict[str, Callable[..., object]] = {}
        self._pending_preamble: list[str] = []
        self._closed = False
        self._started = False
        self._thread: threading.Thread | None = None
        self._thread_error: BaseException | None = None
        self._request_queue: queue.Queue[dict[str, object]] = queue.Queue()
        self._result_queue: queue.Queue[dict[str, object]] = queue.Queue()
        self._run_lock = threading.Lock()
        self._callback_lock = threading.RLock()
        self._current_request_id: str | None = None
        self._stdout_parts: list[bytes] = []
        self._stderr_parts: list[bytes] = []
        self._engine = None
        self._store = None
        self._timeout_timer: threading.Timer | None = None
        self._thread_host_functions: dict[str, Callable[..., object]] | None = None
        for name, func in (host_functions or {}).items():
            self.register_function(func, name=name)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def host_functions(self) -> Mapping[str, Callable[..., object]]:
        with self._callback_lock:
            return dict(self._host_functions)

    def register_function(
        self,
        func: Callable[..., object],
        *,
        name: str | None = None,
    ) -> None:
        if self._closed:
            raise MicroPythonSessionClosed("MicroPythonSession is closed")

        if not callable(func):
            raise TypeError("register_function() expected a callable")
        name = name or getattr(func, "__name__", "")

        if not name.isidentifier():
            raise ValueError(
                f"host function name is not a valid Python identifier: {name!r}"
            )

        wrapper = _host_function_wrapper_code(name)
        with self._callback_lock:
            self._host_functions[name] = func
            if self._thread_host_functions is not None:
                self._thread_host_functions[name] = func

        if self._started:
            self.run(wrapper)
        else:
            self._pending_preamble = [
                preamble
                for preamble in self._pending_preamble
                if not preamble.startswith(f"# host:{name}\n")
            ]
            self._pending_preamble.append(wrapper)

    def run(self, code: str) -> RunResult:
        if self._closed:
            raise MicroPythonSessionClosed("MicroPythonSession is closed")
        self._ensure_started()

        with self._run_lock:
            request_id = uuid.uuid4().hex
            if self._pending_preamble:
                code = "\n\n".join([*self._pending_preamble, code])
                self._pending_preamble.clear()

            with self._callback_lock:
                self._current_request_id = request_id
                self._stdout_parts = []
                self._stderr_parts = []

            self._request_queue.put({"op": "run", "id": request_id, "code": code})

            while True:
                self._raise_thread_error_if_any()
                try:
                    result = self._result_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                if result.get("id") == request_id:
                    break

            with self._callback_lock:
                stdout = b"".join(self._stdout_parts).decode("utf-8", "replace")
                stderr = b"".join(self._stderr_parts).decode("utf-8", "replace")
                self._current_request_id = None

            fuel_remaining = -1
            if self._store is not None:
                try:
                    fuel_remaining = self._store.get_fuel()
                except Exception:
                    fuel_remaining = -1

            if not result.get("ok"):
                raise MicroPythonWasmError(
                    str(result.get("error", "guest execution failed"))
                )

            return RunResult(
                stdout=stdout, stderr=stderr, fuel_remaining=fuel_remaining
            )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._started:
            self._request_queue.put({"op": "close"})
        if self._thread is not None:
            self._thread.join()

    def __enter__(self) -> MicroPythonSession:
        if self._closed:
            raise MicroPythonSessionClosed("MicroPythonSession is closed")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _ensure_started(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

    def _raise_thread_error_if_any(self) -> None:
        if self._thread_error is not None:
            raise MicroPythonWasmError(
                f"MicroPythonSession stopped: {self._thread_error}"
            )

    def _thread_main(self) -> None:
        try:
            self._run_bootstrap()
        except BaseException as exc:
            exc.__traceback__ = None
            exc.__cause__ = None
            exc.__context__ = None
            self._thread_error = exc

    def _run_bootstrap(self) -> None:
        try:
            from wasmtime import (
                Config,
                Engine,
                ExitTrap,
                Func,
                FuncType,
                Linker,
                Module,
                Store,
            )
            from wasmtime import Trap, ValType, WasiConfig, WasmtimeError
        except (
            ImportError
        ) as exc:  # pragma: no cover - dependency metadata should install it
            raise MicroPythonWasmError(
                "The wasmtime package is required. Install micropython-wasm with dependencies."
            ) from exc

        _validate_execution_options(
            self.wasm_path,
            self.memory_bytes,
            self.fuel,
            self.wall_timeout_seconds,
            self.host_result_bytes,
        )

        cfg = Config()
        cfg.consume_fuel = True
        cfg.epoch_interruption = self.wall_timeout_seconds is not None
        cfg.wasm_exceptions = True
        cfg.max_wasm_stack = 512 * 1024

        engine = Engine(cfg)
        store = Store(engine)
        self._engine = engine
        self._store = store
        store.set_limits(
            memory_size=self.memory_bytes,
            instances=1,
            memories=1,
            tables=8,
            table_elements=10_000,
        )
        store.set_fuel(self.fuel)
        if self.wall_timeout_seconds is not None:
            store.set_epoch_deadline(1)

        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []

        def capture_stdout(data: bytes) -> None:
            data = bytes(data)
            with self._callback_lock:
                if self._current_request_id is None:
                    stdout_parts.append(data)
                else:
                    self._stdout_parts.append(data)

        def capture_stderr(data: bytes) -> None:
            data = bytes(data)
            with self._callback_lock:
                if self._current_request_id is None:
                    stderr_parts.append(data)
                else:
                    self._stderr_parts.append(data)

        wasi = WasiConfig()
        wasi.argv = ["micropython", "-c", _THREAD_BOOTSTRAP]
        wasi.env = []
        wasi.stdout_custom = capture_stdout
        wasi.stderr_custom = capture_stderr
        _configure_readonly_dir(wasi, self.readonly_dir, MicroPythonWasmError)
        store.set_wasi(wasi)

        linker = Linker(engine)
        linker.define_wasi()
        host_functions: dict[str, Callable[..., object]] = {
            "__session_next__": self._session_next,
            "__session_result__": self._session_result,
        }
        with self._callback_lock:
            host_functions.update(self._host_functions)
            self._thread_host_functions = host_functions
        _define_host_call(
            linker,
            store,
            host_functions,
            self.host_result_bytes,
            Func,
            FuncType,
            ValType,
        )

        try:
            try:
                module = Module.from_file(engine, str(self.wasm_path))
                instance = linker.instantiate(store, module)
                start = instance.exports(store).get("_start")
                if start is None:
                    raise MicroPythonWasmError("WASI module does not export _start")
                if not isinstance(start, Func):
                    raise MicroPythonWasmError(
                        "WASI module _start export is not callable"
                    )
                start(store)
            except ExitTrap as exc:
                if getattr(exc, "code", 0) not in (0, None):
                    raise MicroPythonWasmError(
                        f"guest exited with code {exc.code}"
                    ) from exc
            except Trap as exc:
                raise MicroPythonWasmError(f"guest trapped: {exc}") from exc
            except WasmtimeError as exc:
                raise MicroPythonWasmError(f"wasmtime error: {exc}") from exc
        finally:
            self._cancel_wall_timeout_timer()
            with self._callback_lock:
                self._engine = None
                self._store = None
                self._thread_host_functions = None

    def _session_next(self) -> dict[str, object]:
        request = self._request_queue.get()
        if request.get("op") == "run" and self._store is not None:
            self._store.set_fuel(self.fuel)
            if self.wall_timeout_seconds is not None:
                self._store.set_epoch_deadline(1)
                self._start_wall_timeout_timer()
        return request

    def _session_result(self, result: dict[str, object]) -> None:
        self._cancel_wall_timeout_timer()
        self._result_queue.put(result)
        return None

    def _start_wall_timeout_timer(self) -> None:
        self._cancel_wall_timeout_timer()
        if self._engine is None or self.wall_timeout_seconds is None:
            return

        timer = threading.Timer(self.wall_timeout_seconds, self._engine.increment_epoch)
        timer.daemon = True
        timer.start()
        self._timeout_timer = timer

    def _cancel_wall_timeout_timer(self) -> None:
        timer = self._timeout_timer
        self._timeout_timer = None
        if timer is None:
            return
        timer.cancel()
        if timer is not threading.current_thread():
            timer.join()


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
    fuel: int = DEFAULT_FUEL,
    wall_timeout_seconds: Optional[float] = 1.0,
    readonly_dir: str | Path | None = None,
    host_functions: Mapping[str, Callable[..., object]] | None = None,
    host_result_bytes: int = DEFAULT_HOST_RESULT_BYTES,
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
        host_result_bytes=host_result_bytes,
    )


def run_micropython_wasi(
    code: str,
    wasm_path: str | Path,
    *,
    memory_bytes: int = 16 * 1024 * 1024,
    fuel: int = DEFAULT_FUEL,
    wall_timeout_seconds: Optional[float] = 1.0,
    readonly_dir: str | Path | None = None,
    host_functions: Mapping[str, Callable[..., object]] | None = None,
    host_result_bytes: int = DEFAULT_HOST_RESULT_BYTES,
) -> RunResult:
    """
    Run code through a WASI MicroPython command module.

    The wasm module is expected to behave like ``micropython -c <code>`` and
    export ``_start``. Each call creates a fresh Wasmtime engine, store, WASI
    config, and module instance.
    """

    wasm_path = Path(wasm_path)
    _validate_execution_options(
        wasm_path, memory_bytes, fuel, wall_timeout_seconds, host_result_bytes
    )

    try:
        from wasmtime import (
            Config,
            Engine,
            ExitTrap,
            Func,
            FuncType,
            Linker,
            Module,
            Store,
        )
        from wasmtime import Trap, ValType, WasiConfig, WasmtimeError
    except (
        ImportError
    ) as exc:  # pragma: no cover - dependency metadata should install it
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

    stdout_parts: list[bytes] = []
    stderr_parts: list[bytes] = []

    def capture_stdout(data: bytes) -> None:
        stdout_parts.append(bytes(data))

    def capture_stderr(data: bytes) -> None:
        stderr_parts.append(bytes(data))

    wasi = WasiConfig()
    wasi.argv = ["micropython", "-c", code]
    wasi.env = []
    wasi.stdout_custom = capture_stdout
    wasi.stderr_custom = capture_stderr

    _configure_readonly_dir(wasi, readonly_dir, MicroPythonWasmError)

    store.set_wasi(wasi)

    linker = Linker(engine)
    linker.define_wasi()
    _define_host_call(
        linker,
        store,
        dict(host_functions or {}),
        host_result_bytes,
        Func,
        FuncType,
        ValType,
    )

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
        if not isinstance(start, Func):
            raise MicroPythonWasmError("WASI module _start export is not callable")
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
            timer.join()

    return RunResult(
        stdout=b"".join(stdout_parts).decode("utf-8", "replace"),
        stderr=b"".join(stderr_parts).decode("utf-8", "replace"),
        fuel_remaining=store.get_fuel(),
    )


def _validate_execution_options(
    wasm_path: Path,
    memory_bytes: int,
    fuel: int,
    wall_timeout_seconds: Optional[float],
    host_result_bytes: int,
) -> None:
    if not wasm_path.exists():
        raise MicroPythonWasmArtifactNotFound(
            f"MicroPython WASI artifact not found: {wasm_path}. "
            "Build it with scripts/build_micropython_wasi.py or pass wasm_path."
        )
    if not wasm_path.is_file():
        raise MicroPythonWasmError(
            f"MicroPython WASI artifact is not a file: {wasm_path}"
        )
    if memory_bytes <= 0:
        raise ValueError("memory_bytes must be greater than zero")
    if fuel <= 0:
        raise ValueError("fuel must be greater than zero")
    if wall_timeout_seconds is not None and wall_timeout_seconds <= 0:
        raise ValueError("wall_timeout_seconds must be greater than zero or None")
    if host_result_bytes <= 0:
        raise ValueError("host_result_bytes must be greater than zero")
    if host_result_bytes > 2_147_483_647:
        raise ValueError("host_result_bytes must fit in a WebAssembly i32")


def _configure_readonly_dir(wasi, readonly_dir: str | Path | None, error_cls) -> None:
    if readonly_dir is None:
        return
    readonly_path = Path(readonly_dir)
    if not readonly_path.is_dir():
        raise error_cls(f"readonly_dir is not a directory: {readonly_path}")
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


def _define_host_call(
    linker,
    store,
    host_functions,
    host_result_bytes,
    func_cls,
    func_type_cls,
    val_type_cls,
) -> None:
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
            name = bytes(memory.read(caller, name_ptr, name_ptr + name_len)).decode(
                "utf-8"
            )
            payload = bytes(
                memory.read(caller, payload_ptr, payload_ptr + payload_len)
            ).decode("utf-8")
            request = json.loads(payload)
            args = request.get("args", [])
            kwargs = request.get("kwargs", {})
            func = host_functions[name]
            value = func(*args, **kwargs)
            response = {"ok": True, "value": value}
        except Exception as exc:
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        try:
            response_bytes = json.dumps(
                response, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        except Exception as exc:
            response_bytes = json.dumps(
                {
                    "ok": False,
                    "error": f"{type(exc).__name__}: host result is not JSON serializable",
                },
                ensure_ascii=False,
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
    cap_ty = func_type_cls([], [val_type_cls.i32()])
    linker.define(
        store,
        "micropython_wasm",
        "host_result_cap",
        func_cls(store, cap_ty, lambda: host_result_bytes),
    )
