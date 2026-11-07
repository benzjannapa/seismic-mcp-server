# Seismic MCP Server (Streamable HTTP + Docker)

An MCP server for earthquake data (USGS, TMD) and insurance / parametric analytics, built to `spec.md`.
The default transport is **Streamable HTTP** at `http://localhost:8000/mcp`. stdio is still supported.
MCP server สำหรับข้อมูลแผ่นดินไหว (USGS + TMD) และเครื่องมือวิเคราะห์ประกันภัยแบบ parametric ตาม spec.md แต่ใช้ transport แบบ Streamable HTTP (endpoint /mcp) และรันใน Docker

Tools
Tool	คำอธิบาย
usgs_search_earthquakes	ค้นหาแผ่นดินไหวจาก USGS FDSN
tmd_get_daily_earthquakes	แผ่นดินไหวรายวันจาก TMD (default = วันนี้ ตาม TZ)
tmd_get_recent_earthquakes	แผ่นดินไหวย้อนหลัง N วัน (1–7) จาก TMD
match_events_to_assets	จับคู่เหตุการณ์กับทรัพย์สินที่เอาประกันตามระยะ/ขนาด
compute_parametric_payout	คำนวณ payout แบบ parametric ตาม payout curve
ทุก tool ที่คืนแผ่นดินไหวใช้ schema EarthquakeEvent ตาม spec ผลลัพธ์ส่งทั้ง structuredContent และ content[0].text (JSON string) argument ผิด schema → JSON-RPC error -32602; HTTP/parse error จาก upstream → -32000 
## Run with Docker

```bash
docker compose up -d --build
```

Check that it's running:

```bash
curl http://localhost:8000/health
```

Smoke test (standard library only):

```bash
python3 test_client.py http://localhost:8000/mcp
```

Stop:

```bash
docker compose down
```

Without compose:

```bash
docker build -t seismic-mcp-server . && docker run --rm -p 127.0.0.1:8000:8000 seismic-mcp-server
```

## Connect a client

**Claude Code**

```bash
claude mcp add --transport http seismic-mcp http://localhost:8000/mcp
```

**Clients that support remote HTTP (`mcp.json` / VS Code / Cursor)**

```json
{ "mcpServers": { "seismic-mcp": { "type": "http", "url": "http://localhost:8000/mcp" } } }
```

**Claude Desktop** (uses stdio, bridged with `mcp-remote`)

```json
{ "mcpServers": { "seismic-mcp": { "command": "npx", "args": ["-y", "mcp-remote", "http://localhost:8000/mcp"] } } }
```

## Transport details

| Method | Path | Behavior |
|---|---|---|
| POST | `/mcp` | JSON-RPC (single or batch). A request returns `application/json` (or SSE if the client accepts only `text/event-stream`); notifications only return `202` |
| GET | `/mcp` | `405`: the server sends no server-initiated messages |
| DELETE | `/mcp` | Ends the session given in `Mcp-Session-Id` |
| GET | `/health` | Healthcheck |

- `initialize` returns an `Mcp-Session-Id` header. Later requests should send it back (an unknown id gets `404`).
- Checks the `Origin` header to prevent DNS rebinding: only localhost is allowed by default. Add more with `ALLOWED_ORIGINS`.
- Supported protocol versions: `2025-06-18`, `2025-03-26`, `2024-11-05`

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `MCP_TRANSPORT` | `streamable-http` | or `stdio` |
| `HOST` / `PORT` / `MCP_PATH` | `0.0.0.0` (in container) / `8000` / `/mcp` | |
| `SERVER_TZ` | `Asia/Bangkok` | Time zone used to work out "today" |
| `ALLOWED_ORIGINS` | (empty) | Extra allowed Origins, comma-separated; `*` = all |
| `REQUIRE_SESSION` | `false` | `true` = require `Mcp-Session-Id` on every request |
| `CACHE_TTL` | `60` | Seconds to cache upstream API responses |
| `HTTP_TIMEOUT` | `30` | Timeout for calls to USGS/TMD |

## Tools

`usgs_search_earthquakes`, `tmd_get_daily_earthquakes`, `tmd_get_recent_earthquakes`,
`match_events_to_assets`, `compute_parametric_payout`. Schemas follow `spec.md`.

`tools/call` returns `structuredContent` (the result object from the spec) plus `content[0].text` (the same thing as a JSON string).
Errors come back as JSON-RPC `error`:

- `-32602`: arguments fail schema validation
- `-32601`: unknown method
- `-32001`: HTTP error from USGS/TMD
- `-32002`: could not parse the upstream API response

## Notes on the real TMD API (differs from the spec)

The live demo API returns `DailyEarthquakes` (not `DailySeismicEvent`) with the fields
`DateTimeUTC`, `DateTimeThai`, `Depth`, `OriginThai`, and has **no `EventID`**. It also **ignores the `Date` parameter** (it always returns the whole archive). So the server:

- Supports both formats (the spec's and the real one)
- Builds `id` as `TMD-<sha1(DateTimeUTC|lat|lon|mag)[:12]>` when there is no `EventID`
- Filters events by date itself, using the Thai calendar day (UTC+7) that matches the requested `date`

The payout curve is sorted by `max_distance_km` ascending before picking a band. `payout_percent` must be between 0 and 1.

## Run without Docker

```bash
pip install -r requirements.txt
python seismic_mcp_server.py                     # HTTP on 127.0.0.1:8000
python seismic_mcp_server.py --transport stdio   # stdio
```
