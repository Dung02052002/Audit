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
- `.env` and `.env.*` files are git-ignored. Only `.env.example` is committed, and it must never hold secrets.

## Run

```sh
uv run uvicorn ai_youtube_agent.main:app --reload
```

Health check: `GET /health`

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
  main.py               FastAPI app
  core/                 foundation, content lifecycle, control gates, persistence
  content/              domain contexts of the production pipeline
  providers/            external-system interfaces and mocks
  pipeline/             orchestration and publishing flow
dashboard/              Command Center (platform not decided yet)
tests/                  pytest tests
docs/                   requirements, architecture and audit reports
```

See `docs/ARCHITECTURE.md` for how the bounded contexts map to these folders.
