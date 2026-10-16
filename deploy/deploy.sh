#!/usr/bin/env bash
# Build the image, apply new migrations, then deploy the website.
# Run after deploy/setup.sh, and again for every release.
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/config.sh
g() { gcloud --project "$PROJECT" --quiet "$@"; }

tag=$(git rev-parse --short HEAD)
[ -z "$(git status --porcelain)" ] || tag="$tag-dirty"
image="$REGION-docker.pkg.dev/$PROJECT/$REPO/$SERVICE:$tag"

g builds submit --region "$REGION" --tag "$image" .

# Migrations run as a one-off job against Cloud SQL before the new code goes live.
g run jobs deploy migrate --region "$REGION" --image "$image" \
  --command python --args=-m,scripts.migrate \
  --service-account "$RUN_SA" --set-cloudsql-instances "$CONNECTION" \
  --set-secrets DATABASE_URL=database-url:latest --max-retries 0
g run jobs execute migrate --region "$REGION" --wait

envfile=$(mktemp); trap 'rm -f "$envfile"' EXIT
cat > "$envfile" <<YAML
SITE_URL: "https://$DOMAIN"
SITE_NAME: "Factory"
CURRENCY: "RM"
CURRENCY_CODE: "MYR"
LLM_MODEL: "$LLM_MODEL"
SECURE_COOKIES: "1"
UPLOAD_DIR: "/uploads"
OPERATOR_NAME: "$OPERATOR_NAME"
PRIVACY_EMAIL: "$PRIVACY_EMAIL"
SEARCH_LOGGING: "1"
YAML

# Photos live in the bucket, mounted at /uploads (the container's own disk is wiped on restart).
# uid/gid match the non-root "web" user in the Dockerfile.
g run deploy "$SERVICE" --region "$REGION" --image "$image" --allow-unauthenticated \
  --service-account "$RUN_SA" --add-cloudsql-instances "$CONNECTION" \
  --set-secrets DATABASE_URL=database-url:latest,SESSION_SECRET=session-secret:latest,GOOGLE_API_KEY=google-api-key:latest \
  --env-vars-file "$envfile" \
  --execution-environment gen2 --memory 1Gi --cpu 1 --min-instances 0 --max-instances 3 \
  --add-volume "name=uploads,type=cloud-storage,bucket=$BUCKET,mount-options=uid=10001;gid=10001" \
  --add-volume-mount volume=uploads,mount-path=/uploads

g run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)'
