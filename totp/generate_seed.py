#!/usr/bin/env python3
"""Generate a fresh TOTP seed for agent mesh authentication."""
import pyotp

seed = pyotp.random_base32()
print(f"TOTP Seed:  {seed}")
print(f"Test code:  {pyotp.TOTP(seed).now()}")
print(f"\nAdd to .env:")
print(f"  AGENT_TOTP_SEED={seed}")
