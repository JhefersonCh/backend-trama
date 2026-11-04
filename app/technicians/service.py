from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from app.config.errors import ConflictError, DomainValidationError, NotFoundError

from .models import Technician, TechnicianCreate, TechnicianPatch, Workload
from .ports import TechnicianUnitOfWork


class TechnicianService:
    def __init__(self, uow_factory: Callable[[], TechnicianUnitOfWork]) -> None:
        self._uow = uow_factory

    async def list(self, active_only: bool = False) -> list[Technician]:
        async with self._uow() as uow:
            return await uow.technicians.list(active_only)

    async def get(self, technician_id: UUID) -> Technician:
        async with self._uow() as uow:
            tech = await uow.technicians.get(technician_id)
        if tech is None:
            raise NotFoundError("technician not found")
        return tech

    async def create(self, body: TechnicianCreate) -> Technician:
        async with self._uow() as uow:
            if body.email_or_username and any(
                t.email_or_username == body.email_or_username for t in await uow.technicians.list(False)
            ):
                raise ConflictError("email_or_username already in use")
            return await uow.technicians.create(body.model_dump())

    async def patch(self, technician_id: UUID, body: TechnicianPatch) -> Technician:
        fields = body.model_dump(exclude_unset=True)
        for k in ("name", "specialties", "zones", "level", "max_open_tickets", "availability", "schedule", "active"):
            if k in fields and fields[k] is None:
                raise DomainValidationError(f"{k} cannot be null")
        async with self._uow() as uow:
            tech = await uow.technicians.update(technician_id, fields)
        if tech is None:
            raise NotFoundError("technician not found")
        return tech

    async def deactivate(self, technician_id: UUID) -> Technician:
        return await self.patch(technician_id, TechnicianPatch(active=False))

    async def workload(self, technician_id: UUID) -> Workload:
        async with self._uow() as uow:
            w = await uow.technicians.workload(technician_id)
        if w is None:
            raise NotFoundError("technician not found")
        return w
