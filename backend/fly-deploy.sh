#!/usr/bin/env bash
set -euo pipefail

# fly-deploy.sh — One-shot deployment helper for UzLex AI on Fly.io
# Run this from the repo root (where fly.toml lives).

APP_NAME="uzlex-ai"
FLY_REGION="fra"   # change if you picked a different region in fly.toml
# ---------------------------------------------------------------------------
echo "==> Checking flyctl is installed"
if ! command -v flyctl &> /dev/null; then
    echo "flyctl not found. Install: https://fly.io/docs/hands-on/install-flyctl/"
    exit 1
fi

# ---------------------------------------------------------------------------
echo "==> Logging in to Fly"
flyctl auth login

# ---------------------------------------------------------------------------
echo "==> Creating app (if it doesn't exist yet)"
flyctl apps create "$APP_NAME" --org personal || true

# ---------------------------------------------------------------------------
echo "==> Provisioning PostgreSQL with pgvector"
# Fly Postgres 16 with 1GB initial storage (expandable later)
if ! flyctl status --app "${APP_NAME}-db" &> /dev/null; then
    flyctl postgres create \
        --name "${APP_NAME}-db" \
        --region "$FLY_REGION" \
        --initial-cluster-size 1 \
        --vm-size shared-cpu-1x \
        --volume-size 10 \
        --org personal
else
    echo "Postgres '${APP_NAME}-db' already exists, skipping creation."
fi

# Attach DB to app (creates DATABASE_URL secret automatically)
flyctl postgres attach "${APP_NAME}-db" --app "$APP_NAME" || true

# Enable pgvector extension
# Note: flyctl postgres attach creates a user & DB. We still need to run:
echo "==> Enabling pgvector extension (requires connecting to DB)"
echo "    Run this manually after first deploy if needed:"
echo "    flyctl postgres connect -a ${APP_NAME}-db -d <dbname> -U <user>"
echo "    Then: CREATE EXTENSION IF NOT EXISTS vector;"

# ---------------------------------------------------------------------------
echo "==> Provisioning Upstash Redis"
if ! flyctl status --app "${APP_NAME}-redis" &> /dev/null; then
    flyctl redis create \
        --name "${APP_NAME}-redis" \
        --region "$FLY_REGION" \
        --eviction true \
        --org personal
else
    echo "Redis '${APP_NAME}-redis' already exists, skipping creation."
fi

# Attach Redis to app (creates REDIS_URL secret automatically)
flyctl redis attach "${APP_NAME}-redis" --app "$APP_NAME" || true

# ---------------------------------------------------------------------------
echo "==> Setting required secrets"
# The app expects certain env vars; we map Fly's auto-created ones where possible,
# and prompt for the sensitive ones.

# Fly's postgres attach sets DATABASE_URL, but our app reads POSTGRES_DSN.
# We need to expose it as POSTGRES_DSN too.
echo "    Reading DATABASE_URL from app secrets..."
DB_URL=$(flyctl secrets list --app "$APP_NAME" | grep DATABASE_URL | awk '{print $3}')
if [[ -n "$DB_URL" ]]; then
    # Convert postgres:// to postgresql+asyncpg:// for our app
    ASYNC_DB_URL="${DB_URL/postgres:\/\//postgresql+asyncpg:\/\/}"
    flyctl secrets set POSTGRES_DSN="$ASYNC_DB_URL" --app "$APP_NAME"
fi

# Fly's redis attach sets REDIS_URL, but our app reads REDIS_DSN.
REDIS_URL=$(flyctl secrets list --app "$APP_NAME" | grep REDIS_URL | awk '{print $3}')
if [[ -n "$REDIS_URL" ]]; then
    flyctl secrets set REDIS_DSN="$REDIS_URL" --app "$APP_NAME"
fi

# Prompt for API keys
echo ""
echo "=== Please enter your LLM provider API keys (press Enter to skip) ==="
read -rp "Anthropic API key: " ANTHROPIC_KEY
if [[ -n "$ANTHROPIC_KEY" ]]; then
    flyctl secrets set ANTHROPIC_API_KEY="$ANTHROPIC_KEY" --app "$APP_NAME"
fi

read -rp "OpenAI API key: " OPENAI_KEY
if [[ -n "$OPENAI_KEY" ]]; then
    flyctl secrets set OPENAI_API_KEY="$OPENAI_KEY" --app "$APP_NAME"
fi

# Generate a real SECRET_KEY if not already set
if ! flyctl secrets list --app "$APP_NAME" | grep -q SECRET_KEY; then
    SECRET_KEY=$(openssl rand -hex 32)
    flyctl secrets set SECRET_KEY="$SECRET_KEY" --app "$APP_NAME"
    echo "    Generated SECRET_KEY"
fi

# ---------------------------------------------------------------------------
echo "==> Deploying app"
flyctl deploy --app "$APP_NAME"

# ---------------------------------------------------------------------------
echo ""
echo "=========================================="
echo "  Deployment complete!"
echo "  App URL: https://${APP_NAME}.fly.dev"
echo "  Health:  https://${APP_NAME}.fly.dev/health"
echo "=========================================="
