#!/usr/bin/env python3
"""Smoke test for the Seismic MCP Server over Streamable HTTP.

Usage:  python test_client.py [http://localhost:8000/mcp]
Only uses the Python standard library.
"""
import json
import sys
import urllib.error
import urllib.request

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/mcp"
session_id = None
_next_id = 0


def post(method, params=None, notify=False):
    global session_id, _next_id
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    if not notify:
        _next_id += 1
        msg["id"] = _next_id
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": "2025-06-18"}
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = urllib.request.Request(URL, json.dumps(msg).encode(), headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            session_id = r.headers.get("Mcp-Session-Id") or session_id
            body = r.read().decode()
            return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "null")


def call(name, args):
    status, resp = post("tools/call", {"name": name, "arguments": args})
    if "error" in resp:
        print(f"  {name}: ERROR {resp['error']['code']} {resp['error']['message']}")
        return None
    return resp["result"]["structuredContent"]


print("initialize:", post("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "test", "version": "0"}})[1]["result"]["serverInfo"])
print("session:", session_id)
print("notifications/initialized ->", post("notifications/initialized", notify=True)[0])
print("tools:", [t["name"] for t in post("tools/list")[1]["result"]["tools"]])

usgs = call("usgs_search_earthquakes", {"starttime": "2026-03-01", "endtime": "2026-03-07", "limit": 5})
print(f"usgs_search_earthquakes: {len(usgs['events'])} events; first =",
      {k: usgs['events'][0][k] for k in ('id', 'time', 'place', 'magnitude')} if usgs["events"] else None)

daily = call("tmd_get_daily_earthquakes", {})
print(f"tmd_get_daily_earthquakes {daily['query']}: {len(daily['events'])} events")

recent = call("tmd_get_recent_earthquakes", {"days": 3})
print(f"tmd_get_recent_earthquakes {recent['query']['days']}d: {len(recent['events'])} events; first =",
      {k: recent['events'][0][k] for k in ('id', 'time', 'place', 'magnitude')} if recent["events"] else None)

event = {"id": "TEST-1", "magnitude": 5.7, "latitude": 18.79, "longitude": 98.98}
assets = [
    {"asset_id": "A-001", "name": "Warehouse Chiang Mai", "latitude": 18.80, "longitude": 98.95, "sum_insured": 1_000_000},
    {"asset_id": "A-002", "name": "Factory Lampang", "latitude": 18.29, "longitude": 99.49, "sum_insured": 2_000_000},
    {"asset_id": "A-003", "name": "Office Bangkok", "latitude": 13.75, "longitude": 100.50, "sum_insured": 5_000_000},
]
m = call("match_events_to_assets", {"events": [event], "assets": assets})
print("match_events_to_assets:", [(x["asset_id"], x["events"][0]["distance_km"]) for x in m["matches"]])
p = call("compute_parametric_payout", {"event": event, "assets": assets})
print("compute_parametric_payout:", [(a["asset_id"], a["distance_km"], a["payout_amount"]) for a in p["asset_payouts"]],
      "total =", p["total_payout"])

print("validation error test:")
call("usgs_search_earthquakes", {"starttime": "2026-03-01"})
call("tmd_get_recent_earthquakes", {"days": 30})
print("unknown method:", post("foo/bar")[1]["error"])
