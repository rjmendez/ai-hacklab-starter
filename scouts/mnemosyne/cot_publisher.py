#!/usr/bin/env python3
"""
cot_publisher.py — Mnemosyne CoT publisher

Sends Cursor-on-Target (CoT) XML events to FreeTAKServer on behalf of
Charlie-Mesh and the agent team.

Usage:
    from cot_publisher import send_cot, send_alert_cot, send_geochat, AGENTS

TAK Server: openclaw-freetakserver:8087
"""

import socket
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    import requests as _requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
    print("[cot_publisher] WARNING: requests package not installed. HTTP functions unavailable.")

# ── Configuration ─────────────────────────────────────────────────────────────

TAK_HOST = "openclaw-freetakserver"
TAK_PORT = 8087
TAK_TIMEOUT = 10  # seconds

# ── Agent Identities ──────────────────────────────────────────────────────────

AGENTS = {
    "charlie": {"uid": "MESH-CHARLIE-001", "callsign": "Charlie-🐀", "type": "a-f-G-U-C"},
    "oxalis":  {"uid": "MESH-OXALIS-001",  "callsign": "Oxalis-🌿",  "type": "a-f-G-U-C"},
    "mrpink":  {"uid": "MESH-MRPINK-001",  "callsign": "MrPink-🔍",  "type": "a-f-G-U-C"},
    "haggis":  {"uid": "MESH-HAGGIS-001",  "callsign": "Haggis",     "type": "a-f-G-U-C"},
    "kimchi":  {"uid": "MESH-KIMCHI-001",  "callsign": "Kimchi",     "type": "a-f-G-U-C"},
}

DEFAULT_AGENT = "charlie"

# ── Internal helpers ───────────────────────────────────────────────────────────

_lock = threading.Lock()


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _send_tcp(cot_xml: str) -> bool:
    """Send raw CoT XML to TAK server over TCP. Returns True on success."""
    try:
        with socket.create_connection((TAK_HOST, TAK_PORT), timeout=TAK_TIMEOUT) as s:
            s.sendall(cot_xml.encode("utf-8"))
        return True
    except Exception as e:
        print(f"[cot_publisher] TCP send error: {e}")
        return False


def _build_position_event(uid: str, callsign: str, cot_type: str,
                           lat: float, lon: float,
                           remarks: str = "", stale_minutes: int = 5) -> str:
    now = datetime.now(timezone.utc)
    stale = now + timedelta(minutes=stale_minutes)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<event version="2.0" uid="{uid}" type="{cot_type}"
       time="{_iso(now)}" start="{_iso(now)}" stale="{_iso(stale)}"
       how="m-g">
  <point lat="{lat}" lon="{lon}" hae="0" ce="10" le="10"/>
  <detail>
    <contact callsign="{callsign}"/>
    <remarks>{remarks}</remarks>
  </detail>
</event>"""


def _build_geochat_event(message: str, sender_uid: str, callsign: str) -> str:
    now = datetime.now(timezone.utc)
    stale = now + timedelta(minutes=10)
    msg_uid = f"GeoChat.{sender_uid}.All.{int(now.timestamp())}"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<event version="2.0" uid="{msg_uid}" type="b-t-f"
       time="{_iso(now)}" start="{_iso(now)}" stale="{_iso(stale)}"
       how="h-g-i-g-o">
  <point lat="0" lon="0" hae="0" ce="9999999" le="9999999"/>
  <detail>
    <__chat parent="RootContactGroup" groupOwner="false"
            chatroom="All Chat Rooms" id="All Chat Rooms"
            senderCallsign="{callsign}">
      <chatgrp uid0="{sender_uid}" uid1="All Chat Rooms" id="All Chat Rooms"/>
    </__chat>
    <link uid="{sender_uid}" type="a-f-G-U-C" relation="p-p"/>
    <remarks source="BAO.F.ATAK.{sender_uid}" to="All Chat Rooms"
             time="{_iso(now)}">{message}</remarks>
    <__serverdestination destinations="All Chat Rooms"/>
  </detail>
</event>"""


# ── Public API ─────────────────────────────────────────────────────────────────

def send_cot(uid: str, callsign: str, lat: float, lon: float,
             remarks: str = "",
             cot_type: str = "a-f-G-U-C",
             stale_minutes: int = 5) -> bool:
    """
    Send a position CoT event to FreeTAKServer.

    Args:
        uid:          Unique identifier for this SA track (e.g. "MESH-CHARLIE-001")
        callsign:     Display name in ATAK (e.g. "Charlie-🐀")
        lat:          Latitude in decimal degrees
        lon:          Longitude in decimal degrees
        remarks:      Optional free-text remarks shown in ATAK
        cot_type:     CoT type string (default: friendly ground unit)
        stale_minutes: How long the track stays visible in ATAK

    Returns:
        True if sent successfully, False otherwise.
    """
    xml = _build_position_event(uid, callsign, cot_type, lat, lon,
                                 remarks=remarks, stale_minutes=stale_minutes)
    ok = _send_tcp(xml)
    status = "OK" if ok else "FAIL"
    print(f"[cot_publisher] send_cot {callsign} ({lat:.5f},{lon:.5f}) → {status}")
    return ok


def send_alert_cot(title: str, message: str,
                   lat: float = 0.0, lon: float = 0.0,
                   agent: str = DEFAULT_AGENT) -> bool:
    """
    Send an emergency/alert CoT event.

    The event appears as a hostile/alert marker in ATAK at the given location.
    If lat/lon are both 0.0, the marker is placed at the null island (invisible
    to most users) but the chat message is still delivered.

    Args:
        title:   Short alert title (used as callsign)
        message: Full alert message (shown in remarks)
        lat:     Alert location latitude (default 0.0)
        lon:     Alert location longitude (default 0.0)
        agent:   Agent identity to send as (key from AGENTS dict)

    Returns:
        True if sent successfully, False otherwise.
    """
    info = AGENTS.get(agent, AGENTS[DEFAULT_AGENT])
    uid = f"ALERT-{info['uid']}-{int(time.time())}"
    remarks = f"[ALERT] {title}: {message}"

    # Use 'a-h-G' (hostile ground) type for visual alert in ATAK
    xml = _build_position_event(uid, title, "a-h-G", lat, lon,
                                 remarks=remarks, stale_minutes=60)
    ok = _send_tcp(xml)

    # Also send as geochat so it appears in the chat window
    chat_xml = _build_geochat_event(
        message=f"🚨 {title}: {message}",
        sender_uid=info["uid"],
        callsign=info["callsign"],
    )
    _send_tcp(chat_xml)

    status = "OK" if ok else "FAIL"
    print(f"[cot_publisher] send_alert '{title}' → {status}")
    return ok


def send_geochat(message: str, callsign: str = "Charlie-Mesh",
                 agent: str = DEFAULT_AGENT) -> bool:
    """
    Send a GeoChat message to all ATAK clients.

    Args:
        message:   Text to broadcast in ATAK chat
        callsign:  Display name in chat (overrides agent default if provided)
        agent:     Agent identity to pull UID from (key from AGENTS dict)

    Returns:
        True if sent successfully, False otherwise.
    """
    info = AGENTS.get(agent, AGENTS[DEFAULT_AGENT])
    # Use provided callsign if it differs from the default argument value
    display_callsign = callsign if callsign != "Charlie-Mesh" else info["callsign"]
    xml = _build_geochat_event(message=message,
                                sender_uid=info["uid"],
                                callsign=display_callsign)
    ok = _send_tcp(xml)
    status = "OK" if ok else "FAIL"
    print(f"[cot_publisher] send_geochat from {display_callsign}: {message[:60]!r} → {status}")
    return ok


def send_agent_position(agent: str, lat: float, lon: float, remarks: str = "") -> bool:
    """
    Convenience wrapper: send a position event for a named agent from AGENTS.

    Args:
        agent:   Key from AGENTS dict (e.g. "charlie", "haggis")
        lat:     Latitude
        lon:     Longitude
        remarks: Optional remarks

    Returns:
        True if sent successfully, False otherwise.
    """
    info = AGENTS.get(agent)
    if not info:
        print(f"[cot_publisher] Unknown agent: {agent}. Known: {list(AGENTS.keys())}")
        return False
    return send_cot(
        uid=info["uid"],
        callsign=info["callsign"],
        lat=lat,
        lon=lon,
        remarks=remarks or f"Agent {info['callsign']} reporting in",
        cot_type=info["type"],
    )


# ── CLI self-test ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: cot_publisher.py <chat|alert|pos>")
        print("  chat  — send a test geochat message")
        print("  alert — send a test alert")
        print("  pos   — send a test position for charlie")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "chat":
        send_geochat("Test message from Mnemosyne CoT publisher 🐀")
    elif cmd == "alert":
        send_alert_cot("TEST ALERT", "This is a test alert from the pipeline", lat=51.5, lon=-0.1)
    elif cmd == "pos":
        send_agent_position("charlie", lat=51.5074, lon=-0.1278,
                            remarks="Charlie online — test position")
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
