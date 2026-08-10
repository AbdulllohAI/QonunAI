"""Cross-encoder reranking as its own service.

Run in-process, the reranker competes with query embedding for the same cores:
one shared-cpu-4x machine holding a 568M embedder and a 278M cross-encoder in a
single Python process took 40s per query, against 0.14s per pair for the same
model loaded in isolation on the same host. Separating them is what makes
reranking affordable without a GPU.

Deliberately tiny: one model, one endpoint, no database and no state. It is
reachable only over Fly's private network — no public IP is allocated — so it
carries no auth of its own.
"""
from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel, Field

MODEL_NAME = os.getenv("RERANKER_MODEL", "jinaai/jina-reranker-v2-base-multilingual")
MAX_LENGTH = int(os.getenv("RERANK_MAX_LENGTH", "320"))
BATCH_SIZE = int(os.getenv("RERANK_BATCH_SIZE", "16"))
TRUST_REMOTE_CODE = os.getenv("RERANKER_TRUST_REMOTE_CODE", "true").lower() == "true"

_state: dict = {"model": None, "error": None}


def _load():
    from sentence_transformers import CrossEncoder

    kwargs = {"device": "cpu", "max_length": MAX_LENGTH}
    if TRUST_REMOTE_CODE:
        kwargs["trust_remote_code"] = True
    return CrossEncoder(MODEL_NAME, **kwargs)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load before serving. Unlike the main app this has nothing useful to do
    # without the model, so binding early would only invite callers to send
    # work that must then wait on the same load anyway.
    try:
        _state["model"] = _load()
        print(f"reranker ready: {MODEL_NAME}", flush=True)
    except Exception as exc:  # noqa: BLE001
        _state["error"] = f"{type(exc).__name__}: {exc}"
        print(f"reranker failed to load: {_state['error']}", flush=True)
    yield


app = FastAPI(title="uzlex reranker", lifespan=lifespan)


class RerankRequest(BaseModel):
    query: str
    passages: list[str] = Field(default_factory=list)


class RerankResponse(BaseModel):
    scores: list[float]
    took_ms: int
    model: str


@app.get("/health")
async def health():
    return {
        "status": "ok" if _state["model"] is not None else "degraded",
        "model": MODEL_NAME,
        "loaded": _state["model"] is not None,
        "error": _state["error"],
    }


@app.post("/rerank", response_model=RerankResponse)
async def rerank(request: RerankRequest):
    """Score each passage against the query.

    Returns raw model scores in the order the passages arrived; ordering and
    any thresholding stay with the caller, which is the only side that knows
    what the scores are for.
    """
    started = time.perf_counter()
    model = _state["model"]
    if model is None or not request.passages:
        # An empty list is a valid answer meaning "no opinion" — the caller
        # keeps its existing order rather than treating this as an error.
        return RerankResponse(
            scores=[], took_ms=int((time.perf_counter() - started) * 1000), model=MODEL_NAME
        )

    pairs = [[request.query, passage] for passage in request.passages]
    scores = model.predict(pairs, batch_size=BATCH_SIZE, show_progress_bar=False)
    return RerankResponse(
        scores=[float(s) for s in scores],
        took_ms=int((time.perf_counter() - started) * 1000),
        model=MODEL_NAME,
    )
