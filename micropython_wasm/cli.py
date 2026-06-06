from __future__ import annotations

import argparse
import ast
import codeop
import sys
from pathlib import Path
from typing import Callable, Sequence, TextIO

from . import DEFAULT_FUEL, MicroPythonSession, MicroPythonWasmError, RunResult, run

DEFAULT_MEMORY_BYTES = 16 * 1024 * 1024

Execute = Callable[..., RunResult]
SessionFactory = Callable[..., MicroPythonSession]


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

    if args.code is not None:
        return _run_code(
            args.code,
            execute,
            stdout,
            stderr,
            memory_bytes=args.memory_bytes,
            fuel=args.fuel,
        )

    if args.file is not None:
        try:
            code = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"micropython-wasm: {exc}", file=stderr)
            return 1
        return _run_code(
            code,
            execute,
            stdout,
            stderr,
            memory_bytes=args.memory_bytes,
            fuel=args.fuel,
        )

    return _repl(
        stdin,
        stdout,
        stderr,
        session_factory,
        memory_bytes=args.memory_bytes,
        fuel=args.fuel,
    )


def _run_code(
    code: str,
    execute: Execute,
    stdout: TextIO,
    stderr: TextIO,
    *,
    memory_bytes: int,
    fuel: int,
) -> int:
    try:
        result = execute(code, memory_bytes=memory_bytes, fuel=fuel)
    except MicroPythonWasmError as exc:
        print(f"micropython-wasm: {exc}", file=stderr)
        return 1

    _write_result(result, stdout, stderr)
    return 0


def _repl(
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    session_factory: SessionFactory,
    *,
    memory_bytes: int,
    fuel: int,
) -> int:
    buffer: list[str] = []
    session = session_factory(memory_bytes=memory_bytes, fuel=fuel)
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
                print(exc, file=stderr)
                continue
            _write_result(result, stdout, stderr)
    finally:
        session.close()


def _source_is_complete(source: str) -> bool:
    try:
        return codeop.compile_command(source, symbol="exec") is not None
    except (OverflowError, SyntaxError, ValueError):
        return True


def _positive_int(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
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
