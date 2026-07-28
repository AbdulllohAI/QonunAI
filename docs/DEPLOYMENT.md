# Deployment guide

## Prerequisites

| Component | Minimum | Recommended |
|---|---|---|
| CPU | 4 cores | 8+ cores |
| RAM | 8 GB | 16 GB (each Celery worker holds ~2.5 GB of model) |
| Disk | 20 GB | 50 GB+ (corpus, embeddings, model weights) |
| GPU | none | any CUDA card — cuts embedding time ~10× |
| Docker | 24+ with Compose v2 | |

A GPU is optional. On CPU, embedding the seed corpus (~650 chunks) takes a few
minutes; a full national corpus takes hours and should run on GPU or overnight.

---

## 1. Quick start (Docker)

```bash
cd uzlex-ai
cp .env.example .env
```

Edit `.env` — at minimum:

```bash
SECRET_KEY=<python -c "import secrets; print(secrets.token_urlsafe(48))">
ANTHROPIC_API_KEY=sk-ant-...
POSTGRES_PASSWORD=<something strong>
```

Then:

```bash
docker compose up -d --build
```

The first build downloads the embedding and reranker weights into the image
(~2.5 GB) so the first request after deploy is not a multi-minute cold start.
Skip with `--build-arg PREFETCH_MODELS=false` if you prefer a lazy first load.

Check it came up:

```bash
curl -s localhost:8000/health | python -m json.tool
```

`corpus.chunks` will be `0` — the index is empty until you load it.

---

## 2. Load the corpus

### Option A — pre-structured CSV (fastest, recommended first)

Instant, deterministic, and gives you a real index to evaluate retrieval
against before pointing anything at lex.uz.

```bash
docker compose exec backend python -m scripts.bootstrap \
  --admin admin@yourfirm.uz \
  --seed-csv-dir /app/data/seed
```

Place your CSVs in `backend/data/seed/` first (the compose file mounts
`./backend/data` into the container). The loader auto-detects two layouts:

| Layout | Columns |
|---|---|
| `jinoyat` | `Qism, Bo'lim, Bob raqami, Bob nomi, Modda raqami, Modda nomi, Modda matni` |
| `konstitutsiya` | `modda_raqami, bolim, bob_raqami, bob_nomi, matn` |

Anything else: pass an explicit `ColumnMap` (see `services/ingestion/seed_csv.py`).

### Option B — crawl lex.uz

**Read the legal note in §6 before running this against production.**

```bash
docker compose exec backend python -m scripts.bootstrap \
  --seed-lexuz --languages uz-Latn,ru --limit 20
```

Or asynchronously via the API (requires an admin token):

```bash
curl -X POST localhost:8000/api/v1/admin/ingest/async \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"connector":"lexuz","seeds":true,"languages":["uz-Latn","ru"]}'
```

Watch progress:

```bash
docker compose logs -f worker
curl -s localhost:8000/api/v1/admin/ingest/runs -H "Authorization: Bearer $ADMIN_TOKEN"
```

### Verify

```bash
curl -s localhost:8000/api/v1/laws/stats | python -m json.tool
```

Then sanity-check retrieval before trusting any answer:

```bash
curl -s -X POST localhost:8000/api/v1/search \
  -H "Content-Type: application/json" \
  -d '{"query":"shartnoma shakli","top_k":5}' | python -m json.tool
```

---

## 3. Bulk-import performance

For an import larger than ~100k chunks, drop the HNSW index first — building it
incrementally during insert is roughly 10× slower than building it once at the
end.

```sql
-- before the import
DROP INDEX IF EXISTS ix_chunk_embedding_hnsw;

-- after
SET maintenance_work_mem = '2GB';
SET max_parallel_maintenance_workers = 4;
CREATE INDEX ix_chunk_embedding_hnsw ON chunks
  USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
ANALYZE chunks;
```

Recall/latency tuning at query time:

```sql
SET hnsw.ef_search = 100;  -- default 40; higher = better recall, slower
```

---

## 4. Switching models

### LLM provider

Switch globally in `.env` (`LLM_PROVIDER=anthropic|openai|ollama`) or per
request via the `provider` field on `/chat`. No restart needed for the
per-request form.

For the local/air-gapped profile:

```bash
docker compose --profile local-llm up -d ollama
docker compose exec ollama ollama pull llama3.1:8b-instruct-q4_K_M
```

Then set `LLM_PROVIDER=ollama`. The provider raises `num_ctx` to 8192 — the
Ollama default of 2048 would silently truncate the retrieved statutes, which is
the most dangerous failure mode in this system.

### Embedding model

⚠️ **Changing `EMBEDDING_MODEL` requires a full reindex.** Vectors from
different models are not comparable, so a model change without a reindex
degrades retrieval silently rather than failing loudly.

```bash
# update EMBEDDING_MODEL and EMBEDDING_DIM in .env, then:
docker compose restart backend worker
curl -X POST localhost:8000/api/v1/admin/reindex -H "Authorization: Bearer $ADMIN_TOKEN"
```

If the new model has a different dimension, also alter the column:

```sql
ALTER TABLE chunks DROP COLUMN embedding;
ALTER TABLE chunks ADD COLUMN embedding vector(<new_dim>);
-- then recreate the HNSW index as in §3
```

---

## 5. Scheduled sync

Celery beat is configured out of the box (`app/workers/celery_app.py`):

| Job | Schedule (Asia/Tashkent) | Purpose |
|---|---|---|
| `sync-lexuz-daily` | 03:00 | Re-check seeded codes; content hashing makes unchanged acts nearly free |
| `discover-new-acts-weekly` | Sun 04:00 | Crawl for newly published acts |
| `connector-selfcheck-daily` | 06:30 | **Detects a lex.uz layout change before it empties the corpus** |
| `resolve-crossrefs-daily` | 05:00 | Resolve references whose target act arrived later |
| `prune-logs-weekly` | Mon 02:00 | Retention on query logs (365 days) |

The self-check job matters more than it looks: scraping degrades silently when
markup changes, and an empty corpus produces "no sources found" rather than an
error. Alert on `connector_selfcheck_task` returning `ok: false`.

---

## 6. Legal and operational obligations

**Before crawling lex.uz in production:**

- lex.uz is the state legal database. Confirm the terms of use, and for
  sustained crawling seek written permission from the Ministry of Justice.
  If you have an access agreement, set `LEXUZ_API_BASE` — the API path is used
  in preference to scraping.
- Keep `respect_robots=True` and the default 2 req/s. Do not raise these to go
  faster; the connectors are polite by design.
- Set `INGEST_USER_AGENT` to identify your organisation with a real contact.

**norma.uz is a commercial publisher.** Much of its content is subscription-only.
Confirm your licence covers derivative indexing before enabling that connector.
Everything from it is typed `COMMENTARY` and is never presented as binding law.

**Compliance features already built in:**

- Every interaction is written to `query_logs` (query, citations, provider,
  latency, refusal reason) — inspect at `/api/v1/admin/logs`.
- The disclaimer is returned with every answer in the user's language and
  rendered in the UI.
- Uploaded documents are stored under `UPLOAD_DIR`; set a retention policy
  appropriate to your jurisdiction and client-confidentiality obligations.

---

## 7. Production hardening

```bash
docker compose --profile prod up -d   # adds nginx
```

Checklist:

- [ ] `SECRET_KEY` from a secrets manager, not `.env` on disk
- [ ] `ENV=prod`, `DEBUG=false`
- [ ] `CORS_ORIGINS` restricted to your real domain
- [ ] TLS terminated at nginx or a load balancer (add `certbot` or an ALB)
- [ ] Postgres on managed storage with PITR; `pg_dump` alone loses the WAL
- [ ] `POSTGRES_PASSWORD` rotated off the default
- [ ] Rate limits reviewed (`RATE_LIMIT_ANON_PER_HOUR`, `..._USER_PER_HOUR`)
- [ ] Backups: `docker compose exec postgres pg_dump -U uzlex uzlex | gzip > backup.sql.gz`

**nginx must not buffer `/api/`** — the supplied config disables
`proxy_buffering` there. With buffering on, SSE answers arrive all at once when
generation completes, which defeats streaming entirely.

---

## 8. Local development (no Docker)

```bash
# infrastructure only
docker compose up -d postgres redis

# backend
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload

# worker (separate shell)
celery -A app.workers.celery_app.celery_app worker --loglevel=info

# frontend (separate shell)
cd frontend && npm install && npm run dev
```

Run the tests — they need no database or network:

```bash
cd backend && pytest tests/ -v
```

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `/health` shows `corpus.chunks: 0` | No corpus loaded | Run the bootstrap script (§2) |
| Answers always "no provisions found" | Empty index, or embeddings missing | Check `corpus.embedded_chunks`; run `/admin/reindex` |
| Keyword search returns nothing, dense works | `search_vector` not populated | Re-run ingestion; the tsvector is written in SQL after chunk insert |
| Uzbek queries miss obvious articles | Script mismatch | Confirm `script_variants` is applied — both scripts must be searched |
| Ingestion succeeds but acts are empty | lex.uz markup changed | `GET /admin/connectors/health` → check `selectors`; update `_SELECTORS` in `connectors/lexuz.py` |
| Scanned PDFs extract nothing | Tesseract language packs missing | Install `tesseract-ocr-uzb` and `-rus` (already in the Dockerfile) |
| SSE arrives all at once | Proxy buffering | Disable `proxy_buffering` for `/api/` |
| Worker OOM | Each worker loads the model | Lower `--concurrency`, or raise the memory limit |
| `budget_tokens` / `temperature` 400 from Anthropic | Removed on current models | The provider uses adaptive thinking + `effort`; don't re-add them |
