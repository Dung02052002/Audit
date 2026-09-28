# ai_youtube_agent

Backend / AI agent service built with FastAPI.

## Requirements

- Python 3.11
- [uv](https://docs.astral.sh/uv/)

## Setup

```sh
uv sync
```

## Configuration

Settings are typed and validated in `src/ai_youtube_agent/core/config.py`. Every variable uses the `AI_YOUTUBE_AGENT_` prefix. See `.env.example` for the available keys.

- Choose the environment (`development`, `test` or `production`) with `AI_YOUTUBE_AGENT_ENVIRONMENT` in the process environment. The default is `development`.
- Values are read from `.env`, then `.env.<environment>`, then process environment variables. A later source wins.
- Feature flags live in `src/ai_youtube_agent/core/flags.py` and are set with `AI_YOUTUBE_AGENT_FLAGS__<NAME>`, for example `AI_YOUTUBE_AGENT_FLAGS__PUBLISH_ENABLED=true`. The defaults are the safe choice: publishing, LongForm and auto-reply are off, and test and approval are required.
- Logging is structured JSON (`src/ai_youtube_agent/core/log.py`). Call `configure_logging(settings.log_level)` once, and wrap work in `log_context(correlation_id=..., session_id=..., job_id=...)` so every record carries those IDs. Set the level with `AI_YOUTUBE_AGENT_LOG_LEVEL`.
- Errors (`src/ai_youtube_agent/core/errors.py`) are `ApplicationError`, `DomainError` or `ProviderError`. Show users only `to_public(exc)`. The internal `detail` goes to logs through `exc.log_fields()`.
- Dependency injection (`src/ai_youtube_agent/core/di.py`): code resolves interfaces from a `Container` and never builds implementations itself. `src/ai_youtube_agent/bootstrap.py` is the only place that registers implementations. `main.create_app()` builds the container and configures logging. In routes, use `Annotated[Service, provide(Service)]`.
- `.env` and `.env.*` files are git-ignored. Only `.env.example` is committed, and it must never hold secrets.

## Run

```sh
uv run uvicorn ai_youtube_agent.main:app --reload
```

Health check: `GET /health` returns `status` (`ok`, `degraded` or `down`), `version` and one entry per check in `checks`. It answers 503 only when an application check fails. A failing provider check gives `degraded` with 200.

## Test and lint

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## Build

```sh
uv build
```

## Layout

```
src/ai_youtube_agent/   application package
  main.py               FastAPI app (`create_app()`)
  bootstrap.py          composition root: registers implementations in the DI container
  core/                 foundation, content lifecycle, control gates, persistence
  content/              domain contexts of the production pipeline
  providers/            external-system interfaces and mocks
  pipeline/             orchestration and publishing flow
dashboard/              Command Center (platform not decided yet)
tests/                  pytest tests
docs/                   requirements, architecture and audit reports
```

See `docs/ARCHITECTURE.md` for how the bounded contexts map to these folders.
