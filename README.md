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
uv run python -m uvicorn ai_youtube_agent.main:app --host 127.0.0.1 --reload
```

Open http://127.0.0.1:8000/app/ (`/` redirects there) for the local web app (APP-01): paste or load a `.txt` script, look at the plan, start a background run, follow its stages live, browse the content items with their jobs, checkpoints, scenes and preview, and download the MP4. The app never publishes, approves or uploads. There is no login yet, so keep `--host 127.0.0.1` (do not expose the server on the network).

- The default render provider is `mock`: its MP4 has no video or audio track, so browsers cannot play it (the app says so). For a playable local demo, install ffmpeg and put `AI_YOUTUBE_AGENT_RENDER_PROVIDER=ffmpeg` in a git-ignored `.env` (see `.env.example`); `GET /system/settings` shows whether ffmpeg is found. On Windows also set `AI_YOUTUBE_AGENT_RENDER_FONT_FILE` (for example `C:/Windows/Fonts/arial.ttf`): without a fontconfig config, ffmpeg's caption filter crashes and the render fails with `render.failed`.
- Background runs (`POST /script-to-video/background-runs`) run one at a time in one worker thread; the others wait as `queued`. Runs live in memory: after a restart the run pages are gone, the content items stay, and a job left running reads as `interrupted`. Because the runs are kept in the memory of one process, run the app as a single uvicorn process (never `--workers N`): another worker would not see the runs of the first one. Running the same script again resumes it (it takes the job over). The synchronous `POST /script-to-video/runs` also takes over a running job of the same script, so do not start the same script through both at once.

- Edge TTS (the `edge_tts` provider) calls the real Microsoft Edge online service, so it needs internet access and the optional `tts` extra: `uv sync --extra tts`. ComfyUI (the `comfyui` provider) needs a running ComfyUI server (`COMFYUI_BASE_URL`, `COMFYUI_CHECKPOINT`). Neither provider silently falls back to a mock: when the service or library is missing, the call fails with a structured error.

Free AI film demo (BUDGET-01, development tool, not part of the package): with ComfyUI running locally (Stable Diffusion 1.5 checkpoint, default `v1-5-pruned-emaonly.safetensors`, or `--checkpoint` / `COMFYUI_CHECKPOINT`), internet access for Edge TTS and ffmpeg on the `PATH` (or `AI_YOUTUBE_AGENT_FFMPEG_BINARY`), run

```sh
uv run --extra tts --extra demo python tools/free_ai_film_demo.py [--out-root artifacts/free_ai_film_demo] [--comfy-url http://127.0.0.1:8188] [--voice vi-VN-HoaiMyNeural] [--size 720x1280] [--fps 24]
```

It checks the paid-API budget (every provider must be free), makes one AI background with ComfyUI, a Vietnamese voice with Edge TTS (a free online service, not local), animates a cartoon character over the background with Pillow frame by frame (software 2D animation, not an AI video model), streams the frames to ffmpeg and checks the MP4. Each step prints PENDING/RUNNING/SUCCESS/FAILED/BLOCKED; the run directory `artifacts/free_ai_film_demo/<YYYYmmdd-HHMMSS>/` (git-ignored) holds the MP4, `manifest.json` (methods, cost 0, budget status, steps) and `run.log`. When ComfyUI or Edge TTS is unreachable the step fails; there is never a placeholder. Subtitles need a Vietnamese TrueType font: `AI_YOUTUBE_AGENT_RENDER_FONT_FILE`, else `C:/Windows/Fonts/arial.ttf` or `segoeui.ttf`.

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
  web/                  local web app (static files served at /app)
dashboard/              Command Center notes and design tokens
tests/                  pytest tests
docs/                   requirements, architecture and audit reports
```

See `docs/ARCHITECTURE.md` for how the bounded contexts map to these folders.
