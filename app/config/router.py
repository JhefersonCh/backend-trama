from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from .deps import get_config_service
from .models import (
    ActivationResult,
    AuditLog,
    ModelProfile,
    ProfileCreate,
    ProfileUpdate,
    Provider,
    ProviderCreate,
    ProviderUpdate,
    Role,
    SecretBody,
    SettingBody,
    SettingOut,
)
from .service import ConfigService

Svc = Annotated[ConfigService, Depends(get_config_service)]

router = APIRouter(prefix="/api")


@router.get("/providers", tags=["providers"], response_model=list[Provider])
async def list_providers(svc: Svc):
    return await svc.list_providers()


@router.post("/providers", tags=["providers"], response_model=Provider, status_code=201)
async def create_provider(body: ProviderCreate, svc: Svc):
    return await svc.create_provider(body)


@router.get("/providers/{id}", tags=["providers"], response_model=Provider)
async def get_provider(id: UUID, svc: Svc):
    return await svc.get_provider(id)


@router.patch("/providers/{id}", tags=["providers"], response_model=Provider)
async def update_provider(id: UUID, body: ProviderUpdate, svc: Svc):
    return await svc.update_provider(id, body)


@router.delete("/providers/{id}", tags=["providers"], status_code=204)
async def delete_provider(id: UUID, svc: Svc):
    await svc.delete_provider(id)
    return Response(status_code=204)


@router.put("/providers/{id}/secret", tags=["providers"], status_code=204)
async def put_secret(id: UUID, body: SecretBody, svc: Svc):
    await svc.set_secret(id, body.value)
    return Response(status_code=204)


@router.delete("/providers/{id}/secret", tags=["providers"], status_code=204)
async def delete_secret(id: UUID, svc: Svc):
    await svc.delete_secret(id)
    return Response(status_code=204)


@router.get("/model-profiles", tags=["model-profiles"], response_model=list[ModelProfile])
async def list_profiles(svc: Svc, role: Role | None = None):
    return await svc.list_profiles(role)


@router.post("/model-profiles", tags=["model-profiles"], response_model=ModelProfile, status_code=201)
async def create_profile(body: ProfileCreate, svc: Svc):
    return await svc.create_profile(body)


@router.get("/model-profiles/{id}", tags=["model-profiles"], response_model=ModelProfile)
async def get_profile(id: UUID, svc: Svc):
    return await svc.get_profile(id)


@router.patch("/model-profiles/{id}", tags=["model-profiles"], response_model=ModelProfile)
async def update_profile(id: UUID, body: ProfileUpdate, svc: Svc):
    return await svc.update_profile(id, body)


@router.delete("/model-profiles/{id}", tags=["model-profiles"], status_code=204)
async def delete_profile(id: UUID, svc: Svc):
    await svc.delete_profile(id)
    return Response(status_code=204)


@router.post("/model-profiles/{id}/activate", tags=["model-profiles"], response_model=ActivationResult)
async def activate_profile(id: UUID, svc: Svc):
    return await svc.activate_profile(id)


@router.get("/settings", tags=["settings"])
async def get_settings(svc: Svc) -> dict[str, Any]:
    return await svc.get_settings()


@router.put("/settings/{key}", tags=["settings"], response_model=SettingOut)
async def put_setting(key: str, body: SettingBody, svc: Svc):
    return SettingOut(key=key, value=await svc.put_setting(key, body.value))


@router.get("/config-audit", tags=["audit"], response_model=list[AuditLog])
async def config_audit(svc: Svc, limit: Annotated[int, Query(ge=1, le=500)] = 50):
    return await svc.list_audit(limit)
