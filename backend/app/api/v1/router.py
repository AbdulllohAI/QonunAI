from fastapi import APIRouter

from app.api.v1.routers import payments, admin, alerts, auth, chat, documents, laws, memory, search

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(chat.router)
api_router.include_router(search.router)
api_router.include_router(laws.router)
api_router.include_router(documents.router)
api_router.include_router(alerts.router)
api_router.include_router(admin.router)
api_router.include_router(memory.router)
api_router.include_router(payments.router)

__all__ = ["api_router"]
