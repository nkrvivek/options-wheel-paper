#!/usr/bin/env bash
# Idempotent deploy for the paper-wheel Cloudflare Worker + Container.
#
# Mirrors autopilot-experiment/scripts/deploy_hackathon.sh: R2 bootstrap,
# RTH guard, secret push, GIT_COMMIT stamping with restore, health check.
#
# Secret values resolve from the process environment first (CI supplies them
# from GitHub repo secrets) and fall back to ./.env for local runs. Values are
# piped straight into `wrangler secret put` and never printed.
#
#   local:  bash scripts/deploy_wheel.sh
#   CI:     .github/workflows/deploy-wheel.yml (Docker is not usable on the Mac)
#   dry:    DRY_RUN=1 bash scripts/deploy_wheel.sh
set -euo pipefail

export PATH="$HOME/.aside/runtime/bin:$PATH"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
WRANGLER_CONFIG="wrangler.toml"
ENV_FILE="$REPO_ROOT/.env"
BUCKET="options-wheel-state"
WORKER_URL="https://options-wheel-paper.nkrvivek.workers.dev"
DRY_RUN="${DRY_RUN:-0}"
HEAD_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

log() { echo "==> $*"; }
err() { echo "ERROR: $*" >&2; exit 1; }
run() {
  if [ "$DRY_RUN" = "1" ]; then echo "[DRY-RUN] $*"; else echo "[RUN] $*"; "$@"; fi
}

# Value for KEY: environment wins, else .env. Never echoed.
secret_value() {
  local key="$1"
  local from_env="${!key-}"
  if [ -n "$from_env" ]; then printf '%s' "$from_env"; return 0; fi
  if [ -f "$ENV_FILE" ]; then
    grep -m1 "^${key}=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'
  fi
}

SECRETS=(
  CONTAINER_AUTH_TOKEN WORKER_AUTH_TOKEN
  ALPACA_API_KEY ALPACA_SECRET_KEY
  TR_WORKER_URL TR_WORKER_TOKEN
  RESEND_API_KEY RESEND_FROM RESEND_TO
  R2_ACCOUNT_ID R2_ENDPOINT R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY
)
# Without these the container either cannot authenticate, cannot trade, or
# silently loses its state. A missing optional (Resend/earnings) degrades
# loudly inside run_daily instead — that behaviour is unchanged from CI.
REQUIRED_SECRETS=(
  CONTAINER_AUTH_TOKEN WORKER_AUTH_TOKEN
  ALPACA_API_KEY ALPACA_SECRET_KEY
  R2_ACCOUNT_ID R2_ENDPOINT R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY
)

log "Step 1: create R2 bucket $BUCKET (idempotent)"
if [ "$DRY_RUN" = "1" ]; then
  echo "[DRY-RUN] npx wrangler r2 bucket create $BUCKET -c $WRANGLER_CONFIG"
else
  CREATE_OUTPUT="$(npx wrangler r2 bucket create "$BUCKET" -c "$WRANGLER_CONFIG" 2>&1)" || {
    if ! grep -qiE "already exists|already owned" <<<"$CREATE_OUTPUT"; then
      printf '%s\n' "$CREATE_OUTPUT" >&2
      exit 1
    fi
    echo "  bucket already exists — continuing"
  }
fi

# The bot trades a live paper session at 10:45 ET; restarting the container
# mid-run would abandon a half-placed ladder. An intentional in-window deploy
# needs an explicit override.
log "Step 2: RTH deploy guard"
DOW="$(date -u +%u)"
HM="$(date -u +%H%M)"
if [ "$DOW" -ge 1 ] && [ "$DOW" -le 5 ] && [ "$HM" -ge 1430 ] && [ "$HM" -le 1530 ]; then
  [ "${FORCE_RTH_DEPLOY:-0}" = "1" ] || err "RTH GUARD: current UTC $(date -u +%H:%M) is inside the 14:30-15:30 run window Mon-Fri; set FORCE_RTH_DEPLOY=1 to override"
fi

log "Step 3: push secrets to options-wheel-paper"
if [ "${SKIP_SECRET_PUSH:-0}" = "1" ]; then
  log "Step 3: SKIPPED (deployed secrets persist)"
else
  for key in "${REQUIRED_SECRETS[@]}"; do
    [ -n "$(secret_value "$key")" ] || err "required secret $key is missing/empty in env and .env"
  done
  for key in "${SECRETS[@]}"; do
    val="$(secret_value "$key")"
    if [ -z "$val" ]; then
      echo "  skipping $key (not provided)"
      continue
    fi
    echo "  pushing $key ..."
    if [ "$DRY_RUN" = "1" ]; then
      echo "  [DRY-RUN] <redacted> | npx wrangler secret put $key -c $WRANGLER_CONFIG"
    else
      printf '%s' "$val" | npx wrangler secret put "$key" -c "$WRANGLER_CONFIG"
    fi
    unset val
  done
fi

log "Step 4: deploy options-wheel-paper (commit $HEAD_COMMIT)"
git checkout -- "$WRANGLER_CONFIG" 2>/dev/null || true
sed -i.gitcommit.bak "s/^GIT_COMMIT = \".*\"/GIT_COMMIT = \"$HEAD_COMMIT\"/" "$WRANGLER_CONFIG"
grep -q "^GIT_COMMIT = \"$HEAD_COMMIT\"" "$WRANGLER_CONFIG" || err "GIT_COMMIT stamp failed"
restore_config() {
  if [ -f "$WRANGLER_CONFIG.gitcommit.bak" ]; then
    git checkout -- "$WRANGLER_CONFIG" 2>/dev/null || true
    rm -f "$WRANGLER_CONFIG.gitcommit.bak"
  fi
}
trap restore_config EXIT
run npx wrangler deploy -c "$WRANGLER_CONFIG"
restore_config
trap - EXIT

if [ "$DRY_RUN" != "1" ]; then
  log "Step 5: recycle container and verify health"
  WORKER_TOKEN="$(secret_value WORKER_AUTH_TOKEN)"
  [ -n "$WORKER_TOKEN" ] || err "WORKER_AUTH_TOKEN is required for post-deploy verification"
  curl -fsS --max-time 60 -X POST -H "X-Worker-Token: ${WORKER_TOKEN}" \
    "$WORKER_URL/container-restart" >/dev/null || true
  BODY="$(mktemp)"
  OK=0
  for attempt in $(seq 1 18); do
    CODE="$(curl -sS --max-time 60 -o "$BODY" -w '%{http_code}' "$WORKER_URL/health" || true)"
    if [ "$CODE" = "200" ] && grep -q '"ok": true' "$BODY"; then
      cat "$BODY"; echo
      OK=1
      break
    fi
    echo "  health attempt $attempt: HTTP $CODE (container warming)"
    sleep 10
  done
  rm -f "$BODY"
  [ "$OK" = "1" ] || err "/health did not return ok after the container warm-up window"
fi

log "Done. options-wheel-paper deployed; state lives in r2://$BUCKET."
