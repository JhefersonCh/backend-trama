"""In-memory UnitOfWork for endpoint tests."""

import copy
from datetime import UTC, datetime
from uuid import uuid4

from app.config.errors import ConflictError
from app.config.models import AuditLog, ModelProfile, Provider


def _now():
    return datetime.now(UTC)


class FakeStore:
    def __init__(self):
        self.providers: dict = {}
        self.secrets: dict = {}
        self.profiles: dict = {}
        self.settings: dict = {}
        self.audit: list = []


class _Providers:
    def __init__(self, s: FakeStore):
        self.s = s

    def _view(self, p):
        return p.model_copy(update={"has_secret": p.id in self.s.secrets})

    async def list(self):
        return [self._view(p) for p in self.s.providers.values()]

    async def get(self, id):
        p = self.s.providers.get(id)
        return self._view(p) if p else None

    async def add(self, data):
        if any(p.name == data.name for p in self.s.providers.values()):
            raise ConflictError("provider name already exists")
        p = Provider(id=uuid4(), has_secret=False, created_at=_now(), updated_at=_now(), **data.model_dump())
        self.s.providers[p.id] = p
        return p

    async def update(self, id, fields):
        p = self.s.providers[id].model_copy(update=fields)
        self.s.providers[id] = p
        return self._view(p)

    async def delete(self, id):
        del self.s.providers[id]

    async def set_secret(self, id, ciphertext):
        self.s.secrets[id] = ciphertext

    async def delete_secret(self, id):
        return self.s.secrets.pop(id, None) is not None


class _Profiles:
    def __init__(self, s: FakeStore):
        self.s = s

    async def list(self, role):
        return [p for p in self.s.profiles.values() if role in (None, p.role)]

    async def get(self, id):
        return self.s.profiles.get(id)

    async def add(self, data):
        p = ModelProfile(
            id=uuid4(),
            measured_memory_mb=None,
            is_active=False,
            created_at=_now(),
            updated_at=_now(),
            **data.model_dump(),
        )
        self.s.profiles[p.id] = p
        return p

    async def update(self, id, fields):
        p = self.s.profiles[id].model_copy(update=fields)
        self.s.profiles[id] = p
        return p

    async def delete(self, id):
        del self.s.profiles[id]

    async def get_active(self, role):
        return next((p for p in self.s.profiles.values() if p.role == role and p.is_active), None)

    async def set_active(self, id, active):
        return await self.update(id, {"is_active": active})

    async def count_by_provider(self, provider_id):
        return sum(p.provider_id == provider_id for p in self.s.profiles.values())


class _Settings:
    def __init__(self, s: FakeStore):
        self.s = s

    async def all(self):
        return dict(self.s.settings)

    async def get(self, key):
        return self.s.settings.get(key)

    async def set(self, key, value):
        self.s.settings[key] = value


class _Audit:
    def __init__(self, s: FakeStore):
        self.s = s

    async def add(self, entity, entity_id, action, old_value, new_value):
        self.s.audit.append(
            AuditLog(
                id=len(self.s.audit) + 1,
                entity=entity,
                entity_id=entity_id,
                action=action,
                old_value=old_value,
                new_value=new_value,
                changed_at=_now(),
            )
        )

    async def list(self, limit):
        return list(reversed(self.s.audit))[:limit]


class FakeUnitOfWork:
    """Snapshot/rollback on exception, like a DB transaction."""

    def __init__(self, store: FakeStore):
        self.store = store
        self.providers = _Providers(store)
        self.profiles = _Profiles(store)
        self.settings = _Settings(store)
        self.audit = _Audit(store)

    async def __aenter__(self):
        self._snap = copy.deepcopy(self.store.__dict__)
        return self

    async def __aexit__(self, exc_type, *_):
        if exc_type:
            self.store.__dict__.update(self._snap)
