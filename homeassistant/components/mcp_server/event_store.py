"""Model Context Protocol event store for streaming state changes.

Stores and streams Home Assistant state change events to MCP clients.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncGenerator
import logging
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback

_LOGGER = logging.getLogger(__name__)


class MCPEventStore:
    """Store and stream Home Assistant state change events."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the event store."""
        self.hass = hass
        self.events: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._event_counters: dict[str, int] = defaultdict(int)
        self._listeners: dict[str, asyncio.Event] = {}

        # Subscribe to state changes
        hass.bus.async_listen("state_changed", self._handle_state_change)

    @callback
    def _handle_state_change(self, event: Event) -> None:
        """Handle Home Assistant state changes."""
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")

        if not entity_id or not new_state:
            return

        # Create MCP-compatible event
        mcp_event = {
            "jsonrpc": "2.0",
            "method": "notifications/state_changed",
            "params": {
                "entity_id": entity_id,
                "state": new_state.state,
                "attributes": dict(new_state.attributes),
            },
        }

        # Add to all active sessions
        for session_id in list(self.events.keys()):
            event_id = self._event_counters[session_id]
            self.events[session_id].append({"id": str(event_id), "data": mcp_event})
            self._event_counters[session_id] = event_id + 1

            # Notify waiting listeners
            if session_id in self._listeners:
                self._listeners[session_id].set()

    def initialize_session(self, session_id: str) -> None:
        """Initialize a new session for event streaming."""
        self.events[session_id] = []
        self._event_counters[session_id] = 0
        self._listeners[session_id] = asyncio.Event()

    def terminate_session(self, session_id: str) -> None:
        """Terminate a session and clean up resources."""
        self.events.pop(session_id, None)
        self._event_counters.pop(session_id, None)

        if session_id in self._listeners:
            self._listeners[session_id].set()  # Wake up any waiting listeners
            self._listeners.pop(session_id, None)

    async def listen(
        self, session_id: str, last_event_id: str | None = None
    ) -> AsyncGenerator[dict[str, Any]]:
        """Stream events for a session with resumption support.

        Args:
            session_id: The session identifier.
            last_event_id: The last event ID received by the client for resumption.

        Yields:
            Event dictionaries with 'id' and 'data' fields.
        """
        if session_id not in self.events:
            _LOGGER.warning("Session %s not found in event store", session_id)
            return

        # Determine starting position
        start_idx = 0
        if last_event_id is not None:
            try:
                start_idx = int(last_event_id) + 1
            except ValueError:
                _LOGGER.warning("Invalid last_event_id: %s", last_event_id)

        # Send any buffered events first
        for event in self.events[session_id][start_idx:]:
            yield event

        # Stream new events as they arrive
        current_idx = len(self.events[session_id])
        while session_id in self.events:
            # Wait for new events
            if session_id not in self._listeners:
                break

            listener_event = self._listeners[session_id]
            listener_event.clear()

            try:
                await asyncio.wait_for(listener_event.wait(), timeout=30.0)
            except TimeoutError:
                # Send keepalive or just continue
                continue

            # Yield new events
            new_events = self.events[session_id][current_idx:]
            for new_event in new_events:
                yield new_event
            current_idx = len(self.events[session_id])
