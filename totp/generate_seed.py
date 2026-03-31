#!/usr/bin/env python3
"""
totp/generate_seed.py — Generate and manage TOTP credentials for agent mesh auth.

Usage:
    # Generate a new seed + backup codes
    python3 totp/generate_seed.py

    # Regenerate backup codes for an existing seed
    python3 totp/generate_seed.py --seed YOUR_EXISTING_SEED --backup-codes

    # Verify a code against a seed
    python3 totp/generate_seed.py --seed YOUR_SEED --verify 123456

    # Save state to a JSON file
    python3 totp/generate_seed.py --save totp_state.json
"""

import argparse
import json
import os
import secrets
import sys
import time

try:
    import pyotp
except ImportError:
    print("ERROR: pyotp not installed. Run: pip install pyotp")
    sys.exit(1)

try:
    import bcrypt
    BCRYPT_AVAILABLE = True
except ImportError:
    BCRYPT_AVAILABLE = False


def generate_seed() -> str:
    return pyotp.random_base32()


def generate_backup_codes(count: int = 10) -> tuple[list[str], list[str]]:
    """
    Generate one-time backup codes.

    Returns:
        (plaintext_codes, hashed_codes)
        Store only the hashed codes. Show plaintext once to the user.
    """
    plaintext = [secrets.token_hex(6).upper() for _ in range(count)]  # e.g. A3F291BC12

    if BCRYPT_AVAILABLE:
        hashed = [
            bcrypt.hashpw(code.encode(), bcrypt.gensalt()).decode()
            for code in plaintext
        ]
    else:
        # Fallback: store as sha256 hex (less secure but functional without bcrypt)
        import hashlib
        hashed = [
            hashlib.sha256(code.encode()).hexdigest()
            for code in plaintext
        ]

    return plaintext, hashed


def verify_backup_code(code: str, hashed_codes: list[str]) -> tuple[bool, int]:
    """
    Check a code against the stored hashed backup codes.

    Returns:
        (matched, index) — index is -1 if no match.
    After a match, remove the code at that index from storage (one-time use).
    """
    code = code.strip().upper()

    if BCRYPT_AVAILABLE:
        for i, h in enumerate(hashed_codes):
            if bcrypt.checkpw(code.encode(), h.encode()):
                return True, i
    else:
        import hashlib
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        for i, h in enumerate(hashed_codes):
            if h == code_hash:
                return True, i

    return False, -1


def verify_totp(seed: str, code: str, valid_window: int = 1) -> bool:
    """Verify a TOTP code against a seed. valid_window=1 allows ±30s drift."""
    return pyotp.TOTP(seed).verify(code.strip(), valid_window=valid_window)


def build_state(seed: str, hashed_codes: list[str]) -> dict:
    return {
        "seed":            seed,
        "backup_codes":    hashed_codes,
        "codes_remaining": len(hashed_codes),
        "created_at":      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rotated_at":      None,
        "hash_algo":       "bcrypt" if BCRYPT_AVAILABLE else "sha256",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="TOTP seed and backup code manager")
    parser.add_argument("--seed",         help="Existing seed (skip generation)")
    parser.add_argument("--backup-codes", action="store_true",
                        help="Generate backup codes for an existing seed")
    parser.add_argument("--verify",       metavar="CODE",
                        help="Verify a TOTP code against --seed")
    parser.add_argument("--save",         metavar="FILE",
                        help="Save state JSON to this file (hashed codes only)")
    parser.add_argument("--count",        type=int, default=10,
                        help="Number of backup codes to generate (default: 10)")
    args = parser.parse_args()

    seed = args.seed or generate_seed()

    # ── Verify mode ───────────────────────────────────────────────────────────
    if args.verify:
        if not args.seed:
            print("ERROR: --verify requires --seed")
            sys.exit(1)
        ok = verify_totp(seed, args.verify)
        print(f"{'✅ Valid' if ok else '❌ Invalid'} TOTP code for seed {seed[:8]}...")
        sys.exit(0 if ok else 1)

    # ── Generate ──────────────────────────────────────────────────────────────
    print("=" * 60)
    print("  TOTP Seed Generated")
    print("=" * 60)
    print(f"  Seed:       {seed}")
    print(f"  Test code:  {pyotp.TOTP(seed).now()}")
    print()
    print("  Add to .env:")
    print(f"    AGENT_TOTP_SEED={seed}")
    print()

    plaintext_codes, hashed_codes = generate_backup_codes(args.count)
    algo = "bcrypt" if BCRYPT_AVAILABLE else "sha256 (install bcrypt for stronger hashing)"

    print(f"  Backup Codes ({algo}):")
    print("  ⚠️  Save these NOW — they won't be shown again:")
    print()
    for i, code in enumerate(plaintext_codes, 1):
        print(f"    {i:2d}. {code}")
    print()

    if not BCRYPT_AVAILABLE:
        print("  ⚠️  bcrypt not installed — codes hashed with sha256.")
        print("       Install bcrypt for stronger backup code security: pip install bcrypt")
        print()

    state = build_state(seed, hashed_codes)

    if args.save:
        with open(args.save, "w") as f:
            json.dump(state, f, indent=2)
        print(f"  State saved to: {args.save}")
        print("  (hashed codes only — plaintext codes NOT saved)")
    else:
        print("  State JSON (hashed codes — safe to store):")
        print(json.dumps(state, indent=2))

    print()
    print("=" * 60)


if __name__ == "__main__":
    main()
