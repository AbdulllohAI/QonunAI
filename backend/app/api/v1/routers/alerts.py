"""Legal alerts: new and amended acts."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_optional_user
from app.db.models import ActType, AlertSubscription, User
from app.db.session import get_session
from app.schemas.common import AlertSubscriptionRequest
from app.services.alerts.service import alert_service

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("")
async def list_alerts(
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(get_optional_user),
    days: int = Query(default=30, ge=1, le=365),
    act_type: ActType | None = None,
    limit: int = Query(default=50, ge=1, le=200),
):
    if user is not None and act_type is None:
        return await alert_service.for_user(session, user.id, days=days, limit=limit)
    return await alert_service.recent(
        session, days=days, act_types=[act_type] if act_type else None, limit=limit
    )


@router.get("/subscriptions")
async def list_subscriptions(
    session: AsyncSession = Depends(get_session), user: User = Depends(get_current_user)
):
    rows = await session.execute(
        select(AlertSubscription).where(AlertSubscription.user_id == user.id)
    )
    return [
        {
            "id": str(s.id),
            "act_type": s.act_type.value if s.act_type else None,
            "keyword": s.keyword,
            "channel": s.channel,
            "created_at": s.created_at.isoformat(),
        }
        for s in rows.scalars()
    ]


@router.post("/subscriptions", status_code=status.HTTP_201_CREATED)
async def subscribe(
    payload: AlertSubscriptionRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
):
    if payload.act_type is None and not payload.keyword:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "provide at least one of act_type or keyword"
        )
    subscription = AlertSubscription(
        user_id=user.id,
        act_type=payload.act_type,
        keyword=payload.keyword,
        channel=payload.channel,
    )
    session.add(subscription)
    await session.flush()
    return {"id": str(subscription.id)}


@router.delete("/subscriptions/{subscription_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unsubscribe(
    subscription_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
):
    await session.execute(
        delete(AlertSubscription).where(
            AlertSubscription.id == subscription_id,
            AlertSubscription.user_id == user.id,
        )
    )
