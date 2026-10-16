#!/usr/bin/env bash
# Point factory.my (and www) at the Cloud Run service, then print the DNS records
# to add at the registrar. Google issues the HTTPS certificate once DNS resolves.
set -euo pipefail
source "$(dirname "$0")/config.sh"
g() { gcloud --project "$PROJECT" --quiet "$@"; }

# Google must see that you own the domain (opens Search Console; add the TXT record it shows).
g domains list-user-verified | grep -qx "$DOMAIN" || { g domains verify "$DOMAIN"; exit 0; }

for d in "$DOMAIN" "www.$DOMAIN"; do
  g beta run domain-mappings describe --domain "$d" --region "$REGION" >/dev/null 2>&1 \
    || g beta run domain-mappings create --service "$SERVICE" --domain "$d" --region "$REGION"
  echo "== DNS records for $d"
  g beta run domain-mappings describe --domain "$d" --region "$REGION" \
    --format 'table(status.resourceRecords[].type,status.resourceRecords[].name,status.resourceRecords[].rrdata)'
done
