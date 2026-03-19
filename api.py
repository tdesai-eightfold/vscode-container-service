#!/usr/bin/env python3
"""
Cloud-agnostic API for container instance management.
Provider config loaded from provider.json; requests only need provider name.
"""
import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from . import get_provider

app = FastAPI(
    title="VSCode Container Manager API",
    description="Cloud-agnostic workspace create/destroy (container + DNS), list, get. Provider config from provider.json.",
)

PROVIDER_JSON = Path(__file__).parent / "provider.json"


@app.on_event("startup")
def _startup_validate_config() -> None:
    """Validate provider.json on startup so failures are visible immediately."""
    if not PROVIDER_JSON.exists():
        raise FileNotFoundError(
            f"provider.json not found at {PROVIDER_JSON}. "
            "Copy provider.json.example to provider.json and fill in your values."
        )
    try:
        providers = _load_providers()
        app.state._provider_names = list(providers.keys())
    except Exception as e:
        raise RuntimeError(f"Invalid provider.json: {e}") from e

# In-memory job store for async create operations: job_id -> {status, result?, error?}
_job_store: dict[str, dict[str, Any]] = {}


def _run_create_workspace_background(job_id: str, req: "CreateWorkspaceRequest") -> None:
    """Run create_workspace in background and update job store."""
    def _create():
        prov = _get_provider_instance(req.provider)
        return prov.create_workspace(
            workspace_hash=req.workspace_hash,
            image=req.image,
            port=req.port,
        )

    try:
        result = _create()
        _job_store[job_id] = {"status": "completed", "result": result}
    except Exception as e:
        _job_store[job_id] = {"status": "failed", "error": str(e)}


def _load_providers() -> dict[str, dict[str, Any]]:
    """Load provider configs from provider.json."""
    if not PROVIDER_JSON.exists():
        raise FileNotFoundError(f"provider.json not found at {PROVIDER_JSON}")
    with open(PROVIDER_JSON, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("provider.json must be a dict: { provider_name: { ...config } }")
    return data


def _get_provider_config(provider: str) -> dict[str, Any]:
    """Get config for provider from provider.json."""
    providers = _load_providers()
    if provider not in providers:
        configured = ", ".join(providers.keys()) or "(none)"
        raise HTTPException(
            status_code=400,
            detail=f"Provider '{provider}' not configured. Configured: {configured}",
        )
    return {k: v for k, v in providers[provider].items() if v is not None and v != ""}


def _get_provider_instance(provider: str):
    """Create provider instance from provider name (config from provider.json)."""
    kwargs = _get_provider_config(provider)
    try:
        return get_provider(provider, **kwargs)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Provider init failed: {e}")


# ---------------------------------------------------------------------------
# Request schemas (provider only, no provider_config)
# ---------------------------------------------------------------------------

class ListRequest(BaseModel):
    """Request to list container instances."""
    provider: str = Field(..., description="Provider name (from provider.json)")


class DestroyRequest(BaseModel):
    """Request to destroy a container instance."""
    instance_id: str = Field(..., description="Instance ID to destroy")
    provider: str = Field(..., description="Provider name (from provider.json)")


class CreateWorkspaceRequest(BaseModel):
    """Request to create a workspace (container + DNS)."""
    provider: str = Field(..., description="Provider name (from provider.json)")
    workspace_hash: str = Field(..., min_length=1, description="Short identifier (e.g. a92f13)")
    image: str = Field(..., description="Container image (e.g. codercom/code-server:latest)")
    port: int = Field(default=8080, description="Container port")


class DestroyWorkspaceRequest(BaseModel):
    """Request to destroy a workspace."""
    provider: str = Field(..., description="Provider name (from provider.json)")
    workspace_hash: str = Field(..., min_length=1, description="Workspace hash to destroy")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def root() -> dict:
    """Basic usage: provider config from provider.json; use /docs for API."""
    return {
        "service": "VSCode Container Manager API",
        "docs": "/docs",
        "health": "/health",
        "providers": "/providers",
    }


@app.get("/providers", response_model=dict)
async def list_providers() -> dict:
    """
    List configured providers from provider.json.
    """
    try:
        providers = _load_providers()
        # Return provider names only (no secrets)
        return {"providers": list(providers.keys())}
    except FileNotFoundError as e:
        raise HTTPException(
            status_code=503,
            detail=f"{e}. Copy provider.json.example to provider.json and fill in values.",
        )


@app.post("/destroy", response_model=dict)
async def destroy_instance(req: DestroyRequest) -> dict:
    """
    Destroy a container instance. Config from provider.json.
    """
    def _destroy():
        prov = _get_provider_instance(req.provider)
        prov.destroy_instance(req.instance_id)

    try:
        await asyncio.to_thread(_destroy)
        return {"status": "destroyed", "instance_id": req.instance_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/instances", response_model=dict)
async def list_instances(req: ListRequest) -> dict:
    """
    List container instances. Config from provider.json.
    """
    def _list():
        prov = _get_provider_instance(req.provider)
        config = _get_provider_config(req.provider)
        filter_val = config.get("compartment_id")
        instances = prov.list_instances(compartment_or_project=filter_val)
        return [
            {
                "id": i.id,
                "name": i.name,
                "status": i.status,
                "url": i.url,
                "private_ip": i.private_ip,
                "provider": i.provider,
            }
            for i in instances
        ]

    try:
        instances = await asyncio.to_thread(_list)
        return {"instances": instances, "count": len(instances)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/instances/{instance_id}", response_model=dict)
async def get_instance(
    instance_id: str,
    provider: str = Query(..., description="Provider name (from provider.json)"),
) -> dict:
    """
    Get a single instance by ID. Config from provider.json.
    """
    def _get():
        prov = _get_provider_instance(provider)
        instance = prov.get_instance(instance_id)
        if not instance:
            raise HTTPException(status_code=404, detail=f"Instance {instance_id} not found")
        return instance

    try:
        instance = await asyncio.to_thread(_get)
        return {
            "id": instance.id,
            "name": instance.name,
            "status": instance.status,
            "url": instance.url,
            "private_ip": instance.private_ip,
            "provider": instance.provider,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/workspace/create", response_model=dict)
async def create_workspace(req: CreateWorkspaceRequest, background_tasks: BackgroundTasks) -> dict:
    """
    Create workspace: container + DNS record.
    Returns immediately with job_id; creation runs in background.
    Poll GET /status/{job_id} for result.
    """
    job_id = str(uuid.uuid4())
    _job_store[job_id] = {"status": "creating"}
    background_tasks.add_task(_run_create_workspace_background, job_id, req)
    return {"status": "creating", "job_id": job_id}


@app.post("/workspace/destroy", response_model=dict)
async def destroy_workspace(req: DestroyWorkspaceRequest) -> dict:
    """
    Destroy workspace: delete DNS record, then delete container.
    """
    def _destroy():
        prov = _get_provider_instance(req.provider)
        prov.destroy_workspace(workspace_hash=req.workspace_hash)

    try:
        await asyncio.to_thread(_destroy)
        return {"status": "destroyed", "workspace_hash": req.workspace_hash}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/status/{job_id}", response_model=dict)
async def get_job_status(job_id: str) -> dict:
    """
    Poll status of a create job. Returns creating | completed | failed.
    For completed: result contains instance or workspace info.
    For failed: error contains message.
    """
    if job_id not in _job_store:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    job = _job_store[job_id]
    status = job["status"]
    if status == "completed":
        return {"status": "completed", "result": job["result"]}
    if status == "failed":
        return {"status": "failed", "error": job["error"]}
    return {"status": "creating"}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "vscode_container_manager"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
