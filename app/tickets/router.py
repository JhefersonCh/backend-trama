from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from .deps import get_ticket_service
from .models import (
    AssignBody,
    ContactBody,
    NoteBody,
    TicketCreate,
    TicketDetail,
    TicketPatch,
    TicketStatus,
    TicketSummary,
    TicketTimeline,
)
from .service import TicketService

Svc = Annotated[TicketService, Depends(get_ticket_service)]

router = APIRouter(prefix="/api/tickets", tags=["tickets"])


@router.get("", response_model=list[TicketSummary])
async def list_tickets(
    svc: Svc,
    status: TicketStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    technician_id: UUID | None = None,
    unassigned: bool = False,
):
    return await svc.list_tickets(status, limit, technician_id, unassigned)


@router.post("", response_model=TicketSummary, status_code=201)
async def create_ticket(body: TicketCreate, svc: Svc):
    return await svc.create_incident_with_ticket(**body.model_dump(), source="manual")


@router.get("/mine", response_model=TicketTimeline)
async def my_ticket(svc: Svc, session_id: str):
    return await svc.mine(session_id)


@router.get("/{ticket_number}", response_model=TicketDetail)
async def get_ticket(ticket_number: str, svc: Svc):
    return await svc.get_detail(ticket_number)


@router.patch("/{ticket_number}", response_model=TicketDetail)
async def patch_ticket(ticket_number: str, body: TicketPatch, svc: Svc):
    return await svc.patch(ticket_number, body)


@router.get("/{ticket_number}/timeline", response_model=TicketTimeline)
async def ticket_timeline(ticket_number: str, svc: Svc):
    return await svc.timeline(ticket_number)


@router.post("/{ticket_number}/assign", response_model=TicketTimeline)
async def assign_ticket(ticket_number: str, body: AssignBody, svc: Svc):
    return await svc.assign(ticket_number, body.technician_id, body.auto)


@router.post("/{ticket_number}/notes", response_model=TicketTimeline, status_code=201)
async def add_note(ticket_number: str, body: NoteBody, svc: Svc):
    return await svc.add_note(ticket_number, body.technician_id, body.text)


@router.put("/{ticket_number}/contact", response_model=TicketTimeline)
async def put_contact(ticket_number: str, body: ContactBody, svc: Svc):
    return await svc.set_contact(ticket_number, body.value)
