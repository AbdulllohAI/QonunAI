"""Document upload and analysis."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_optional_user, rate_limit
from app.core.logging import get_logger
from app.db.models import Language, UploadedDocument, User
from app.db.session import get_session
from app.schemas.common import DocumentOut
from app.services.documents.analyzer import document_analyzer, save_upload
from app.services.lang.detect import detect_language

log = get_logger(__name__)
router = APIRouter(prefix="/documents", tags=["documents"])

_ALLOWED_MIME = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "text/html",
    "text/plain",
}
_ALLOWED_EXT = (".pdf", ".docx", ".doc", ".html", ".htm", ".txt")


@router.post("/analyze", dependencies=[Depends(rate_limit)])
async def analyze_document(
    file: UploadFile = File(...),
    question: str | None = Form(default=None),
    language: Language | None = Form(default=None),
    provider: str | None = Form(default=None),
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(get_optional_user),
):
    """Upload a contract or legal document and analyse it against Uzbek law."""
    filename = file.filename or "document"
    if not filename.lower().endswith(_ALLOWED_EXT) and file.content_type not in _ALLOWED_MIME:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"unsupported file type; accepted: {', '.join(_ALLOWED_EXT)}",
        )

    data = await file.read()
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    if len(data) > max_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"file exceeds {settings.MAX_UPLOAD_MB} MB",
        )
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty file")

    path = save_upload(data, filename)
    record = UploadedDocument(
        user_id=user.id if user else None,
        filename=filename,
        mime_type=file.content_type or "application/octet-stream",
        size_bytes=len(data),
        storage_path=str(path),
        status="processing",
    )
    session.add(record)
    await session.flush()

    try:
        if filename.lower().endswith(".txt"):
            text = data.decode("utf-8", errors="replace")
        else:
            text = document_analyzer.extract_text(
                data, filename=filename, mime_type=file.content_type
            )
    except Exception as exc:
        record.status = "failed"
        record.error = f"extraction failed: {exc}"
        log.exception("text extraction failed", extra={"filename": filename})
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"could not extract text: {exc}"
        ) from exc

    if len(text.strip()) < 100:
        record.status = "failed"
        record.error = "no extractable text"
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "no extractable text found. If this is a scanned PDF, ensure Tesseract with "
            "the uzb/rus language packs is installed on the server.",
        )

    detected = language or detect_language(text)
    record.language = detected
    record.extracted_text = text[:1_000_000]

    try:
        analysis = await document_analyzer.analyze(
            session, text=text, question=question, language=detected, provider=provider
        )
    except Exception as exc:
        record.status = "failed"
        record.error = str(exc)
        log.exception("document analysis failed")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"analysis failed: {exc}") from exc

    payload = analysis.to_dict()
    record.analysis = payload
    record.status = "completed"
    await session.flush()

    payload["document_id"] = str(record.id)
    payload["filename"] = filename
    return payload


@router.get("", response_model=list[DocumentOut])
async def list_documents(
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(get_optional_user),
    limit: int = 50,
):
    if user is None:
        return []
    rows = await session.execute(
        select(UploadedDocument)
        .where(UploadedDocument.user_id == user.id)
        .order_by(UploadedDocument.created_at.desc())
        .limit(limit)
    )
    return list(rows.scalars())


@router.get("/{document_id}")
async def get_document(
    document_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(get_optional_user),
):
    record = (
        await session.execute(
            select(UploadedDocument).where(UploadedDocument.id == document_id)
        )
    ).scalar_one_or_none()
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    if record.user_id and (user is None or record.user_id != user.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your document")

    return {
        "id": str(record.id),
        "filename": record.filename,
        "status": record.status,
        "language": record.language.value if record.language else None,
        "size_bytes": record.size_bytes,
        "created_at": record.created_at.isoformat(),
        "analysis": record.analysis,
        "error": record.error,
    }
