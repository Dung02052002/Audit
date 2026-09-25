from fastapi import FastAPI

from ai_youtube_agent import __version__

app = FastAPI(title="ai_youtube_agent", version=__version__)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
