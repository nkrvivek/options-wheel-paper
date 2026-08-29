"""HTTP entrypoint for the paper-wheel Cloudflare container.

Routes (port 8080):
  GET  /healthz    — liveness, no auth (Docker HEALTHCHECK + worker probe)
  GET  /status     — health + last run summary, token-gated
  POST /run-daily  — the scripts/run_daily.py code path, token-gated

Auth mirrors the reference pattern (autopilot-experiment/server.py): the Worker
holds CONTAINER_AUTH_TOKEN and sends it on every container call; anything
without it gets 401. The token is compared with hmac.compare_digest and never
logged.

The container is the ONLY thing that trades. The Worker cron is the only thing
that starts it. GitHub builds the image and nothing else.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from core.r2_state import (  # noqa: E402
    DAILY_STATE_KEY,
    LAST_RUN_KEY,
    NAV_HISTORY_KEY,
    SPREAD_BOOK_KEY,
    get_state_store,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("wheel-server")

PORT = int(os.environ.get("PORT", "8080"))
AUTH_TOKEN = os.environ.get("CONTAINER_AUTH_TOKEN", "")
BOOT_TS = datetime.now(timezone.utc).isoformat()
GIT_COMMIT = os.environ.get("GIT_COMMIT", "unknown")

# Self-expiring run guard. A wedged run must not block the book forever, and
# two concurrent /run-daily calls must never both trade: a duplicate cron
# delivery or an operator retry lands on 409 while the first run holds the
# deadline, and the deadline auto-expires so the container self-heals.
_run_lock = threading.Lock()
_run_busy_until = [0.0]
MAX_RUN_SECONDS = float(os.environ.get("WHEEL_MAX_RUN_SECONDS", "1500"))


def _authed(handler: BaseHTTPRequestHandler) -> bool:
    if not AUTH_TOKEN:
        # Fail closed: an unset token means misconfiguration, not open season.
        return False
    provided = handler.headers.get("X-Wheel-Token", "") or handler.headers.get(
        "X-Autopilot-Token", ""
    )
    return hmac.compare_digest(provided, AUTH_TOKEN)


def _run_daily() -> dict:
    """Invoke scripts/run_daily.py in a subprocess, same as CI used to.

    Subprocess rather than an import so a hard failure (segfault, OOM kill,
    runaway import) cannot take the HTTP server down with it, and so the
    timeout is enforceable.
    """
    started = datetime.now(timezone.utc)
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_daily.py")],
            env=env,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=MAX_RUN_SECONDS,
        )
        rc, output = proc.returncode, (proc.stdout + proc.stderr)
    except subprocess.TimeoutExpired as e:
        rc = 124
        output = f"run_daily timed out after {MAX_RUN_SECONDS}s\n{e.stdout or ''}{e.stderr or ''}"
    finished = datetime.now(timezone.utc)

    store = get_state_store()
    daily = store.read_json(DAILY_STATE_KEY) or {}
    envelope = {
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 1),
        "rc": rc,
        "git_commit": GIT_COMMIT,
        "state_backend": store.backend(),
        "date": daily.get("date"),
        "status": daily.get("status"),
        "equity": daily.get("equity"),
        "positions": len(daily.get("positions") or []),
        "breaches": daily.get("breaches") or [],
        # Tail only — the full strategy log stays in container stdout, which
        # Cloudflare captures. Enough to triage from /status without paging.
        "output_tail": output[-4000:],
    }
    try:
        store.write_json(LAST_RUN_KEY, envelope)
    except Exception as e:  # a lost breadcrumb must not fail the run
        logger.warning("could not persist last_run envelope: %s", e.__class__.__name__)
    return envelope


def _status() -> dict:
    store = get_state_store()
    last_run = store.read_json(LAST_RUN_KEY) or {}
    daily = store.read_json(DAILY_STATE_KEY) or {}
    nav = store.read_jsonl(NAV_HISTORY_KEY)
    book = store.read_json(SPREAD_BOOK_KEY) or {}
    now = time.monotonic()
    return {
        "ok": True,
        "service": "options-wheel-paper",
        "booted_at": BOOT_TS,
        "now": datetime.now(timezone.utc).isoformat(),
        "git_commit": GIT_COMMIT,
        "state_backend": store.backend(),
        "state_remote": store.remote,
        "is_paper": os.environ.get("IS_PAPER", "true"),
        "run_busy": now < _run_busy_until[0],
        "last_run": {
            k: last_run.get(k)
            for k in (
                "started_at",
                "finished_at",
                "duration_seconds",
                "rc",
                "date",
                "status",
                "equity",
                "positions",
                "breaches",
            )
        }
        if last_run
        else None,
        "daily_state": {
            "date": daily.get("date"),
            "status": daily.get("status"),
            "equity": daily.get("equity"),
            "cash": daily.get("cash"),
            "positions": len(daily.get("positions") or []),
            "breaches": daily.get("breaches") or [],
        }
        if daily
        else None,
        "nav_history_rows": len(nav),
        "nav_history_last": nav[-1] if nav else None,
        "spread_sleeve": {
            "killed": book.get("killed"),
            "realized_pnl": book.get("realized_pnl"),
            "open_positions": len(book.get("open_positions") or []),
            "last_entry_date": book.get("last_entry_date"),
        }
        if book
        else None,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "options-wheel/1.0"

    def log_message(self, fmt, *args):  # route to logging, not stderr scribble
        logger.info("%s %s", self.address_string(), fmt % args)

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, indent=1).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/healthz", "/health"):
            self._send(200, {"ok": True, "service": "options-wheel-paper", "booted_at": BOOT_TS})
            return
        if path == "/status":
            if not _authed(self):
                self._send(401, {"error": "unauthorized"})
                return
            try:
                self._send(200, _status())
            except Exception as e:
                self._send(500, {"error": "status failed", "detail": f"{e.__class__.__name__}: {e}"})
            return
        self._send(404, {"error": "not found", "path": path})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path != "/run-daily":
            self._send(404, {"error": "not found", "path": path})
            return
        if not _authed(self):
            self._send(401, {"error": "unauthorized"})
            return
        # Drain any body so the client doesn't see a reset.
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        query = parse_qs(parsed.query)
        with _run_lock:
            now = time.monotonic()
            if now < _run_busy_until[0]:
                self._send(409, {"error": "run already in progress", "service": "options-wheel-paper"})
                return
            _run_busy_until[0] = now + MAX_RUN_SECONDS
        try:
            logger.info("run-daily starting (trigger=%s)", (query.get("trigger") or ["manual"])[0])
            envelope = _run_daily()
            logger.info("run-daily finished rc=%s status=%s", envelope.get("rc"), envelope.get("status"))
            self._send(200, envelope)
        except Exception as e:
            logger.exception("run-daily failed")
            self._send(500, {"error": "run failed", "detail": f"{e.__class__.__name__}: {e}"})
        finally:
            with _run_lock:
                _run_busy_until[0] = 0.0


def main():
    store = get_state_store()
    # Loud boot assertion: in the container, local state is a filesystem that
    # evaporates on restart. If R2 did not resolve, say so at boot rather than
    # discovering a month of missing NAV history later.
    if not store.remote and os.environ.get("WHEEL_REQUIRE_R2", "0") == "1":
        raise SystemExit(
            "WHEEL_REQUIRE_R2=1 but the R2 client did not build — check "
            "R2_BUCKET / R2_ENDPOINT (or R2_ACCOUNT_ID) / R2_ACCESS_KEY_ID / "
            "R2_SECRET_ACCESS_KEY in the container env."
        )
    logger.info(
        "options-wheel server on :%s auth=%s state=%s commit=%s",
        PORT,
        "on" if AUTH_TOKEN else "OFF(all requests will 401)",
        store.backend(),
        GIT_COMMIT,
    )
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
