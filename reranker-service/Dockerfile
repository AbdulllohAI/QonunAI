# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models

WORKDIR /app

# torch >= 2.6 is a hard floor: transformers refuses to call torch.load on
# older versions (CVE-2025-32434), and several reranker checkpoints ship .bin
# rather than safetensors.
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir torch==2.6.0 --index-url ${TORCH_INDEX_URL}

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 1000 rerank \
    && mkdir -p /models \
    && chown -R rerank:rerank /app /models
USER rerank

# Bake the weights in. This image has none of the main app's OCR and parsing
# dependencies, so even with a 278M model it stays small enough to deploy —
# the main app's 4.8 GB image was not, and Fly's machine update timed out
# pulling it. Baking them here means a machine restart does not re-download,
# which matters because the /models cache is ephemeral.
ARG PREFETCH=true
ARG RERANKER_MODEL=jinaai/jina-reranker-v2-base-multilingual
RUN if [ "$PREFETCH" = "true" ]; then \
        python -c "from sentence_transformers import CrossEncoder; \
CrossEncoder('${RERANKER_MODEL}', device='cpu', max_length=320, trust_remote_code=True)" ; \
    fi

COPY --chown=rerank:rerank app.py .

EXPOSE 8080
# Bind the IPv6 wildcard, not 0.0.0.0. Fly's private network is IPv6-only, so a
# service bound to the IPv4 wildcard resolves over .internal DNS and then
# refuses every connection — which looks exactly like the app being down.
# "::" accepts IPv4 too on Linux's dual-stack sockets.
CMD ["uvicorn", "app:app", "--host", "::", "--port", "8080", "--workers", "1"]
