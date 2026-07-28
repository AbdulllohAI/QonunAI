"""Legal alerts and the law-change timeline.

Alerts are written by the ingestion pipeline when an act's content hash changes.
This module turns them into per-user feeds (filtered by subscription) and builds
the article-level diff timeline the UI renders.
"""
from __future__ import annotations

import difflib
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ActType,
    AlertSubscription,
    Language,
    LegalAct,
    LegalActVersion,
    LegalAlert,
)


@dataclass(slots=True)
class TimelineEntry:
    version_id: uuid.UUID
    article_number: str | None
    valid_from: date | None
    valid_to: date | None
    change_note: str | None
    captured_at: datetime
    is_current: bool
    body: str | None = None

    def to_dict(self, include_body: bool = False) -> dict:
        payload = {
            "version_id": str(self.version_id),
            "article_number": self.article_number,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "change_note": self.change_note,
            "captured_at": self.captured_at.isoformat(),
            "is_current": self.is_current,
        }
        if include_body:
            payload["body"] = self.body
        return payload


class AlertService:
    async def recent(
        self,
        session: AsyncSession,
        *,
        days: int = 30,
        kinds: list[str] | None = None,
        act_types: list[ActType] | None = None,
        limit: int = 50,
    ) -> list[dict]:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = (
            select(LegalAlert, LegalAct)
            .join(LegalAct, LegalAct.id == LegalAlert.act_id)
            .where(LegalAlert.detected_at >= since)
            .order_by(LegalAlert.detected_at.desc())
            .limit(limit)
        )
        if kinds:
            stmt = stmt.where(LegalAlert.kind.in_(kinds))
        if act_types:
            stmt = stmt.where(LegalAct.act_type.in_(act_types))

        rows = (await session.execute(stmt)).all()
        return [
            {
                "id": str(alert.id),
                "kind": alert.kind,
                "summary": alert.summary,
                "detected_at": alert.detected_at.isoformat(),
                "act": {
                    "id": str(act.id),
                    "short_name": act.short_name,
                    "act_type": act.act_type.value,
                    "status": act.status.value,
                    "source_url": act.source_url,
                    "last_updated": act.last_updated.isoformat() if act.last_updated else None,
                },
                "payload": alert.payload,
            }
            for alert, act in rows
        ]

    async def for_user(
        self, session: AsyncSession, user_id: uuid.UUID, *, days: int = 30, limit: int = 50
    ) -> list[dict]:
        subs = list(
            (
                await session.execute(
                    select(AlertSubscription).where(AlertSubscription.user_id == user_id)
                )
            ).scalars()
        )
        if not subs:
            return await self.recent(session, days=days, limit=limit)

        act_types = [s.act_type for s in subs if s.act_type]
        keywords = [s.keyword for s in subs if s.keyword]

        since = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = (
            select(LegalAlert, LegalAct)
            .join(LegalAct, LegalAct.id == LegalAlert.act_id)
            .where(LegalAlert.detected_at >= since)
            .order_by(LegalAlert.detected_at.desc())
            .limit(limit)
        )
        conditions = []
        if act_types:
            conditions.append(LegalAct.act_type.in_(act_types))
        for keyword in keywords:
            needle = f"%{keyword.lower()}%"
            conditions.append(
                or_(
                    LegalAct.short_name.ilike(needle),
                    LegalAct.title_uz.ilike(needle),
                    LegalAct.title_ru.ilike(needle),
                    LegalAct.title_en.ilike(needle),
                )
            )
        if conditions:
            stmt = stmt.where(or_(*conditions))

        rows = (await session.execute(stmt)).all()
        return [
            {
                "id": str(alert.id),
                "kind": alert.kind,
                "summary": alert.summary,
                "detected_at": alert.detected_at.isoformat(),
                "act": {
                    "id": str(act.id),
                    "short_name": act.short_name,
                    "act_type": act.act_type.value,
                },
            }
            for alert, act in rows
        ]

    # ------------------------------------------------------------- timeline
    async def article_timeline(
        self,
        session: AsyncSession,
        act_id: uuid.UUID,
        article_number: str,
        *,
        language: Language = Language.UZ_LATN,
        include_body: bool = False,
    ) -> list[TimelineEntry]:
        stmt = (
            select(LegalActVersion)
            .where(
                LegalActVersion.act_id == act_id,
                LegalActVersion.article_number == article_number,
                LegalActVersion.language == language,
            )
            .order_by(LegalActVersion.captured_at.asc())
        )
        versions = list((await session.execute(stmt)).scalars())
        return [
            TimelineEntry(
                version_id=v.id,
                article_number=v.article_number,
                valid_from=v.valid_from,
                valid_to=v.valid_to,
                change_note=v.change_note,
                captured_at=v.captured_at,
                is_current=v.valid_to is None,
                body=v.body if include_body else None,
            )
            for v in versions
        ]

    async def diff_versions(
        self, session: AsyncSession, old_version_id: uuid.UUID, new_version_id: uuid.UUID
    ) -> dict:
        rows = list(
            (
                await session.execute(
                    select(LegalActVersion).where(
                        LegalActVersion.id.in_([old_version_id, new_version_id])
                    )
                )
            ).scalars()
        )
        by_id = {v.id: v for v in rows}
        old, new = by_id.get(old_version_id), by_id.get(new_version_id)
        if not old or not new:
            return {"error": "version not found"}

        old_lines = old.body.splitlines()
        new_lines = new.body.splitlines()
        diff = list(
            difflib.unified_diff(
                old_lines, new_lines, fromfile="previous", tofile="current", lineterm="", n=2
            )
        )
        matcher = difflib.SequenceMatcher(None, old.body, new.body)

        return {
            "article_number": new.article_number,
            "old": {
                "version_id": str(old.id),
                "valid_from": old.valid_from.isoformat() if old.valid_from else None,
                "valid_to": old.valid_to.isoformat() if old.valid_to else None,
            },
            "new": {
                "version_id": str(new.id),
                "valid_from": new.valid_from.isoformat() if new.valid_from else None,
            },
            "similarity": round(matcher.ratio(), 4),
            "diff": diff,
        }


alert_service = AlertService()
