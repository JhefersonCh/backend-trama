from typing import Any
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from app.config.models import ModelProfile, Provider
from app.config.pg_repos import PgProfiles, PgProviders, PgSettings


class PgRuntimeStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def list_providers(self) -> list[Provider]:
        async with self._pool.connection() as c:
            return await PgProviders(c).list()

    async def get_provider(self, id: UUID) -> Provider | None:
        async with self._pool.connection() as c:
            return await PgProviders(c).get(id)

    async def list_profiles(self) -> list[ModelProfile]:
        async with self._pool.connection() as c:
            return await PgProfiles(c).list(None)

    async def get_profile(self, id: UUID) -> ModelProfile | None:
        async with self._pool.connection() as c:
            return await PgProfiles(c).get(id)

    async def get_active(self, role: str) -> ModelProfile | None:
        async with self._pool.connection() as c:
            return await PgProfiles(c).get_active(role)

    async def get_secret(self, provider_id: UUID) -> bytes | None:
        async with self._pool.connection() as c:
            cur = await c.execute("SELECT ciphertext FROM provider_secrets WHERE provider_id = %s", (provider_id,))
            row = await cur.fetchone()
        return bytes(row[0]) if row else None

    async def settings(self) -> dict[str, Any]:
        async with self._pool.connection() as c:
            return await PgSettings(c).all()

    async def set_measured_memory(self, profile_id: UUID, mb: int) -> None:
        async with self._pool.connection() as c:
            await c.execute("UPDATE model_profiles SET measured_memory_mb = %s WHERE id = %s", (mb, profile_id))

    async def add_event(
        self, profile_id: UUID | None, event: str, duration_ms: int | None, memory_mb: int | None
    ) -> None:
        async with self._pool.connection() as c:
            await c.execute(
                "INSERT INTO model_load_events (profile_id, event, duration_ms, memory_mb) VALUES (%s,%s,%s,%s)",
                (profile_id, event, duration_ms, memory_mb),
            )
