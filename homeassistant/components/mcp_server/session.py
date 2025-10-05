"""Model Context Protocol sessions.

A session is a long-lived connection between the client and server that is used
to exchange messages. The server pushes messages to the client over the session
and the client sends messages to the server over the session.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import logging

from anyio.streams.memory import MemoryObjectSendStream
from mcp.shared.message import SessionMessage

from homeassistant.util import ulid as ulid_util

_LOGGER = logging.getLogger(__name__)


@dataclass
class Session:
    """A session for the Model Context Protocol."""

    read_stream_writer: MemoryObjectSendStream[SessionMessage | Exception]


class SessionManager:
    """Manage SSE sessions for the MCP transport layer.

    This class is used to manage the lifecycle of SSE sessions. It is responsible for
    creating new sessions, resuming existing sessions, and closing sessions.
    """

    def __init__(self) -> None:
        """Initialize the SSE server transport."""
        self._sessions: dict[str, Session] = {}

    @asynccontextmanager
    async def create(self, session: Session) -> AsyncGenerator[str]:
        """Context manager to create a new session ID and close when done."""
        session_id = ulid_util.ulid_now()
        _LOGGER.debug("Creating session: %s", session_id)
        self._sessions[session_id] = session
        try:
            yield session_id
        finally:
            _LOGGER.debug("Closing session: %s", session_id)
            if session_id in self._sessions:  # close() may have already been called
                self._sessions.pop(session_id)

    def create_streamable_session(self) -> str:
        """Create a new streamable session (without SSE streams).

        Returns:
            The session identifier.
        """
        session_id = ulid_util.ulid_now()
        _LOGGER.debug("Creating streamable session: %s", session_id)
        # For streamable sessions, we don't need the read_stream_writer
        # Just track that the session exists
        # We'll use None as a placeholder to indicate streamable session
        self._sessions[session_id] = None  # type: ignore[assignment]
        return session_id

    def get(self, session_id: str) -> Session | None:
        """Get an existing session.

        Returns the session if it exists, or None for a streamable session
        (which is valid), or None if the session doesn't exist.
        """
        return self._sessions.get(session_id)

    def has_session(self, session_id: str) -> bool:
        """Check if a session exists (works for both SSE and streamable sessions).

        Returns:
            True if the session exists, False otherwise.
        """
        return session_id in self._sessions

    def terminate_streamable_session(self, session_id: str) -> None:
        """Terminate a streamable session.

        Args:
            session_id: The session identifier to terminate.
        """
        if session_id in self._sessions:
            _LOGGER.debug("Terminating streamable session: %s", session_id)
            self._sessions.pop(session_id)

    def close(self) -> None:
        """Close any open sessions."""
        for session in self._sessions.values():
            # Streamable sessions are stored as None, skip them
            if session is not None:
                session.read_stream_writer.close()
        self._sessions.clear()
