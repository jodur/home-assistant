#!/bin/bash
# Quick test script for MCP endpoint

set -e

# Configuration
BASE_URL="${BASE_URL:-http://localhost:8123}"
TOKEN="${HA_TOKEN:-}"

if [ -z "$TOKEN" ]; then
  echo "ERROR: Please set HA_TOKEN environment variable"
  echo "Usage: HA_TOKEN=your_token ./test_mcp_endpoint.sh"
  exit 1
fi

echo "Testing MCP endpoint at: $BASE_URL"
echo "=========================================="
echo ""

# Test 1: Initialize session
echo "1. Initializing session..."
INIT_RESPONSE=$(curl -s -X POST "$BASE_URL/mcp_server/mcp" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2025-03-26",
      "capabilities": {},
      "clientInfo": {"name": "test", "version": "1.0"}
    }
  }')

echo "$INIT_RESPONSE" | jq '.'

# Extract session ID
SESSION_ID=$(echo "$INIT_RESPONSE" | jq -r '.result.session_id')

if [ "$SESSION_ID" = "null" ] || [ -z "$SESSION_ID" ]; then
  echo "ERROR: Failed to get session ID"
  exit 1
fi

echo ""
echo "Session ID: $SESSION_ID"
echo ""

# Test 2: NDJSON batch request
echo "2. Testing NDJSON batch request..."
curl -s -X POST "$BASE_URL/mcp_server/mcp" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -H "Accept: application/x-ndjson" \
  -H "Content-Type: application/json" \
  -d '[
    {"jsonrpc": "2.0", "id": 2, "method": "test1"},
    {"jsonrpc": "2.0", "id": 3, "method": "test2"}
  ]'

echo ""
echo ""

# Test 3: SSE/NDJSON streaming (GET) - with timeout
echo "3. Testing state change streaming (5 second timeout)..."
echo "   Toggle a light to see state changes..."
timeout 5 curl -s "$BASE_URL/mcp_server/mcp" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -H "Accept: application/x-ndjson" \
  || echo "No state changes in 5 seconds (this is normal if nothing changed)"

echo ""
echo ""

# Test 4: Delete session
echo "4. Deleting session..."
curl -s -X DELETE "$BASE_URL/mcp_server/mcp" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -w "\nHTTP Status: %{http_code}\n"

echo ""
echo "=========================================="
echo "Testing complete!"
