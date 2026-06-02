from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from micropython_wasm import (
    MicroPythonWasmArtifactNotFound,
    default_wasm_path,
    run,
    run_micropython_wasi,
)


def load_build_script():
    script_path = Path(__file__).parents[1] / "scripts" / "build_micropython_wasi.py"
    spec = importlib.util.spec_from_file_location("build_micropython_wasi", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_default_wasm_path_points_to_packaged_artifact_location():
    assert default_wasm_path().name == "micropython-wasi.wasm"
    assert default_wasm_path().parent.name == "artifacts"


def test_run_raises_clear_error_when_artifact_is_missing(tmp_path):
    missing_artifact = tmp_path / "missing.wasm"
    with pytest.raises(MicroPythonWasmArtifactNotFound, match="Build it with"):
        run("print(1 + 1)", wasm_path=missing_artifact)


def test_run_executes_packaged_artifact_when_available():
    if not default_wasm_path().exists():
        pytest.skip("packaged MicroPython WASI artifact is not built")

    result = run("print(1 + 1)")

    assert result.stdout == "2\n"
    assert result.stderr == ""


def test_run_micropython_wasi_validates_resource_limits_before_wasmtime(tmp_path):
    wasm_path = tmp_path / "micropython-wasi.wasm"
    wasm_path.write_bytes(b"\0asm")

    with pytest.raises(ValueError, match="memory_bytes"):
        run_micropython_wasi("print(1)", wasm_path, memory_bytes=0)

    with pytest.raises(ValueError, match="fuel"):
        run_micropython_wasi("print(1)", wasm_path, fuel=0)

    with pytest.raises(ValueError, match="wall_timeout_seconds"):
        run_micropython_wasi("print(1)", wasm_path, wall_timeout_seconds=0)


def test_build_script_finds_and_copies_unix_wasi_artifact(tmp_path):
    build_script = load_build_script()
    repo_dir = tmp_path / "micropython"
    artifact = repo_dir / "ports" / "unix" / "build-wasi" / "micropython.wasm"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"wasm")

    found = build_script.find_wasm_artifact(repo_dir, "wasi")
    assert found == artifact

    output = tmp_path / "package" / "micropython-wasi.wasm"
    build_script.copy_artifact(found, output)
    assert output.read_bytes() == b"wasm"


def test_build_script_accepts_wasm_artifact_without_extension(tmp_path):
    build_script = load_build_script()
    repo_dir = tmp_path / "micropython"
    artifact = repo_dir / "ports" / "unix" / "build-wasi" / "micropython"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"\0asm")

    assert build_script.find_wasm_artifact(repo_dir, "wasi") == artifact
