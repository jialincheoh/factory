#!/usr/bin/env bash
# One-time Google Cloud setup: services, database, photo bucket, secrets, permissions.
# Safe to re-run; anything that already exists is left alone.
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/config.sh
g() { gcloud --project "$PROJECT" --quiet "$@"; }

g services enable run.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com storage.googleapis.com

# The website runs as its own service account with only the roles it needs.
g iam service-accounts describe "$RUN_SA" >/dev/null 2>&1 \
  || g iam service-accounts create web --display-name "factory.my website"

g artifacts repositories describe "$REPO" --location "$REGION" >/dev/null 2>&1 \
  || g artifacts repositories create "$REPO" --location "$REGION" --repository-format docker

# Database: smallest shared-core Postgres 16, daily backups (18:00 UTC = 02:00 UTC+8).
if ! g sql instances describe "$SQL_INSTANCE" >/dev/null 2>&1; then
  g sql instances create "$SQL_INSTANCE" --region "$REGION" --database-version POSTGRES_16 \
    --edition enterprise --tier db-f1-micro --storage-size 10 --storage-auto-increase \
    --backup-start-time 18:00
fi
g sql databases describe "$DB_NAME" --instance "$SQL_INSTANCE" >/dev/null 2>&1 \
  || g sql databases create "$DB_NAME" --instance "$SQL_INSTANCE"

# Secrets. Values are generated here and never printed.
secret() {  # secret NAME VALUE  (creates it only if missing)
  g secrets describe "$1" >/dev/null 2>&1 && return 0
  printf '%s' "$2" | g secrets create "$1" --data-file=- --replication-policy automatic
}
if ! g secrets describe database-url >/dev/null 2>&1; then
  pw=$(openssl rand -hex 24)
  g sql users create "$DB_USER" --instance "$SQL_INSTANCE" --password "$pw" >/dev/null 2>&1 \
    || g sql users set-password "$DB_USER" --instance "$SQL_INSTANCE" --password "$pw"
  secret database-url "postgresql://$DB_USER:$pw@/$DB_NAME?host=/cloudsql/$CONNECTION"
fi
secret session-secret "$(openssl rand -hex 32)"
if ! g secrets describe google-api-key >/dev/null 2>&1; then
  key=$(grep -E '^GOOGLE_API_KEY=' .env 2>/dev/null | cut -d= -f2- || true)
  if [ -z "$key" ]; then read -rsp "Gemini API key (from aistudio.google.com/apikey): " key; echo; fi
  secret google-api-key "$key"
fi

# Photo bucket: private, served only through the app (which checks who may see each photo).
g storage buckets describe "gs://$BUCKET" >/dev/null 2>&1 \
  || g storage buckets create "gs://$BUCKET" --location "$REGION" \
       --uniform-bucket-level-access --public-access-prevention

# Permissions for the website's service account.
g storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member "serviceAccount:$RUN_SA" --role roles/storage.objectUser >/dev/null
g projects add-iam-policy-binding "$PROJECT" \
  --member "serviceAccount:$RUN_SA" --role roles/cloudsql.client --condition None >/dev/null
for s in database-url session-secret google-api-key; do
  g secrets add-iam-policy-binding "$s" \
    --member "serviceAccount:$RUN_SA" --role roles/secretmanager.secretAccessor >/dev/null
done

echo "Setup done. Next: deploy/deploy.sh"
