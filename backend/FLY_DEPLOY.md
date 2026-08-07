# Fly.io Deployment Guide for UzLex AI Backend

## Prerequisites

1. Install `flyctl`: https://fly.io/docs/hands-on/install-flyctl/
2. Login: `flyctl auth login`

## Quick Deploy (Automated)

```bash
cd backend
chmod +x fly-deploy.sh
./fly-deploy.sh
```

This script will:
1. Create the Fly app
2. Provision PostgreSQL with pgvector
3. Provision Upstash Redis
4. Set all required secrets
5. Deploy

## Manual Deploy (Step by Step)

### 1. Create the App

```bash
flyctl apps create uzlex-ai --org personal
```

### 2. Create PostgreSQL with pgvector

```bash
# Create Postgres 16 instance
flyctl postgres create \
  --name uzlex-ai-db \
  --region fra \
  --initial-cluster-size 1 \
  --vm-size shared-cpu-1x \
  --volume-size 10 \
  --org personal

# Attach to app (sets DATABASE_URL secret)
flyctl postgres attach uzlex-ai-db --app uzlex-ai
```

**Enable pgvector extension:**

```bash
# Connect to the database
flyctl postgres connect -a uzlex-ai-db -d uzlex_ai -U uzlex_ai

# In the psql prompt, run:
CREATE EXTENSION IF NOT EXISTS vector;
\q
```

### 3. Create Redis (Upstash)

```bash
flyctl redis create \
  --name uzlex-ai-redis \
  --region fra \
  --eviction true \
  --org personal

# Attach to app (sets REDIS_URL secret)
flyctl redis attach uzlex-ai-redis --app uzlex-ai
```

### 4. Set Secrets

```bash
# Map Fly's DATABASE_URL to your app's POSTGRES_DSN (with asyncpg driver)
flyctl secrets set POSTGRES_DSN="postgresql+asyncpg://..." --app uzlex-ai

# Map Fly's REDIS_URL to your app's REDIS_DSN
flyctl secrets set REDIS_DSN="redis://..." --app uzlex-ai

# Set your LLM API keys
flyctl secrets set ANTHROPIC_API_KEY="sk-ant-..." --app uzlex-ai
flyctl secrets set OPENAI_API_KEY="sk-..." --app uzlex-ai

# Generate a secure secret key
flyctl secrets set SECRET_KEY="$(openssl rand -hex 32)" --app uzlex-ai
```

### 5. Deploy

```bash
flyctl deploy --app uzlex-ai
```

## Post-Deploy

### Verify Deployment

```bash
# Check app status
flyctl status --app uzlex-ai

# Check logs
flyctl logs --app uzlex-ai

# Test health endpoint
curl https://uzlex-ai.fly.dev/health
```

### Run Migrations Manually

Migrations run automatically on startup via `lifespan`, but if you need to run them manually:

```bash
flyctl ssh console --app uzlex-ai
# Inside the machine:
alembic upgrade head
```

### Scale Resources

```bash
# Scale to more memory (if OOM on embedding)
flyctl scale memory 4096 --app uzlex-ai

# Scale to dedicated CPU (better performance, more expensive)
flyctl scale vm performance-2x --app uzlex-ai

# Scale to 2 machines (for redundancy)
flyctl scale count 2 --app uzlex-ai
```

## Cost Optimization

### Free Tier Limits (as of 2025)
- **Shared CPU**: 2340 hours/month (enough for 3 machines running 24/7)
- **Volumes**: 3GB free
- **Postgres**: No free tier — cheapest is ~$1.94/month for shared-cpu-1x
- **Redis (Upstash)**: Free tier available (10,000 commands/day)

### Cost-Saving Tips
1. Use `auto_stop_machines = 'stop'` (already in fly.toml) — machines stop when idle
2. Set `min_machines_running = 0` — no standby machines
3. Use shared-cpu-4x (2GB) as minimum; bump only if OOM
4. For the database, shared-cpu-1x with 10GB is usually sufficient to start

## Troubleshooting

### "Out of Memory" (OOM) during embedding
- Increase memory: `flyctl scale memory 4096 --app uzlex-ai`
- Reduce `EMBEDDING_BATCH_SIZE` env var (default 16, try 8)

### Cold start is slow (30+ seconds)
- Set `PREFETCH_MODELS = 'true'` in fly.toml to download models at build time
- This makes the Docker image ~7GB larger but startup is instant
- Trade-off: slower deploys, faster cold starts

### Database connection errors
- Check `POSTGRES_DSN` is correctly set with `+asyncpg` driver
- Ensure pgvector extension is enabled: `CREATE EXTENSION vector;`

### Redis connection errors
- Check `REDIS_DSN` is correctly set
- Upstash Redis uses TLS by default: `rediss://...`

## Custom Domain

```bash
# Add your custom domain
flyctl certs create your-domain.com --app uzlex-ai

# Update DNS to point to your Fly app
# A/AAAA records or CNAME to uzlex-ai.fly.dev
```

## Monitoring

```bash
# Live logs
flyctl logs --app uzlex-ai

# Metrics dashboard
flyctl dashboard --app uzlex-ai
```
