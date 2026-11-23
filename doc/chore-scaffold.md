# Branch `chore/scaffold`

Sets up the project skeleton. No application logic yet.

## What changed

| File | What it does |
|---|---|
| `pyproject.toml` | Turned into an installable package: project name unchanged (`company-recommendation-system`), import package `company_recs`, `src/` layout, `uv_build` backend. Adds the runtime and dev dependencies, and configures ruff, pyright and pytest. |
| `src/company_recs/config.py` | `Settings` (pydantic-settings) with `DATABASE_URL`, `RABBITMQ_URL`, `EMBED_MODEL`, `TOP_K`, `LOG_LEVEL`, and a cached `get_settings()`. |
| `src/company_recs/py.typed` | Marks the package as typed. |
| `tests/test_config.py` | Three tests for the settings: defaults, env overrides, and validation. |
| `Justfile` | Adds `migrate`, `consumer`, `api`, `test`, `lint`, `eval` as stubs, runs `pre-commit install` in `setup` if a config exists, and uses Git Bash on Windows. |
| `.env.example` | Example settings for local development. |
| `.github/workflows/ci.yml` | On every push and PR: `uv sync --locked`, `ruff check`, `pyright`, `pytest -q`. |
| `SETUP.md` | Local setup on Windows/PowerShell: tools, first run, checks, settings, and Windows gotchas. The original `README.md` (the assessment brief) is unchanged. |

## Decisions

- **Kept the provided `producer/` where it is**, outside `src/`. It is the assessment's code, and `just publish`
  runs it from the repo root. That also means **`aio-pika` stays as a dependency**: the producer needs it, even though
  our consumer will use `pika`.
- **Kept `just publish` and `--wait` on `just up`.** The original Justfile had both; dropping them would break the
  assessment's own instructions and let commands run before the containers are healthy.
- **On Windows, `just` runs recipes with Git Bash** (`windows-shell` in the Justfile). From PowerShell, a bare `bash`
  resolves to WSL's `System32\bash.exe`, which runs in Linux and can't find Windows tools like `uv`
  (`uv: command not found`). The path assumes Git for Windows' default install location; macOS, Linux and CI are
  unaffected.
- **`setup` uses `uv sync --locked`**, as before, so installs match `uv.lock` exactly.
- **Settings have defaults** matching `docker-compose.yml`, so local development works without a `.env`. Production
  sets the environment variables instead. `TOP_K` is limited to 1–50 and `LOG_LEVEL` to the standard level names.
- **Added a small config test** so `pytest -q` has something to run. With no tests at all pytest exits with code 5 and
  CI would fail.
- **ruff ignores line length (E501) in `producer/`.** The provided producer has one 126-character docstring line;
  we leave the assessment's code unchanged rather than reformat it.
- **pyright** is strict on `src/` only; `tests/` and `producer/` are checked in basic mode.
- **Embedding model default** is `BAAI/bge-small-en-v1.5`, as specified. Part of the feed is in Malay, so a
  multilingual model is likely to do better; that will be compared in a later branch.

## How to check it

```bash
uv sync --locked
uv run ruff check .
uv run pyright
uv run pytest -q
```
