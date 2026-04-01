#!/usr/bin/env python3
"""
meshtastic_tak_bridge.py — Meshtastic → TAK CoT bridge

Connects to Meshtastic TCP interface and forwards position/message packets
to FreeTAKServer as Cursor-on-Target (CoT) XML events.

Meshtastic node: 100.73.200.19:4403
TAK Server:      openclaw-freetakserver:8087

Known nodes: Haggis, Kimchi, Oxalis (COM20)
"""

import socket
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    import meshtastic
    import meshtastic.tcp_interface
    from pubsub import pub
    MESHTASTIC_AVAILABLE = True
except ImportError:
    MESHTASTIC_AVAILABLE = False
    print("[bridge] ERROR: meshtastic package not installed. Install with: pip install meshtastic")

# ── Configuration ─────────────────────────────────────────────────────────────

MESH_HOST = "100.73.200.19"
MESH_PORT = 4403

TAK_HOST = "openclaw-freetakserver"
TAK_PORT = 8087

RECONNECT_DELAY = 10  # seconds between reconnect attempts

# Known node name overrides (node_id → callsign)
KNOWN_NODES = {
    # Add actual Meshtastic node IDs here once known
    # "!abcd1234": "Haggis",
    # "!abcd5678": "Kimchi",
    # "!abcdabcd": "Oxalis",
}

# ── CoT XML Helpers ────────────────────────────────────────────────────────────

def iso_time(dt: datetime) -> str:
    """Format datetime as CoT ISO8601 string."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def make_position_cot(node_id: str, callsign: str, lat: float, lon: float,
                      hae: float = 0.0, rssi: Optional[int] = None,
                      battery: Optional[int] = None) -> str:
    """Build a CoT position event XML string from a Meshtastic position packet."""
    now = datetime.now(timezone.utc)
    stale = now + timedelta(minutes=5)

    uid = f"MESHTASTIC-{node_id.replace('!', '')}"

    remarks_parts = ["Via Meshtastic LoRa"]
    if rssi is not None:
        remarks_parts.append(f"RSSI: {rssi}")
    if battery is not None:
        remarks_parts.append(f"Battery: {battery}%")
    remarks = " | ".join(remarks_parts)

    cot = f"""<?xml version="1.0" encoding="UTF-8"?>
<event version="2.0" uid="{uid}" type="a-f-G-U-C"
       time="{iso_time(now)}" start="{iso_time(now)}" stale="{iso_time(stale)}"
       how="m-g">
  <point lat="{lat}" lon="{lon}" hae="{hae}" ce="10" le="10"/>
  <detail>
    <contact callsign="{callsign}"/>
    <remarks>{remarks}</remarks>
  </detail>
</event>"""
    return cot


def make_geochat_cot(message: str, sender_uid: str, callsign: str) -> str:
    """Build a TAK GeoChat CoT XML string from a Meshtastic text message."""
    now = datetime.now(timezone.utc)
    stale = now + timedelta(minutes=10)

    uid = f"GeoChat.{sender_uid}.All.{int(now.timestamp())}"

    cot = f"""<?xml version="1.0" encoding="UTF-8"?>
<event version="2.0" uid="{uid}" type="b-t-f"
       time="{iso_time(now)}" start="{iso_time(now)}" stale="{iso_time(stale)}"
       how="h-g-i-g-o">
  <point lat="0" lon="0" hae="0" ce="9999999" le="9999999"/>
  <detail>
    <__chat parent="RootContactGroup" groupOwner="false"
            chatroom="All Chat Rooms" id="All Chat Rooms" senderCallsign="{callsign}">
      <chatgrp uid0="{sender_uid}" uid1="All Chat Rooms" id="All Chat Rooms"/>
    </__chat>
    <link uid="{sender_uid}" type="a-f-G-U-C" relation="p-p"/>
    <remarks source="BAO.F.ATAK.{sender_uid}" to="All Chat Rooms"
             time="{iso_time(now)}">[Mesh] {message}</remarks>
    <__serverdestination destinations="All Chat Rooms"/>
  </detail>
</event>"""
    return cot


# ── TAK TCP Sender ─────────────────────────────────────────────────────────────

class TAKSender:
    """Maintains a persistent TCP connection to FreeTAKServer and sends CoT XML."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._connect()

    def _connect(self):
        """Attempt to connect to TAK server."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(10)
            s.connect((self.host, self.port))
            self._sock = s
            print(f"[TAK] Connected to {self.host}:{self.port}")
        except Exception as e:
            print(f"[TAK] Connection failed: {e}")
            self._sock = None

    def send(self, cot_xml: str) -> bool:
        """Send CoT XML to TAK server. Returns True on success."""
        with self._lock:
            for attempt in range(2):
                if self._sock is None:
                    self._connect()
                if self._sock is None:
                    return False
                try:
                    data = cot_xml.encode("utf-8")
                    self._sock.sendall(data)
                    return True
                except Exception as e:
                    print(f"[TAK] Send error (attempt {attempt+1}): {e}")
                    try:
                        self._sock.close()
                    except Exception:
                        pass
                    self._sock = None
        return False

    def close(self):
        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None


# ── Meshtastic Callbacks ───────────────────────────────────────────────────────

tak_sender: Optional[TAKSender] = None


def get_callsign(node_id: str, node_info: Optional[dict] = None) -> str:
    """Resolve a callsign from node_id, known nodes map, or node info."""
    if node_id in KNOWN_NODES:
        return KNOWN_NODES[node_id]
    if node_info:
        user = node_info.get("user", {})
        long_name = user.get("longName", "")
        short_name = user.get("shortName", "")
        if long_name:
            return long_name
        if short_name:
            return short_name
    return f"Mesh-{node_id[-4:]}"


def on_receive(packet, interface):
    """Called for every received Meshtastic packet."""
    global tak_sender
    if tak_sender is None:
        return

    decoded = packet.get("decoded", {})
    portnum = decoded.get("portnum", "")
    from_id = packet.get("fromId", "unknown")

    # Try to get node info for callsign resolution
    node_info = None
    try:
        node_info = interface.nodes.get(from_id)
    except Exception:
        pass

    callsign = get_callsign(from_id, node_info)
    rssi = packet.get("rxRssi")

    # Battery from device metrics
    battery = None
    try:
        battery = decoded.get("telemetry", {}).get("deviceMetrics", {}).get("batteryLevel")
    except Exception:
        pass

    if portnum == "POSITION_APP":
        pos = decoded.get("position", {})
        lat = pos.get("latitudeI", 0) / 1e7
        lon = pos.get("longitudeI", 0) / 1e7
        alt = pos.get("altitude", 0)

        if lat == 0.0 and lon == 0.0:
            print(f"[bridge] Skipping zero-position from {callsign}")
            return

        cot = make_position_cot(
            node_id=from_id,
            callsign=callsign,
            lat=lat,
            lon=lon,
            hae=float(alt),
            rssi=rssi,
            battery=battery,
        )
        ok = tak_sender.send(cot)
        print(f"[bridge] Position {callsign} ({lat:.5f},{lon:.5f}) → TAK {'OK' if ok else 'FAIL'}")

    elif portnum == "TEXT_MESSAGE_APP":
        text = decoded.get("text", "")
        if not text:
            return
        sender_uid = f"MESHTASTIC-{from_id.replace('!', '')}"
        cot = make_geochat_cot(message=text, sender_uid=sender_uid, callsign=callsign)
        ok = tak_sender.send(cot)
        print(f"[bridge] Chat from {callsign}: {text[:60]!r} → TAK {'OK' if ok else 'FAIL'}")

    else:
        # Silently ignore other port types (telemetry, routing, etc.)
        pass


def on_connection(interface, topic=pub.AUTO_TOPIC):
    """Called when Meshtastic connection is established."""
    print(f"[bridge] Meshtastic connected: {topic.getName()}")


# ── Main Loop ─────────────────────────────────────────────────────────────────

def run_bridge():
    """Main bridge loop with reconnection logic."""
    global tak_sender

    if not MESHTASTIC_AVAILABLE:
        print("[bridge] Cannot run: meshtastic package missing.")
        return

    tak_sender = TAKSender(TAK_HOST, TAK_PORT)

    pub.subscribe(on_receive, "meshtastic.receive")
    pub.subscribe(on_connection, "meshtastic.connection.established")

    while True:
        iface = None
        try:
            print(f"[bridge] Connecting to Meshtastic at {MESH_HOST}:{MESH_PORT}...")
            iface = meshtastic.tcp_interface.TCPInterface(hostname=MESH_HOST, portNumber=MESH_PORT)
            print("[bridge] Bridge running. Ctrl-C to stop.")
            # Keep alive — the pubsub callbacks handle incoming packets
            while True:
                time.sleep(30)
                # Ping to detect dead connection
                try:
                    iface.localNode.getMetadata()
                except Exception:
                    print("[bridge] Meshtastic heartbeat failed, reconnecting...")
                    break

        except KeyboardInterrupt:
            print("[bridge] Shutting down.")
            break
        except Exception as e:
            print(f"[bridge] Meshtastic error: {e}")
        finally:
            if iface:
                try:
                    iface.close()
                except Exception:
                    pass

        print(f"[bridge] Reconnecting in {RECONNECT_DELAY}s...")
        time.sleep(RECONNECT_DELAY)

    if tak_sender:
        tak_sender.close()


if __name__ == "__main__":
    run_bridge()
