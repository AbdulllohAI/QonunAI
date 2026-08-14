"""Payme merchant endpoint.

Payme posts JSON-RPC here. Two things about the shape are deliberate and look
wrong at a glance:

**Always HTTP 200.** Errors go in the JSON-RPC `error` object, not the status
line. Payme treats a non-200 as a transport failure and retries hard, so
returning 401 for a bad key would turn one rejected call into a retry storm.

**No rate limiter.** This endpoint is authenticated by the merchant key and
called by Payme on their schedule, including bursts during reconciliation.
Throttling it would drop real payment callbacks.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import get_session
from app.services.billing import payme

log = get_logger(__name__)
router = APIRouter(prefix="/payments", tags=["payments"])


@router.post("/payme")
async def payme_endpoint(
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    if not settings.PAYME_ENABLED:
        # Refuse plainly rather than half-answering. Without a merchant account
        # there is no key to check against, and a partially working payment
        # endpoint is worse than one that is clearly off.
        return JSONResponse(
            payme.PaymeError(
                payme.ERR_ACCESS_DENIED, "Payment provider is not enabled"
            ).to_response(None)
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            payme.PaymeError(payme.ERR_TRANSPORT, "Malformed JSON").to_response(None)
        )

    if not isinstance(body, dict):
        return JSONResponse(
            payme.PaymeError(payme.ERR_TRANSPORT, "Expected a JSON object").to_response(None)
        )

    try:
        payme.check_auth(authorization)
    except payme.PaymeError as exc:
        log.warning("payme_auth_rejected", extra={"method": body.get("method")})
        return JSONResponse(exc.to_response(body.get("id")))

    return JSONResponse(await payme.dispatch(session, body))
