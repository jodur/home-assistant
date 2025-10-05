"""Test the Model Context Protocol Server init module."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from http import HTTPStatus
import json
import logging

import aiohttp
import mcp
import mcp.client.session
import mcp.client.sse
from mcp.shared.exceptions import McpError
import pytest

from homeassistant.components.conversation import DOMAIN as CONVERSATION_DOMAIN
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.components.mcp_server.const import STATELESS_LLM_API
from homeassistant.components.mcp_server.http import MESSAGES_API, SSE_API
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_LLM_HASS_API, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    llm,
)
from homeassistant.setup import async_setup_component

from tests.common import MockConfigEntry, setup_test_component_platform
from tests.components.light.common import MockLight
from tests.typing import ClientSessionGenerator

_LOGGER = logging.getLogger(__name__)

TEST_ENTITY = "light.kitchen"
INITIALIZE_MESSAGE = {
    "jsonrpc": "2.0",
    "id": "request-id-1",
    "method": "initialize",
    "params": {
        "protocolVersion": "1.0",
        "capabilities": {},
        "clientInfo": {
            "name": "test",
            "version": "1",
        },
    },
}
EVENT_PREFIX = "event: "
DATA_PREFIX = "data: "
EXPECTED_PROMPT_SUFFIX = """
- names: Kitchen Light
  domain: light
  areas: Kitchen
"""


@pytest.fixture
async def setup_integration(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """Set up the config entry."""
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.LOADED


@pytest.fixture(autouse=True)
async def mock_entities(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    area_registry: ar.AreaRegistry,
    setup_integration: None,
) -> None:
    """Fixture to expose entities to the conversation agent."""
    entity = MockLight("Kitchen Light", STATE_OFF)
    entity.entity_id = TEST_ENTITY
    entity.unique_id = "test-light-unique-id"
    setup_test_component_platform(hass, LIGHT_DOMAIN, [entity])

    assert await async_setup_component(
        hass,
        LIGHT_DOMAIN,
        {LIGHT_DOMAIN: [{"platform": "test"}]},
    )
    await hass.async_block_till_done()
    kitchen = area_registry.async_get_or_create("Kitchen")
    entity_registry.async_update_entity(TEST_ENTITY, area_id=kitchen.id)

    async_expose_entity(hass, CONVERSATION_DOMAIN, TEST_ENTITY, True)


async def sse_response_reader(
    response: aiohttp.ClientResponse,
) -> AsyncGenerator[tuple[str, str]]:
    """Read SSE responses from the server and emit event messages.

    SSE responses are formatted as:
        event: event-name
        data: event-data
    and this function emits each event-name and event-data as a tuple.
    """
    it = aiter(response.content)
    while True:
        line = (await anext(it)).decode()
        if not line.startswith(EVENT_PREFIX):
            raise ValueError("Expected event")
        event = line[len(EVENT_PREFIX) :].strip()
        line = (await anext(it)).decode()
        if not line.startswith(DATA_PREFIX):
            raise ValueError("Expected data")
        data = line[len(DATA_PREFIX) :].strip()
        line = (await anext(it)).decode()
        assert line == "\r\n"
        yield event, data


async def test_http_sse(
    hass: HomeAssistant,
    setup_integration: None,
    hass_client: ClientSessionGenerator,
) -> None:
    """Test SSE endpoint can be used to receive MCP messages."""

    client = await hass_client()

    # Start an SSE session
    response = await client.get(SSE_API)
    assert response.status == HTTPStatus.OK

    # Decode a single SSE response that sends the messages endpoint
    reader = sse_response_reader(response)
    event, endpoint_url = await anext(reader)
    assert event == "endpoint"

    # Send an initialize message on the messages endpoint
    response = await client.post(endpoint_url, json=INITIALIZE_MESSAGE)
    assert response.status == HTTPStatus.OK

    # Decode the initialize response event message from the SSE stream
    event, data = await anext(reader)
    assert event == "message"
    message = json.loads(data)
    assert message.get("jsonrpc") == "2.0"
    assert message.get("id") == "request-id-1"
    assert "serverInfo" in message.get("result", {})
    assert "protocolVersion" in message.get("result", {})


async def test_http_messages_missing_session_id(
    hass: HomeAssistant,
    setup_integration: None,
    hass_client: ClientSessionGenerator,
) -> None:
    """Test the tools list endpoint."""

    client = await hass_client()
    response = await client.post(MESSAGES_API.format(session_id="invalid-session-id"))
    assert response.status == HTTPStatus.NOT_FOUND
    response_data = await response.text()
    assert response_data == "Could not find session ID 'invalid-session-id'"


async def test_http_messages_invalid_message_format(
    hass: HomeAssistant,
    setup_integration: None,
    hass_client: ClientSessionGenerator,
) -> None:
    """Test the tools list endpoint."""

    client = await hass_client()
    response = await client.get(SSE_API)
    assert response.status == HTTPStatus.OK
    reader = sse_response_reader(response)
    event, endpoint_url = await anext(reader)
    assert event == "endpoint"

    response = await client.post(endpoint_url, json={"invalid": "message"})
    assert response.status == HTTPStatus.BAD_REQUEST
    response_data = await response.text()
    assert response_data == "Could not parse message"


async def test_http_sse_multiple_config_entries(
    hass: HomeAssistant,
    setup_integration: None,
    hass_client: ClientSessionGenerator,
) -> None:
    """Test the SSE endpoint will fail with multiple config entries.

    This cannot happen in practice as the integration only supports a single
    config entry, but this is added for test coverage.
    """

    config_entry = MockConfigEntry(
        domain="mcp_server", data={CONF_LLM_HASS_API: ["llm-api-id"]}
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)

    client = await hass_client()

    # Attempt to start an SSE session will fail
    response = await client.get(SSE_API)
    assert response.status == HTTPStatus.NOT_FOUND
    response_data = await response.text()
    assert "Found multiple Model Context Protocol" in response_data


async def test_http_sse_no_config_entry(
    hass: HomeAssistant,
    setup_integration: None,
    config_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
) -> None:
    """Test the SSE endpoint fails with a missing config entry."""

    await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED

    client = await hass_client()

    # Start an SSE session
    response = await client.get(SSE_API)
    assert response.status == HTTPStatus.NOT_FOUND
    response_data = await response.text()
    assert "Model Context Protocol server is not configured" in response_data


async def test_http_messages_no_config_entry(
    hass: HomeAssistant,
    setup_integration: None,
    config_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
) -> None:
    """Test the message endpoint will fail if the config entry is unloaded."""

    client = await hass_client()

    # Start an SSE session
    response = await client.get(SSE_API)
    assert response.status == HTTPStatus.OK
    reader = sse_response_reader(response)
    event, endpoint_url = await anext(reader)
    assert event == "endpoint"

    # Invalidate the session by unloading the config entry
    await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED

    # Reload the config entry and ensure the session is not found
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.LOADED

    response = await client.post(endpoint_url, json=INITIALIZE_MESSAGE)
    assert response.status == HTTPStatus.NOT_FOUND
    response_data = await response.text()
    assert "Could not find session ID" in response_data


async def test_http_requires_authentication(
    hass: HomeAssistant,
    setup_integration: None,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """Test the SSE endpoint requires authentication."""

    client = await hass_client_no_auth()

    response = await client.get(SSE_API)
    assert response.status == HTTPStatus.UNAUTHORIZED

    response = await client.post(MESSAGES_API.format(session_id="session-id"))
    assert response.status == HTTPStatus.UNAUTHORIZED


@pytest.fixture
async def mcp_sse_url(hass_client: ClientSessionGenerator) -> str:
    """Fixture to get the MCP integration SSE URL."""
    client = await hass_client()
    return str(client.make_url(SSE_API))


@asynccontextmanager
async def mcp_session(
    mcp_sse_url: str,
    hass_supervisor_access_token: str,
) -> AsyncGenerator[mcp.client.session.ClientSession]:
    """Create an MCP session."""

    headers = {"Authorization": f"Bearer {hass_supervisor_access_token}"}

    async with (
        mcp.client.sse.sse_client(mcp_sse_url, headers=headers) as streams,
        mcp.client.session.ClientSession(*streams) as session,
    ):
        await session.initialize()
        yield session


@pytest.mark.parametrize("llm_hass_api", [llm.LLM_API_ASSIST, STATELESS_LLM_API])
async def test_mcp_tools_list(
    hass: HomeAssistant,
    setup_integration: None,
    mcp_sse_url: str,
    hass_supervisor_access_token: str,
) -> None:
    """Test the tools list endpoint."""

    async with mcp_session(mcp_sse_url, hass_supervisor_access_token) as session:
        result = await session.list_tools()

    # Pick a single arbitrary tool and test that description and parameters
    # are converted correctly.
    tool = next(iter(tool for tool in result.tools if tool.name == "HassTurnOn"))
    assert tool.name == "HassTurnOn"
    assert tool.description is not None
    assert tool.inputSchema
    assert tool.inputSchema.get("type") == "object"
    properties = tool.inputSchema.get("properties")
    assert properties.get("name") == {"type": "string"}


@pytest.mark.parametrize("llm_hass_api", [llm.LLM_API_ASSIST, STATELESS_LLM_API])
async def test_mcp_tool_call(
    hass: HomeAssistant,
    setup_integration: None,
    mcp_sse_url: str,
    hass_supervisor_access_token: str,
) -> None:
    """Test the tool call endpoint."""

    state = hass.states.get("light.kitchen")
    assert state
    assert state.state == STATE_OFF

    async with mcp_session(mcp_sse_url, hass_supervisor_access_token) as session:
        result = await session.call_tool(
            name="HassTurnOn",
            arguments={"name": "kitchen light"},
        )

    assert not result.isError
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    # The content is the raw tool call payload
    content = json.loads(result.content[0].text)
    assert content.get("data", {}).get("success")
    assert not content.get("data", {}).get("failed")

    # Verify tool call invocation
    state = hass.states.get("light.kitchen")
    assert state
    assert state.state == STATE_ON


async def test_mcp_tool_call_failed(
    hass: HomeAssistant,
    setup_integration: None,
    mcp_sse_url: str,
    hass_supervisor_access_token: str,
) -> None:
    """Test the tool call endpoint with a failure."""

    async with mcp_session(mcp_sse_url, hass_supervisor_access_token) as session:
        result = await session.call_tool(
            name="HassTurnOn",
            arguments={"name": "backyard"},
        )

    assert result.isError
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    assert "Error calling tool" in result.content[0].text


@pytest.mark.parametrize("llm_hass_api", [llm.LLM_API_ASSIST, STATELESS_LLM_API])
async def test_prompt_list(
    hass: HomeAssistant,
    setup_integration: None,
    mcp_sse_url: str,
    hass_supervisor_access_token: str,
) -> None:
    """Test the list prompt endpoint."""

    async with mcp_session(mcp_sse_url, hass_supervisor_access_token) as session:
        result = await session.list_prompts()

    assert len(result.prompts) == 1
    prompt = result.prompts[0]
    assert prompt.name == "Assist"
    assert prompt.description == "Default prompt for Home Assistant Assist API"


@pytest.mark.parametrize("llm_hass_api", [llm.LLM_API_ASSIST, STATELESS_LLM_API])
async def test_prompt_get(
    hass: HomeAssistant,
    setup_integration: None,
    mcp_sse_url: str,
    hass_supervisor_access_token: str,
) -> None:
    """Test the get prompt endpoint."""

    async with mcp_session(mcp_sse_url, hass_supervisor_access_token) as session:
        result = await session.get_prompt(name="Assist")

    assert result.description == "Default prompt for Home Assistant Assist API"
    assert len(result.messages) == 1
    assert result.messages[0].role == "assistant"
    assert result.messages[0].content.type == "text"
    assert "When controlling Home Assistant" in result.messages[0].content.text
    assert result.messages[0].content.text.endswith(EXPECTED_PROMPT_SUFFIX)


async def test_get_unknwon_prompt(
    hass: HomeAssistant,
    setup_integration: None,
    mcp_sse_url: str,
    hass_supervisor_access_token: str,
) -> None:
    """Test the get prompt endpoint."""

    async with mcp_session(mcp_sse_url, hass_supervisor_access_token) as session:
        with pytest.raises(McpError):
            await session.get_prompt(name="Unknown")


async def test_streamable_initialize(
    hass: HomeAssistant,
    setup_integration: None,
    hass_client: ClientSessionGenerator,
) -> None:
    """Test the streamable endpoint initialization."""
    client = await hass_client()

    # Send initialize request
    resp = await client.post(
        "/mcp_server/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1.0"},
            },
        },
    )

    assert resp.status == HTTPStatus.OK
    data = await resp.json()
    assert data["jsonrpc"] == "2.0"
    assert data["id"] == 1
    assert "result" in data
    assert "session_id" in data["result"]
    assert data["result"]["protocolVersion"] == "2025-03-26"


async def test_streamable_ndjson_batch(
    hass: HomeAssistant,
    setup_integration: None,
    hass_client: ClientSessionGenerator,
) -> None:
    """Test the streamable endpoint with NDJSON batch requests."""
    client = await hass_client()

    # Initialize session first
    init_resp = await client.post(
        "/mcp_server/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
        },
    )
    init_data = await init_resp.json()
    session_id = init_data["result"]["session_id"]

    # Send batch request with NDJSON accept header
    resp = await client.post(
        "/mcp_server/mcp",
        headers={
            "Mcp-Session-Id": session_id,
            "Accept": "application/x-ndjson",
        },
        json=[
            {"jsonrpc": "2.0", "id": 2, "method": "test1"},
            {"jsonrpc": "2.0", "id": 3, "method": "test2"},
        ],
    )

    assert resp.status == HTTPStatus.OK
    assert resp.content_type == "application/x-ndjson"

    # Read NDJSON responses
    text = await resp.text()
    lines = [line for line in text.split("\n") if line]
    assert len(lines) == 2

    response1 = json.loads(lines[0])
    assert response1["jsonrpc"] == "2.0"
    assert response1["id"] == 2

    response2 = json.loads(lines[1])
    assert response2["jsonrpc"] == "2.0"
    assert response2["id"] == 3


async def test_streamable_missing_session(
    hass: HomeAssistant,
    setup_integration: None,
    hass_client: ClientSessionGenerator,
) -> None:
    """Test the streamable endpoint with missing session ID."""
    client = await hass_client()

    resp = await client.post(
        "/mcp_server/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "test"},
    )

    assert resp.status == HTTPStatus.BAD_REQUEST


async def test_streamable_delete_session(
    hass: HomeAssistant,
    setup_integration: None,
    hass_client: ClientSessionGenerator,
) -> None:
    """Test deleting a streamable session."""
    client = await hass_client()

    # Initialize session
    init_resp = await client.post(
        "/mcp_server/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
        },
    )
    init_data = await init_resp.json()
    session_id = init_data["result"]["session_id"]

    # Delete session
    resp = await client.delete(
        "/mcp_server/mcp",
        headers={"Mcp-Session-Id": session_id},
    )

    assert resp.status == HTTPStatus.NO_CONTENT

    # Verify session is gone
    resp = await client.post(
        "/mcp_server/mcp",
        headers={"Mcp-Session-Id": session_id},
        json={"jsonrpc": "2.0", "id": 2, "method": "test"},
    )

    assert resp.status == HTTPStatus.NOT_FOUND
