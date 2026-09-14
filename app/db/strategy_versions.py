"""Bootstrap the pinned baseline strategy into `strategy_versions`.

The MVP runs a single strategy (no champion/challenger evolution yet);
this just makes sure a row exists so `Proposal.strategy_version_id` and
the journal have something real to reference.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.strategy.configs import StrategyConfig


def get_or_create_champion(engine: Engine, config: StrategyConfig) -> int:
    with engine.begin() as conn:
        existing = conn.execute(
            text("select id from strategy_versions where version_tag = :tag"),
            {"tag": config.version_tag},
        ).scalar()
        if existing is not None:
            return int(existing)
        new_id = conn.execute(
            text(
                """
                insert into strategy_versions
                    (version_tag, parent_version_id, created_by, config, config_hash, status)
                values
                    (:version_tag, null, :created_by, cast(:config as jsonb), :config_hash, 'champion')
                returning id
                """
            ),
            {
                "version_tag": config.version_tag,
                "created_by": "system",
                "config": config.model_dump_json(),
                "config_hash": config.canonical_hash(),
            },
        ).scalar_one()
        return int(new_id)
