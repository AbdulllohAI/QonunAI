"""Memory management endpoints — view and clear per-user memory."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.models import SemanticMemory, User, UserProfile
from app.db.session import get_session
from app.services.memory.manager import memory_manager

router = APIRouter(prefix="/memory", tags=["memory"])


@router.get("")
async def get_memory(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Return the current user's memory profile and semantic memory count."""
    profile = (
        await session.execute(
            select(UserProfile).where(UserProfile.user_id == user.id)
        )
    ).scalar_one_or_none()

    count_row = await session.execute(
        select(func.count()).select_from(SemanticMemory).where(
            SemanticMemory.user_id == user.id
        )
    )
    memory_count = count_row.scalar() or 0

    return {
        "profile": {
            "preferred_style": profile.preferred_style if profile else "auto",
            "topics": profile.topics if profile else [],
            "query_count": profile.query_count if profile else 0,
            "last_language": profile.last_language if profile else None,
        },
        "semantic_memory_count": memory_count,
    }


@router.delete("/all", status_code=status.HTTP_200_OK)
async def clear_memory(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Delete all semantic memories and reset the profile for the current user."""
    result = await memory_manager.clear(session, user.id)
    await session.commit()
    return result
