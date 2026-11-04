from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from .deps import get_technician_service
from .models import Technician, TechnicianCreate, TechnicianPatch, Workload
from .service import TechnicianService

Svc = Annotated[TechnicianService, Depends(get_technician_service)]

router = APIRouter(prefix="/api/technicians", tags=["technicians"])


@router.get("", response_model=list[Technician])
async def list_technicians(svc: Svc, active_only: bool = False):
    return await svc.list(active_only)


@router.post("", response_model=Technician, status_code=201)
async def create_technician(body: TechnicianCreate, svc: Svc):
    return await svc.create(body)


@router.patch("/{technician_id}", response_model=Technician)
async def patch_technician(technician_id: UUID, body: TechnicianPatch, svc: Svc):
    return await svc.patch(technician_id, body)


@router.delete("/{technician_id}", response_model=Technician)
async def deactivate_technician(technician_id: UUID, svc: Svc):
    return await svc.deactivate(technician_id)


@router.get("/{technician_id}/workload", response_model=Workload)
async def technician_workload(technician_id: UUID, svc: Svc):
    return await svc.workload(technician_id)
