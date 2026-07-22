#!/bin/bash
# deploy-on-merge.sh — refresh deps, rebuild the Next.js web, restart the Metron
# services, health-check. Invoked via SSM from the metron / metron-ops deploy
# workflows AFTER the caller has already pulled both repos to their target refs.
#
# Runs as ec2-user (owns the venv + node_modules + build artifacts); uses sudo only
# for the systemctl restarts (ec2-user has passwordless sudo on the box). Output stays
# on stdout so it surfaces in the GitHub Actions deploy log. Exits non-zero on a failed
# build or health check so the deploy is marked failed (fail loud).
#
# Usage (typically via SSM, not direct):
#   bash infrastructure/deploy-on-merge.sh
set -uo pipefail

REPO=/home/ec2-user/metron
echo "=== metron deploy $(date -u +%FT%TZ) — metron@$(git -C "$REPO" rev-parse --short HEAD) metron-ops@$(git -C "$REPO/../metron-ops" rev-parse --short HEAD) ==="

cd "$REPO"
# Python deps — idempotent; picks up metron / metron-ops / boto3 changes. Fast when satisfied.
.venv/bin/pip install -q -e . -e ../metron-ops boto3 || { echo "pip install FAILED"; exit 1; }

# Web deps — the only web build is the /dash variant below (the primary no-basePath
# build served portfolio.nousergon.ai, retired 2026-07-22 per Brian's ruling:
# metron.nousergon.ai/dash is the sole app entry point).
cd "$REPO/web"
npm install --no-audit --no-fund --silent || { echo "npm install FAILED"; exit 1; }

# Hydrate secrets + durable config flags from SSM Parameter Store into the
# metron-ops/.env EnvironmentFile so SSM is the durable source of truth — the .env is a
# generated cache, refreshed every deploy, so a rebuilt/replaced box self-heals instead
# of needing a hand-pasted token/flag (metron-ops#82). Only the marked block is
# rewritten; hand-set lines are preserved. METRON_ADVISOR_SFT_CAPTURE_ENABLED rides this
# loop (non-secret, but capture must survive a box rebuild or it silently stops accruing
# the distillation corpus). OPENROUTER_API_KEY likewise rides it so the advisor's
# open-weight provider (config#1658) self-heals on a box rebuild instead of needing a
# hand-pasted key; ANTHROPIC_API_KEY stays hand-set (no /metron/anthropic_api_key param).
# Values are written straight to the file and NEVER echoed (they'd leak into the GHA log).
ENVF="$REPO/../metron-ops/.env"
echo "=== hydrating SSM secrets → metron-ops/.env ==="
touch "$ENVF"
BLOCK=$(mktemp)
{
  echo "# >>> ssm-hydrated (managed by deploy-on-merge.sh — do not edit) >>>"
  for pair in \
    "FLEX_TOKEN:/metron/flex_token" \
    "FLEX_QUERY_ID:/metron/flex_query_id" \
    "OPENROUTER_API_KEY:/metron/openrouter_api_key" \
    "METRON_ADVISOR_SFT_CAPTURE_ENABLED:/metron/advisor_sft_capture_enabled" \
    "TELEGRAM_BOT_TOKEN:/metron/telegram_bot_token" \
    "TELEGRAM_CHAT_ID:/metron/telegram_chat_id"; do
    var=${pair%%:*}; path=${pair#*:}
    val=$(aws ssm get-parameter --region us-east-1 --name "$path" --with-decryption --query Parameter.Value --output text 2>/dev/null)
    [ -n "$val" ] && [ "$val" != "None" ] && printf '%s=%s\n' "$var" "$val"
  done
  echo "# <<< ssm-hydrated <<<"
} >> "$BLOCK"
HYDRATED=$(grep -cE '^[A-Z][A-Z0-9_]*=' "$BLOCK" || true)
# Replace any prior managed block in place, then append the fresh one (idempotent).
sed -i '/# >>> ssm-hydrated/,/# <<< ssm-hydrated/d' "$ENVF"
cat "$BLOCK" >> "$ENVF"
rm -f "$BLOCK"
echo "  hydrated ${HYDRATED} var(s) from SSM (values not logged)"

# Install tracked systemd units when the repo copy differs from the live one, so a unit
# edit deploys via the merge button alone (metron-ops DEPLOY.md declares
# infrastructure/systemd/ the source of truth — before this step the box copy drifted
# until someone hand-copied it; the 2026-07-08 flex-sync env-overlay fix is the case in
# point). First-time unit INSTALLS still need a manual `systemctl enable` (see DEPLOY.md);
# this handles updates to already-enabled units.
UNITS_DIR="$REPO/../metron-ops/infrastructure/systemd"
UNITS_CHANGED=0
for f in "$UNITS_DIR"/*.service "$UNITS_DIR"/*.timer; do
  [ -e "$f" ] || continue
  dest="/etc/systemd/system/$(basename "$f")"
  if ! cmp -s "$f" "$dest"; then
    sudo cp "$f" "$dest" || { echo "unit install FAILED: $(basename "$f")"; exit 1; }
    UNITS_CHANGED=1
    echo "  installed unit $(basename "$f")"
  fi
done
if [ "$UNITS_CHANGED" = 1 ]; then
  sudo systemctl daemon-reload
fi

# One-shot retirement of metron-web.service (:3000, portfolio.nousergon.ai —
# deprecated 2026-07-22, metron-ops#225). Idempotent — a no-op once the unit is
# gone. Companion metron-ops PR removes the tracked unit from
# infrastructure/systemd/ so the install loop above can't re-copy it; until that
# merges, a deploy may re-copy then immediately re-remove the file (harmless —
# the service is never in the restart list, so it can't run).
if [ -e /etc/systemd/system/metron-web.service ]; then
  sudo systemctl disable --now metron-web.service || true
  sudo rm /etc/systemd/system/metron-web.service
  sudo systemctl daemon-reload
  echo "retired metron-web.service (portfolio.nousergon.ai deprecation)"
fi

sudo systemctl restart metron-api

# Health checks — poll with a bounded retry instead of a fixed sleep. A fixed sleep races
# cold-start time (credential-provider lookups, first-import cost, Next.js server boot)
# that varies run to run; a fixed `sleep 6` here false-failed an otherwise-good deploy on
# 2026-07-06 when the API took ~6-7s to bind, exiting red even though the service came up
# correctly moments later. Poll up to 30s (1s interval) per service and only fail loud if
# it never comes up in that window.
wait_for_200() {
  local url=$1 label=$2 tries=30 code
  for ((i = 1; i <= tries; i++)); do
    code=$(curl -s -o /dev/null -w '%{http_code}' "$url")
    case "$code" in
      200 | 307) echo "${label} healthy (HTTP $code, ${i}s)"; return 0 ;;
    esac
    sleep 1
  done
  echo "${label} health FAILED (last HTTP $code after ${tries}s)"
  return 1
}

wait_for_200 "http://127.0.0.1:8000/health" "metron-api" || exit 1
echo "api deploy OK — metron-api healthy"

# ── /dash web process (metron-ops#180; sole web surface since 2026-07-22) ────
# metron.nousergon.ai/dash is served by metron-dash-web.service (:3003), built
# from this checkout with METRON_WEB_BASE_PATH=/dash. Next.js bakes basePath in
# at build time, so the variant builds into its own distDir (web/.next-dash —
# see web/next.config.mjs); the unit starts `next start` with the same env var
# so it serves the matching output.
#
# MANDATORY (fail loud): with the portfolio.nousergon.ai primary retired, this
# is the only web process — a box missing the unit is a broken box, not a
# pending bootstrap.
systemctl is-enabled --quiet metron-dash-web.service 2>/dev/null \
  || { echo "metron-dash-web.service not enabled — sole web surface missing (see metron-ops#180 bootstrap)"; exit 1; }
echo "=== building /dash variant (METRON_WEB_BASE_PATH=/dash → web/.next-dash) ==="
cd "$REPO/web"
NODE_OPTIONS=--max-old-space-size=700 METRON_WEB_BASE_PATH=/dash npm run build \
  || { echo "dash web build FAILED"; exit 1; }
sudo systemctl restart metron-dash-web
wait_for_200 "http://127.0.0.1:3003/dash" "metron-dash-web" || exit 1
echo "dash deploy OK — metron-dash-web healthy"
