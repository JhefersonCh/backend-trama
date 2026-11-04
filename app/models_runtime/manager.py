"""Model manager: RAM-budgeted lifecycle of loaded models across runtimes (doc 4.3-4.6).

Single instance per process (app.state.model_manager). All loads/evictions are serialized by one asyncio.Lock.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.config.errors import ConflictError, DomainValidationError, NotFoundError
from app.config.models import ModelProfile, Provider
from app.runtimes.base import LoadedModel, RuntimeAdapter, RuntimeCallError

from .errors import RuntimeUnavailableError
from .models import LoadedOut, LoadResult, ProviderHealth, RuntimeStatus, TestResult
from .ports import RuntimeStore

log = logging.getLogger("trama.runtime")

AdapterBuilder = Callable[[Provider, str | None], RuntimeAdapter]
OnLoad = Callable[[], Awaitable[None]] | None  # awaited right before the manager really evicts/loads a model
_EPOCH = datetime.min.replace(tzinfo=UTC)


@dataclass
class _Entry:
    provider_id: UUID
    runtime_ref: str
    name: str
    profile: ModelProfile | None
    memory_mb: int | None
    source: str  # "manager" | "external"
    last_used: datetime | None = None
    busy: int = 0


@dataclass
class Lease:
    adapter: RuntimeAdapter
    profile: ModelProfile


def _runtime_ref(p: ModelProfile) -> str:
    return p.params.get("router_model") or p.model_ref


def _same(a: str, b: str) -> bool:
    return a == b or a.removesuffix(":latest") == b.removesuffix(":latest")


def _est(p: ModelProfile) -> int:
    return p.measured_memory_mb or p.est_memory_mb or 0


def _now() -> datetime:
    return datetime.now(UTC)


class ModelManager:
    def __init__(
        self,
        store: RuntimeStore,
        build_adapter: AdapterBuilder,
        decrypt: Callable[[bytes], str],
    ) -> None:
        self._store = store
        self._build = build_adapter
        self._decrypt = decrypt
        self._lock = asyncio.Lock()
        self._loaded: dict[tuple[UUID, str], _Entry] = {}
        self._adapters: dict[UUID, tuple[tuple, RuntimeAdapter]] = {}

    # ------------------------------------------------------------------ adapters
    async def adapter_for(self, provider: Provider) -> RuntimeAdapter:
        secret = await self._store.get_secret(provider.id)
        sig = (provider.kind, provider.base_url, secret)
        cached = self._adapters.get(provider.id)
        if cached and cached[0] == sig:
            return cached[1]
        if cached:
            await cached[1].aclose()
        token = self._decrypt(secret) if secret else None
        adapter = self._build(provider, token)
        self._adapters[provider.id] = (sig, adapter)
        return adapter

    async def aclose(self) -> None:
        for _, a in self._adapters.values():
            await a.aclose()
        self._adapters.clear()

    # ------------------------------------------------------------------ settings
    async def _cfg(self) -> tuple[int | None, bool]:
        s = await self._store.settings()
        budget = s.get("ram_budget_mb")
        return (budget if isinstance(budget, int) and not isinstance(budget, bool) else None), bool(
            s.get("strict_single_model", False)
        )

    # ------------------------------------------------------------------ reconcile
    async def reconcile(self) -> list[ProviderHealth]:
        """Sync bookkeeping with what runtimes report. Never raises on unreachable runtimes."""
        providers = await self._store.list_providers()
        profiles = await self._store.list_profiles()
        probes = await asyncio.gather(*(self._probe(p) for p in providers if p.enabled))
        by_id = {p.id: r for p, r in zip([p for p in providers if p.enabled], probes, strict=True)}
        out: list[ProviderHealth] = []
        for p in providers:
            reachable, latency, loaded = by_id.get(p.id, (False, None, None))
            out.append(
                ProviderHealth(provider_id=p.id, name=p.name, kind=p.kind, reachable=reachable, latency_ms=latency)
            )
            if not p.enabled:
                continue
            if not reachable:
                self._drop_provider(p.id)
            elif loaded is not None:
                self._apply(p.id, loaded, [x for x in profiles if x.provider_id == p.id])
        return out

    async def _probe(self, p: Provider) -> tuple[bool, int | None, list[LoadedModel] | None]:
        try:
            a = await self.adapter_for(p)
            t0 = time.perf_counter()
            ok = await a.health()
            latency = round((time.perf_counter() - t0) * 1000)
            if not ok:
                return False, None, None
            try:
                return True, latency, await a.list_loaded()
            except RuntimeCallError:
                return True, latency, None
        except Exception as e:  # a broken provider must never break status
            log.warning("probe failed for %s: %s", p.name, type(e).__name__)
            return False, None, None

    def _drop_provider(self, pid: UUID) -> None:
        for k in [k for k, e in self._loaded.items() if e.provider_id == pid]:
            del self._loaded[k]

    def _apply(self, pid: UUID, loaded: list[LoadedModel], profiles: list[ModelProfile]) -> None:
        seen: set[tuple[UUID, str]] = set()
        for m in loaded:
            known = next(
                (k for k, e in self._loaded.items() if e.provider_id == pid and _same(e.runtime_ref, m.model_ref)), None
            )
            if known:
                seen.add(known)
                if m.memory_mb:
                    self._loaded[known].memory_mb = m.memory_mb
                continue
            prof = next((x for x in profiles if _same(_runtime_ref(x), m.model_ref)), None)
            key = (pid, m.model_ref)
            self._loaded[key] = _Entry(
                pid,
                m.model_ref,
                prof.name if prof else m.model_ref,
                prof,
                m.memory_mb or (_est(prof) if prof else None),
                "external",
            )
            seen.add(key)
        for k in [k for k, e in self._loaded.items() if e.provider_id == pid and k not in seen]:
            del self._loaded[k]

    # ------------------------------------------------------------------ status
    async def status(self, evicted: list[str] | None = None, warning: str | None = None) -> RuntimeStatus | LoadResult:
        providers = await self.reconcile()
        return await self._snapshot(providers, evicted, warning)

    async def _snapshot(self, providers: list[ProviderHealth], evicted: list[str] | None, warning: str | None):
        budget, strict = await self._cfg()
        loaded = [
            LoadedOut(
                profile_id=e.profile.id if e.profile else None,
                name=e.name,
                role=e.profile.role if e.profile else None,
                memory_mb=e.memory_mb,
                source=e.source,  # type: ignore[arg-type]
                last_used_at=e.last_used,
            )
            for e in self._loaded.values()
        ]
        base = dict(
            ram_budget_mb=budget,
            strict_single_model=strict,
            used_mb=sum(e.memory_mb or 0 for e in self._loaded.values()),
            loaded=loaded,
            providers=providers,
        )
        if evicted is None:
            return RuntimeStatus(**base)
        return LoadResult(**base, evicted=evicted, warning=warning)

    # ------------------------------------------------------------------ load / unload
    async def load_profile(self, id: UUID) -> LoadResult:
        profile = await self._profile(id)
        _, evicted, warning = await self._ensure(profile)
        return await self.status(evicted=evicted, warning=warning)  # type: ignore[return-value]

    async def unload_profile(self, id: UUID) -> RuntimeStatus:
        profile = await self._profile(id)
        provider = await self._provider(profile.provider_id)
        async with self._lock:
            await self.reconcile()
            adapter = await self.adapter_for(provider)
            entry = self._find(profile)
            t0 = time.perf_counter()
            try:
                await adapter.unload(_runtime_ref(profile), profile.params)
            except RuntimeCallError as e:
                await self._event(profile.id, "error", t0, None)
                raise RuntimeUnavailableError(str(e)) from e
            if entry:
                del self._loaded[(entry.provider_id, entry.runtime_ref)]
                await self._event(profile.id, "unload", t0, entry.memory_mb)
        return await self.status()  # type: ignore[return-value]

    def _find(self, p: ModelProfile) -> _Entry | None:
        return next(
            (
                e
                for e in self._loaded.values()
                if e.provider_id == p.provider_id and _same(e.runtime_ref, _runtime_ref(p))
            ),
            None,
        )

    async def ensure_loaded(self, role: str, on_load: OnLoad = None) -> Lease:
        profile = await self._store.get_active(role)
        if profile is None:
            raise ConflictError(f"no active model profile for role '{role}'")
        await self._ensure(profile, on_load=on_load)
        return Lease(await self.adapter_for(await self._provider(profile.provider_id)), profile)

    @asynccontextmanager
    async def use(self, role: str, on_load: OnLoad = None) -> AsyncIterator[Lease]:
        """Lease the active model of `role`. `on_load` is awaited only if a model actually has to be loaded."""
        profile = await self._store.get_active(role)
        if profile is None:
            raise ConflictError(f"no active model profile for role '{role}'")
        async with self._lease(profile, on_load) as lease:
            yield lease

    @asynccontextmanager
    async def _lease(self, profile: ModelProfile, on_load: OnLoad = None) -> AsyncIterator[Lease]:
        entry, _, _ = await self._ensure(profile, acquire=True, on_load=on_load)
        try:
            yield Lease(await self.adapter_for(await self._provider(profile.provider_id)), profile)
        finally:
            entry.busy -= 1
            entry.last_used = _now()

    async def _refresh_one(self, provider: Provider) -> None:
        """Hot path: sync only this provider (2 local HTTP calls) instead of probing every runtime."""
        reachable, _, loaded = await self._probe(provider)
        if reachable and loaded is not None:
            profiles = [x for x in await self._store.list_profiles() if x.provider_id == provider.id]
            self._apply(provider.id, loaded, profiles)

    async def _ensure(
        self, profile: ModelProfile, acquire: bool = False, on_load: OnLoad = None
    ) -> tuple[_Entry, list[str], str | None]:
        provider = await self._provider(profile.provider_id)
        if not provider.enabled:
            raise ConflictError("provider is disabled")
        evicted: list[str] = []
        warning: str | None = None
        async with self._lock:
            await self._refresh_one(provider)
            entry = self._find(profile)
            if entry is None:  # a real load is needed: full picture of every runtime for the RAM budget
                await self.reconcile()
                entry = self._find(profile)
            adapter = await self.adapter_for(provider)
            if entry is None:
                budget, strict = await self._cfg()
                need = _est(profile)
                others = sorted(self._loaded.values(), key=lambda e: e.last_used or _EPOCH)
                victims: list[_Entry] = []
                if strict:
                    victims = others
                    remaining: list[_Entry] = []
                else:
                    remaining = others
                if budget is not None:
                    if need > budget:
                        if remaining or any(e.busy for e in victims):
                            raise ConflictError(f"model needs ~{need} MB but the RAM budget is {budget} MB")
                        warning = "exceeds_ram_budget"
                    else:
                        used = sum(e.memory_mb or 0 for e in self._loaded.values())
                        for e in list(remaining):
                            if used + need <= budget:
                                break
                            victims.append(e)
                            remaining.remove(e)
                            used -= e.memory_mb or 0
                        if used + need > budget:
                            raise ConflictError("not enough free RAM: remaining models are in use")
                if any(e.busy for e in victims):
                    raise ConflictError("cannot evict: a model is in use")
                if on_load is not None:
                    try:
                        await on_load()
                    except Exception as e:  # a notification must never break the load
                        log.warning("on_load callback failed: %s", type(e).__name__)
                for v in victims:
                    await self._evict(v)
                    evicted.append(v.name)
                entry = await self._do_load(profile, adapter)
            elif entry.profile is None:
                entry.profile = profile
            entry.source = "manager"
            entry.last_used = _now()
            if acquire:
                entry.busy += 1
        return entry, evicted, warning

    async def _evict(self, e: _Entry) -> None:
        provider = await self._provider(e.provider_id)
        adapter = await self.adapter_for(provider)
        t0 = time.perf_counter()
        try:
            await adapter.unload(e.runtime_ref, e.profile.params if e.profile else None)
        except RuntimeCallError as err:
            await self._event(e.profile.id if e.profile else None, "error", t0, e.memory_mb)
            raise RuntimeUnavailableError(f"could not evict {e.name}: {err}") from err
        self._loaded.pop((e.provider_id, e.runtime_ref), None)
        await self._event(e.profile.id if e.profile else None, "evict", t0, e.memory_mb)

    async def _do_load(self, profile: ModelProfile, adapter: RuntimeAdapter) -> _Entry:
        t0 = time.perf_counter()
        try:
            await adapter.load(profile)
            reported = await adapter.list_loaded()
        except RuntimeCallError as err:
            await self._event(profile.id, "error", t0, None)
            raise RuntimeUnavailableError(str(err)) from err
        mem = next(
            (m.memory_mb for m in reported or [] if _same(m.model_ref, _runtime_ref(profile)) and m.memory_mb), None
        )
        entry = _Entry(
            profile.provider_id, _runtime_ref(profile), profile.name, profile, mem or _est(profile) or None, "manager"
        )
        self._loaded[(entry.provider_id, entry.runtime_ref)] = entry
        await self._event(profile.id, "load", t0, entry.memory_mb)
        return entry

    # ------------------------------------------------------------------ probe
    async def warmup(
        self, roles: tuple[str, ...] = ("embedding", "llm"), attempts: int = 3, backoff: float = 5.0
    ) -> None:
        """Load the ACTIVE profile of each role (RAM budget respected by `_ensure`) and prime the LLM with a 1-token
        completion. Never raises: runtimes being down only logs a warning (retries with backoff)."""
        for role in roles:
            for n in range(1, attempts + 1):
                try:
                    async with self.use(role) as lease:
                        if role == "llm":
                            async for _ in lease.adapter.chat(
                                lease.profile.model_ref,
                                [{"role": "user", "content": "hola"}],
                                stream=False,
                                params={**lease.profile.params, "max_tokens": 1},
                            ):
                                pass
                    logging.getLogger("uvicorn.error").info("warm-up: %s ready (%s)", role, lease.profile.name)
                    break
                except Exception as e:
                    log.warning("warm-up %s attempt %d/%d failed: %s", role, n, attempts, type(e).__name__)
                    if n < attempts:
                        await asyncio.sleep(backoff * n)

    async def test_profile(self, id: UUID, audio: bytes | None = None, mime: str = "audio/wav") -> TestResult:
        profile = await self._profile(id)
        t0 = time.perf_counter()
        dimension: int | None = None
        skipped = False
        try:
            async with self._lease(profile) as lease:
                a, p = lease.adapter, profile.params
                if profile.role == "llm":
                    text = ""
                    async for d in a.chat(
                        profile.model_ref,
                        [{"role": "user", "content": "Decí hola en una frase corta, en español."}],
                        stream=False,
                        params={**p, "max_tokens": 128},
                    ):
                        text += d.content or d.thinking
                    ok, detail = bool(text.strip()), text.strip()[:200] or "empty completion"
                elif profile.role == "embedding":
                    vecs = await a.embed(profile.model_ref, ["prueba"], p)
                    dimension = len(vecs[0]) if vecs else 0
                    ok, detail = dimension > 0, f"dimension {dimension}"
                elif profile.role == "tts":
                    data = await a.synthesize(
                        profile.model_ref, "Hola, soy TRAMA", p.get("voice"), p.get("format", "wav"), p
                    )
                    ok, detail = len(data) > 0, f"{len(data)} bytes of audio"
                elif audio is None:
                    ok, skipped, detail = True, True, "loaded; no audio sample supplied, transcription skipped"
                else:
                    text = await a.transcribe(profile.model_ref, audio, mime, p.get("language"), p)
                    ok, detail = True, text[:200] or "(empty transcription)"
        except (RuntimeCallError, RuntimeUnavailableError) as e:
            return TestResult(ok=False, latency_ms=round((time.perf_counter() - t0) * 1000), detail=str(e))
        latency = round((time.perf_counter() - t0) * 1000)
        measured = None
        entry = self._find(profile)
        if entry and entry.memory_mb and entry.source == "manager":
            measured = await self._measured(profile, entry)
        return TestResult(
            ok=ok, latency_ms=latency, detail=detail, dimension=dimension, measured_memory_mb=measured, skipped=skipped
        )

    async def _measured(self, profile: ModelProfile, entry: _Entry) -> int | None:
        """Only runtime-reported memory counts as measured (not our own estimate)."""
        try:
            adapter = await self.adapter_for(await self._provider(profile.provider_id))
            reported = await adapter.list_loaded()
        except Exception:
            return None
        mem = next((m.memory_mb for m in reported or [] if _same(m.model_ref, entry.runtime_ref) and m.memory_mb), None)
        if mem:
            await self._store.set_measured_memory(profile.id, mem)
        return mem

    # ------------------------------------------------------------------ pull
    async def pull(self, provider_id: UUID, model_ref: str) -> AsyncIterator[dict[str, Any]]:
        provider = await self._provider(provider_id)
        if provider.kind != "ollama":
            raise DomainValidationError("pull is only supported for ollama providers")
        return (await self.adapter_for(provider)).pull(model_ref)

    # ------------------------------------------------------------------ helpers
    async def _profile(self, id: UUID) -> ModelProfile:
        p = await self._store.get_profile(id)
        if p is None:
            raise NotFoundError("model profile not found")
        return p

    async def _provider(self, id: UUID) -> Provider:
        p = await self._store.get_provider(id)
        if p is None:
            raise NotFoundError("provider not found")
        return p

    async def _event(self, profile_id: UUID | None, event: str, t0: float, mem: int | None) -> None:
        try:
            await self._store.add_event(profile_id, event, round((time.perf_counter() - t0) * 1000), mem)
        except Exception as e:  # auditing must not break the operation
            log.warning("could not record %s event: %s", event, type(e).__name__)
