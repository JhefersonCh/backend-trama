"""Fake runtime store + adapter for model-manager endpoint tests."""

from app.runtimes.base import ChatDelta, LoadedModel
from tests.fakes import FakeStore


class FakeRuntimeStore:
    def __init__(self, s: FakeStore):
        self.s = s
        self.events: list[tuple] = []

    async def list_providers(self):
        return list(self.s.providers.values())

    async def get_provider(self, id):
        return self.s.providers.get(id)

    async def list_profiles(self):
        return list(self.s.profiles.values())

    async def get_profile(self, id):
        return self.s.profiles.get(id)

    async def get_active(self, role):
        return next((p for p in self.s.profiles.values() if p.role == role and p.is_active), None)

    async def get_secret(self, provider_id):
        return self.s.secrets.get(provider_id)

    async def settings(self):
        return dict(self.s.settings)

    async def set_measured_memory(self, profile_id, mb):
        self.s.profiles[profile_id] = self.s.profiles[profile_id].model_copy(update={"measured_memory_mb": mb})

    async def add_event(self, profile_id, event, duration_ms, memory_mb):
        self.events.append((profile_id, event, memory_mb))


class FakeAdapter:
    """Pretends to be a runtime; `sizes` maps model_ref -> MB reported once loaded."""

    supports_management = True

    def __init__(self, kind, sizes):
        self.kind = kind
        self.sizes = sizes
        self.loaded: dict[str, int] = {}
        self.up = True

    async def health(self):
        return self.up

    async def list_loaded(self):
        return [LoadedModel(r, mb) for r, mb in self.loaded.items()]

    async def load(self, profile):
        self.loaded[profile.model_ref] = self.sizes.get(profile.model_ref, 0)

    async def unload(self, model_ref, params=None):
        self.loaded.pop(model_ref, None)

    async def chat(self, model, messages, tools=None, stream=True, params=None):
        yield ChatDelta(content="hola", done=True)

    async def embed(self, model, texts, params=None):
        return [[0.1] * 768 for _ in texts]

    async def pull(self, model_ref):
        yield {"status": "pulling", "completed": 1, "total": 2}
        yield {"status": "success"}

    async def aclose(self):
        pass
