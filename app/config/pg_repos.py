from typing import Any
from uuid import UUID

import psycopg.errors as pge
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .errors import ConflictError
from .models import AuditLog, ModelProfile, ProfileCreate, Provider, ProviderCreate

_PROVIDER_COLS = {"name", "kind", "base_url", "enabled"}
_PROFILE_COLS = {"name", "role", "provider_id", "model_ref", "params", "est_memory_mb"}

_PROVIDER_SELECT = """
SELECT p.id, p.name, p.kind, p.base_url, p.enabled,
       (s.provider_id IS NOT NULL) AS has_secret, p.created_at, p.updated_at
FROM model_providers p LEFT JOIN provider_secrets s ON s.provider_id = p.id"""
_PROFILE_COLS_SQL = (
    "id, name, role, provider_id, model_ref, params, est_memory_mb, "
    "measured_memory_mb, is_active, created_at, updated_at"
)


def _wrap(v: Any) -> Any:
    return Jsonb(v) if isinstance(v, dict) else v


class _Base:
    def __init__(self, conn) -> None:
        self.conn = conn

    async def _all(self, sql: str, params: tuple | list = ()) -> list[dict]:
        async with self.conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()

    async def _one(self, sql: str, params: tuple | list = ()) -> dict | None:
        rows = await self._all(sql, params)
        return rows[0] if rows else None

    async def _exec(self, sql: str, params: tuple | list = ()) -> int:
        try:
            cur = await self.conn.execute(sql, params)
        except pge.UniqueViolation as e:
            raise ConflictError("name already exists or role already has an active profile") from e
        except pge.ForeignKeyViolation as e:
            raise ConflictError("row is referenced by other rows") from e
        return cur.rowcount


class PgProviders(_Base):
    async def list(self) -> list[Provider]:
        rows = await self._all(_PROVIDER_SELECT + " ORDER BY p.name")
        return [Provider(**r) for r in rows]

    async def get(self, id: UUID) -> Provider | None:
        r = await self._one(_PROVIDER_SELECT + " WHERE p.id = %s", (id,))
        return Provider(**r) if r else None

    async def add(self, data: ProviderCreate) -> Provider:
        async with self.conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(
                    "INSERT INTO model_providers (name, kind, base_url, enabled) VALUES (%s,%s,%s,%s) RETURNING id",
                    (data.name, data.kind, data.base_url, data.enabled),
                )
            except pge.UniqueViolation as e:
                raise ConflictError("provider name already exists") from e
            row = await cur.fetchone()
        return await self.get(row["id"])  # type: ignore[return-value]

    async def update(self, id: UUID, fields: dict[str, Any]) -> Provider:
        cols = [k for k in fields if k in _PROVIDER_COLS]
        sets = ", ".join(f"{c} = %s" for c in cols)
        await self._exec(
            f"UPDATE model_providers SET {sets} WHERE id = %s",
            [fields[c] for c in cols] + [id],
        )
        return await self.get(id)  # type: ignore[return-value]

    async def delete(self, id: UUID) -> None:
        await self._exec("DELETE FROM model_providers WHERE id = %s", (id,))

    async def set_secret(self, id: UUID, ciphertext: bytes) -> None:
        await self._exec(
            "INSERT INTO provider_secrets (provider_id, ciphertext) VALUES (%s,%s) "
            "ON CONFLICT (provider_id) DO UPDATE "
            "SET ciphertext = EXCLUDED.ciphertext, updated_at = now()",
            (id, ciphertext),
        )

    async def delete_secret(self, id: UUID) -> bool:
        return await self._exec("DELETE FROM provider_secrets WHERE provider_id = %s", (id,)) > 0


class PgProfiles(_Base):
    async def list(self, role: str | None) -> list[ModelProfile]:
        if role:
            rows = await self._all(
                f"SELECT {_PROFILE_COLS_SQL} FROM model_profiles WHERE role = %s ORDER BY name",
                (role,),
            )
        else:
            rows = await self._all(f"SELECT {_PROFILE_COLS_SQL} FROM model_profiles ORDER BY name")
        return [ModelProfile(**r) for r in rows]

    async def get(self, id: UUID) -> ModelProfile | None:
        r = await self._one(f"SELECT {_PROFILE_COLS_SQL} FROM model_profiles WHERE id = %s", (id,))
        return ModelProfile(**r) if r else None

    async def add(self, data: ProfileCreate) -> ModelProfile:
        async with self.conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(
                    "INSERT INTO model_profiles "
                    "(name, role, provider_id, model_ref, params, est_memory_mb, is_active) "
                    "VALUES (%s,%s,%s,%s,%s,%s,false) RETURNING id",
                    (data.name, data.role, data.provider_id, data.model_ref, Jsonb(data.params), data.est_memory_mb),
                )
            except pge.UniqueViolation as e:
                raise ConflictError("profile name already exists") from e
            row = await cur.fetchone()
        return await self.get(row["id"])  # type: ignore[return-value]

    async def update(self, id: UUID, fields: dict[str, Any]) -> ModelProfile:
        cols = [k for k in fields if k in _PROFILE_COLS]
        sets = ", ".join(f"{c} = %s" for c in cols)
        await self._exec(
            f"UPDATE model_profiles SET {sets} WHERE id = %s",
            [_wrap(fields[c]) for c in cols] + [id],
        )
        return await self.get(id)  # type: ignore[return-value]

    async def delete(self, id: UUID) -> None:
        await self._exec("DELETE FROM model_profiles WHERE id = %s", (id,))

    async def get_active(self, role: str) -> ModelProfile | None:
        r = await self._one(
            f"SELECT {_PROFILE_COLS_SQL} FROM model_profiles WHERE role = %s AND is_active",
            (role,),
        )
        return ModelProfile(**r) if r else None

    async def set_active(self, id: UUID, active: bool) -> ModelProfile:
        await self._exec("UPDATE model_profiles SET is_active = %s WHERE id = %s", (active, id))
        return await self.get(id)  # type: ignore[return-value]

    async def count_by_provider(self, provider_id: UUID) -> int:
        r = await self._one("SELECT count(*) AS n FROM model_profiles WHERE provider_id = %s", (provider_id,))
        return r["n"]  # type: ignore[index]


class PgSettings(_Base):
    async def all(self) -> dict[str, Any]:
        rows = await self._all("SELECT key, value FROM app_settings")
        return {r["key"]: r["value"] for r in rows}

    async def get(self, key: str) -> Any | None:
        r = await self._one("SELECT value FROM app_settings WHERE key = %s", (key,))
        return r["value"] if r else None

    async def set(self, key: str, value: Any) -> None:
        await self._exec(
            "INSERT INTO app_settings (key, value) VALUES (%s,%s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (key, Jsonb(value)),
        )


class PgAudit(_Base):
    async def add(self, entity, entity_id, action, old_value, new_value) -> None:
        await self._exec(
            "INSERT INTO config_audit_logs (entity, entity_id, action, old_value, new_value) VALUES (%s,%s,%s,%s,%s)",
            (
                entity,
                entity_id,
                action,
                None if old_value is None else Jsonb(old_value),
                None if new_value is None else Jsonb(new_value),
            ),
        )

    async def list(self, limit: int) -> list[AuditLog]:
        rows = await self._all(
            "SELECT id, entity, entity_id, action, old_value, new_value, changed_at "
            "FROM config_audit_logs ORDER BY id DESC LIMIT %s",
            (limit,),
        )
        return [AuditLog(**r) for r in rows]


class PgUnitOfWork:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def __aenter__(self) -> "PgUnitOfWork":
        self._cm = self._pool.connection()
        conn = await self._cm.__aenter__()
        self.providers = PgProviders(conn)
        self.profiles = PgProfiles(conn)
        self.settings = PgSettings(conn)
        self.audit = PgAudit(conn)
        return self

    async def __aexit__(self, *exc) -> None:
        # pool.connection(): commits on clean exit, rolls back on exception
        await self._cm.__aexit__(*exc)
