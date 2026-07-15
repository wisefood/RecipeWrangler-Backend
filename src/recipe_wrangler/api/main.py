"""FastAPI application exposing RecipeWrangler services."""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from recipe_wrangler.api.routers.generic import install_error_handler
import recipe_wrangler.api.logsys as logsys
import uvicorn
from recipe_wrangler.utils.env_loader import load_runtime_env

# Load env once before importing modules that resolve settings at import time.
load_runtime_env()

from .config import get_settings


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""

    app = FastAPI(title="RecipeWrangler API", version="0.2.0")
    settings = get_settings()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return app


from .routers import health, recipes

# Get settings
settings = get_settings()
logsys.configure()

app = create_app()
install_error_handler(app)

# Serve locally generated images (e.g. Irish_SafeFood FLUX images)
_data_dir = Path(__file__).resolve().parents[3] / "data"
if _data_dir.exists():
    app.mount("/static/data", StaticFiles(directory=str(_data_dir)), name="static_data")

# Register routers
app.include_router(health.router)
app.include_router(recipes.router, prefix="/api/v1")

# Adaptation endpoints (adapt/suggestions, adapt/simulate). The router carries
# its own /api/v1/recipes prefix; it also remains runnable standalone via
# recipe_wrangler.services.adaptation.app for isolated development.
from recipe_wrangler.services.adaptation.router import router as adaptation_router  # noqa: E402

app.include_router(adaptation_router)


@app.on_event("startup")
async def startup_event():
    """Prime connection pools and query plan caches to avoid first-request latency."""
    try:
        from recipe_wrangler.tools.param_search import warmup as param_search_warmup
        param_search_warmup()
    except Exception:
        # Warmup is best-effort; never block startup on a transient dep.
        import logging
        logging.getLogger(__name__).warning("param_search warmup failed", exc_info=True)

    def _warm_search_app() -> None:
        """Build the NL search app off the request path so the first user
        never pays its init (Groq client + prompt chains)."""
        import logging
        import time as _time
        try:
            from recipe_wrangler.api.dependencies import get_recipe_search_app
            started = _time.perf_counter()
            get_recipe_search_app()
            logging.getLogger(__name__).info(
                "search app warmed in %.2fs", _time.perf_counter() - started
            )
        except Exception:
            logging.getLogger(__name__).warning("search app warmup failed", exc_info=True)

    import threading
    threading.Thread(target=_warm_search_app, name="search-app-warmup", daemon=True).start()


@app.on_event("shutdown")
async def shutdown_event():
    """Clean up resources on application shutdown."""
    from recipe_wrangler.utils.nutrition_postgres import close_engine
    close_engine()

if __name__ == "__main__":
    uvicorn.run(
        "recipe_wrangler.api.main:app",
        host="0.0.0.0",
        port=settings.api_port or int(os.getenv("PORT", "8001")),
        reload=True,
    )
