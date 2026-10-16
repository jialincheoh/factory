#!/usr/bin/env bash
# Create a real estate agent login on the production database, through a local
# Cloud SQL Auth Proxy. The password is prompted for by scripts.create_agent.
#   deploy/create-agent.sh --email you@example.com --name "Your Name" --phone "+60 ..."
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/config.sh
command -v cloud-sql-proxy >/dev/null || { echo "Install it first: gcloud components install cloud-sql-proxy"; exit 1; }

port=5439
cloud-sql-proxy --port "$port" "$CONNECTION" >/dev/null 2>&1 & proxy=$!
trap 'kill $proxy' EXIT
sleep 3
url=$(gcloud --project "$PROJECT" secrets versions access latest --secret database-url)
DATABASE_URL="${url%%@*}@127.0.0.1:$port/$DB_NAME" .venv/bin/python -m scripts.create_agent "$@"
