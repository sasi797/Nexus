from datetime import datetime

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String, Table, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
import uuid

from app.database import Base


booking_support_agents_table = Table(
    "booking_support_agents",
    Base.metadata,
    Column("booking_id", String(25), ForeignKey("bookings.id", ondelete="CASCADE"), primary_key=True),
    Column("agent_id", UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True),
    Column("assigned_at", DateTime(timezone=True), server_default=func.now()),
)


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[str] = mapped_column(String(25), primary_key=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    priority: Mapped[str] = mapped_column(
        Enum("Very Urgent", "Urgent", "Not Urgent", name="priority_enum"), nullable=False, default="Urgent"
    )
    status: Mapped[str] = mapped_column(
        Enum("Pending", "In Progress", "Completed", name="booking_status_enum"), nullable=False, default="Pending"
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True)
    sender_email: Mapped[str] = mapped_column(String(150), nullable=False)
    da_number: Mapped[str | None] = mapped_column(String(100))
    da_description: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    agent: Mapped["Agent"] = relationship("Agent", back_populates="bookings")  # type: ignore[name-defined]
    support_agents: Mapped[list["Agent"]] = relationship("Agent", secondary=booking_support_agents_table, lazy="selectin")  # type: ignore[name-defined]
    allocation_logs: Mapped[list["AllocationLog"]] = relationship("AllocationLog", back_populates="booking")  # type: ignore[name-defined]
    pending_queue: Mapped["PendingQueue"] = relationship("PendingQueue", back_populates="booking", uselist=False)  # type: ignore[name-defined]
    email_messages: Mapped[list["EmailMessage"]] = relationship("EmailMessage", back_populates="booking", cascade="all, delete-orphan")  # type: ignore[name-defined]
    events: Mapped[list["BookingEvent"]] = relationship("BookingEvent", back_populates="booking", cascade="all, delete-orphan", order_by="BookingEvent.created_at")  # type: ignore[name-defined]


class BookingEvent(Base):
    __tablename__ = "booking_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    booking_id: Mapped[str] = mapped_column(String(25), ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_name: Mapped[str | None] = mapped_column(String(255))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    booking: Mapped["Booking"] = relationship("Booking", back_populates="events")
