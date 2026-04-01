#!/usr/bin/env python3
"""
Base Matrix Reporter for Charlie Scouts
"""

import os
import sys
import json
import redis
import requests
import time
import warnings
from datetime import datetime
from typing import Optional, Dict, List

warnings.filterwarnings('ignore', message='Unverified HTTPS')

# Base Config
HOMESERVER = "https://mrpink.tail9c4667.ts.net"
USER_ID = "@charlie:openclaw.local"
PASSWORD = os.getenv("MATRIX_PASSWORD", "VE4nYVorHHc4toefgzqNW28I")

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = 6379
REDIS_PASS = os.environ.get("REDIS_PASSWORD", "")

# Room Mapping
ROOM_MAP = {
    "q": "!bOtgZXdjKURJPsOryG:openclaw.local",
    "ratchet": "!zGseFBKGMlylldyUik:openclaw.local",
    "hermes": "!wBqfGmahTqPHQOIFSl:openclaw.local",
    "atlas": "!fxhnnYvweFTBLckodY:openclaw.local",
    "rate": "!jefHoCCkWZeGoRqVYm:openclaw.local",
    "paxos": "!PbReHHZXTBVTuLSAcf:openclaw.local"
}

class MatrixReporter:
    def __init__(self, scout_name: str):
        self.scout_name = scout_name.lower()
        self.homeserver = HOMESERVER
        self.user_id = USER_ID
        self.password = PASSWORD
        self.token = None
        self.room_id = ROOM_MAP.get(self.scout_name)
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
                return True
            else:
                return False
        except Exception as e:
            print(f"❌ Login error: {e}")
            return False
    
    def send_message(self, body: str, formatted_body: Optional[str] = None):
        """Send message to room"""
        if not self.token or not self.room_id:
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
            
            return resp.ok
        except Exception as e:
            return False
    
    def format_finding(self, finding: Dict) -> str:
        """Format finding for Matrix (override in subclass)"""
        return f"{self.scout_name.title()} Finding: {json.dumps(finding, indent=2)}"
    
    def run(self):
        """Main reporter loop"""
        print(f"{self.scout_name.title()} Scout Matrix Reporter Starting...")
        
        if not self.room_id:
            print(f"❌ No room configured for {self.scout_name}")
            return
        
        if not self.connect_redis():
            return
        
        if not self.login():
            print("❌ Matrix login failed")
            return
        
        print(f"✅ Monitoring {self.scout_name} findings...")
        
        # Monitor Redis for findings
        redis_key = f"{self.scout_name}:findings"
        last_report = time.time()
        
        while True:
            try:
                # Check for new findings in Redis
                findings = self.redis.lrange(redis_key, 0, -1)
                
                if findings:
                    for finding_json in findings:
                        try:
                            finding = json.loads(finding_json)
                            msg = self.format_finding(finding)
                            self.send_message(msg)
                        except Exception as e:
                            print(f"⚠️  Error processing finding: {e}")
                    
                    # Clear processed findings
                    self.redis.delete(redis_key)
                
                time.sleep(10)
            except KeyboardInterrupt:
                print(f"\n🛑 Shutting down {self.scout_name} reporter")
                break
            except Exception as e:
                print(f"⚠️  Error in reporting loop: {e}")
                time.sleep(5)