"""Relay routes for executor configuration.

Proxies GET/POST /config to the Playwright executor service so the frontend
can manage browser pre-warm settings without direct access to the executor.
"""

import os

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/executor", tags=["executor"])

EXECUTOR_URL = os.getenv("PLAYWRIGHT_EXECUTOR_URL", "http://localhost:8932")


class ExecutorConfigUpdate(BaseModel):
    preload: bool


@router.get("/config")
async def get_executor_config():
    """Fetch executor configuration (preload flag + browser running status)."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{EXECUTOR_URL}/config", timeout=5.0)
            resp.raise_for_status()
            return resp.json()
        except httpx.RequestError as exc:
            raise HTTPException(status_code=503, detail=f"Executor unreachable: {exc}")
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text)


@router.post("/config")
async def update_executor_config(update: ExecutorConfigUpdate):
    """Update executor runtime configuration (preload flag).

    Setting preload=true starts any idle configured browsers immediately.
    Setting preload=false only flips the flag; running browsers stay open.
    """
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"{EXECUTOR_URL}/config",
                json=update.model_dump(),
                timeout=30.0,  # starting browsers can take a few seconds each
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.RequestError as exc:
            raise HTTPException(status_code=503, detail=f"Executor unreachable: {exc}")
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text)
