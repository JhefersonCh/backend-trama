from fastapi.requests import HTTPConnection

from .service import ConversationService


def get_conversation_service(conn: HTTPConnection) -> ConversationService:
    return conn.app.state.conversation_service
