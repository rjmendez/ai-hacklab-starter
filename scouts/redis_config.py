#!/usr/bin/env python3
"""
Redis configuration for all scouts.
Updated to use external Redis master since container network is locked.
"""

import redis
import sys

# Use Oxalis's Redis master instead of local audit-redis
# (Charlie container shares network namespace, can't add openclaw-audit network)
REDIS_CONFIG = {
    'host': __import__('os').environ.get('REDIS_HOST','localhost'),  # Oxalis host (was 'audit-redis')
    'port': int(__import__('os').environ.get('REDIS_PORT','6379')),              # External port (was 6379)
    'password': __import__('os').environ.get('REDIS_PASSWORD') or None,
    'decode_responses': True,
    'socket_connect_timeout': 5,
    'socket_timeout': 5
}

def get_redis_client():
    """Get a Redis client with the configured settings."""
    try:
        client = redis.Redis(**REDIS_CONFIG)
        # Test connection
        client.ping()
        return client
    except Exception as e:
        print(f"❌ Redis connection failed: {e}")
        print(f"   Host: {REDIS_CONFIG['host']}:{REDIS_CONFIG['port']}")
        return None

if __name__ == '__main__':
    # Test the connection
    client = get_redis_client()
    if client:
        print(f"✅ Redis connected: {REDIS_CONFIG['host']}:{REDIS_CONFIG['port']}")
        client.set('charlie:test', 'connection_ok', ex=60)
        print(f"✅ Test write successful")
    else:
        sys.exit(1)