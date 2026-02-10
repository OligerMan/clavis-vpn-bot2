# Subscription Server

FastAPI-based subscription server that serves VLESS URIs to v2ray clients (v2rayNG, v2raytun, v2rayN, V2Box).

## Overview

The subscription server runs alongside the Telegram bot in a single process. It provides HTTP endpoints that v2ray clients can poll to get updated server configurations.

### Key Features

- **Base64-encoded VLESS URI list** (standard v2ray subscription format)
- **Multi-server support** - Returns all keys from different servers in one response
- **Expired subscription handling** - Returns keys with modified remarks instead of 404
- **Thread-safe caching** - 5-minute TTL with LRU eviction
- **Health monitoring** - `/health` and `/cache/stats` endpoints

## Architecture

```
┌─────────────────────┐
│   Telegram Bot      │
│   (main thread)     │
└─────────────────────┘
          │
          │ starts
          ▼
┌─────────────────────┐
│ Subscription Server │
│  (daemon thread)    │
│                     │
│  ┌──────────────┐  │
│  │   FastAPI    │  │
│  │   Uvicorn    │  │
│  │   Port 8080  │  │
│  └──────────────┘  │
└─────────────────────┘
          │
          │ queries
          ▼
┌─────────────────────┐
│   SQLite Database   │
│  ┌──────────────┐  │
│  │Subscriptions │  │
│  │     Keys     │  │
│  │   Servers    │  │
│  └──────────────┘  │
└─────────────────────┘
```

## API Endpoints

### `GET /sub/{token}`

Main subscription endpoint. Returns base64-encoded VLESS URIs.

**Response Format:**
```
Base64(URI1\nURI2\nURI3...)
```

**Behavior:**
- **Active subscription**: Returns all active keys
- **Expired/inactive subscription**: Returns keys with modified remarks like "⏰ Clavis VPN - Expired, please renew subscription"
- **Invalid token**: Returns 404
- **No keys**: Returns 404

**Example:**
```bash
curl http://localhost:8080/sub/550e8400-e29b-41d4-a716-446655440000
```

### `GET /info/{token}`

Debug endpoint. Returns subscription metadata as JSON.

**Response:**
```json
{
  "token": "550e8400...0000",
  "is_active": true,
  "is_expired": false,
  "expires_at": "2024-03-01T00:00:00",
  "days_remaining": 30,
  "is_test": false,
  "device_limit": 5,
  "key_count": 2,
  "server_count": 2,
  "server_ids": [1, 2],
  "protocols": ["xui"]
}
```

### `GET /health`

Health check endpoint.

**Response:**
```json
{
  "status": "ok",
  "service": "clavis-subscription-server"
}
```

### `GET /cache/stats`

Cache statistics endpoint.

**Response:**
```json
{
  "total_entries": 42,
  "expired_entries": 5,
  "active_entries": 37,
  "max_size": 1000,
  "ttl_seconds": 300,
  "hits": 1250,
  "misses": 180,
  "hit_rate_percent": 87.41
}
```

### `GET /`

Root endpoint with API documentation.

## Configuration

Environment variables in `.env`:

```env
# Subscription server port (default: 8080)
SUBSCRIPTION_PORT=8080

# Cache TTL in seconds (default: 300 = 5 minutes)
SUBSCRIPTION_CACHE_TTL=300

# Max cache entries (default: 1000)
SUBSCRIPTION_CACHE_SIZE=1000

# Base URL for subscription links (used by bot)
SUBSCRIPTION_BASE_URL=http://localhost:8080
```

For production with nginx reverse proxy:
```env
SUBSCRIPTION_BASE_URL=https://vpn.example.com
```

## Multi-Server Support

The subscription server supports multiple VPN servers automatically:

1. Each `Key` has a `server_id` pointing to a `Server`
2. One `Subscription` can have multiple `Key` objects with different `server_id` values
3. The `/sub/{token}` endpoint returns ALL active keys in a single response
4. v2ray clients will see multiple servers and can switch between them

**Example:**
```
Subscription (token=abc123)
├─ Key 1 (server_id=1, host=server1.example.com)
├─ Key 2 (server_id=2, host=server2.example.com)
└─ Key 3 (server_id=3, host=server3.example.com)
```

Response will contain 3 VLESS URIs, one for each server.

## Expired Subscription Behavior

When a subscription is expired or inactive:

1. **Still returns 200 OK** (not 404)
2. **Returns all keys** with modified remarks
3. **Modified remark format**: `⏰ Clavis {server} - Expired, please renew subscription`
4. Keys won't work (expired on 3x-ui server side)
5. Client shows keys but connection fails with helpful message

**Why?** This provides better UX than returning 404. Users see their expired keys and understand they need to renew.

## Caching Strategy

### Cache Key
- Key: `token` (subscription UUID)
- Value: Base64-encoded response string

### Cache Behavior
- **TTL**: 5 minutes (configurable via `SUBSCRIPTION_CACHE_TTL`)
- **Eviction**: LRU (Least Recently Used)
- **Max Size**: 1000 entries (configurable via `SUBSCRIPTION_CACHE_SIZE`)
- **Thread Safety**: `threading.Lock` for concurrent access

### Cache Invalidation
- Automatic after TTL expires
- No manual invalidation (5 min delay is acceptable for config updates)
- Can be cleared via `clear_cache()` if needed

## Testing

### Manual Test Script

Run the manual test script to create test data and verify functionality:

```bash
python tests/test_subscription_manual.py
```

This will:
1. Create test subscriptions (active and expired)
2. Test formatter and cache
3. Print subscription URLs for testing

### Unit Tests

Run pytest tests:

```bash
pytest tests/test_subscription_server.py -v
```

Tests cover:
- Subscription formatting (active and expired)
- Cache functionality (set, get, expiry, LRU eviction)
- API endpoints (all routes)
- Multi-server support
- Error handling

### Manual Endpoint Testing

```bash
# Start the bot (starts subscription server automatically)
python main.py

# Health check
curl http://localhost:8080/health

# Get subscription (replace {token} with actual token)
curl http://localhost:8080/sub/{token}

# Decode subscription response
curl -s http://localhost:8080/sub/{token} | base64 -d

# Get subscription info
curl http://localhost:8080/info/{token} | jq .

# Get cache stats
curl http://localhost:8080/cache/stats | jq .
```

### Client Testing

1. Get subscription URL from bot: `/key` command
2. Copy subscription URL (looks like `https://vpn.example.com/sub/{token}`)
3. Import into v2rayNG:
   - Open app
   - Tap `+` → `Import config from custom configuration`
   - Select `Import from subscription`
   - Paste URL
   - Tap `Update subscription`
4. Verify servers appear in list
5. Connect and test

## Deployment

### Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Configure .env
echo "SUBSCRIPTION_PORT=8080" >> .env
echo "SUBSCRIPTION_BASE_URL=http://localhost:8080" >> .env

# Run (starts both bot and subscription server)
python main.py
```

### Production with Nginx

**1. Configure nginx** (`/etc/nginx/sites-available/clavis-vpn`):

```nginx
server {
    listen 443 ssl http2;
    server_name vpn.example.com;

    ssl_certificate /etc/letsencrypt/live/vpn.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/vpn.example.com/privkey.pem;

    # Subscription endpoints
    location /sub/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    location /info/ {
        proxy_pass http://127.0.0.1:8080;
    }

    location /health {
        proxy_pass http://127.0.0.1:8080;
    }

    location /cache/stats {
        proxy_pass http://127.0.0.1:8080;
        # Optional: restrict to local IPs only
        allow 127.0.0.1;
        deny all;
    }
}
```

**2. Enable and reload:**

```bash
sudo ln -s /etc/nginx/sites-available/clavis-vpn /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

**3. Update .env:**

```env
SUBSCRIPTION_BASE_URL=https://vpn.example.com
```

**4. Restart bot:**

```bash
sudo systemctl restart clavis-vpn-bot
```

## Logging

### Access Logs

Each request logs:
- Token (first 8 chars for privacy)
- Client IP
- User-Agent (detect v2rayNG, v2raytun, etc.)
- Number of keys served
- Number of servers
- Cache hit/miss

**Example:**
```
2024-02-01 12:00:00 - subscription.router - INFO - Subscription access: token=550e8400..., ip=1.2.3.4, ua=v2rayNG/1.8.0
2024-02-01 12:00:00 - subscription.router - INFO - Subscription served: token=550e8400..., keys=2, servers=2, expired=False
```

### Error Logs

All errors include full traceback:
```
2024-02-01 12:00:00 - subscription.router - ERROR - Error serving subscription 550e8400...: division by zero
Traceback (most recent call last):
  ...
```

## Troubleshooting

### Server not starting

**Symptom:** No "Starting subscription server..." log message

**Solution:**
1. Check port 8080 is not in use: `netstat -ano | findstr :8080`
2. Check logs for errors
3. Verify `SUBSCRIPTION_PORT` in `.env`

### 404 for valid token

**Symptom:** `/sub/{token}` returns 404 for active subscription

**Possible causes:**
1. Token not in database: Check `SELECT * FROM subscriptions WHERE token = '{token}'`
2. No active keys: Check `SELECT * FROM keys WHERE subscription_id = X AND is_active = TRUE`
3. Database connection issue: Check logs for SQLAlchemy errors

### Empty response

**Symptom:** `/sub/{token}` returns 200 but empty base64 string

**Possible causes:**
1. Keys have empty `key_data`: Check `SELECT key_data FROM keys WHERE subscription_id = X`
2. Keys have invalid VLESS URIs (not starting with `vless://`)

### Client can't import subscription

**Symptom:** v2rayNG says "Import failed" or similar

**Possible causes:**
1. Invalid base64 encoding (shouldn't happen, but check logs)
2. VLESS URI format issues (missing required parameters)
3. Network issue (client can't reach server)

**Debug:**
```bash
# Get response and decode manually
curl -s http://localhost:8080/sub/{token} > response.txt
base64 -d response.txt > decoded.txt
cat decoded.txt
```

Check if decoded URIs are valid VLESS URIs.

## Performance

### Expected Metrics

- **Cached response**: < 50ms
- **Database query**: < 200ms
- **Total response time**: < 250ms
- **Cache hit rate**: > 80% (after warmup)

### Optimization Tips

1. **Increase cache TTL** if configs rarely change:
   ```env
   SUBSCRIPTION_CACHE_TTL=600  # 10 minutes
   ```

2. **Monitor cache stats**:
   ```bash
   watch -n 5 'curl -s http://localhost:8080/cache/stats | jq .'
   ```

3. **Add database indexes** (already done by default):
   - `subscriptions.token` (unique index)
   - `keys.subscription_id` (foreign key index)

## Security

### Token Security

- UUID v4 provides 128-bit entropy
- Brute force attack requires 2^128 attempts (infeasible)
- Token is only exposed in subscription URL (not logged fully)

### CORS Policy

- Allow all origins (`allow_origins=["*"]`)
- Necessary for subscription access from mobile clients
- Safe because endpoints are read-only

### Rate Limiting

Not implemented at application level. Add nginx rate limiting if needed:

```nginx
limit_req_zone $binary_remote_addr zone=subscription:10m rate=10r/s;

location /sub/ {
    limit_req zone=subscription burst=20 nodelay;
    proxy_pass http://127.0.0.1:8080;
}
```

### HTTPS

**Required for production**. Use Let's Encrypt:

```bash
sudo certbot --nginx -d vpn.example.com
```

## Module Structure

```
subscription/
├── __init__.py         # Package exports
├── app.py              # FastAPI application
├── router.py           # API route handlers
├── formatter.py        # Response formatting (base64, VLESS URIs)
├── cache.py            # Thread-safe TTL cache
└── README.md           # This file
```

## Future Enhancements

Planned features for future versions:

1. **User-Agent detection**: Return different formats based on client
2. **Subscription headers**: Add `profile-update-interval` header
3. **Traffic quota**: Include usage in response metadata
4. **WebSocket support**: Real-time config updates
5. **CDN integration**: Cloudflare for global distribution

Not planned:
- Prometheus metrics (deferred)
- Rate limiting at app level (use nginx)
