#!/usr/bin/env python3
"""Interactive helper: prints a bcrypt hash for the web UI password.

Usage: python3 scripts/hash_password.py
Paste the resulting hash into /etc/audiorec/config.toml under [web].password_hash.
"""
from __future__ import annotations

import getpass
import sys

try:
    import bcrypt
except ImportError:
    sys.stderr.write("bcrypt not installed. Run: pip install bcrypt\n")
    sys.exit(1)


def main() -> int:
    pw1 = getpass.getpass("New password: ")
    if not pw1:
        sys.stderr.write("Empty password, aborting.\n")
        return 1
    pw2 = getpass.getpass("Confirm:     ")
    if pw1 != pw2:
        sys.stderr.write("Passwords do not match.\n")
        return 1
    hashed = bcrypt.hashpw(pw1.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    print()
    print("Paste this into /etc/audiorec/config.toml under [web]:")
    print(f'password_hash = "{hashed}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
