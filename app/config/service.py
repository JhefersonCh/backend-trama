from collections.abc import Callable
from typing import Any
from uuid import UUID

from app.shared.crypto import SecretCipher

from .errors import ConflictError, DomainValidationError, NotFoundError
from .models import (
    ActivationResult,
    AuditLog,
    ModelProfile,
    ProfileCreate,
    ProfileUpdate,
    Provider,
    ProviderCreate,
    ProviderUpdate,
)
from .ports import UnitOfWork
from .setting_rules import validate_setting

_NON_NULL_PROVIDER = ("name", "kind", "base_url", "enabled")
_NON_NULL_PROFILE = ("name", "role", "provider_id", "model_ref", "params")


def _dump(m: Any) -> dict[str, Any]:
    return m.model_dump(mode="json")


def _reject_nulls(fields: dict[str, Any], keys: tuple[str, ...]) -> None:
    for k in keys:
        if k in fields and fields[k] is None:
            raise DomainValidationError(f"{k} cannot be null")


class ConfigService:
    """Business rules for configuration. Every mutation audits in the same transaction."""

    def __init__(self, uow_factory: Callable[[], UnitOfWork], cipher: SecretCipher) -> None:
        self._uow = uow_factory
        self._cipher = cipher

    # ---- providers
    async def list_providers(self) -> list[Provider]:
        async with self._uow() as u:
            return await u.providers.list()

    async def get_provider(self, id: UUID) -> Provider:
        async with self._uow() as u:
            return await self._provider(u, id)

    async def create_provider(self, data: ProviderCreate) -> Provider:
        async with self._uow() as u:
            p = await u.providers.add(data)
            await u.audit.add("provider", str(p.id), "create", None, _dump(p))
            return p

    async def update_provider(self, id: UUID, data: ProviderUpdate) -> Provider:
        fields = data.model_dump(exclude_unset=True)
        _reject_nulls(fields, _NON_NULL_PROVIDER)
        async with self._uow() as u:
            old = await self._provider(u, id)
            new = await u.providers.update(id, fields) if fields else old
            await u.audit.add("provider", str(id), "update", _dump(old), _dump(new))
            return new

    async def delete_provider(self, id: UUID) -> None:
        async with self._uow() as u:
            old = await self._provider(u, id)
            if await u.profiles.count_by_provider(id) > 0:
                raise ConflictError("provider is referenced by model profiles")
            await u.providers.delete(id)
            await u.audit.add("provider", str(id), "delete", _dump(old), None)

    async def set_secret(self, id: UUID, value: str) -> None:
        async with self._uow() as u:
            await self._provider(u, id)
            await u.providers.set_secret(id, self._cipher.encrypt(value))
            await u.audit.add("provider", str(id), "secret_set", None, None)

    async def delete_secret(self, id: UUID) -> None:
        async with self._uow() as u:
            await self._provider(u, id)
            if await u.providers.delete_secret(id):
                await u.audit.add("provider", str(id), "secret_deleted", None, None)

    # ---- model profiles
    async def list_profiles(self, role: str | None) -> list[ModelProfile]:
        async with self._uow() as u:
            return await u.profiles.list(role)

    async def get_profile(self, id: UUID) -> ModelProfile:
        async with self._uow() as u:
            return await self._profile(u, id)

    async def create_profile(self, data: ProfileCreate) -> ModelProfile:
        async with self._uow() as u:
            if await u.providers.get(data.provider_id) is None:
                raise DomainValidationError("provider_id does not exist")
            p = await u.profiles.add(data)  # always inactive
            await u.audit.add("model_profile", str(p.id), "create", None, _dump(p))
            return p

    async def update_profile(self, id: UUID, data: ProfileUpdate) -> ModelProfile:
        fields = data.model_dump(exclude_unset=True)
        _reject_nulls(fields, _NON_NULL_PROFILE)
        async with self._uow() as u:
            old = await self._profile(u, id)
            if old.is_active and "role" in fields and fields["role"] != old.role:
                raise ConflictError("cannot change role of an active profile")
            if "provider_id" in fields and await u.providers.get(fields["provider_id"]) is None:
                raise DomainValidationError("provider_id does not exist")
            new = await u.profiles.update(id, fields) if fields else old
            await u.audit.add("model_profile", str(id), "update", _dump(old), _dump(new))
            return new

    async def delete_profile(self, id: UUID) -> None:
        async with self._uow() as u:
            old = await self._profile(u, id)
            if old.is_active:
                raise ConflictError("cannot delete an active profile")
            await u.profiles.delete(id)
            await u.audit.add("model_profile", str(id), "delete", _dump(old), None)

    async def activate_profile(self, id: UUID) -> ActivationResult:
        async with self._uow() as u:
            target = await self._profile(u, id)
            provider = await u.providers.get(target.provider_id)
            if provider is None or not provider.enabled:
                raise ConflictError("provider is disabled")
            prev = await u.profiles.get_active(target.role)
            if prev is not None and prev.id == target.id:
                return ActivationResult(profile=target, reindex_required=False)
            if prev is not None:
                await u.profiles.set_active(prev.id, False)
                await u.audit.add("model_profile", str(prev.id), "deactivate", _dump(prev), None)
            new = await u.profiles.set_active(id, True)
            await u.audit.add(
                "model_profile",
                str(id),
                "activate",
                _dump(prev) if prev else None,
                _dump(new),
            )
            return ActivationResult(profile=new, reindex_required=_reindex(target.role, prev, target))

    # ---- app settings
    async def get_settings(self) -> dict[str, Any]:
        async with self._uow() as u:
            return await u.settings.all()

    async def put_setting(self, key: str, value: Any) -> Any:
        clean = validate_setting(key, value)
        async with self._uow() as u:
            old = await u.settings.get(key)
            await u.settings.set(key, clean)
            await u.audit.add("app_setting", key, "update", old, clean)
            return clean

    # ---- audit
    async def list_audit(self, limit: int) -> list[AuditLog]:
        async with self._uow() as u:
            return await u.audit.list(limit)

    # ---- helpers
    @staticmethod
    async def _provider(u: UnitOfWork, id: UUID) -> Provider:
        p = await u.providers.get(id)
        if p is None:
            raise NotFoundError("provider not found")
        return p

    @staticmethod
    async def _profile(u: UnitOfWork, id: UUID) -> ModelProfile:
        p = await u.profiles.get(id)
        if p is None:
            raise NotFoundError("model profile not found")
        return p


def _reindex(role: str, prev: ModelProfile | None, new: ModelProfile) -> bool:
    if role != "embedding" or prev is None:
        return False
    # Vectors from different models live in different spaces even if the dimension matches.
    if prev.model_ref != new.model_ref:
        return True
    return prev.params.get("dimension") != new.params.get("dimension")
