"""Health check router."""

from fastapi import APIRouter

router = APIRouter(tags=["ops"])


@router.get("/health")
def healthcheck() -> dict[str, str]:
    """Simple readiness probe."""
    return {"status": "ok"}
