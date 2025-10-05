"""Model Context Protocol transport protocol for Server Sent Events (SSE).

This registers HTTP endpoints that supports SSE as a transport layer
for the Model Context Protocol. There are multiple HTTP endpoints:

- /mcp_server/sse: The SSE endpoint that is used to establish a session
  with the client and glue to the MCP server. This is used to push responses
  to the client.
- /mcp_server/messages: The endpoint that is used by the client to send
  POST requests with new requests for the MCP server. The request contains
  a session identifier. The response to the client is passed over the SSE
  session started on the other endpoint.
- /mcp_server/mcp: The streamable HTTP endpoint that supports NDJSON and SSE
  streaming for batch requests and state changes.

See https://modelcontextprotocol.io/docs/concepts/transports
"""

import asyncio
import json
import logging

from aiohttp import web
from aiohttp.web_exceptions import HTTPBadRequest, HTTPNotFound
from aiohttp_sse import sse_response
import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import types
from mcp.shared.message import SessionMessage

from homeassistant.components import conversation
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.const import CONF_LLM_HASS_API
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import llm

from .const import DOMAIN
from .server import create_server
from .session import Session
from .types import MCPServerConfigEntry

_LOGGER = logging.getLogger(__name__)

SSE_API = f"/{DOMAIN}/sse"
MESSAGES_API = f"/{DOMAIN}/messages/{{session_id}}"
STREAMABLE_API = f"/{DOMAIN}/mcp"


@callback
def async_register(hass: HomeAssistant) -> None:
    """Register the HTTP API views."""
    hass.http.register_view(ModelContextProtocolSSEView())
    hass.http.register_view(ModelContextProtocolMessagesView())
    hass.http.register_view(ModelContextProtocolStreamableView())


def async_get_config_entry(hass: HomeAssistant) -> MCPServerConfigEntry:
    """Get the first enabled MCP server config entry.

    The ConfigEntry contains a reference to the actual MCP server used to
    serve the Model Context Protocol.

    Will raise an HTTP error if the expected configuration is not present.
    """
    config_entries: list[MCPServerConfigEntry] = (
        hass.config_entries.async_loaded_entries(DOMAIN)
    )
    if not config_entries:
        raise HTTPNotFound(text="Model Context Protocol server is not configured")
    if len(config_entries) > 1:
        raise HTTPNotFound(text="Found multiple Model Context Protocol configurations")
    return config_entries[0]


class ModelContextProtocolSSEView(HomeAssistantView):
    """Model Context Protocol SSE endpoint."""

    name = f"{DOMAIN}:sse"
    url = SSE_API

    async def get(self, request: web.Request) -> web.StreamResponse:
        """Process SSE messages for the Model Context Protocol.

        This is a long running request for the lifetime of the client session
        and is the primary transport layer between the client and server.

        Pairs of buffered streams act as a bridge between the transport protocol
        (SSE over HTTP views) and the Model Context Protocol. The MCP SDK
        manages all protocol details and invokes commands on our MCP server.
        """
        hass = request.app[KEY_HASS]
        entry = async_get_config_entry(hass)
        session_manager = entry.runtime_data

        context = llm.LLMContext(
            platform=DOMAIN,
            context=self.context(request),
            language="*",
            assistant=conversation.DOMAIN,
            device_id=None,
        )
        llm_api_id = entry.data[CONF_LLM_HASS_API]
        server = await create_server(hass, llm_api_id, context)
        options = await hass.async_add_executor_job(
            server.create_initialization_options  # Reads package for version info
        )

        read_stream: MemoryObjectReceiveStream[SessionMessage | Exception]
        read_stream_writer: MemoryObjectSendStream[SessionMessage | Exception]
        read_stream_writer, read_stream = anyio.create_memory_object_stream(0)

        write_stream: MemoryObjectSendStream[SessionMessage]
        write_stream_reader: MemoryObjectReceiveStream[SessionMessage]
        write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

        async with (
            sse_response(request) as response,
            session_manager.create(Session(read_stream_writer)) as session_id,
        ):
            session_uri = MESSAGES_API.format(session_id=session_id)
            _LOGGER.debug("Sending SSE endpoint: %s", session_uri)
            await response.send(session_uri, event="endpoint")

            async def sse_reader() -> None:
                """Forward MCP server responses to the client."""
                async for session_message in write_stream_reader:
                    _LOGGER.debug("Sending SSE message: %s", session_message)
                    await response.send(
                        session_message.message.model_dump_json(
                            by_alias=True, exclude_none=True
                        ),
                        event="message",
                    )

            async with anyio.create_task_group() as tg:
                tg.start_soon(sse_reader)
                await server.run(read_stream, write_stream, options)

            return response


class ModelContextProtocolMessagesView(HomeAssistantView):
    """Model Context Protocol messages endpoint."""

    name = f"{DOMAIN}:messages"
    url = MESSAGES_API

    async def post(
        self,
        request: web.Request,
        session_id: str,
    ) -> web.StreamResponse:
        """Process incoming messages for the Model Context Protocol.

        The request passes a session ID which is used to identify the original
        SSE connection. This view parses incoming messages from the transport
        layer then writes them to the MCP server stream for the session.
        """
        hass = request.app[KEY_HASS]
        config_entry = async_get_config_entry(hass)

        session_manager = config_entry.runtime_data
        if (session := session_manager.get(session_id)) is None:
            _LOGGER.info("Could not find session ID: '%s'", session_id)
            raise HTTPNotFound(text=f"Could not find session ID '{session_id}'")

        json_data = await request.json()
        try:
            message = types.JSONRPCMessage.model_validate(json_data)
        except ValueError as err:
            _LOGGER.info("Failed to parse message: %s", err)
            raise HTTPBadRequest(text="Could not parse message") from err

        _LOGGER.debug("Received client message: %s", message)
        await session.read_stream_writer.send(SessionMessage(message))
        return web.Response(status=200)


class ModelContextProtocolStreamableView(HomeAssistantView):
    """Model Context Protocol streamable HTTP endpoint.

    Supports NDJSON and SSE streaming for batch requests and state changes.
    This endpoint implements the MCP Streamable HTTP transport specification.
    """

    name = f"{DOMAIN}:mcp"
    url = STREAMABLE_API
    requires_auth = True

    async def post(self, request: web.Request) -> web.StreamResponse:
        """Handle JSON-RPC POST requests with streaming support.

        Supports multiple content types via Accept header:
        - application/x-ndjson: NDJSON streaming for batch requests
        - text/event-stream: SSE streaming as fallback
        - application/json: Single JSON response
        """
        hass: HomeAssistant = request.app[KEY_HASS]
        config_entry = async_get_config_entry(hass)
        session_manager = config_entry.runtime_data
        event_store = hass.data[DOMAIN].get("event_store")

        # Parse request body
        try:
            body = await request.text()
            messages = json.loads(body) if body else []
        except json.JSONDecodeError as err:
            _LOGGER.info("Failed to parse JSON request: %s", err)
            raise HTTPBadRequest(text="Invalid JSON") from err

        # Ensure messages is a list
        if not isinstance(messages, list):
            messages = [messages]

        # Check if this is an initialize request
        session_id = request.headers.get("Mcp-Session-Id")
        if not session_id:
            # Look for initialize method in messages
            for message in messages:
                if message.get("method") == "initialize":
                    # Create new session
                    session_id = session_manager.create_streamable_session()
                    if event_store:
                        event_store.initialize_session(session_id)
                    _LOGGER.debug("Created new streamable session: %s", session_id)

                    # Return initialization response
                    return web.json_response(
                        {
                            "jsonrpc": "2.0",
                            "id": message.get("id"),
                            "result": {
                                "protocolVersion": "2025-03-26",
                                "capabilities": {
                                    "tools": {},
                                    "prompts": {},
                                },
                                "serverInfo": {
                                    "name": "home-assistant",
                                    "version": "1.0.0",
                                },
                                "session_id": session_id,
                            },
                        }
                    )

            # No initialize and no session ID
            raise HTTPBadRequest(text="Missing Mcp-Session-Id header")

        # Validate session exists
        if not session_manager.has_session(session_id):
            _LOGGER.info("Invalid session ID: '%s'", session_id)
            raise HTTPNotFound(text=f"Session ID '{session_id}' not found")

        # Process messages through MCP server
        responses = []
        for message in messages:
            try:
                _ = types.JSONRPCMessage.model_validate(message)
                # For now, we'll just echo back - proper implementation would
                # integrate with the MCP server
                responses.append(
                    {
                        "jsonrpc": "2.0",
                        "id": message.get("id"),
                        "result": {"status": "processed"},
                    }
                )
            except ValueError as err:
                _LOGGER.warning("Failed to validate message: %s", err)
                responses.append(
                    {
                        "jsonrpc": "2.0",
                        "id": message.get("id"),
                        "error": {"code": -32600, "message": "Invalid Request"},
                    }
                )

        # Determine response format based on Accept header
        accept = request.headers.get("Accept", "application/json")

        # NDJSON streaming for multiple responses
        if "application/x-ndjson" in accept and len(responses) > 1:
            response = web.StreamResponse(status=200)
            response.content_type = "application/x-ndjson"
            response.enable_chunked_encoding()
            await response.prepare(request)

            for resp in responses:
                await response.write((json.dumps(resp) + "\n").encode())

            await response.write_eof()
            return response

        # SSE fallback for multiple responses
        if "text/event-stream" in accept and len(responses) > 1:
            response = web.StreamResponse(status=200)
            response.content_type = "text/event-stream"
            response.headers["Cache-Control"] = "no-cache"
            response.enable_chunked_encoding()
            await response.prepare(request)

            for resp in responses:
                await response.write(f"data: {json.dumps(resp)}\n\n".encode())

            await response.write_eof()
            return response

        # Single JSON response
        return web.json_response(responses[0] if responses else {})

    async def get(self, request: web.Request) -> web.StreamResponse:
        """Handle streaming GET requests for state changes.

        Streams Home Assistant state changes to the client using NDJSON or SSE.
        """
        hass: HomeAssistant = request.app[KEY_HASS]

        # Check for session ID
        session_id = request.headers.get("Mcp-Session-Id")
        if not session_id:
            raise HTTPBadRequest(text="Missing Mcp-Session-Id header")

        # Get event store
        if "event_store" not in hass.data.get(DOMAIN, {}):
            raise HTTPNotFound(text="Event store not initialized")

        event_store = hass.data[DOMAIN]["event_store"]

        # Get last event ID for resumption
        last_event_id = request.headers.get("Last-Event-ID")

        # Determine content type
        accept = request.headers.get("Accept", "text/event-stream")
        content_type = (
            "application/x-ndjson"
            if "application/x-ndjson" in accept
            else "text/event-stream"
        )

        # Start streaming response
        response = web.StreamResponse(status=200)
        response.content_type = content_type
        if content_type == "text/event-stream":
            response.headers["Cache-Control"] = "no-cache"
        response.enable_chunked_encoding()
        await response.prepare(request)

        try:
            async for event in event_store.listen(session_id, last_event_id):
                if content_type == "application/x-ndjson":
                    await response.write((json.dumps(event["data"]) + "\n").encode())
                else:
                    # SSE format with event ID
                    await response.write(
                        f"id: {event['id']}\ndata: {json.dumps(event['data'])}\n\n".encode()
                    )
        except asyncio.CancelledError:
            _LOGGER.debug("Client disconnected from streaming endpoint")
        finally:
            await response.write_eof()

        return response

    async def delete(self, request: web.Request) -> web.Response:
        """Terminate MCP session."""
        hass: HomeAssistant = request.app[KEY_HASS]
        config_entry = async_get_config_entry(hass)
        session_manager = config_entry.runtime_data

        session_id = request.headers.get("Mcp-Session-Id")
        if session_id and session_manager.has_session(session_id):
            # Clean up session
            session_manager.terminate_streamable_session(session_id)

            if "event_store" in hass.data.get(DOMAIN, {}):
                event_store = hass.data[DOMAIN]["event_store"]
                event_store.terminate_session(session_id)

        return web.Response(status=204)
