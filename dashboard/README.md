# Dashboard

Home of the Command Center (context X3 in `docs/ARCHITECTURE.md`, prompts #191–#203).

The requirements call for an "iOS-style" design system (#191) without saying whether the Command Center is a native iOS app or a web dashboard (open question Q4 in `docs/ARCHITECTURE.md`). APP-01 answered Q4 (user decision, 2026-10-22): the Command Center is a web app served by FastAPI. Its code lives inside the Python package, in `src/ai_youtube_agent/web/` (static HTML, vanilla JavaScript ES modules and CSS in `static/`, mounted at `/app`), so it ships in the wheel.

This folder keeps `design_system.css`, the source of the design tokens. The web app uses a copy in `src/ai_youtube_agent/web/static/css/design_system.css`; change both together.

The dashboard reads from the backend HTTP API and sends only user actions. It owns no domain rules. The APP-01 web app sends one action only, starting a background script-to-video run; it has no publish, approve or upload action.
