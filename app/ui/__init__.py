"""Generative UI: closed component catalog (Pydantic) + pure builders. The backend decides what is shown;
the LLM only writes the sentence that introduces it."""

from .builders import confidence_label, ui_component
from .models import COMPONENTS, EVENT_ACTIONS

__all__ = ["COMPONENTS", "EVENT_ACTIONS", "confidence_label", "ui_component"]
