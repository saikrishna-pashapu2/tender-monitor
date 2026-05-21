"""FastAPI app factory + module-level ``app`` for uvicorn import-string use."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from tender_monitor.api.routes.api import router as api_router
from tender_monitor.api.routes.settings import router as settings_router
from tender_monitor.api.routes.web import router as web_router

STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(
        title="Tender Monitor",
        description=(
            "Read-only browsing UI and JSON API over the tenders the scheduler "
            "has ingested from KZ/UZ procurement platforms."
        ),
        version="0.1.0",
    )
    # Vendor JS (tailwind, htmx, lucide) lives under static/vendor/.
    # Keeping it local removes any dependency on external CDNs, which
    # silently hang the UI when blocked by a corporate proxy.
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(web_router)
    app.include_router(settings_router)
    app.include_router(api_router)
    return app


app: FastAPI = create_app()


__all__ = ["app", "create_app"]
