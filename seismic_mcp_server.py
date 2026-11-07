#!/usr/bin/env python3
"""
Seismic MCP Server
==================

MCP server (JSON-RPC 2.0) for seismic data & insurance / parametric analytics.

Transports
----------
* ``streamable-http`` (default) – MCP Streamable HTTP transport, endpoint ``/mcp``.
    - POST  /mcp  : client -> server JSON-RPC messages (single or batch).
                    Requests get a JSON response (``application/json``) or, if the
                    client only accepts SSE, a single-event ``text/event-stream``.
                    Notifications / responses only -> ``202 Accepted``.
    - GET   /mcp  : 405 (this server never initiates messages).
    - DELETE /mcp : terminate the session given in ``Mcp-Session-Id``.
    - GET /health : liveness probe (used by Docker HEALTHCHECK).
* ``stdio`` – newline-delimited JSON-RPC on stdin/stdout (Claude Desktop style).

Logs always go to stderr.

Data sources
------------
* USGS Earthquake FDSN API (no auth)
* TMD DailySeismicEvent demo API (uid=api / ukey=api12345)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from jsonschema import Draft7Validator

# --------------------------------------------------------------------------- #
# Config / logging
# --------------------------------------------------------------------------- #

SERVER_NAME = "seismic-mcp-server"
SERVER_VERSION = "0.1.0"
SUPPORTED_PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
DEFAULT_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

USGS_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
TMD_URL = "https://data.tmd.go.th/api/DailySeismicEvent/v1/"
TMD_UID = "api"
TMD_UKEY = "api12345"

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30"))
SERVER_TZ = ZoneInfo(os.getenv("SERVER_TZ", "Asia/Bangkok"))
TMD_LOCAL_TZ = ZoneInfo("Asia/Bangkok")  # TMD "DateTimeThai" is UTC+7
CACHE_TTL = float(os.getenv("CACHE_TTL", "60"))

logging.basicConfig(
    stream=sys.stderr,
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(SERVER_NAME)

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UPSTREAM_HTTP_ERROR = -32001
UPSTREAM_PARSE_ERROR = -32002


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self) -> dict:
        err = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return err


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #

_http_client: httpx.AsyncClient | None = None
_cache: dict[str, tuple[float, Any]] = {}


def http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": f"{SERVER_NAME}/{SERVER_VERSION}"},
            follow_redirects=True,
        )
    return _http_client


async def fetch_json(url: str, params: dict[str, Any], provider: str) -> Any:
    key = url + "?" + json.dumps(params, sort_keys=True)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    try:
        resp = await http_client().get(url, params=params)
    except httpx.HTTPError as exc:
        raise RpcError(UPSTREAM_HTTP_ERROR, f"{provider} request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise RpcError(
            UPSTREAM_HTTP_ERROR,
            f"{provider} returned HTTP {resp.status_code}",
            {"status": resp.status_code, "body": resp.text[:500]},
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise RpcError(
            UPSTREAM_PARSE_ERROR,
            f"{provider} returned non-JSON response",
            {"body": resp.text[:500]},
        ) from exc
    _cache[key] = (time.monotonic(), data)
    return data


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# --------------------------------------------------------------------------- #
# Normalisation -> EarthquakeEvent
# --------------------------------------------------------------------------- #

def usgs_feature_to_event(feature: dict) -> dict:
    props = feature.get("properties") or {}
    coords = (feature.get("geometry") or {}).get("coordinates") or [None, None, None]
    coords = list(coords) + [None] * (3 - len(coords))
    t = props.get("time")
    iso = (
        datetime.fromtimestamp(t / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        if isinstance(t, (int, float))
        else None
    )
    return {
        "id": feature.get("id"),
        "time": iso,
        "place": props.get("place"),
        "magnitude": _num(props.get("mag")),
        "latitude": _num(coords[1]),
        "longitude": _num(coords[0]),
        "depth_km": _num(coords[2]),
        "source": "USGS",
        "url": props.get("url"),
        "raw": feature,
    }


def _parse_tmd_naive(s: str | None, tz: ZoneInfo | timezone) -> datetime | None:
    if not s:
        return None
    s = s.strip().replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def tmd_event_time(item: dict) -> datetime | None:
    """Return an aware datetime for a TMD item (supports spec & live formats)."""
    return (
        _parse_tmd_naive(item.get("DateTime"), TMD_LOCAL_TZ)
        or _parse_tmd_naive(item.get("DateTimeUTC"), timezone.utc)
        or _parse_tmd_naive(item.get("DateTimeThai"), TMD_LOCAL_TZ)
    )


def tmd_item_to_event(item: dict) -> dict:
    """Map a TMD item to EarthquakeEvent.

    Supports the documented shape (EventID/DateTime/DepthKm/Location) and the
    shape actually returned by the live demo API
    (DateTimeUTC/DateTimeThai/Depth/OriginThai, no EventID).
    """
    dt = tmd_event_time(item)
    lat = _num(item.get("Latitude"))
    lon = _num(item.get("Longitude"))
    event_id = item.get("EventID")
    if not event_id:
        basis = f"{item.get('DateTimeUTC') or item.get('DateTime')}|{lat}|{lon}|{item.get('Magnitude')}"
        event_id = hashlib.sha1(basis.encode()).hexdigest()[:12]
    return {
        "id": f"TMD-{event_id}",
        "time": dt.isoformat() if dt else item.get("DateTime"),
        "place": item.get("Location") or item.get("OriginThai") or item.get("TitleThai"),
        "magnitude": _num(item.get("Magnitude")),
        "latitude": lat,
        "longitude": lon,
        "depth_km": _num(item.get("DepthKm", item.get("Depth"))),
        "source": "TMD_DailySeismicEvent",
        "url": item.get("URL") or item.get("Url") or item.get("url"),
        "raw": item,
    }


def _tmd_items(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        raise RpcError(UPSTREAM_PARSE_ERROR, "TMD response is not a JSON object")
    items = payload.get("DailySeismicEvent")
    if items is None:
        items = payload.get("DailyEarthquakes")
    if items is None:
        return []
    if isinstance(items, dict):  # single item collapsed into object
        items = [items]
    if not isinstance(items, list):
        raise RpcError(UPSTREAM_PARSE_ERROR, "Unexpected TMD event list format")
    return [i for i in items if isinstance(i, dict)]


async def tmd_fetch_day(day: str) -> list[dict]:
    """Fetch TMD events for one Thai-local calendar day (YYYY-MM-DD).

    The demo API may ignore ``Date`` and return a rolling archive, so items are
    also filtered client-side to the requested day (Asia/Bangkok).
    """
    params = {"uid": TMD_UID, "ukey": TMD_UKEY, "format": "json", "Date": day}
    payload = await fetch_json(TMD_URL, params, "TMD")
    out = []
    for item in _tmd_items(payload):
        dt = tmd_event_time(item)
        if dt is None or dt.astimezone(TMD_LOCAL_TZ).date().isoformat() == day:
            out.append(tmd_item_to_event(item))
    return out


def _sort_by_time_desc(events: list[dict]) -> list[dict]:
    def key(e):
        try:
            return datetime.fromisoformat(str(e.get("time")).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return float("-inf")
    return sorted(events, key=key, reverse=True)


# --------------------------------------------------------------------------- #
# Geo / analytics helpers
# --------------------------------------------------------------------------- #

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _event_coords(ev: dict, where: str) -> tuple[float, float]:
    lat, lon = _num(ev.get("latitude")), _num(ev.get("longitude"))
    if lat is None or lon is None:
        raise RpcError(INVALID_PARAMS, f"{where}: event '{ev.get('id')}' has no latitude/longitude")
    return lat, lon


DEFAULT_PAYOUT_CURVE = [
    {"max_distance_km": 50, "payout_percent": 0.5},
    {"max_distance_km": 100, "payout_percent": 0.25},
    {"max_distance_km": 200, "payout_percent": 0.1},
]


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #

def _parse_date(s: str, field: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError as exc:
        raise RpcError(INVALID_PARAMS, f"'{field}' must be YYYY-MM-DD") from exc


async def tool_usgs_search_earthquakes(args: dict) -> dict:
    has = [k in args for k in ("latitude", "longitude", "maxradiuskm")]
    if any(has) and not all(has):
        raise RpcError(INVALID_PARAMS, "latitude, longitude and maxradiuskm must be given together")
    if "maxmagnitude" in args and args["maxmagnitude"] < args["minmagnitude"]:
        raise RpcError(INVALID_PARAMS, "maxmagnitude must be >= minmagnitude")
    params = {"format": "geojson", "orderby": "time", **args}
    payload = await fetch_json(USGS_URL, params, "USGS")
    if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
        raise RpcError(UPSTREAM_PARSE_ERROR, "USGS response is not a GeoJSON FeatureCollection")
    events = [usgs_feature_to_event(f) for f in payload["features"] if isinstance(f, dict)]
    return {"events": events, "query": params}


async def tool_tmd_get_daily_earthquakes(args: dict) -> dict:
    if "date" in args:
        day = _parse_date(args["date"], "date").isoformat()
    else:
        day = datetime.now(SERVER_TZ).date().isoformat()
    events = _sort_by_time_desc(await tmd_fetch_day(day))
    return {"events": events, "query": {"date": day}}


async def tool_tmd_get_recent_earthquakes(args: dict) -> dict:
    days = args["days"]
    today = datetime.now(SERVER_TZ).date()
    dates = [(today - timedelta(days=d)).isoformat() for d in range(days)]
    results = await asyncio.gather(*(tmd_fetch_day(d) for d in dates))
    seen: dict[str, dict] = {}
    for evs in results:
        for e in evs:
            seen.setdefault(e["id"], e)
    events = _sort_by_time_desc(list(seen.values()))
    return {"events": events, "query": {"days": days, "dates": dates}}


async def tool_match_events_to_assets(args: dict) -> dict:
    max_d = args["max_distance_km"]
    min_m = args["min_magnitude"]
    events = [
        e for e in args["events"]
        if _num(e.get("magnitude")) is not None and _num(e.get("magnitude")) >= min_m
    ]
    coords = [(e, *_event_coords(e, "events")) for e in events]
    matches = []
    for asset in args["assets"]:
        hits = []
        for ev, lat, lon in coords:
            d = haversine_km(lat, lon, asset["latitude"], asset["longitude"])
            if d <= max_d:
                hits.append({"event": ev, "distance_km": round(d, 2)})
        if hits:
            hits.sort(key=lambda h: h["distance_km"])
            matches.append({
                "asset_id": asset["asset_id"],
                "name": asset.get("name"),
                "sum_insured": asset.get("sum_insured"),
                "events": hits,
            })
    return {
        "matches": matches,
        "parameters": {"max_distance_km": max_d, "min_magnitude": min_m},
        "summary": {
            "events_considered": len(events),
            "assets_total": len(args["assets"]),
            "assets_matched": len(matches),
        },
    }


async def tool_compute_parametric_payout(args: dict) -> dict:
    event = args["event"]
    min_m = args["min_magnitude_trigger"]
    max_d = args["max_distance_km_trigger"]
    curve = args.get("payout_curve") or DEFAULT_PAYOUT_CURVE
    for band in curve:
        if not 0.0 <= band["payout_percent"] <= 1.0:
            raise RpcError(INVALID_PARAMS, "payout_percent must be between 0.0 and 1.0")
    curve = sorted(curve, key=lambda b: b["max_distance_km"])

    mag = _num(event.get("magnitude"))
    if mag is None:
        raise RpcError(INVALID_PARAMS, "event.magnitude is required")
    lat, lon = _event_coords(event, "event")
    triggered = mag >= min_m

    payouts = []
    total = 0.0
    for a in args["assets"]:
        d = haversine_km(lat, lon, a["latitude"], a["longitude"])
        pct = 0.0
        if triggered and d <= max_d:
            for band in curve:
                if d <= band["max_distance_km"]:
                    pct = band["payout_percent"]
                    break
        amount = round(a["sum_insured"] * pct, 2)
        total += amount
        payouts.append({
            "asset_id": a["asset_id"],
            "name": a.get("name"),
            "distance_km": round(d, 2),
            "sum_insured": a["sum_insured"],
            "payout_percent": pct,
            "payout_amount": amount,
        })
    return {
        "event_id": event.get("id"),
        "event_magnitude": mag,
        "triggered": triggered,
        "parameters": {
            "min_magnitude_trigger": min_m,
            "max_distance_km_trigger": max_d,
            "payout_curve": curve,
        },
        "asset_payouts": payouts,
        "total_payout": round(total, 2),
    }


# --------------------------------------------------------------------------- #
# Tool registry (schemas from spec)
# --------------------------------------------------------------------------- #

_ASSET_PROPS = {
    "asset_id": {"type": "string"},
    "name": {"type": "string"},
    "latitude": {"type": "number", "minimum": -90, "maximum": 90},
    "longitude": {"type": "number", "minimum": -180, "maximum": 180},
}

TOOLS: dict[str, dict] = {
    "usgs_search_earthquakes": {
        "description": "Search earthquakes from the USGS FDSN API with basic filters.",
        "handler": tool_usgs_search_earthquakes,
        "inputSchema": {
            "type": "object",
            "properties": {
                "starttime": {"type": "string", "description": "YYYY-MM-DD or ISO8601."},
                "endtime": {"type": "string", "description": "YYYY-MM-DD or ISO8601."},
                "minmagnitude": {"type": "number", "description": "Minimum magnitude.", "default": 4.5},
                "maxmagnitude": {"type": "number", "description": "Maximum magnitude, optional."},
                "latitude": {"type": "number", "description": "Center latitude for radius search, optional.",
                             "minimum": -90, "maximum": 90},
                "longitude": {"type": "number", "description": "Center longitude for radius search, optional.",
                              "minimum": -180, "maximum": 180},
                "maxradiuskm": {"type": "number", "description": "Search radius in kilometers, optional.",
                                "exclusiveMinimum": 0, "maximum": 20001.6},
                "limit": {"type": "integer", "description": "Maximum number of events.",
                          "default": 20, "minimum": 1, "maximum": 200},
            },
            "required": ["starttime", "endtime"],
            "additionalProperties": False,
        },
    },
    "tmd_get_daily_earthquakes": {
        "description": "Get daily earthquakes from the TMD DailySeismicEvent demo API.",
        "handler": tool_tmd_get_daily_earthquakes,
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD. If omitted, use today's date."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "tmd_get_recent_earthquakes": {
        "description": "Get recent earthquakes from TMD over the last N days using DailySeismicEvent.",
        "handler": tool_tmd_get_recent_earthquakes,
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Number of days back from today (including today).",
                         "default": 3, "minimum": 1, "maximum": 7},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "match_events_to_assets": {
        "description": "Match earthquake events to insured assets within a given radius and magnitude threshold.",
        "handler": tool_match_events_to_assets,
        "inputSchema": {
            "type": "object",
            "properties": {
                "events": {"type": "array", "description": "List of EarthquakeEvent objects.",
                           "items": {"type": "object"}},
                "assets": {
                    "type": "array",
                    "description": "Insured assets with coordinates.",
                    "items": {
                        "type": "object",
                        "properties": {**_ASSET_PROPS,
                                       "sum_insured": {"type": "number",
                                                       "description": "Optional total insured value."}},
                        "required": ["asset_id", "latitude", "longitude"],
                        "additionalProperties": True,
                    },
                },
                "max_distance_km": {"type": "number", "description": "Max distance to consider an asset affected.",
                                    "default": 100},
                "min_magnitude": {"type": "number", "description": "Minimum magnitude to consider.",
                                  "default": 4.5},
            },
            "required": ["events", "assets"],
            "additionalProperties": False,
        },
    },
    "compute_parametric_payout": {
        "description": ("Compute a simple parametric payout for an earthquake event and a set of assets, "
                        "based on distance and magnitude thresholds."),
        "handler": tool_compute_parametric_payout,
        "inputSchema": {
            "type": "object",
            "properties": {
                "event": {"type": "object", "description": "Single EarthquakeEvent to evaluate."},
                "assets": {
                    "type": "array",
                    "description": "Assets with coordinates and sum_insured.",
                    "items": {
                        "type": "object",
                        "properties": {**_ASSET_PROPS, "sum_insured": {"type": "number", "minimum": 0}},
                        "required": ["asset_id", "latitude", "longitude", "sum_insured"],
                        "additionalProperties": True,
                    },
                },
                "min_magnitude_trigger": {"type": "number", "description": "Minimum magnitude to trigger any payout.",
                                          "default": 5.0},
                "max_distance_km_trigger": {"type": "number", "description": "Max distance for eligibility.",
                                            "default": 200},
                "payout_curve": {
                    "type": "array",
                    "description": "Optional distance-based payout curve.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "max_distance_km": {"type": "number"},
                            "payout_percent": {"type": "number", "description": "Fraction of sum_insured (0.0–1.0)."},
                        },
                        "required": ["max_distance_km", "payout_percent"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["event", "assets"],
            "additionalProperties": False,
        },
    },
}

_VALIDATORS = {name: Draft7Validator(t["inputSchema"]) for name, t in TOOLS.items()}


def validate_and_fill(name: str, arguments: Any) -> dict:
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise RpcError(INVALID_PARAMS, "params.arguments must be an object")
    errors = sorted(_VALIDATORS[name].iter_errors(arguments), key=lambda e: list(e.path))
    if errors:
        details = [
            {"path": "/".join(str(p) for p in e.path) or "(root)", "message": e.message}
            for e in errors
        ]
        raise RpcError(INVALID_PARAMS, f"Invalid arguments for {name}: {details[0]['message']}",
                       {"errors": details})
    filled = dict(arguments)
    for key, prop in TOOLS[name]["inputSchema"]["properties"].items():
        if key not in filled and "default" in prop:
            filled[key] = prop["default"]
    return filled


# --------------------------------------------------------------------------- #
# JSON-RPC / MCP dispatcher (transport independent)
# --------------------------------------------------------------------------- #

async def handle_method(method: str, params: dict) -> dict:
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
        log.info("initialize: client=%s requested=%s -> %s",
                 (params.get("clientInfo") or {}).get("name"), requested, version)
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"list": True, "call": True, "listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": ("Seismic data (USGS, TMD) and insurance parametric analytics. "
                             "Fetch events with usgs_*/tmd_* tools, then pass them to "
                             "match_events_to_assets or compute_parametric_payout."),
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [
            {"name": n, "description": t["description"], "inputSchema": t["inputSchema"]}
            for n, t in TOOLS.items()
        ]}
    if method == "tools/call":
        name = params.get("name")
        if name not in TOOLS:
            raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
        args = validate_and_fill(name, params.get("arguments"))
        log.info("tools/call %s", name)
        result = await TOOLS[name]["handler"](args)
        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "structuredContent": result,
            "isError": False,
        }
    raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")


async def handle_message(msg: Any) -> dict | None:
    """Handle one JSON-RPC message. Returns a response dict, or None for notifications."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": INVALID_REQUEST, "message": "Invalid JSON-RPC 2.0 message"}}
    if "method" not in msg:  # a response from the client – nothing to do
        return None
    is_notification = "id" not in msg
    msg_id = msg.get("id")
    params = msg.get("params") or {}
    try:
        if not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, "params must be an object")
        if is_notification:
            log.debug("notification %s", msg["method"])
            return None
        result = await handle_method(msg["method"], params)
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    except RpcError as e:
        log.warning("%s -> error %s: %s", msg.get("method"), e.code, e.message)
        return None if is_notification else {"jsonrpc": "2.0", "id": msg_id, "error": e.to_dict()}
    except Exception as e:  # noqa: BLE001
        log.exception("internal error in %s", msg.get("method"))
        return None if is_notification else {
            "jsonrpc": "2.0", "id": msg_id,
            "error": {"code": INTERNAL_ERROR, "message": f"Internal error: {e}"}}


# --------------------------------------------------------------------------- #
# Streamable HTTP transport (Starlette + Uvicorn)
# --------------------------------------------------------------------------- #

def build_http_app(endpoint: str = "/mcp"):
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    sessions: dict[str, float] = {}
    allowed_origins = {o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()}
    require_session = os.getenv("REQUIRE_SESSION", "false").lower() == "true"

    def origin_ok(request: Request) -> bool:
        # Protect against DNS-rebinding: only allow local origins unless configured.
        origin = request.headers.get("origin")
        if not origin or "*" in allowed_origins or origin in allowed_origins:
            return True
        host = urlparse(origin).hostname or ""
        return host in {"localhost", "127.0.0.1", "::1"}

    def rpc_error_response(status: int, code: int, message: str) -> JSONResponse:
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": code, "message": message}}, status_code=status)

    async def mcp_post(request: Request) -> Response:
        if not origin_ok(request):
            return rpc_error_response(403, INVALID_REQUEST, "Origin not allowed")
        try:
            body = json.loads(await request.body())
        except (ValueError, UnicodeDecodeError):
            return rpc_error_response(400, PARSE_ERROR, "Parse error")

        messages = body if isinstance(body, list) else [body]
        if not messages:
            return rpc_error_response(400, INVALID_REQUEST, "Empty batch")

        is_init = any(isinstance(m, dict) and m.get("method") == "initialize" for m in messages)
        session_id = request.headers.get("mcp-session-id")
        headers: dict[str, str] = {}
        if is_init:
            session_id = uuid.uuid4().hex
            sessions[session_id] = time.time()
            headers["Mcp-Session-Id"] = session_id
        elif session_id:
            if session_id not in sessions:
                return rpc_error_response(404, INVALID_REQUEST, "Session not found")
            headers["Mcp-Session-Id"] = session_id
        elif require_session:
            return rpc_error_response(400, INVALID_REQUEST, "Missing Mcp-Session-Id header")

        responses = [r for r in await asyncio.gather(*(handle_message(m) for m in messages)) if r]
        if not responses:
            return Response(status_code=202, headers=headers)
        payload = responses if isinstance(body, list) else responses[0]

        accept = request.headers.get("accept", "")
        if "text/event-stream" in accept and "application/json" not in accept:
            data = json.dumps(payload, ensure_ascii=False)
            return Response(f"event: message\ndata: {data}\n\n", media_type="text/event-stream",
                            headers={**headers, "Cache-Control": "no-cache"})
        return JSONResponse(payload, headers=headers)

    async def mcp_get(request: Request) -> Response:
        # No server-initiated stream is offered.
        return Response(status_code=405, headers={"Allow": "POST, DELETE"})

    async def mcp_delete(request: Request) -> Response:
        sid = request.headers.get("mcp-session-id")
        if not sid or sessions.pop(sid, None) is None:
            return Response(status_code=404)
        return Response(status_code=200)

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok", "server": SERVER_NAME, "version": SERVER_VERSION,
                             "tools": list(TOOLS), "sessions": len(sessions)})

    async def shutdown():
        if _http_client is not None:
            await _http_client.aclose()

    return Starlette(
        routes=[
            Route(endpoint, mcp_post, methods=["POST"]),
            Route(endpoint, mcp_get, methods=["GET"]),
            Route(endpoint, mcp_delete, methods=["DELETE"]),
            Route("/health", health, methods=["GET"]),
        ],
        on_shutdown=[shutdown],
    )


# --------------------------------------------------------------------------- #
# stdio transport
# --------------------------------------------------------------------------- #

async def run_stdio() -> None:
    loop = asyncio.get_running_loop()
    log.info("%s %s listening on stdio", SERVER_NAME, SERVER_VERSION)
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            resp: Any = {"jsonrpc": "2.0", "id": None,
                         "error": {"code": PARSE_ERROR, "message": "Parse error"}}
        else:
            if isinstance(msg, list):
                resp = [r for r in [await handle_message(m) for m in msg] if r] or None
            else:
                resp = await handle_message(msg)
        if resp:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(description="Seismic MCP Server")
    parser.add_argument("--transport", choices=["streamable-http", "stdio"],
                        default=os.getenv("MCP_TRANSPORT", "streamable-http"))
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    parser.add_argument("--path", default=os.getenv("MCP_PATH", "/mcp"))
    a = parser.parse_args()

    if a.transport == "stdio":
        asyncio.run(run_stdio())
        return

    import uvicorn
    log.info("%s %s – streamable HTTP on http://%s:%d%s", SERVER_NAME, SERVER_VERSION, a.host, a.port, a.path)
    uvicorn.run(build_http_app(a.path), host=a.host, port=a.port,
                log_level=os.getenv("LOG_LEVEL", "info").lower())


if __name__ == "__main__":
    main()
