# Contributing

## Setup and checks

Use Python 3.12 and [uv](https://docs.astral.sh/uv/) (CI pins 0.7.19):

```powershell
uv sync --locked --python 3.12 --group dev
uv run --locked ado-ingest prepare-tokenizer --cache-dir .\.cache\tiktoken
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy src tests
uv run --locked pytest -q
uv build
```

Run the affected tests while developing and all five checks before opening a pull request.
Tests are offline and use synthetic content and fake Azure transports; do not add tests that
call Azure or the network.

## Ground rules

- Keep customer identifiers, endpoints, tenant and service-connection IDs out of the repository.
  Put local configuration, admission records and run evidence in `.local/` (git-ignored).
- Do not log corpus text, vectors, credentials or full process environments.
- Never execute Markdown code or fetch its includes or links.
- Do not add a code path that creates or changes an index schema, or that publishes without an
  explicit admission.
- Changes to parsing, chunking, embedding text or budgets change the processing fingerprint and
  require a rebuild of existing indexes; call that out in the pull request and the
  [changelog](CHANGELOG.md).
- Keep `uv.lock` in sync with `pyproject.toml` (`uv lock`).
