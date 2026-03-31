import os
import time
import subprocess
import requests

def watchdog():
    pid_file = os.getenv("WATCHDOG_PID_FILE", "/tmp/a2a_server.pid")
    health_url = os.getenv("WATCHDOG_HEALTH_URL", "http://localhost:8000/health")
    interval = int(os.getenv("WATCHDOG_INTERVAL", "30"))

    while True:
        try:
            response = requests.get(health_url, timeout=5)
            if response.status_code == 200:
                time.sleep(interval)
                continue
        except Exception:
            pass

        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
        except Exception:
            subprocess.Popen(["python", "a2a/server.py"])

        time.sleep(interval)

if __name__ == "__main__":
    watchdog()