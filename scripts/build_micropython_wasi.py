#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
from pathlib import Path

DEFAULT_REPO_URL = "https://github.com/micropython/micropython.git"
DEFAULT_WORK_DIR = Path("/tmp/micropython-wasm-build")
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "micropython_wasm"
    / "artifacts"
    / "micropython-wasi.wasm"
)
DEFAULT_USER_C_MODULES = (
    Path(__file__).resolve().parents[1] / "micropython_wasm" / "usercmodule"
)


class BuildError(RuntimeError):
    pass


def run_command(
    command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def ensure_repo(repo_url: str, ref: str, work_dir: Path, clean: bool) -> Path:
    repo_dir = work_dir / "micropython"
    if clean and repo_dir.exists():
        shutil.rmtree(repo_dir)

    work_dir.mkdir(parents=True, exist_ok=True)
    if repo_dir.exists():
        run_command(["git", "fetch", "--tags", "--prune", "origin"], cwd=repo_dir)
    else:
        run_command(["git", "clone", repo_url, str(repo_dir)])

    checkout = subprocess.run(["git", "checkout", ref], cwd=repo_dir)
    if checkout.returncode != 0:
        run_command(["git", "fetch", "origin", ref], cwd=repo_dir)
        run_command(["git", "checkout", "FETCH_HEAD"], cwd=repo_dir)
    return repo_dir


def build_micropython(
    repo_dir: Path,
    variant: str,
    jobs: int,
    extra_make_args: list[str],
    wasi_sdk: Path | None,
    user_c_modules: Path | None,
) -> None:
    job_arg = f"-j{jobs}"
    mpy_cross_args: list[str] = []
    if platform.system() == "Darwin":
        # The experimental WASI branch currently trips this newer Apple Clang
        # warning in host-only mpy-cross builds while using -Werror.
        mpy_cross_args.append("CFLAGS_EXTRA=-Wno-error=gnu-folding-constant")
    run_command(["make", "-C", str(repo_dir / "mpy-cross"), job_arg, *mpy_cross_args])
    unix_make_args = [f"VARIANT={variant}", *extra_make_args]
    if wasi_sdk is not None:
        unix_make_args.append(f"WASI_SDK={wasi_sdk}")
    if user_c_modules is not None:
        unix_make_args.append(f"USER_C_MODULES={user_c_modules}")

    run_command(
        ["make", "-C", str(repo_dir / "ports" / "unix"), "submodules", *unix_make_args]
    )
    unix_dir = repo_dir / "ports" / "unix"
    try:
        run_command(
            [
                "make",
                "-C",
                str(unix_dir),
                job_arg,
                *unix_make_args,
            ]
        )
    except subprocess.CalledProcessError:
        raw_artifact = unix_dir / f"build-{variant}" / "micropython"
        if variant != "wasi" or not raw_artifact.exists():
            raise
        translated_artifact = raw_artifact.with_suffix(".exnref")
        run_command(
            [
                "wasm-opt",
                "--translate-to-exnref",
                "--enable-exception-handling",
                "-o",
                str(translated_artifact),
                str(raw_artifact),
            ]
        )


def is_wasm_file(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.suffix == ".wasm":
        return True
    return path.read_bytes()[:4] == b"\0asm"


def find_wasm_artifact(repo_dir: Path, variant: str) -> Path:
    candidates = [
        repo_dir / "ports" / "unix" / f"build-{variant}" / "micropython.spilled.exnref",
        repo_dir / "ports" / "unix" / f"build-{variant}" / "micropython.exnref",
        repo_dir / "ports" / "unix" / f"build-{variant}" / "micropython.wasm",
        repo_dir / "ports" / "unix" / f"build-{variant}" / "micropython",
    ]
    for candidate in candidates:
        if is_wasm_file(candidate):
            return candidate

    matches = sorted((repo_dir / "ports" / "unix").glob(f"build-{variant}/**/*.wasm"))
    if not matches:
        matches = sorted((repo_dir / "ports" / "unix").glob("build*/**/*.wasm"))
    if not matches:
        raise BuildError(
            "MicroPython build finished, but no .wasm artifact was found under "
            f"{repo_dir / 'ports' / 'unix'}. If upstream changed its output path, "
            "rerun with --extra-make-arg V=1 and inspect the build directory."
        )
    return matches[0]


def copy_artifact(source: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, output)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a WASI MicroPython wasm artifact from a MicroPython checkout."
    )
    parser.add_argument("--repo-url", default=DEFAULT_REPO_URL)
    parser.add_argument(
        "--ref",
        default="master",
        help=(
            "MicroPython git ref to build. Use a pinned commit or an experimental WASI "
            "PR ref such as pull/<number>/head when needed."
        ),
    )
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--variant", default="wasi")
    parser.add_argument(
        "--user-c-modules",
        type=Path,
        default=DEFAULT_USER_C_MODULES,
        help="Path to MicroPython USER_C_MODULES directory.",
    )
    parser.add_argument(
        "--wasi-sdk",
        type=Path,
        default=None,
        help="Path to a wasi-sdk directory. Overrides the variant default.",
    )
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    parser.add_argument("--clean", action="store_true")
    parser.add_argument(
        "--extra-make-arg",
        action="append",
        default=[],
        help="Additional argument forwarded to the ports/unix make invocation.",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Only clone/checkout and locate an existing artifact.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.jobs <= 0:
        raise SystemExit("--jobs must be greater than zero")

    repo_dir = ensure_repo(args.repo_url, args.ref, args.work_dir, args.clean)
    if not args.skip_build:
        build_micropython(
            repo_dir,
            args.variant,
            args.jobs,
            args.extra_make_arg,
            args.wasi_sdk,
            args.user_c_modules,
        )

    artifact = find_wasm_artifact(repo_dir, args.variant)
    copy_artifact(artifact, args.output)
    print(f"Copied {artifact} to {args.output}")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from exc
    except BuildError as exc:
        raise SystemExit(str(exc)) from exc
