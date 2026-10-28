"""Add collection_recipe.run_schedule JSON; migrate from run_interval_hours.

Revision ID: 0032_collection_recipe_run_schedule
Revises: 0031_placeholder_kind
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSON


revision = "0032_collection_recipe_run_schedule"
down_revision = "0031_placeholder_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "collection_recipe",
        sa.Column("run_schedule", JSON(), nullable=True),
    )
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, run_interval_hours FROM collection_recipe WHERE run_interval_hours IS NOT NULL")
    ).fetchall()
    for row in rows:
        hours = int(row[1] or 0)
        if hours <= 0:
            continue
        payload = json.dumps(
            {
                "enabled": True,
                "mode": "interval",
                "interval_hours": hours,
                "time": None,
                "weekday": None,
                "monthday": None,
            }
        )
        conn.execute(
            sa.text("UPDATE collection_recipe SET run_schedule = CAST(:sched AS json) WHERE id = :id"),
            {"sched": payload, "id": int(row[0])},
        )


def downgrade() -> None:
    op.drop_column("collection_recipe", "run_schedule")
