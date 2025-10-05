# Testing MCP Server with M365 Copilot

## Prerequisites

1. **Home Assistant running** with the MCP server integration enabled
2. **Access token** for authentication
3. **Network access** to your Home Assistant instance
4. **CORS configured** (see configuration.yaml)

## Configuration Steps

### 1. Configure CORS (Already Done)

The `configuration.yaml` has been updated with CORS settings for M365 Copilot:
- `https://copilot.microsoft.com`
- `https://*.microsoft.com`
- Local testing origins

### 2. Restart Home Assistant

After updating configuration.yaml:
```bash
# In VS Code terminal
cd /workspaces/home-assistant
# Stop current instance (if running in another terminal)
# Then restart:
python -m homeassistant -c ./config
```

Or use the Home Assistant UI: Developer Tools → Restart

### 3. Get a Long-Lived Access Token

**Via Home Assistant UI:**
1. Go to your profile (click your username in the sidebar)
2. Scroll down to "Long-Lived Access Tokens"
3. Click "Create Token"
4. Give it a name (e.g., "MCP Testing")
5. Copy the token (you won't see it again!)

**Via CLI (for testing):**
```bash
# Get the content_user from auth file
cat config/.storage/auth

# Or use the REST API to create a token (if you have an existing token)
```

### 4. Expose Home Assistant Externally

**Option A: Use VS Code Port Forwarding (Easiest!)**

1. Start Home Assistant (if not already running)
2. In VS Code, open the **Ports** panel:
   - Click "Ports" tab in the bottom panel
   - OR press `Ctrl+Shift+P` (or `Cmd+Shift+P` on Mac) → "Ports: Focus on Ports View"
3. Click "Forward a Port" and enter `8123`
4. **Make port public**:
   - Right-click on port 8123 → "Port Visibility" → "Public"
   - This gives you a public URL like: `https://abc123-8123.app.github.dev`
5. Copy the forwarded address and use it as your base URL

**Benefits:**
- ✅ No installation needed
- ✅ Automatic HTTPS
- ✅ Works with GitHub Codespaces and VS Code Remote
- ✅ Easy to start/stop

**Option B: Use ngrok (Alternative)**
```bash
# Install ngrok in dev container
mkdir -p ~/.local/bin
curl -sSL https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz | tar xz -C ~/.local/bin

# Start ngrok tunnel (will be available in PATH)
ngrok http 8123

# You'll get a URL like: https://abc123.ngrok.io
# Use this as your base URL
```

**Option C: Use Cloudflare Tunnel**
```bash
# Install cloudflared
curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared.deb

# Start tunnel
cloudflared tunnel --url http://localhost:8123
```

**Option D: Port Forwarding (Production Only)**
- Configure your router to forward port 8123
- Use a domain name with SSL certificate
- Consider using Home Assistant Cloud or DuckDNS add-on

## Testing the MCP Endpoint

### 1. Test Locally First

**Initialize Session:**
```bash
curl -X POST http://localhost:8123/mcp_server/mcp \
  -H "Authorization: Bearer YOUR_TOKEN_HERE" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2025-03-26",
      "capabilities": {},
      "clientInfo": {
        "name": "test-client",
        "version": "1.0"
      }
    }
  }'
```

Expected response:
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": "2025-03-26",
    "capabilities": {...},
    "serverInfo": {...},
    "session_id": "01K6T5..."
  }
}
```

**Test NDJSON Batch Request:**
```bash
# Save the session_id from above
SESSION_ID="01K6T5..."

curl -X POST http://localhost:8123/mcp_server/mcp \
  -H "Authorization: Bearer YOUR_TOKEN_HERE" \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -H "Accept: application/x-ndjson" \
  -H "Content-Type: application/json" \
  -d '[
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "prompts/list"}
  ]'
```

**Test State Change Streaming (GET):**
```bash
curl http://localhost:8123/mcp_server/mcp \
  -H "Authorization: Bearer YOUR_TOKEN_HERE" \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -H "Accept: application/x-ndjson"
```

### 2. Test from M365 Copilot

**JavaScript Example (for M365 Copilot Plugin):**
```javascript
// 1. Initialize session
const initResponse = await fetch('https://your-ngrok-url.ngrok.io/mcp_server/mcp', {
  method: 'POST',
  headers: {
    'Authorization': 'Bearer YOUR_TOKEN_HERE',
    'Content-Type': 'application/json'
  },
  body: JSON.stringify({
    jsonrpc: '2.0',
    id: 1,
    method: 'initialize',
    params: {
      protocolVersion: '2025-03-26',
      capabilities: {},
      clientInfo: {
        name: 'm365-copilot',
        version: '1.0'
      }
    }
  })
});

const initData = await initResponse.json();
const sessionId = initData.result.session_id;
console.log('Session ID:', sessionId);

// 2. Send batch request with NDJSON
const batchResponse = await fetch('https://your-ngrok-url.ngrok.io/mcp_server/mcp', {
  method: 'POST',
  headers: {
    'Authorization': 'Bearer YOUR_TOKEN_HERE',
    'Content-Type': 'application/json',
    'Mcp-Session-Id': sessionId,
    'Accept': 'application/x-ndjson'
  },
  body: JSON.stringify([
    { jsonrpc: '2.0', id: 2, method: 'tools/list' },
    { jsonrpc: '2.0', id: 3, method: 'prompts/list' }
  ])
});

// Read NDJSON response
const reader = batchResponse.body.getReader();
const decoder = new TextDecoder();
let buffer = '';

while (true) {
  const { done, value } = await reader.read();
  if (done) break;

  buffer += decoder.decode(value, { stream: true });
  const lines = buffer.split('\n');
  buffer = lines.pop(); // Keep incomplete line

  for (const line of lines) {
    if (line.trim()) {
      const response = JSON.parse(line);
      console.log('Response:', response);
    }
  }
}

// 3. Stream state changes
const streamResponse = await fetch('https://your-ngrok-url.ngrok.io/mcp_server/mcp', {
  method: 'GET',
  headers: {
    'Authorization': 'Bearer YOUR_TOKEN_HERE',
    'Mcp-Session-Id': sessionId,
    'Accept': 'application/x-ndjson'
  }
});

// Process streaming events
const streamReader = streamResponse.body.getReader();
while (true) {
  const { done, value } = await streamReader.read();
  if (done) break;

  const text = decoder.decode(value);
  const lines = text.split('\n').filter(l => l.trim());

  for (const line of lines) {
    const event = JSON.parse(line);
    console.log('State change event:', event);
  }
}
```

### 3. Test CORS Headers

Verify CORS is working:
```bash
curl -X OPTIONS http://localhost:8123/mcp_server/mcp \
  -H "Origin: https://copilot.microsoft.com" \
  -H "Access-Control-Request-Method: POST" \
  -H "Access-Control-Request-Headers: authorization,content-type,mcp-session-id" \
  -v
```

Should see headers like:
```
Access-Control-Allow-Origin: https://copilot.microsoft.com
Access-Control-Allow-Methods: POST, GET, DELETE
Access-Control-Allow-Headers: authorization,content-type,mcp-session-id
```

## Troubleshooting

### CORS Issues
- Check that the origin is in `cors_allowed_origins`
- Wildcards must be at subdomain level: `https://*.microsoft.com`
- Protocol (http/https) must match exactly

### Authentication Issues
- Verify token is valid and not expired
- Token must be in `Authorization: Bearer TOKEN` format
- Check Home Assistant logs for auth errors

### Session Issues
- Session IDs are temporary and session-specific
- Initialize creates a new session each time
- Use the returned `session_id` in subsequent requests

### Network Issues
- Ensure Home Assistant is reachable from the internet
- Check firewall rules
- Verify ngrok/cloudflare tunnel is running

## Security Considerations for Production

1. **Use HTTPS only** - Never expose HTTP endpoints publicly
2. **Restrict CORS origins** - Remove `*` wildcards and localhost
3. **Use strong tokens** - Rotate regularly
4. **Enable rate limiting** - Consider nginx/reverse proxy
5. **Monitor access** - Check logs for suspicious activity
6. **Use Home Assistant Cloud** - Or proper SSL certificates

## Example Production Configuration

```yaml
http:
  ssl_certificate: /ssl/fullchain.pem
  ssl_key: /ssl/privkey.pem
  cors_allowed_origins:
    - https://copilot.microsoft.com
  use_x_forwarded_for: true
  trusted_proxies:
    - 127.0.0.1
    - ::1
  ip_ban_enabled: true
  login_attempts_threshold: 5
```

## Useful Commands

**Check if endpoint is accessible:**
```bash
curl -I http://localhost:8123/mcp_server/mcp
```

**Watch logs in real-time:**
```bash
tail -f config/home-assistant.log | grep mcp_server
```

**Test from browser console:**
```javascript
fetch('http://localhost:8123/mcp_server/mcp', {
  method: 'POST',
  headers: {
    'Authorization': 'Bearer YOUR_TOKEN',
    'Content-Type': 'application/json'
  },
  body: JSON.stringify({
    jsonrpc: '2.0',
    id: 1,
    method: 'initialize'
  })
}).then(r => r.json()).then(console.log);
```
