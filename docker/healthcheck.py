# Code by AkinoAlice@TyrantRey

"""The container's HEALTHCHECK (DESIGN/v0-5-0.md §10.3).

``GET /health`` on the address the daemon recorded in ``.tfs/lock``, with the
root's token; exit 0 only for a daemon that answers ``"ok"`` (a daemon that
is stopping, has no lock yet, or does not answer is unhealthy). Standard
library only: it runs every 30 seconds for the life of the container.
"""

import json
import os
import sys
import urllib.request
from pathlib import Path


def main() -> int:
    root = Path(os.environ.get("TFS_ROOT", "/data"))
    try:
        lock = json.loads((root / ".tfs" / "lock").read_text(encoding="utf-8"))
        token = (root / ".tfs" / "token").read_text(encoding="utf-8").strip()
    except (OSError, ValueError) as e:
        print(f"no daemon: {e}")
        return 1
    port = lock.get("port") if isinstance(lock, dict) else None
    if isinstance(port, bool) or not isinstance(port, int):
        print("no daemon: the lock records no control port")
        return 1
    host = str(lock.get("bind") or "127.0.0.1")
    if host == "0.0.0.0":
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    request = urllib.request.Request(
        f"http://{host}:{port}/health", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            payload = json.load(response)
    except Exception as e:  # noqa: BLE001 - any failure is "unhealthy"
        print(f"unreachable: {e}")
        return 1
    status = payload.get("status")
    print(f"{status}: tfs {payload.get('version')} ({payload.get('hash') or 'unknown'})")
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
