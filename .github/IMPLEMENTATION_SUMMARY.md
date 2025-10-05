# MCP Server Streamable HTTP Endpoint Implementation

## Summary

Successfully implemented a new streamable HTTP endpoint (`/mcp_server/mcp`) for the Home Assistant MCP Server integration, adding NDJSON and SSE streaming support while preserving the existing SSE-only endpoint.

## Changes Made

### 1. Dependencies
- **File**: `homeassistant/components/mcp_server/manifest.json`
- **Change**: Added `fastmcp==0.5.0` to requirements

### 2. Event Store Module
- **File**: `homeassistant/components/mcp_server/event_store.py` (NEW)
- **Purpose**: Streams Home Assistant state changes to MCP clients
- **Features**:
  - Subscribes to HA state change events
  - Buffers events per session
  - Supports resumption via `Last-Event-ID`
  - Async generator for streaming
  - Event notifications for new state changes

### 3. HTTP Endpoints
- **File**: `homeassistant/components/mcp_server/http.py`
- **Changes**:
  - Added `ModelContextProtocolStreamableView` class
  - **POST `/mcp_server/mcp`**:
    - Handles session initialization (initialize method)
    - Processes JSON-RPC batch requests
    - Supports multiple content types:
      - `application/x-ndjson` for NDJSON streaming
      - `text/event-stream` for SSE fallback
      - `application/json` for single responses
  - **GET `/mcp_server/mcp`**:
    - Streams state change events
    - Supports NDJSON and SSE formats
    - Resumption via `Last-Event-ID` header
  - **DELETE `/mcp_server/mcp`**:
    - Terminates streamable sessions
    - Cleans up event store resources

### 4. Session Management
- **File**: `homeassistant/components/mcp_server/session.py`
- **Changes**:
  - Added `create_streamable_session()` method
  - Added `has_session()` method for session validation
  - Added `terminate_streamable_session()` method
  - Updated `close()` to handle streamable sessions (stored as `None`)

### 5. Integration Setup
- **File**: `homeassistant/components/mcp_server/__init__.py`
- **Changes**:
  - Initialize `MCPEventStore` in `async_setup()`
  - Store event store in `hass.data[DOMAIN]`

### 6. Tests
- **File**: `tests/components/mcp_server/test_http.py`
- **Added Tests**:
  - `test_streamable_initialize`: Tests session initialization
  - `test_streamable_ndjson_batch`: Tests NDJSON batch request handling
  - `test_streamable_missing_session`: Tests error handling for missing sessions
  - `test_streamable_delete_session`: Tests session termination

## Key Features

1. **NDJSON Streaming**: Supports `application/x-ndjson` for M365 Copilot compatibility
2. **SSE Fallback**: Maintains SSE support via `text/event-stream`
3. **Session Management**: Unique session IDs via `Mcp-Session-Id` header
4. **Event Streaming**: Real-time Home Assistant state changes
5. **Resumption**: Supports resuming streams via `Last-Event-ID`
6. **Backward Compatibility**: Preserves existing `/mcp_server/sse` endpoint

## API Usage

### Initialize Session
```bash
curl -X POST http://localhost:8123/mcp_server/mcp \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "initialize"}'
```

### NDJSON Batch Request
```bash
curl -X POST http://localhost:8123/mcp_server/mcp \
  -H "Authorization: Bearer <token>" \
  -H "Mcp-Session-Id: <session_id>" \
  -H "Accept: application/x-ndjson" \
  -H "Content-Type: application/json" \
  -d '[{"jsonrpc": "2.0", "id": 2, "method": "test"}]'
```

### Stream State Changes
```bash
curl http://localhost:8123/mcp_server/mcp \
  -H "Authorization: Bearer <token>" \
  -H "Mcp-Session-Id: <session_id>" \
  -H "Accept: application/x-ndjson"
```

### Terminate Session
```bash
curl -X DELETE http://localhost:8123/mcp_server/mcp \
  -H "Authorization: Bearer <token>" \
  -H "Mcp-Session-Id: <session_id>"
```

## Testing

All tests pass successfully:
```
pytest tests/components/mcp_server/test_http.py::test_streamable_* -v
```

## Next Steps

To complete the implementation as per the original instructions:

1. **Integrate with MCP Server**: Connect the streamable endpoint to the actual MCP server for tool execution
2. **Add Entity State Handlers**: Implement `get_entity_state` and `set_entity_state` methods
3. **CORS Configuration**: Document CORS setup for M365 Copilot
4. **Config Flow Updates**: Add transport mode selection in config flow
5. **Documentation**: Update integration docs with new endpoint details

## Compliance

- ✅ Passes `ruff` linting and formatting
- ✅ Passes `hassfest` validation
- ✅ All tests passing
- ✅ Follows Home Assistant coding standards
- ✅ Preserves backward compatibility
