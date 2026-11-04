"""WebSocket frame contract (client -> server)."""

from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, TypeAdapter


class StartSession(BaseModel):
    type: Literal["start_session"]
    session_id: str | None = None
    voice: bool = False


class PageEntity(BaseModel):
    type: Literal["ticket", "case", "technician"]
    id: str = Field(min_length=1, max_length=64, pattern=r"^[\w.\-]+$")


class PageContext(BaseModel):
    """Where the person is in the UI (floating assistant). Optional, never contains contact data."""

    route: str = Field(default="", max_length=200)
    entity: PageEntity | None = None


class TextMessage(BaseModel):
    type: Literal["text_message"]
    turn_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=8000)
    context: PageContext | None = None


class AudioSegment(BaseModel):
    type: Literal["audio_segment"]
    turn_id: str = Field(min_length=1, max_length=100)
    mime: str = "audio/wav"
    data: str


class Cancel(BaseModel):
    type: Literal["cancel"]
    turn_id: str


class SetVoice(BaseModel):
    type: Literal["set_voice"]
    voice: bool


class PlaybackProgress(BaseModel):
    type: Literal["playback_progress"]
    turn_id: str
    played_seq: int


class ConfirmResponse(BaseModel):
    """Answer to a server `confirm_request` (e.g. ticket_create); runs a turn `<turn_id>-confirm`."""

    type: Literal["confirm_response"]
    turn_id: str = Field(min_length=1, max_length=100)
    action: str = Field(default="ticket_create", max_length=60)
    accepted: bool


class NotificationAck(BaseModel):
    type: Literal["notification_ack"]
    id: str = Field(min_length=1, max_length=100)


class UiEvent(BaseModel):
    """Interaction with a generative-UI component (contract §2). Starts a turn, like a text message."""

    type: Literal["ui_event"]
    turn_id: str = Field(default_factory=lambda: f"ui-{uuid4().hex[:8]}", min_length=1, max_length=100)
    component_id: str = Field(default="", max_length=100)
    component: str = Field(min_length=1, max_length=40)
    action: str = Field(min_length=1, max_length=40)
    payload: dict[str, Any] = Field(default_factory=dict)


class MediaMessage(BaseModel):
    """A photo or audio already uploaded with POST /api/media (contract §3). Starts a turn."""

    type: Literal["media_message"]
    turn_id: str = Field(min_length=1, max_length=100)
    media_id: str = Field(min_length=1, max_length=100)
    text: str | None = Field(default=None, max_length=8000)


ClientFrame = Annotated[
    StartSession
    | TextMessage
    | AudioSegment
    | Cancel
    | SetVoice
    | PlaybackProgress
    | ConfirmResponse
    | NotificationAck
    | UiEvent
    | MediaMessage,
    Field(discriminator="type"),
]
client_frame = TypeAdapter(ClientFrame)
