# ai_youtube_agent

Backend / AI agent service built with FastAPI.

## Requirements

- Python 3.11
- [uv](https://docs.astral.sh/uv/)

## Setup

```sh
uv sync
```

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
tests/                  pytest tests
```
