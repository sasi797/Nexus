from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.dependencies import get_current_user, get_db
from app.models.agent import Agent
from app.models.user import User
from app.schemas.agent import AgentCreate, AgentOut, AgentUpdate
from app.utils.jwt import hash_password

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("", response_model=list[AgentOut])
async def list_agents(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Agent)
        .options(selectinload(Agent.shift), selectinload(Agent.user))
        .where(Agent.is_active == True)
        .order_by(Agent.name)
    )
    return result.scalars().all()


@router.post("", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
async def create_agent(
    body: AgentCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    existing_agent = await db.execute(select(Agent).where(Agent.email == body.email))
    if existing_agent.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Agent email already exists")

    existing_user = await db.execute(select(User).where(User.email == body.email))
    if existing_user.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="A user account with this email already exists")

    user = User(
        name=body.name,
        email=body.email,
        password_hash=hash_password(body.password),
        role=body.role,
    )
    db.add(user)
    await db.flush()

    agent = Agent(name=body.name, email=body.email, shift_id=body.shift_id, user_id=user.id)
    db.add(agent)
    await db.commit()
    result = await db.execute(
        select(Agent).options(selectinload(Agent.shift), selectinload(Agent.user)).where(Agent.id == agent.id)
    )
    return result.scalar_one()


@router.get("/{agent_id}", response_model=AgentOut)
async def get_agent(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    from uuid import UUID
    result = await db.execute(
        select(Agent).options(selectinload(Agent.shift), selectinload(Agent.user)).where(Agent.id == UUID(agent_id))
    )
    agent = result.scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.put("/{agent_id}", response_model=AgentOut)
async def update_agent(
    agent_id: str,
    body: AgentUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    from uuid import UUID
    result = await db.execute(
        select(Agent).options(selectinload(Agent.shift), selectinload(Agent.user)).where(Agent.id == UUID(agent_id))
    )
    agent = result.scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    update_data = body.model_dump(exclude_unset=True)
    role = update_data.pop("role", None)
    for field, value in update_data.items():
        setattr(agent, field, value)
    if role and agent.user:
        agent.user.role = role

    await db.commit()
    result = await db.execute(
        select(Agent).options(selectinload(Agent.shift), selectinload(Agent.user)).where(Agent.id == UUID(agent_id))
    )
    return result.scalar_one()


@router.get("/{agent_id}/bookings")
async def agent_bookings(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    from uuid import UUID
    from sqlalchemy import func
    from app.models.booking import Booking

    agent_uuid = UUID(agent_id)
    result = await db.execute(
        select(Booking).where(Booking.agent_id == agent_uuid).order_by(Booking.received_at.desc())
    )
    bookings = result.scalars().all()

    counts = {"Pending": 0, "In Progress": 0, "Completed": 0}
    for b in bookings:
        counts[b.status] = counts.get(b.status, 0) + 1

    return {
        "agent_id": agent_id,
        "total": len(bookings),
        "pending": counts["Pending"],
        "in_progress": counts["In Progress"],
        "completed": counts["Completed"],
        "bookings": [
            {
                "id": b.id,
                "subject": b.subject,
                "priority": b.priority,
                "status": b.status,
                "received_at": b.received_at,
                "assigned_at": b.assigned_at,
                "completed_at": b.completed_at,
            }
            for b in bookings
        ],
    }


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_agent(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    from uuid import UUID
    agent = await db.get(Agent, UUID(agent_id))
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent.is_active = False
    if agent.user_id:
        user = await db.get(User, agent.user_id)
        if user:
            user.is_active = False
    await db.commit()
