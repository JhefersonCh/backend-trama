from app.config.errors import DomainError


class RuntimeUnavailableError(DomainError):
    """Provider unreachable or failed a runtime call -> 502."""
