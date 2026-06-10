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

# Web build with a capped Node heap so a build spike can't OOM the co-resident services.
cd "$REPO/web"
npm install --no-audit --no-fund --silent || { echo "npm install FAILED"; exit 1; }
NODE_OPTIONS=--max-old-space-size=700 npm run build || { echo "web build FAILED"; exit 1; }

sudo systemctl restart metron-api metron-web
sleep 6

# Health checks — non-zero exit (red deploy) if either service didn't come back.
curl -fsS http://127.0.0.1:8000/health >/dev/null || { echo "metron-api health FAILED"; exit 1; }
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3000/)
case "$code" in
  200 | 307) echo "deploy OK — metron-api healthy, metron-web HTTP $code" ;;
  *) echo "metron-web check FAILED (HTTP $code)"; exit 1 ;;
esac
