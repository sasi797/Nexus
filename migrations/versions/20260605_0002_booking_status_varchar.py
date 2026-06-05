"""Convert bookings.status from booking_status_enum to VARCHAR(50)

Revision ID: 0002_booking_status_varchar
Revises: 0001_baseline
Create Date: 2026-06-05

"""
from typing import Sequence, Union
from alembic import op

revision: str = "0002_booking_status_varchar"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE bookings ALTER COLUMN status DROP DEFAULT")
    op.execute(
        "ALTER TABLE bookings ALTER COLUMN status TYPE VARCHAR(50) USING status::text"
    )
    op.execute("ALTER TABLE bookings ALTER COLUMN status SET DEFAULT 'Pending'")
    op.execute("DROP TYPE IF EXISTS booking_status_enum")


def downgrade() -> None:
    op.execute(
        "CREATE TYPE booking_status_enum AS ENUM ('Pending', 'In Progress', 'Completed')"
    )
    op.execute("ALTER TABLE bookings ALTER COLUMN status DROP DEFAULT")
    op.execute(
        "ALTER TABLE bookings ALTER COLUMN status TYPE booking_status_enum "
        "USING status::booking_status_enum"
    )
    op.execute(
        "ALTER TABLE bookings ALTER COLUMN status "
        "SET DEFAULT 'Pending'::booking_status_enum"
    )
