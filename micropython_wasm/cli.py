from __future__ import annotations

import argparse
import ast
import codeop
import sys
from pathlib import Path
from typing import Callable, Protocol, Sequence, TextIO

from . import (
    DEFAULT_FUEL,
    DEFAULT_MEMORY_BYTES,
    DEFAULT_SESSION_WALL_TIMEOUT_SECONDS,
    MicroPythonSession,
    MicroPythonWasmError,
    RunResult,
    run,
)

DEFAULT_RUN_WALL_TIMEOUT_SECONDS = 1.0


class ReplSession(Protocol):
    """The parts of MicroPythonSession the REPL uses."""

    @property
    def stopped(self) -> bool: ...

    def run(self, code: str) -> RunResult: ...

    def close(self) -> None: ...


Execute = Callable[..., RunResult]
SessionFactory = Callable[..., ReplSession]


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    execute: Execute = run,
    session_factory: SessionFactory = MicroPythonSession,
) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr

    parser = argparse.ArgumentParser(prog="micropython-wasm")
    parser.add_argument(
        "--memory",
        dest="memory_bytes",
        type=_positive_int,
        default=DEFAULT_MEMORY_BYTES,
        metavar="BYTES",
        help=f"WebAssembly memory limit in bytes (default: {DEFAULT_MEMORY_BYTES})",
    )
    parser.add_argument(
        "--fuel",
        type=_positive_int,
        default=DEFAULT_FUEL,
        metavar="COUNT",
        help=f"Wasmtime fuel budget per run (default: {DEFAULT_FUEL})",
    )
    parser.add_argument(
        "--timeout",
        type=_timeout_seconds,
        default=None,
        metavar="SECONDS",
        help=(
            "wall-clock limit in seconds, or 0 for no limit (default: "
            f"{DEFAULT_RUN_WALL_TIMEOUT_SECONDS:g} for -c and files, "
            f"{DEFAULT_SESSION_WALL_TIMEOUT_SECONDS:g} for each line entered "
            "in the REPL)"
        ),
    )
    parser.add_argument(
        "-c",
        dest="code",
        metavar="CODE",
        help="execute MicroPython code and exit",
    )
    parser.add_argument(
        "file",
        nargs="?",
        help="MicroPython source file to execute",
    )

    args = parser.parse_args(argv)
    if args.code is not None and args.file is not None:
        parser.error("cannot use -c and a file at the same time")

    options: dict[str, object] = {
        "memory_bytes": args.memory_bytes,
        "fuel": args.fuel,
    }
    if args.timeout is not None:
        # Left out unless asked for, so each mode keeps its own default.
        options["wall_timeout_seconds"] = args.timeout or None

    if args.code is not None:
        return _run_code(args.code, execute, stdout, stderr, options)

    if args.file is not None:
        try:
            code = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"micropython-wasm: {exc}", file=stderr)
            return 1
        return _run_code(code, execute, stdout, stderr, options)

    return _repl(stdin, stdout, stderr, session_factory, options)


def _run_code(
    code: str,
    execute: Execute,
    stdout: TextIO,
    stderr: TextIO,
    options: dict[str, object],
) -> int:
    try:
        result = execute(code, **options)
    except MicroPythonWasmError as exc:
        # Show what the guest printed before it failed, including the
        # traceback MicroPython writes to stderr for an uncaught exception.
        stdout.write(exc.stdout)
        stderr.write(exc.stderr)
        if exc.exit_code is not None:
            # The guest exited by itself, through sys.exit() or an uncaught
            # exception. Pass its status on as a MicroPython binary would.
            return exc.exit_code
        print(f"micropython-wasm: {exc}", file=stderr)
        return 1

    _write_result(result, stdout, stderr)
    return 0


def _repl(
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    session_factory: SessionFactory,
    options: dict[str, object],
) -> int:
    buffer: list[str] = []
    session = session_factory(**options)
    try:
        while True:
            stdout.write("... " if buffer else ">>> ")
            stdout.flush()

            line = stdin.readline()
            if line == "":
                if hasattr(stdin, "isatty") and stdin.isatty():
                    stdout.write("\n")
                return 0

            line = line.rstrip("\n")
            if not buffer and line in {"exit()", "quit()"}:
                return 0

            buffer.append(line)
            source = "\n".join(buffer)
            if not _source_is_complete(source):
                continue

            buffer.clear()
            if not source.strip():
                continue

            try:
                result = session.run(_repl_code(source))
            except MicroPythonWasmError as exc:
                stdout.write(exc.stdout)
                stderr.write(exc.stderr)
                if session.stopped and exc.exit_code is not None:
                    # Guest code exited the VM, for example with sys.exit().
                    return exc.exit_code
                # An uncaught guest exception already ends its traceback with
                # the same "Type: message" line that the error carries.
                if not exc.stderr.rstrip().endswith(str(exc)):
                    print(_brief(exc), file=stderr)
                if session.stopped:
                    # A trap or timeout ends the VM. Carry on with a new one
                    # rather than failing every line from here on.
                    session.close()
                    session = session_factory(**options)
                    print(
                        "micropython-wasm: session restarted, "
                        "variables and imports were lost",
                        file=stderr,
                    )
                continue
            _write_result(result, stdout, stderr)
    finally:
        session.close()


def _source_is_complete(source: str) -> bool:
    # "single" is the interactive grammar: a compound statement such as a def
    # is only complete once it is followed by a blank line, so its body can
    # span several lines.
    try:
        return codeop.compile_command(source, symbol="single") is not None
    except (OverflowError, SyntaxError, ValueError):
        return True


def _brief(exc: MicroPythonWasmError) -> str:
    """Reduce a Wasmtime trap, which comes with a wasm backtrace, to its cause."""

    lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
    if len(lines) <= 1:
        return str(exc)
    return lines[-1]


def _positive_int(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _timeout_seconds(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number of seconds") from exc
    if not 0 <= parsed < float("inf"):
        raise argparse.ArgumentTypeError("must be zero or a positive number")
    return parsed


def _repl_code(source: str) -> str:
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError:
        return source

    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Expr):
        return source

    expression = ast.get_source_segment(source, tree.body[0]) or source.strip()
    return (
        f"__micropython_wasm_repl_value = ({expression})\n"
        "if __micropython_wasm_repl_value is not None:\n"
        "    print(repr(__micropython_wasm_repl_value))"
    )


def _write_result(result: RunResult, stdout: TextIO, stderr: TextIO) -> None:
    stdout.write(result.stdout)
    stderr.write(result.stderr)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
