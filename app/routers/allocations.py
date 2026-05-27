from datetime import datetime, timezone

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.dependencies import get_current_user, get_db
from app.models.agent import Agent
from app.models.allocation import AllocationLog
from app.models.attendance import Attendance
from app.models.booking import Booking
from app.models.pending_queue import PendingQueue
from app.models.user import User
from app.redis_client import get_redis
from app.schemas.allocation import AllocationLogOut, AllocationStatus, RunAllocationRequest

router = APIRouter(prefix="/allocations", tags=["allocations"])

POINTER_KEY = "bts:allocation:pointer"


async def _get_present_agents(db: AsyncSession) -> list[Agent]:
    from datetime import date
    today = date.today()
    result = await db.execute(
        select(Agent)
        .join(Attendance, (Attendance.agent_id == Agent.id) & (Attendance.date == today), isouter=True)
        .where(Attendance.status == "Present")
        .options(selectinload(Agent.shift))
        .order_by(Agent.name)
    )
    return result.scalars().all()


@router.get("/status", response_model=AllocationStatus)
async def allocation_status(
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    _: User = Depends(get_current_user),
):
    agents = await _get_present_agents(db)
    pool_size = len(agents)
    raw = await redis.get(POINTER_KEY)
    pointer = int(raw) if raw is not None else 0
    next_agent = agents[pointer % pool_size] if pool_size else None
    return AllocationStatus(
        pointer=pointer,
        pool_size=pool_size,
        next_agent_id=next_agent.id if next_agent else None,
        next_agent_name=next_agent.name if next_agent else None,
    )


@router.post("/run", response_model=AllocationLogOut)
async def run_allocation(
    body: RunAllocationRequest,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    _: User = Depends(get_current_user),
):
    agents = await _get_present_agents(db)
    if not agents:
        raise HTTPException(status_code=409, detail="No present agents available for allocation")

    pool_size = len(agents)
    # Atomically get-then-increment pointer in Redis
    pointer = await redis.incr(POINTER_KEY)
    pointer -= 1  # incr returns new value; we want the old one (pre-increment)
    # Keep within pool bounds
    await redis.set(POINTER_KEY, (pointer + 1) % pool_size)
    assigned_agent = agents[pointer % pool_size]

    # Verify booking exists
    booking_result = await db.execute(
        select(Booking).options(selectinload(Booking.agent)).where(Booking.id == body.booking_id)
    )
    booking = booking_result.scalar_one_or_none()
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")

    # Update booking
    booking.agent_id = assigned_agent.id
    booking.status = "In Progress"
    booking.assigned_at = datetime.now(timezone.utc)

    # Remove from pending queue
    pq_result = await db.execute(
        select(PendingQueue).where(PendingQueue.booking_id == body.booking_id)
    )
    pq = pq_result.scalar_one_or_none()
    if pq:
        await db.delete(pq)

    # Create allocation log entry
    log = AllocationLog(
        booking_id=body.booking_id,
        agent_id=assigned_agent.id,
        pointer_value=pointer % pool_size,
        pool_size=pool_size,
        allocated_at=datetime.now(timezone.utc),
    )
    db.add(log)
    await db.commit()
    await db.refresh(log)

    result = await db.execute(
        select(AllocationLog).options(selectinload(AllocationLog.agent)).where(AllocationLog.id == log.id)
    )
    return result.scalar_one()


@router.get("/log", response_model=list[AllocationLogOut])
async def allocation_log(
    booking_id: str | None = None,
    skip: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    q = (
        select(AllocationLog)
        .options(selectinload(AllocationLog.agent))
        .order_by(AllocationLog.allocated_at.asc())
        .offset(skip)
        .limit(limit)
    )
    if booking_id:
        q = q.where(AllocationLog.booking_id == booking_id)
    result = await db.execute(q)
    return result.scalars().all()


@router.post("/run-all-pending", response_model=dict)
async def run_all_pending(
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    _: User = Depends(get_current_user),
):
    agents = await _get_present_agents(db)
    if not agents:
        raise HTTPException(status_code=409, detail="No present agents available for allocation")

    pool_size = len(agents)
    result = await db.execute(
        select(Booking).where(Booking.status == "Pending").order_by(Booking.received_at.asc())
    )
    pending_bookings = result.scalars().all()

    if not pending_bookings:
        return {"allocated": 0, "message": "No pending bookings found"}

    raw = await redis.get(POINTER_KEY)
    pointer = int(raw) if raw is not None else 0

    allocated_count = 0
    for booking in pending_bookings:
        assigned_agent = agents[pointer % pool_size]
        booking.agent_id = assigned_agent.id
        booking.status = "In Progress"
        booking.assigned_at = datetime.now(timezone.utc)

        db.add(AllocationLog(
            booking_id=booking.id,
            agent_id=assigned_agent.id,
            pointer_value=pointer % pool_size,
            pool_size=pool_size,
            allocated_at=datetime.now(timezone.utc),
        ))

        pointer += 1
        allocated_count += 1

    await redis.set(POINTER_KEY, pointer % pool_size)
    await db.commit()
    return {"allocated": allocated_count, "message": f"{allocated_count} bookings assigned to agents"}


@router.post("/reset-pointer", status_code=204)
async def reset_pointer(
    redis: aioredis.Redis = Depends(get_redis),
    _: User = Depends(get_current_user),
):
    await redis.set(POINTER_KEY, 0)
