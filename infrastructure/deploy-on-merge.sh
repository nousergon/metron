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

# ── Fail loudly, to a human (metron-ops#268) ─────────────────────────────────
# This script failed on every deploy from 2026-08-02 to 2026-08-03 and nobody knew for
# four days. It dies EARLY (Alembic is the first real step), so nothing below ever ran —
# no restart, no health check — while the PREVIOUSLY deployed services stayed up and kept
# answering :8000/health with a 200. A red GitHub Actions run was the only signal, on a
# surface nobody watches.
#
# The trap sends the alert from HERE rather than from the workflow because this is where
# credentials already work: krepis.alerts resolves the fleet bot from
# /alpha-engine/TELEGRAM_BOT_TOKEN and publishes to the alerts SNS topic, both of which
# the dashbox instance role can already reach. It is a best-effort send — an alerting
# failure must never change the deploy's own exit code, which is what the workflow reads.
#
# It is NOT the only line of defence: `metron-deploy-drift.timer` independently compares
# the box's HEAD against origin/main every hour, so a deploy that never started, an SSM
# outage, or a failure of this very trap is still caught. State, not events.
DEPLOY_STAGE="startup"
_deploy_failed() {
  local rc=$?
  trap - EXIT
  [ "$rc" -eq 0 ] && exit 0
  echo "=== deploy FAILED (rc=${rc}) during stage: ${DEPLOY_STAGE} ==="
  "$REPO"/.venv/bin/python -c '
import sys
from krepis import alerts
alerts.publish(sys.argv[1], severity="error", source="metron/deploy",
               dedup_key="metron-deploy-failed", dedup_window_min=60)
' "metron deploy FAILED at stage '${DEPLOY_STAGE}' (rc=${rc}), commit $(git -C "$REPO" rev-parse --short HEAD). Nothing was restarted — the box is still serving the PREVIOUSLY deployed code, so health checks will look green. See the deploy.yml run log." \
    || echo "  (alert publish failed — the drift timer remains the backstop)"
  exit "$rc"
}
trap _deploy_failed EXIT

DEPLOY_STAGE="pip install"
cd "$REPO"
# Python deps — idempotent; picks up metron / metron-ops / boto3 changes. Fast when satisfied.
.venv/bin/pip install -q -e . -e ../metron-ops boto3 alembic || { echo "pip install FAILED"; exit 1; }

# Run pending Alembic migrations against Postgres (idempotent — a no-op
# when at head). Reads DATABASE_URL from SSM directly because the deploy
# shell doesn't source any env file. Skips gracefully when the SSM param
# doesn't exist yet (pre-provisioning) so the deploy doesn't fail before
# the Neon project is created.
DEPLOY_STAGE="alembic migrations"
echo "=== running Alembic migrations ==="
ALEMBIC_DB_URL=$(aws ssm get-parameter --region us-east-1 --name /metron/database_url --with-decryption --query Parameter.Value --output text 2>/dev/null || true)
if [ -z "$ALEMBIC_DB_URL" ] || [ "$ALEMBIC_DB_URL" = "None" ]; then
    echo "  /metron/database_url not in SSM — skipping Alembic (pre-migration)"
else
    # Neon projects created via API may need explicit schema permissions
    # for the role before Alembic can create its version table (idempotent).
    DATABASE_URL="$ALEMBIC_DB_URL" "$REPO"/.venv/bin/python3 -c "
from sqlalchemy import create_engine, text
e = create_engine(__import__('os').environ['DATABASE_URL'])
with e.connect() as c:
    c.execute(text('GRANT ALL ON SCHEMA public TO metron_owner'))
    c.commit()
" 2>/dev/null || true
    DATABASE_URL="$ALEMBIC_DB_URL" .venv/bin/alembic upgrade head || { echo "Alembic migration FAILED"; exit 1; }
    echo "  migrations up to date"
fi


# Web deps — the only web build is the /dash variant below (the primary no-basePath
# build served portfolio.nousergon.ai, retired 2026-07-22 per Brian's ruling:
# metron.nousergon.ai/dash is the sole app entry point).
DEPLOY_STAGE="npm install"
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
#
# TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID were dropped 2026-08-03: they pointed at
# /metron/telegram_bot_token and /metron/telegram_chat_id, which have never existed, so
# the loop's `[ -n "$val" ]` guard silently skipped them on every deploy and every alert
# metron ever raised degraded to a log line (metron-ops#260). Alerting now goes through
# krepis.alerts, which resolves the FLEET bot from /alpha-engine/TELEGRAM_BOT_TOKEN —
# one bot, one rotation point, and a parameter that exists.
ENVF="$REPO/../metron-ops/.env"
DEPLOY_STAGE="SSM hydration + unit install"
echo "=== hydrating SSM secrets → metron-ops/.env ==="
touch "$ENVF"
BLOCK=$(mktemp)
MISSING_REQUIRED=""
# Each entry is VAR:SSM_PATH:CRITICALITY.
#
# CRITICALITY is not decoration — it is the fix for metron-ops#260. This loop used to be
# `[ -n "$val" ] && printf ...`, i.e. a parameter it could not read was SILENTLY skipped.
# TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID pointed at /metron/telegram_bot_token and
# /metron/telegram_chat_id, which never existed, so they were skipped on every deploy for
# months and every alert metron raised degraded to a log line. Nothing was ever red.
#
#   required — absent ⇒ FAIL the deploy. The box cannot do its job without it.
#   optional — absent ⇒ an explicit "NOT SET" line in the deploy log. Never silent.
#
# A value that is legitimately not configured yet is `optional`; it becomes `required` the
# moment something depends on it. There is no third state, and "just don't list it" is not
# one either — an unlisted variable is invisible, which is how this class hides.
for pair in \
  "DATABASE_URL:/metron/database_url:required" \
  "FLEX_TOKEN:/metron/flex_token:required" \
  "FLEX_QUERY_ID:/metron/flex_query_id:required" \
  "SNAPTRADE_CLIENT_ID:/metron/snaptrade_client_id:optional" \
  "SNAPTRADE_CONSUMER_KEY:/metron/snaptrade_consumer_key:optional" \
  "SNAPTRADE_USER_ID:/metron/snaptrade_user_id:optional" \
  "SNAPTRADE_USER_SECRET:/metron/snaptrade_user_secret:optional" \
  "OPENROUTER_API_KEY:/metron/openrouter_api_key:optional" \
  "METRON_ADVISOR_SFT_CAPTURE_ENABLED:/metron/advisor_sft_capture_enabled:optional"; do
  var=${pair%%:*}; rest=${pair#*:}; path=${rest%:*}; crit=${rest##*:}
  val=$(aws ssm get-parameter --region us-east-1 --name "$path" --with-decryption --query Parameter.Value --output text 2>/dev/null)
  if [ -n "$val" ] && [ "$val" != "None" ]; then
    printf '%s=%s\n' "$var" "$val" >> "$BLOCK"
  elif [ "$crit" = "required" ]; then
    MISSING_REQUIRED="${MISSING_REQUIRED} ${var}(${path})"
  else
    # Loud on stdout, which lands in the deploy log — the signal that did not exist before.
    echo "  NOT SET: ${var} — ${path} is absent from SSM (declared optional)"
  fi
done
HYDRATED=$(grep -cE '^[A-Z][A-Z0-9_]*=' "$BLOCK" || true)
if [ -n "$MISSING_REQUIRED" ]; then
  echo "SSM hydration FAILED — required parameter(s) absent:${MISSING_REQUIRED}"
  rm -f "$BLOCK"
  exit 1
fi
# Wrap the collected assignments in the managed markers, then replace any prior managed
# block in place and append the fresh one (idempotent).
{ echo "# >>> ssm-hydrated (managed by deploy-on-merge.sh — do not edit) >>>"
  cat "$BLOCK"
  echo "# <<< ssm-hydrated <<<"; } > "${BLOCK}.wrapped"
sed -i '/# >>> ssm-hydrated/,/# <<< ssm-hydrated/d' "$ENVF"
cat "${BLOCK}.wrapped" >> "$ENVF"
rm -f "$BLOCK" "${BLOCK}.wrapped"
echo "  hydrated ${HYDRATED} var(s) from SSM (values not logged)"

# Install tracked systemd units when the repo copy differs from the live one, so a unit
# edit deploys via the merge button alone (metron-ops DEPLOY.md declares
# infrastructure/systemd/ the source of truth — before this step the box copy drifted
# until someone hand-copied it; the 2026-07-08 flex-sync env-overlay fix is the case in
# point).
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

# Enable + start every tracked TIMER. This step used to be a manual `systemctl enable`
# after a first-time install, which is how metron-reconcile.timer came to be copied onto
# the box and never started: the custodian-reconciliation job (metron-ops#216) had ZERO
# journal entries on 2026-08-03, months after shipping, and it is the job that would have
# caught the SnapTrade 401 the same night it began (metron-ops#260). A scheduled job that
# depends on someone remembering to enable it is not scheduled.
# Idempotent: `enable --now` on an already-running timer is a no-op.
for f in "$UNITS_DIR"/*.timer; do
  [ -e "$f" ] || continue
  unit=$(basename "$f")
  sudo systemctl enable --now "$unit" || { echo "timer enable FAILED: $unit"; exit 1; }
done
# Fail the deploy if any tracked timer is not actually armed — an enable that silently
# no-ops leaves the box looking deployed while nothing runs.
for f in "$UNITS_DIR"/*.timer; do
  [ -e "$f" ] || continue
  unit=$(basename "$f")
  systemctl is-active --quiet "$unit" \
    || { echo "timer $unit installed but NOT active after enable"; exit 1; }
done
echo "  all tracked timers enabled + active"

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

DEPLOY_STAGE="metron-api restart + health check"
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
DEPLOY_STAGE="web build + dash restart"
echo "=== building /dash variant (METRON_WEB_BASE_PATH=/dash → web/.next-dash) ==="
cd "$REPO/web"
NODE_OPTIONS=--max-old-space-size=700 METRON_WEB_BASE_PATH=/dash npm run build \
  || { echo "dash web build FAILED"; exit 1; }
sudo systemctl restart metron-dash-web
wait_for_200 "http://127.0.0.1:3003/dash" "metron-dash-web" || exit 1
echo "dash deploy OK — metron-dash-web healthy"
