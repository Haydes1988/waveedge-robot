"""
Shared credential loader — reads /root/waveedge/security/.env and puts its
values into the environment. All three scripts (webhook, position_manager,
outcome_tracker) import this instead of hardcoding credentials directly.

This file itself contains NO secrets — safe to commit to GitHub, safe for
anyone with repo access (a Collaborator or a GitHub App) to read. The real
values live only in security/.env on the VPS, which never gets committed.
"""

import os


def load_env(path="/root/waveedge/security/.env"):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())
