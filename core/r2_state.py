"""R2-backed state store for the paper-wheel bot (boto3, S3-compatible).

Why this exists
---------------
State used to live in git: .github/workflows/wheel-daily.yml ran the bot on a
GitHub runner and committed state/ back to the repo on every run. That made
GitHub part of the *execution* path, not just CI, and it made the bot's memory
a function of whether a push succeeded. The bot now runs in a Cloudflare
container on a Worker cron, so its memory has to live somewhere the container
can reach: R2.

Layout (bucket = R2_BUCKET, default 'options-wheel-state'):
  state/daily_state.json   — last run's status/equity/positions/breaches
  state/nav_history.jsonl  — append-only one row per session date
  state/spread_book.json   — spy-spread sleeve ledger (core/spread_sleeve.py)
  state/last_run.json      — run envelope for GET /status (rc, output tail)

Mirrors autopilot-experiment/state_sync.py: R2 primary, local fallback for
dev. The local fallback root defaults to the REPO ROOT, so a key like
"state/daily_state.json" maps to ./state/daily_state.json — byte-identical to
the pre-migration layout, which is what makes `python scripts/run_daily.py`
still work on a laptop with no R2 credentials.

Fallback is a *dev* affordance, never a silent cloud degradation: the container
asserts remote-ness at boot (server.py) so a credential typo fails loudly
instead of quietly writing state into a container filesystem that evaporates.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
except ImportError:  # dev machines without boto3 still run locally
    boto3 = None  # type: ignore
    ClientError = Exception  # type: ignore

ROOT = Path(__file__).parent.parent

DAILY_STATE_KEY = "state/daily_state.json"
NAV_HISTORY_KEY = "state/nav_history.jsonl"
SPREAD_BOOK_KEY = "state/spread_book.json"
LAST_RUN_KEY = "state/last_run.json"

DEFAULT_BUCKET = "options-wheel-state"


def _local_root() -> Path:
    configured = os.environ.get("WHEEL_LOCAL_STATE_DIR")
    if not configured:
        return ROOT
    path = Path(configured).expanduser()
    return path if path.is_absolute() else ROOT / path


class WheelState:
    """R2 state store with a local-filesystem fallback."""

    def __init__(
        self,
        bucket: str | None = None,
        endpoint: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        local_root: Path | str | None = None,
    ) -> None:
        self.bucket = bucket or os.environ.get("R2_BUCKET", DEFAULT_BUCKET)
        ep = endpoint or os.environ.get("R2_ENDPOINT", "")
        if not ep:
            acct = os.environ.get("R2_ACCOUNT_ID", "")
            if acct:
                ep = f"https://{acct}.r2.cloudflarestorage.com"
        self._endpoint = ep
        self._access_key = access_key or os.environ.get("R2_ACCESS_KEY_ID", "")
        self._secret_key = secret_key or os.environ.get("R2_SECRET_ACCESS_KEY", "")
        self.local_root = Path(local_root) if local_root else _local_root()
        self._client = self._build_client()

    def _build_client(self):
        # An explicit local root is an isolation request, not merely a
        # different emergency fallback — honour it even if creds are present.
        if os.environ.get("WHEEL_LOCAL_STATE_DIR"):
            return None
        if boto3 is None or not (self._endpoint and self._access_key and self._secret_key):
            return None
        return boto3.client(
            "s3",
            endpoint_url=self._endpoint,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
            region_name="auto",
        )

    @property
    def remote(self) -> bool:
        return self._client is not None

    def backend(self) -> str:
        return f"r2://{self.bucket}" if self.remote else f"local://{self.local_root}"

    # ---- raw object I/O -------------------------------------------------

    def get_bytes(self, key: str) -> bytes | None:
        if self._client:
            try:
                resp = self._client.get_object(Bucket=self.bucket, Key=key)
                return resp["Body"].read()
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                    return None
                raise
        path = self.local_root / key
        if not path.exists():
            return None
        return path.read_bytes()

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        if self._client:
            self._client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
            return
        path = self.local_root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def exists(self, key: str) -> bool:
        if self._client:
            try:
                self._client.head_object(Bucket=self.bucket, Key=key)
                return True
            except ClientError:
                return False
        return (self.local_root / key).exists()

    # ---- typed helpers --------------------------------------------------

    def read_json(self, key: str, default: Any = None) -> Any:
        raw = self.get_bytes(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return default

    def write_json(self, key: str, obj: Any) -> None:
        self.put_bytes(
            key,
            (json.dumps(obj, indent=1) + "\n").encode(),
            content_type="application/json",
        )

    def append_jsonl(self, key: str, record: dict) -> None:
        """Read-modify-write append.

        R2 has no native append. The blobs here are one short row per trading
        day, so a full round trip is cheap and keeps the file human-readable.
        A read that fails is NOT treated as "file is empty" — that would
        silently truncate NAV history — it raises and the caller degrades loudly.
        """
        existing = self.get_bytes(key) or b""
        if existing and not existing.endswith(b"\n"):
            existing += b"\n"
        blob = existing + (json.dumps(record) + "\n").encode()
        self.put_bytes(key, blob, content_type="application/x-ndjson")

    def read_jsonl(self, key: str) -> list[dict]:
        raw = self.get_bytes(key)
        if not raw:
            return []
        out = []
        for line in raw.decode().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
        return out


_singleton: WheelState | None = None


def get_state_store() -> WheelState:
    """Process-wide store. Cheap to build, but the boto3 client is reusable."""
    global _singleton
    if _singleton is None:
        _singleton = WheelState()
    return _singleton
