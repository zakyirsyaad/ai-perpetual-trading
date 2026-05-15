# Repository Notes

- Use PowerShell on Windows.
- Install runtime deps with `pip install -r requirements.txt`.
- Run tests with `$env:PYTHONPATH="src"; python -m unittest discover -s tests`.
- Run a cheap syntax/import check with `$env:PYTHONPATH="src"; python -m compileall src tests`.
- There is no configured lint tool in this repo at the moment.
- Do not read or print `.env`; use `.env.example` for public config shape.
