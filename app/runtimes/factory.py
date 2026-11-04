from app.config.models import Provider

from .base import RuntimeAdapter
from .ollama import OllamaAdapter
from .openai_like import LlamaCppAdapter, OpenAICompatAdapter, VoiceServiceAdapter

_KINDS = {
    "ollama": OllamaAdapter,
    "llamacpp": LlamaCppAdapter,
    "openai_compatible": OpenAICompatAdapter,
    "voice_service": VoiceServiceAdapter,
}


def build_adapter(provider: Provider, token: str | None = None) -> RuntimeAdapter:
    return _KINDS[provider.kind](provider.base_url, token)  # type: ignore[return-value]
