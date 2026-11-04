"""REST for the agent core: /api/cases and /api/rules."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.requests import HTTPConnection

from app.config.errors import NotFoundError

from .models import FactIn, RuleContent
from .repo import PgAgentRepo
from .service import AgentService

cases_router = APIRouter(prefix="/api/cases", tags=["agent"])
rules_router = APIRouter(prefix="/api/rules", tags=["agent"])


def get_agent_service(conn: HTTPConnection) -> AgentService:
    return AgentService(PgAgentRepo(conn.app.state.pool))


Svc = Annotated[AgentService, Depends(get_agent_service)]


@cases_router.get("/{session_id}")
async def get_case(session_id: UUID, svc: Svc) -> dict[str, Any]:
    return await svc.case_view(session_id)


@cases_router.get("/{session_id}/explain")
async def explain_case(session_id: UUID, svc: Svc, target: Annotated[str | None, Query()] = None) -> dict[str, Any]:
    return await svc.explain(session_id, target)


@cases_router.post("/{session_id}/facts")
async def add_fact(session_id: UUID, body: FactIn, svc: Svc) -> dict[str, Any]:
    """INTERNAL (dev/demo/tests): upserts a fact and recomputes hypotheses. The conversation turn will use the
    service directly; this is not part of the public contract."""
    await svc.upsert_fact(session_id, body.key, body.value, body.source, body.certainty)
    await svc.recompute(session_id)
    return await svc.case_view(session_id)


@rules_router.get("")
async def list_rules(svc: Svc) -> list[dict[str, Any]]:
    return await svc.repo.list_rule_sets()


@rules_router.post("/validate")
async def validate_rules(body: RuleContent, svc: Svc) -> dict[str, Any]:
    return svc.validate(body.content_yaml)


@rules_router.post("", status_code=201)
async def create_rules(body: RuleContent, svc: Svc) -> dict[str, Any]:
    return await svc.create_rule_set(body.content_yaml)


@rules_router.get("/{version}")
async def get_rules(version: int, svc: Svc) -> dict[str, Any]:
    row = await svc.repo.get_rule_set(version)
    if row is None:
        raise NotFoundError(f"rule set version {version} not found")
    return {**row, **svc.validate(row["content_yaml"])}


@rules_router.post("/{version}/activate")
async def activate_rules(version: int, svc: Svc) -> dict[str, Any]:
    return await svc.activate_rule_set(version)
