"""Ports (Protocols) the tool executors depend on. Implementations: app/tools/pg.py."""

from typing import Any, Protocol


class AuditPort(Protocol):
    async def record(
        self,
        session_id: str | None,
        tool_name: str,
        arguments_summary: str,
        result_summary: str,
        status: str,
        *,
        tool_version: int | None = None,
        duration_ms: int | None = None,
        authorized_by: str | None = None,
    ) -> None: ...


class InventoryPort(Protocol):
    async def inventory(self, site: str | None) -> dict[str, Any]: ...

    async def network_details(self, name_or_cidr: str) -> list[dict[str, Any]]: ...


class DiagnosticsPort(Protocol):
    async def record(self, session_id: str | None, observations: list[dict[str, Any]]) -> int:
        """Persist observations (source='user_provided') into the session's diagnostic run. Returns rows stored."""
        ...
