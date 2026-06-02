# micropython-wasm

[![PyPI](https://img.shields.io/pypi/v/micropython-wasm.svg)](https://pypi.org/project/micropython-wasm/)
[![Tests](https://github.com/simonw/micropython-wasm/actions/workflows/test.yml/badge.svg)](https://github.com/simonw/micropython-wasm/actions/workflows/test.yml)
[![Changelog](https://img.shields.io/github/v/release/simonw/micropython-wasm?include_prereleases&label=changelog)](https://github.com/simonw/micropython-wasm/releases)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/simonw/micropython-wasm/blob/main/LICENSE)

MicroPython packaged in WASM for wasmtime

## Installation

Install this library using `pip`:
```bash
pip install micropython-wasm
```
## Usage

Usage instructions go here.

## Development

To contribute to this library, first checkout the code. Then create a new virtual environment:
```bash
cd micropython-wasm
python -m venv venv
source venv/bin/activate
```
Now install the dependencies and test dependencies:
```bash
python -m pip install -e '.[test]'
```
To run the tests:
```bash
python -m pytest
```
