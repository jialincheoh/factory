# Shared settings for the deploy scripts. Sourced, not run.
PROJECT=factory-my
REGION=asia-southeast1                 # Singapore
SERVICE=factory                        # Cloud Run service name
DOMAIN=factory.my

SQL_INSTANCE=factory-db
DB_NAME=listing_ads
DB_USER=listing
BUCKET="$PROJECT-uploads"              # listing photos
REPO=web                               # Artifact Registry repository for images
RUN_SA="web@$PROJECT.iam.gserviceaccount.com"

LLM_MODEL=google_genai:gemini-3.5-flash
# Shown in /privacy. Leave empty and the page shows a "Before launch" warning.
OPERATOR_NAME=""
PRIVACY_EMAIL=""

CONNECTION="$PROJECT:$REGION:$SQL_INSTANCE"
