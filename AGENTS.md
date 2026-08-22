# Repository Notes

- Use PowerShell on Windows; on macOS/Linux prefix the var inline (`PYTHONPATH=src python ...`).
- Install runtime deps with `pip install -r requirements.txt` (includes `coverage`).
- Run tests with `$env:PYTHONPATH="src"; python -m unittest discover -s tests`.
- Run coverage with `$env:PYTHONPATH="src"; python -m coverage run -m unittest discover -s tests` then
  `python -m coverage report`. The gate is **80% line coverage** (`fail_under` in `pyproject.toml`)
  and must pass before merge.
- Run a cheap syntax/import check with `$env:PYTHONPATH="src"; python -m compileall src tests`.
- There is no configured lint tool in this repo at the moment.
- Do not read or print `.env`; use `.env.example` for public config shape.
- The execution venue is **BingX Perpetual Futures VST only**. Live BingX endpoints fail closed.
