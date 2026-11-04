from fastapi import Request

from .manager import ModelManager


def get_model_manager(request: Request) -> ModelManager:
    return request.app.state.model_manager
