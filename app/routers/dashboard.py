import json
from datetime import datetime, timedelta, timezone

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models.agent import Agent
from app.models.booking import Booking
from app.models.user import User
from app.redis_client import get_redis
from app.schemas.reports import DashboardStats

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _da_count_expr():
    """Sum of individual DA numbers across all matching completed bookings."""
    return func.coalesce(
        func.sum(
            func.array_length(func.string_to_array(Booking.da_number, ','), 1)
        ),
        0,
    )


@router.get("/stats", response_model=DashboardStats)
async def dashboard_stats(
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: User = Depends(get_current_user),
):
    sla_cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    if current_user.role == "agent":
        agent_result = await db.execute(select(Agent).where(Agent.user_id == current_user.id))
        agent = agent_result.scalar_one_or_none()
        if agent is None:
            return DashboardStats(total_bookings=0, pending=0, in_progress=0, completed=0, da_numbers_count=0, at_risk=0)

        aid = agent.id
        total       = await db.scalar(select(func.count(Booking.id)).where(Booking.agent_id == aid)) or 0
        pending     = await db.scalar(select(func.count(Booking.id)).where(Booking.agent_id == aid, Booking.status == "Pending")) or 0
        in_progress = await db.scalar(select(func.count(Booking.id)).where(Booking.agent_id == aid, Booking.status == "In Progress")) or 0
        completed   = await db.scalar(select(func.count(Booking.id)).where(Booking.agent_id == aid, Booking.status == "Completed")) or 0
        at_risk     = await db.scalar(
            select(func.count(Booking.id)).where(
                Booking.agent_id == aid,
                Booking.status.in_(["Pending", "In Progress"]),
                Booking.received_at < sla_cutoff,
            )
        ) or 0
        da_count    = await db.scalar(
            select(_da_count_expr()).where(
                Booking.agent_id == aid,
                Booking.status == "Completed",
                Booking.da_number.isnot(None),
                Booking.da_number != '',
            )
        ) or 0
        return DashboardStats(total_bookings=total, pending=pending, in_progress=in_progress, completed=completed, da_numbers_count=int(da_count), at_risk=at_risk)

    cached = await redis.get("bts:dashboard:stats")
    if cached:
        return DashboardStats(**json.loads(cached))

    total       = await db.scalar(select(func.count(Booking.id))) or 0
    pending     = await db.scalar(select(func.count(Booking.id)).where(Booking.status == "Pending")) or 0
    in_progress = await db.scalar(select(func.count(Booking.id)).where(Booking.status == "In Progress")) or 0
    completed   = await db.scalar(select(func.count(Booking.id)).where(Booking.status == "Completed")) or 0
    at_risk     = await db.scalar(
        select(func.count(Booking.id)).where(
            Booking.status.in_(["Pending", "In Progress"]),
            Booking.received_at < sla_cutoff,
        )
    ) or 0
    da_count    = await db.scalar(
        select(_da_count_expr()).where(
            Booking.status == "Completed",
            Booking.da_number.isnot(None),
            Booking.da_number != '',
        )
    ) or 0

    stats = DashboardStats(total_bookings=total, pending=pending, in_progress=in_progress, completed=completed, da_numbers_count=int(da_count), at_risk=at_risk)
    await redis.setex("bts:dashboard:stats", 60, json.dumps(stats.model_dump()))
    return stats
