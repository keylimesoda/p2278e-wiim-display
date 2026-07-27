# Contributing

Thanks for helping make the display easier to build and adapt.

## Development setup

```bash
cd pi-app
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q
./scripts/build_spectrum_analyzer.sh
```

## Before opening a pull request

- Keep state/reducer and rendering logic hardware-independent when possible.
- Add tests for behavior, failure paths, and any security boundary changed.
- Never commit `config.json`, `run/`, cache/log data, WiiM URLs, tokens, or
  household/device identifiers.
- Run `python -m pytest -q`, `python -m py_compile ...`, `bash -n scripts/*.sh`,
  the native analyzer build, and `git diff --check`.
- Describe any physical-panel verification separately from hardware-free tests.
- Preserve third-party license notices for vendored assets.

## Good first changes

- New panel/pinout profiles behind the matrix abstraction
- Better setup diagnostics and clearer errors
- Deterministic renderer fixtures/golden tests
- Documentation for alternate enclosures and diffusers
- Reproducible Raspberry Pi image/release tooling

## Pull requests

Keep changes focused and explain the user-visible behavior, test evidence, and
hardware tested. Large behavior changes should update the appliance spec or
state transition map.
