# GitHub Copilot Agent Mode Instructions for MCP Server Rewrite

Rewrite the MCP server in Home Assistant's `dev` branch to add a `/mcp_server/mcp` Streamable HTTP endpoint using `fastmcp` for JSON-RPC and `aiohttp` (HA's `http` component) for transport, avoiding ASGI (e.g., PR #153362's `mangum`). Support NDJSON streaming (`application/x-ndjson`) for M365 Copilot, SSE fallback (`text/event-stream`), and preserve `/mcp_server/sse`. Handle state requests/sets (e.g., `light.kitchen`). Manage CORS via `http` component's `cors_allowed_origins`. Use GitHub Copilot Agent Mode (Copilot Chat or inline suggestions) in VSCode on a dedicated branch. Align with MCP spec (2025-03-26) and HA's PR review process ([Home Assistant Review Process](https://developers.home-assistant.io/docs/review-process)).

## Prerequisites

- **VSCode**: Installed with GitHub Copilot, Python, GitLens, and Markdown Preview Enhanced extensions. Enable Copilot Agent Mode (Copilot Chat: Ctrl+Shift+I or inline comments).
- **Dev Environment**: HA dev setup with dedicated branch (e.g., `feature/mcp-streamable-http`).
- **Knowledge**: Python, `asyncio`, `aiohttp`, MCP spec ([link](https://docs.anthropic.com/en/docs/tool-use#mcp-spec)), HA architecture (`__init__.py`, `http.py`, `config_flow.py`), CORS via `cors_allowed_origins`, HA PR process ([link](https://developers.home-assistant.io/docs/review-process)).
- **Reference**: PR #153362 for `fastmcp` structure, but use HA `http` component.

## Implementation Steps

Navigate to `homeassistant/components/mcp_server`. Use Copilot Agent Mode (e.g., type `# Add NDJSON streaming` or ask Copilot Chat, "Generate FastMCP server for HA"). Ensure HA PR compliance (PEP 8 via `ruff`, docstrings, tests, minimal scope).

### 1. Update Dependencies
Add `fastmcp` to `pyproject.toml` (or `requirements.txt`):
```toml
[project]
dependencies = ["fastmcp>=0.2.0"]
```
- **Copilot**: Ask, "Add fastmcp to pyproject.toml." Run `pip install -r requirements.txt`.
- **PR**: Ensure minimal, compatible dependencies.

### 2. Refactor Server (`server.py`)
Use `FastMCPServer` for MCP handling with NDJSON streaming.
```python
from fastmcp import FastMCPServer
from homeassistant.core import HomeAssistant
from typing import Dict
from .session import MCPSessionManager

class HAServer(FastMCPServer):
    def __init__(self, hass: HomeAssistant):
        super().__init__(version="2025-03-26")
        self.hass = hass
        self.manager = MCPSessionManager()
        self.add_tool("get_entity_state", self._get_state_handler)
        self.add_tool("set_entity_state", self._set_state_handler)
        self.transports["streamable_http"] = {
            "use_sse": False,
            "content_type": "application/x-ndjson",
            "streaming": True
        }

    async def _get_state_handler(self, params: Dict) -> Dict:
        """Get HA entity state."""
        entity_id = params.get("entity_id")
        state = self.hass.states.get(entity_id)
        return {"state": state.state if state else "unavailable"}

    async def _set_state_handler(self, params: Dict) -> Dict:
        """Set HA entity state."""
        entity_id = params.get("entity_id")
        state = params.get("state")
        await self.hass.services.async_call(
            "homeassistant", "turn_on" if state == "on" else "turn_off",
            {"entity_id": entity_id}
        )
        return {"success": True}

    async def handle_stream(self, messages: list, session_id: str) -> list:
        """Process batch messages for streaming."""
        return await self.handle_batch(messages)
```
- **Copilot**: Type `# Subclass FastMCPServer` or ask, "Generate FastMCPServer for HA." Add tools manually.
- **PR**: Use `ruff`, add docstrings, keep minimal.

### 3. Add HTTP Endpoints (`http.py`)
Add `/mcp_server/mcp` with NDJSON/SSE support. CORS via `http` component.
```python
from aiohttp import web
from homeassistant.components.http import HomeAssistantView, KEY_AUTHENTICATED
import json
from typing import Optional
from .server import HAServer

class MCPStreamView(HomeAssistantView):
    """Handle MCP Streamable HTTP endpoint."""
    url = "/mcp_server/mcp"
    name = "mcp_server:mcp"

    async def post(self, request: web.Request) -> web.Response:
        """Handle JSON-RPC POST requests."""
        if not request.app[KEY_AUTHENTICATED]:
            raise web.HTTPUnauthorized()
        server: HAServer = request.app["hass"].data["mcp_server"]
        session_id = request.headers.get("Mcp-Session-Id")
        if not session_id or not server.manager.get(session_id):
            raise web.HTTPBadRequest(reason="Invalid or missing Mcp-Session-Id")

        body = await request.text()
        messages = json.loads(body) if body else []
        responses = await server.handle_stream(messages, session_id)

        accept = request.headers.get("Accept", "application/json")
        if "application/x-ndjson" in accept and len(responses) > 1:
            response = web.Response(status=200, content_type="application/x-ndjson")
            response.enable_chunked_encoding()
            async with response:
                for resp in responses:
                    await response.write((json.dumps(resp) + "\n").encode())
            return response
        elif "text/event-stream" in accept and len(responses) > 1:
            response = web.Response(status=200, content_type="text/event-stream")
            response.enable_chunked_encoding()
            async with response:
                for resp in responses:
                    await response.write(f"data: {json.dumps(resp)}\n\n".encode())
            return response
        return web.json_response(responses[0] if responses else {})

    async def get(self, request: web.Request) -> web.Response:
        """Handle streaming GET requests."""
        if not request.app[KEY_AUTHENTICATED]:
            raise web.HTTPUnauthorized()
        session_id = request.headers.get("Mcp-Session-Id")
        last_event_id = request.headers.get("Last-Event-ID")
        if not session_id:
            raise web.HTTPBadRequest(reason="Missing Mcp-Session-Id")

        accept = request.headers.get("Accept", "application/json")
        content_type = "application/x-ndjson" if "application/x-ndjson" in accept else "text/event-stream"
        response = web.Response(status=200, content_type=content_type)
        response.enable_chunked_encoding()
        async with response:
            async for event in request.app["hass"].data["event_store"].listen(session_id, last_event_id):
                if content_type == "application/x-ndjson":
                    await response.write((json.dumps(event) + "\n").encode())
                else:
                    await response.write(f"data: {json.dumps(event)}\n\n".encode())
        return response

    async def delete(self, request: web.Request) -> web.Response:
        """Terminate MCP session."""
        if not request.app[KEY_AUTHENTICATED]:
            raise web.HTTPUnauthorized()
        session_id = request.headers.get("Mcp-Session-Id")
        if session_id:
            server: HAServer = request.app["hass"].data["mcp_server"]
            server.manager.terminate(session_id)
        return web.Response(status=204)
```
- **Copilot**: Ask, "Generate aiohttp view for MCP with NDJSON/SSE." Remove manual CORS headers.
- **PR**: Add docstrings, test all paths (NDJSON, SSE, JSON). CORS via `configuration.yaml`:
  ```yaml
  http:
    cors_allowed_origins:
      - https://copilot.microsoft.com  # Adjust for M365 Copilot
  ```
- **Note**: Use `*` for testing; restrict in production.

### 4. Add Session Management (`session.py`)
Handle `Mcp-Session-Id` for sessions.
```python
from uuid import uuid4
from typing import Dict, Optional

class MCPSession:
    """MCP session with unique ID."""
    def __init__(self):
        self.id = str(uuid4())
        self.active = True

    def terminate(self):
        """Mark session as terminated."""
        self.active = False

class MCPSessionManager:
    """Manages MCP sessions."""
    def __init__(self):
        self.sessions: Dict[str, MCPSession] = {}

    def create(self) -> str:
        """Create new session."""
        session = MCPSession()
        self.sessions[session.id] = session
        return session.id

    def get(self, session_id: str) -> Optional[MCPSession]:
        """Retrieve session by ID."""
        return self.sessions.get(session_id)

    def terminate(self, session_id: str):
        """Terminate session."""
        if session_id in self.sessions:
            self.sessions[session_id].terminate()
            del self.sessions[session_id]
```
- **Copilot**: Ask, "Generate UUID session manager for MCP." Add docstrings.
- **PR**: Use type hints, keep minimal.

### 5. Add Event Store (`event_store.py`)
Stream HA state changes as NDJSON/SSE.
```python
from typing import AsyncGenerator, Optional
from collections import defaultdict
from homeassistant.core import HomeAssistant, Event
import asyncio

class MCPEventStore:
    """Stores and streams HA state change events."""
    def __init__(self, hass: HomeAssistant):
        self.hass = hass
        self.events: Dict[str, list] = defaultdict(list)
        self.hass.bus.async_add_listener("state_changed", self._handle_state_change)

    def _handle_state_change(self, event: Event):
        """Handle HA state changes."""
        for session_id in list(self.events.keys()):
            self.events[session_id].append({
                "jsonrpc": "2.0",
                "method": "state_changed",
                "params": {
                    "entity_id": event.data.get("entity_id"),
                    "new_state": event.data.get("new_state", {}).get("state")
                }
            })

    async def listen(self, session_id: str, last_event_id: Optional[str] = None) -> AsyncGenerator[Dict, None]:
        """Stream events with resumption."""
        start_idx = int(last_event_id) + 1 if last_event_id else 0
        for event in self.events[session_id][start_idx:]:
            yield event
        while True:
            await asyncio.sleep(1)  # Polling; use asyncio.Event in prod
            new_events = self.events[session_id][start_idx:]
            if new_events:
                for event in new_events:
                    yield event
                start_idx += len(new_events)
```
- **Copilot**: Type `# Async generator for HA events` or ask, "Generate HA event store."
- **PR**: Add docstrings. Note polling-to-`asyncio.Event` upgrade in PR.

### 6. Update Setup (`__init__.py`)
Initialize server, sessions, and HTTP view.
```python
from homeassistant.core import HomeAssistant
from .server import HAServer
from .http import MCPStreamView
from .session import MCPSessionManager
from .event_store import MCPEventStore

DOMAIN = "mcp_server"

async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up MCP Server component."""
    server = HAServer(hass)
    manager = MCPSessionManager()
    event_store = MCPEventStore(hass)
    hass.data[DOMAIN] = {
        "mcp_server": server,
        "manager": manager,
        "event_store": event_store
    }
    hass.http.register_view(MCPStreamView)
    return True
```
- **Copilot**: Ask, "Generate HA component setup with HTTP view."
- **PR**: Add docstring, preserve SSE setup.

### 7. Update Config Flow (`config_flow.py`)
Add transport mode selection.
```python
import voluptuous as vol
from homeassistant import config_entries

class MCPConfigFlow(config_entries.ConfigFlow, domain="mcp_server"):
    """MCP Server config flow."""
    async def async_step_user(self, user_input=None):
        """Handle user config."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(
                title="MCP Server",
                data={"transport_mode": user_input.get("transport_mode", "sse")}
            )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Optional("transport_mode", default="sse"): vol.In(["sse", "streamable_http"])
            })
        )
```
- **Copilot**: Ask, "Generate HA config flow for transport selection."
- **PR**: Add docstrings, keep simple.

## Testing
1. **Start Server**: Run `script/hass`, access `http://localhost:8123`.
2. **Add Integration**: HA UI > Settings > Devices & Services > Add "Model Context Protocol Server" > Select `streamable_http`.
3. **CORS**: Add to `configuration.yaml`:
   ```yaml
   http:
     cors_allowed_origins:
       - https://copilot.microsoft.com
   ```
4. **Test Endpoints** (Postman/curl):
   - **Initialize**:
     ```
     curl -X POST -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
     -d '{"jsonrpc": "2.0", "id": 1, "method": "initialize"}' http://localhost:8123/mcp_server/mcp
     ```
     Expect: `{"jsonrpc": "2.0", "id": 1, "result": {"transports": {"streamable_http": {}}, "session_id": "<uuid>"}}`.
   - **NDJSON Batch**:
     ```
     curl -X POST -H "Authorization: Bearer <token>" -H "Mcp-Session-Id: <uuid>" \
     -H "Accept: application/x-ndjson" -H "Content-Type: application/json" \
     -d '[{"jsonrpc": "2.0", "id": 2, "method": "get_entity_state", "params": {"entity_id": "light.kitchen"}}, {"jsonrpc": "2.0", "id": 3, "method": "set_entity_state", "params": {"entity_id": "light.kitchen", "state": "off"}}]' \
     http://localhost:8123/mcp_server/mcp
     ```
     Expect: NDJSON response.
   - **SSE Fallback**:
     ```
     curl -X POST -H "Authorization: Bearer <token>" -H "Mcp-Session-Id: <uuid>" \
     -H "Accept: text/event-stream" -H "Content-Type: application/json" \
     -d '[{"jsonrpc": "2.0", "id": 2, "method": "get_entity_state", "params": {"entity_id": "light.kitchen"}}]' \
     http://localhost:8123/mcp_server/mcp
     ```
     Expect: SSE `data: {...}\n\n`.
   - **Streaming GET**:
     ```
     curl -H "Authorization: Bearer <token>" -H "Mcp-Session-Id: <uuid>" \
     -H "Accept: application/x-ndjson" http://localhost:8123/mcp_server/mcp
     ```
     Toggle `light.kitchen`; expect NDJSON events.
   - **Legacy SSE**:
     ```
     curl -H "Authorization: Bearer <token>" http://localhost:8123/mcp_server/sse
     ```
5. **M365 Copilot**:
   ```javascript
   const initResp = await fetch('http://localhost:8123/mcp_server/mcp', {
       method: 'POST',
       headers: { 'Authorization': 'Bearer <token>', 'Content-Type': 'application/json' },
       body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize' })
   });
   const { session_id } = (await initResp.json()).result;
   const streamResp = await fetch('http://localhost:8123/mcp_server/mcp', {
       method: 'POST',
       headers: {
           'Authorization': 'Bearer <token>',
           'Content-Type': 'application/json',
           'Mcp-Session-Id': session_id,
           'Accept': 'application/x-ndjson'
       },
       body: JSON.stringify([{ jsonrpc: '2.0', id: 2, method: 'get_entity_state', params: { entity_id: 'light.kitchen' } }])
   });
   const reader = streamResp.body.getReader();
   let result = '';
   while (true) {
       const { done, value } = await reader.read();
       if (done) break;
       result += new TextDecoder().decode(value);
   }
   console.log(result.split('\n').filter(line => line).map(JSON.parse));
   ```
   Verify CORS and NDJSON parsing.
6. **Debug**: Set breakpoints (F9) in VSCode, run debugger (F5, "Python: Current File"), check logs (`hass --debug`). Ask Copilot Chat, "Debug aiohttp streaming."

## Submitting PR
Follow HA PR process ([link](https://developers.home-assistant.io/docs/review-process)):
1. **Commit**: Use GitLens: `git add .`, `git commit -m "Add /mcp_server/mcp with NDJSON"`. Ask Copilot Chat, "Suggest commit message."
2. **Push**: `git push origin feature/mcp-streamable-http`.
3. **Create PR**:
   - Target: `home-assistant/core:dev`.
   - **Title**: "Add Streamable HTTP Endpoint to MCP Server".
   - **Description**:
     - Add `/mcp_server/mcp` with NDJSON, SSE fallback, and legacy `/mcp_server/sse`.
     - Reference PR #153362, note HA `http` usage, CORS via `cors_allowed_origins`.
     - Confirm HA PR compliance (style, tests, docs).
   - Complete PR checklist.
   - Copilot: Ask, "Draft HA PR description for MCP endpoint."
4. **PR Compliance**:
   - **Code**: Run `ruff check --fix && ruff format`. Use type hints, docstrings.
   - **Tests**: Add to `tests/components/mcp_server/test_http.py`:
     ```python
     async def test_mcp_post_ndjson(hass, aiohttp_client):
         """Test NDJSON streaming."""
         # Test POST /mcp_server/mcp
     ```
     Copilot: Ask, "Generate HA unit tests for NDJSON streaming."
   - **Docs**: Update `homeassistant/components/mcp_server/README.md` for endpoint and CORS.
   - **No Breaking Changes**: Preserve `/mcp_server/sse`.
   - **Scope**: Minimal, only state requests/sets.
   - **Labels/Reviewers**: Add `integration: mcp_server`, request code owners.
   - **Feedback**: Monitor GitHub, use VSCode PR extension. Ask Copilot Chat, "Fix ruff errors."

## Notes
- **MCP Spec**: NDJSON (`application/x-ndjson`) extends `text/event-stream`. Use `Accept` header for format.
- **M365 Copilot**: Needs `cors_allowed_origins` for `fetch` APIs.
- **Stateless**: HA manages states, sessions in-memory (Redis for scale).
- **PR #153362**: Guides `fastmcp`, but use `aiohttp`.
- **Challenges**:
  - Non-NDJSON clients use SSE or `/mcp_server/sse`.
  - Polling in `event_store.py`; note `asyncio.Event` for prod.
  - Restrict `cors_allowed_origins` in production.
- **PR Tips**: Test all endpoints/formats, respond to feedback, avoid scope creep.

## Timeline
- **Prototype**: 4-6 hours.
- **PR-Ready**: 1-2 days (tests, polish).
- **Review**: 1-2 weeks (monitor GitHub).

For issues, check logs (`hass --debug`) or ask Copilot Chat, "Why is my MCP stream failing?"