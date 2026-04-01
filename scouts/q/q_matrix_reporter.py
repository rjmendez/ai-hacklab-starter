#!/usr/bin/env python3
"""
Q Scout Matrix Reporter
Reports Q intelligence findings to #q-intelligence Matrix room
"""

import os
import sys
import json
import redis
import requests
import time
import warnings
from datetime import datetime
from typing import Optional

warnings.filterwarnings('ignore', message='Unverified HTTPS')

# Config
HOMESERVER = "https://mrpink.tail9c4667.ts.net"
USER_ID = "@charlie:openclaw.local"
PASSWORD = os.getenv("MATRIX_PASSWORD", "VE4nYVorHHc4toefgzqNW28I")
ROOM_ID = "!..." # Will get from #ops

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = 6379
REDIS_PASS = os.environ.get("REDIS_PASSWORD", "")

class QMatrixReporter:
    def __init__(self):
        self.homeserver = HOMESERVER
        self.user_id = USER_ID
        self.password = PASSWORD
        self.token = None
        self.room_id = None
        self.redis = None
        
    def connect_redis(self):
        """Connect to Redis"""
        try:
            self.redis = redis.Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                password=REDIS_PASS,
                decode_responses=True,
                socket_timeout=5
            )
            self.redis.ping()
            print("✅ Connected to Redis")
            return True
        except Exception as e:
            print(f"❌ Redis connection failed: {e}")
            return False
    
    def login(self) -> bool:
        """Login to Matrix"""
        try:
            resp = requests.post(
                f"{self.homeserver}/_matrix/client/v3/login",
                json={
                    "type": "m.login.password",
                    "user": self.user_id.split(':')[0][1:],
                    "password": self.password
                },
                verify=False,
                timeout=5
            )
            
            if resp.ok:
                self.token = resp.json()['access_token']
                print(f"✅ Logged in as {self.user_id}")
                return True
            else:
                print(f"❌ Login failed: {resp.status_code}")
                return False
        except Exception as e:
            print(f"❌ Login error: {e}")
            return False
    
    def get_room_id(self, room_name: str) -> Optional[str]:
        """Get room ID by name"""
        try:
            rooms_resp = requests.get(
                f"{self.homeserver}/_matrix/client/v3/joined_rooms",
                headers={"Authorization": f"Bearer {self.token}"},
                verify=False,
                timeout=5
            )
            
            if rooms_resp.ok:
                rooms = rooms_resp.json().get('joined_rooms', [])
                
                for room_id in rooms:
                    state_resp = requests.get(
                        f"{self.homeserver}/_matrix/client/v3/rooms/{room_id}/state",
                        headers={"Authorization": f"Bearer {self.token}"},
                        verify=False,
                        timeout=5
                    )
                    
                    if state_resp.ok:
                        for state in state_resp.json():
                            if state.get('type') == 'm.room.name':
                                name = state.get('content', {}).get('name', '')
                                if room_name.lower() in name.lower():
                                    self.room_id = room_id
                                    print(f"✅ Found room: {name} ({room_id})")
                                    return room_id
        except Exception as e:
            print(f"❌ Failed to get room: {e}")
        
        return None
    
    def send_message(self, body: str, formatted_body: Optional[str] = None):
        """Send message to room"""
        if not self.token or not self.room_id:
            print("❌ Not authenticated or room not found")
            return False
        
        try:
            msg_data = {
                "msgtype": "m.text",
                "body": body
            }
            if formatted_body:
                msg_data["format"] = "org.matrix.custom.html"
                msg_data["formatted_body"] = formatted_body
            
            resp = requests.put(
                f"{self.homeserver}/_matrix/client/r0/rooms/{self.room_id}/send/m.room.message/{int(time.time()*1000)}",
                json=msg_data,
                headers={"Authorization": f"Bearer {self.token}"},
                verify=False,
                timeout=5
            )
            
            if resp.ok:
                return True
            else:
                print(f"❌ Send failed: {resp.status_code}")
                return False
        except Exception as e:
            print(f"❌ Send error: {e}")
            return False
    
    def report_finding(self, finding: dict):
        """Report Q finding to Matrix"""
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        
        body = f"""
Q Intelligence Report
Timestamp: {timestamp}

Type: {finding.get('type', 'N/A')}
Source: {finding.get('source', 'N/A')}
Content: {finding.get('content', 'N/A')[:500]}
        """.strip()
        
        self.send_message(body)
    
    def run(self):
        """Main reporter loop"""
        print("Q Scout Matrix Reporter Starting...")
        
        if not self.connect_redis():
            return
        
        if not self.login():
            return
        
        if not self.get_room_id("q-intelligence"):
            print("❌ Could not find #q-intelligence room")
            return
        
        # Send startup message
        self.send_message("✅ **Q Scout Reporter Active**\nServer: https://mrpink.tail9c4667.ts.net\nMonitoring Q intelligence findings...")
        
        # Monitor Redis for Q findings
        print("\n📨 Monitoring Q findings...")
        last_report = 0
        
        while True:
            try:
                # Check Redis for new findings
                findings = self.redis.lrange("q:findings", 0, -1)
                
                for finding_json in findings:
                    finding = json.loads(finding_json)
                    report_ts = finding.get('timestamp', 0)
                    
                    # Only report if newer than last check
                    if report_ts > last_report:
                        self.report_finding(finding)
                        last_report = report_ts
                
                time.sleep(10)  # Check every 10 seconds
            except KeyboardInterrupt:
                print("\n🛑 Shutting down Q reporter")
                break
            except Exception as e:
                print(f"⚠️  Error in reporting loop: {e}")
                time.sleep(5)

if __name__ == "__main__":
    reporter = QMatrixReporter()
    reporter.run()
