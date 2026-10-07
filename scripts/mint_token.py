#!/usr/bin/env python
"""Mint a local HS256 dev token, optionally scoped to an organization.

    python -m scripts.mint_token --user-id alice --role admin
    python -m scripts.mint_token --user-id alice --org-id org_ab12cd34ef56

The `org_id` claim (or the X-Org-Id header) is only a *hint*: the server still
verifies a real membership before granting access. With no --org-id the token
resolves to the caller's auto-created personal org.
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from jose import jwt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import JWT_SECRET, JWT_ALGORITHM  # noqa: E402
from app.orgs import personal_org_id  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Mint a local dev JWT")
    parser.add_argument("--user-id", required=True, help="The `sub` claim")
    parser.add_argument("--org-id", default=None, help="Target org (default: personal org)")
    parser.add_argument("--role", default=None, help="Hint only; the real role comes from the DB membership")
    parser.add_argument("--days", type=int, default=7, help="Token lifetime in days")
    args = parser.parse_args()

    if not JWT_SECRET:
        print("JWT_SECRET is not set — cannot mint an HS256 token", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(timezone.utc)
    payload = {
        "sub": args.user_id,
        "iat": int(time.time()),
        "exp": int((now + timedelta(days=args.days)).timestamp()),
    }
    org_id = args.org_id or personal_org_id(args.user_id)
    payload["org_id"] = org_id
    if args.role:
        payload["role"] = args.role

    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    print(token)
    print(f"\n# user: {args.user_id}  org: {org_id}", file=sys.stderr)


if __name__ == "__main__":
    main()
