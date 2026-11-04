from app.config.errors import ConflictError


class ReindexRequiredError(ConflictError):
    """Chunks exist but none is embedded for the active embedding profile -> 409."""
