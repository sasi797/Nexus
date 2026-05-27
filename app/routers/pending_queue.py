from datetime import datetime, timezone

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.dependencies import get_current_user, get_db
from app.models.allocation import AllocationLog
from app.models.booking import Booking
from app.models.pending_queue import PendingQueue
from app.models.user import User
from app.redis_client import get_redis
from app.routers.allocations import POINTER_KEY, _get_present_agents
from app.schemas.pending_queue import AssignRequest, PendingQueueOut

router = APIRouter(prefix="/pending-queue", tags=["pending-queue"])


@router.get("", response_model=list[PendingQueueOut])
async def list_pending(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(
        select(PendingQueue)
        .options(selectinload(PendingQueue.booking))
        .order_by(PendingQueue.pending_since.asc())
    )
    return result.scalars().all()


@router.post("/assign", response_model=dict)
async def assign_from_queue(
    body: AssignRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    pq_result = await db.execute(
        select(PendingQueue).where(PendingQueue.booking_id == body.booking_id)
    )
    pq = pq_result.scalar_one_or_none()
    if pq is None:
        raise HTTPException(status_code=404, detail="Not in pending queue")

    booking_result = await db.execute(
        select(Booking).where(Booking.id == body.booking_id)
    )
    booking = booking_result.scalar_one_or_none()
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")

    booking.agent_id = body.agent_id
    booking.status = "In Progress"
    booking.assigned_at = datetime.now(timezone.utc)
    await db.delete(pq)
    await db.commit()
    return {"message": "Assigned successfully"}


@router.post("/auto-assign-all", response_model=dict)
async def auto_assign_all(
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    _: User = Depends(get_current_user),
):
    agents = await _get_present_agents(db)
    if not agents:
        raise HTTPException(status_code=409, detail="No present agents available")

    pool_size = len(agents)
    raw = await redis.get(POINTER_KEY)
    pointer = int(raw) if raw is not None else 0

    pq_result = await db.execute(
        select(PendingQueue).order_by(PendingQueue.pending_since.asc())
    )
    queue = pq_result.scalars().all()

    assigned_count = 0
    for item in queue:
        agent = agents[pointer % pool_size]
        booking_result = await db.execute(select(Booking).where(Booking.id == item.booking_id))
        booking = booking_result.scalar_one_or_none()
        if booking:
            booking.agent_id = agent.id
            booking.status = "In Progress"
            booking.assigned_at = datetime.now(timezone.utc)
            log = AllocationLog(
                booking_id=item.booking_id,
                agent_id=agent.id,
                pointer_value=pointer % pool_size,
                pool_size=pool_size,
            )
            db.add(log)
        await db.delete(item)
        pointer += 1
        assigned_count += 1

    await redis.set(POINTER_KEY, pointer % pool_size)
    await db.commit()
    return {"assigned": assigned_count}


@router.delete("/{booking_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_from_queue(
    booking_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    pq_result = await db.execute(select(PendingQueue).where(PendingQueue.booking_id == booking_id))
    pq = pq_result.scalar_one_or_none()
    if pq is None:
        raise HTTPException(status_code=404, detail="Not in pending queue")
    await db.delete(pq)
    await db.commit()
