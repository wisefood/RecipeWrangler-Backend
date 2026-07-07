"""Standalone FastAPI app for the experimental adaptation service.

Runs the adaptation router on its own port without touching the production
`recipe_wrangler.api.main` module.

Local run:
    PYTHONPATH=src uvicorn \\
        recipe_wrangler.services.adaptation.app:app \\
        --reload --port 8101

Swagger UI: http://localhost:8101/docs
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from recipe_wrangler.utils.env_loader import load_runtime_env

load_runtime_env()

from .router import router as adaptation_router  # noqa: E402


def create_app() -> FastAPI:
    app = FastAPI(
        title="RecipeWrangler Adaptation Service (experimental)",
        version="0.0.1",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "*").split(","),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(adaptation_router)

    @app.get("/health", tags=["health"])
    def _health():
        return {"status": "ok", "service": "adaptation"}

    @app.on_event("shutdown")
    async def _shutdown():
        from recipe_wrangler.utils.nutrition_postgres import close_engine
        close_engine()

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "recipe_wrangler.services.adaptation.app:app",
        host="0.0.0.0",
        port=int(os.getenv("ADAPTATION_PORT", "8101")),
        reload=True,
    )
