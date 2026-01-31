"""Application configuration and feature flag API routes."""

from fastapi import APIRouter
from pydantic import BaseModel

from core.config import INTELLIGENT_RETRY_ENABLED

router = APIRouter(tags=["config"])


class FeaturesResponse(BaseModel):
    """Response for features endpoint."""
    intelligent_retry: bool


@router.get("/features", response_model=FeaturesResponse)
def get_features():
    """Return enabled features for UI configuration.

    This endpoint allows the frontend to conditionally show/hide
    features based on deployment configuration.
    """
    return FeaturesResponse(
        intelligent_retry=INTELLIGENT_RETRY_ENABLED,
    )
