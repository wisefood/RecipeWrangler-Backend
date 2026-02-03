"""FastAPI application exposing RecipeWrangler services."""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from recipe_wrangler.api.routers.generic import install_error_handler
from dotenv import load_dotenv
import recipe_wrangler.api.logsys as logsys
import uvicorn

# Load env before importing heavy dependencies that expect keys.
API_DIR = Path(__file__).resolve().parent
load_dotenv(API_DIR / ".env")
load_dotenv()

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


from .config import get_settings
from .routers import health, recipes

# Get settings
settings = get_settings()
logsys.configure()

app = create_app()
install_error_handler(app)

# Register routers
app.include_router(health.router)
app.include_router(recipes.router, prefix="/api/v1")


@app.on_event("shutdown")
async def shutdown_event():
    """Clean up resources on application shutdown."""
    from recipe_wrangler.utils.nutrition_postgres_v2 import close_engine
    close_engine()

if __name__ == "__main__":
    uvicorn.run(
        "recipe_wrangler.api.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8001")),
        reload=True,
    )
