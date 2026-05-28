import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.core.config import settings as core_settings
from app.redis_client import close_redis, get_redis
from app.routers import agents, allocations, attendance, auth, bookings, dashboard, email_messages, events, notifications, pending_queue, reports, shifts


async def _email_poll_loop():
    await asyncio.sleep(15)  # let uvicorn finish binding before first poll
    while True:
        try:
            from app.tasks.tasks import _poll_inbox_async
            await _poll_inbox_async()
        except Exception as e:
            print(f"[BTS] Poll error: {e}")
        await asyncio.sleep(core_settings.EMAIL_POLL_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_redis()
    poll_task = asyncio.create_task(_email_poll_loop())
    yield
    poll_task.cancel()
    await close_redis()


app = FastAPI(
    title="BTS — Bookings to Ticket System API",
    version="1.0.0",
    description="Backend API for BTS frontend. Handles bookings, agents, attendance, allocation, and reports.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(bookings.router)
app.include_router(agents.router)
app.include_router(attendance.router)
app.include_router(allocations.router)
app.include_router(pending_queue.router)
app.include_router(shifts.router)
app.include_router(reports.router)
app.include_router(dashboard.router)
app.include_router(email_messages.router)
app.include_router(notifications.router)
app.include_router(events.router)


@app.get("/health", tags=["health"])
async def health():
    return {"status": "ok", "version": "1.0.0"}
