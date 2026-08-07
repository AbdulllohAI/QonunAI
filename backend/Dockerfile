# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models \
    TRANSFORMERS_CACHE=/models

# tesseract-ocr-uzb / -rus are required for scanned Uzbek and Russian PDFs;
# without them, scanned acts extract as empty text.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        libpq-dev \
        poppler-utils \
        procps \
        tesseract-ocr \
        tesseract-ocr-uzb \
        tesseract-ocr-rus \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# CPU-only torch keeps the image ~2 GB smaller and needs no host GPU/driver —
# the safe default. Override TORCH_INDEX_URL to a CUDA wheel index (e.g.
# https://download.pytorch.org/whl/cu124) for a GPU deployment; the compose
# file wires this to the TORCH_INDEX_URL env var.
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir torch==2.5.1 --index-url ${TORCH_INDEX_URL} \
    && pip install --no-cache-dir -r requirements.txt

# Create the user and hand over ownership *before* downloading the model
# weights. Layers are immutable — chown -R after the download would force
# OverlayFS to copy every byte of the multi-GB model cache into a new layer
# (roughly doubling image size); owning the directory upfront avoids that.
RUN useradd --create-home --uid 1000 uzlex \
    && mkdir -p /app/data /models \
    && chown -R uzlex:uzlex /app /models
USER uzlex

# Pre-download the embedding + reranker weights into the image so the first
# request after a deploy is not a multi-minute cold start. Deliberately placed
# *before* COPY . . — this step only depends on the installed packages, not
# the app source, so routine app-code changes reuse this layer from cache
# instead of re-downloading ~7GB of models on every rebuild.
ARG PREFETCH_MODELS=true
ARG EMBEDDING_MODEL=BAAI/bge-m3
RUN if [ "$PREFETCH_MODELS" = "true" ]; then \
        python -c "from sentence_transformers import SentenceTransformer; \
SentenceTransformer('${EMBEDDING_MODEL}', device='cpu')" ; \
    else \
        echo "PREFETCH_MODELS=$PREFETCH_MODELS - skipping weight download; the first query pays the load" ; \
    fi

COPY --chown=uzlex:uzlex . .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# Default 2 workers parallelizes CPU-bound embedding/reranking across cores.
# On a single GPU, multiple worker processes would each load their own copy
# of both models onto the same limited VRAM instead of actually running in
# parallel (CUDA serializes anyway) — set UVICORN_WORKERS=1 for a GPU deploy.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS:-2}"]
