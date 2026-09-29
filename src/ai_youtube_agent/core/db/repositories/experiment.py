"""Repository for the experiment registry (C13). Variants never change."""

import sqlite3
from datetime import datetime

from ai_youtube_agent.content.experiment import (
    Experiment,
    ExperimentConclusion,
    ExperimentStatus,
    ExperimentType,
    ExperimentVariant,
)
from ai_youtube_agent.core.db.repositories.base import (
    Repository,
    actor_columns,
    actor_from,
    dt,
    from_json,
    parse_dt,
    to_json,
)


class ExperimentRepository(Repository):
    table = "experiments"

    def add(self, experiment: Experiment) -> None:
        self._insert(self.table, _experiment_row(experiment))
        for position, variant in enumerate(experiment.variants):
            self._insert(
                "experiment_variants",
                {
                    "experiment_id": experiment.id,
                    "position": position,
                    "key": variant.key,
                    "value": variant.value,
                },
            )

    def get(self, experiment_id: str) -> Experiment | None:
        row = self._one("SELECT * FROM experiments WHERE id = ?", (experiment_id,))
        return self._experiment(row) if row else None

    def list_by_channel(self, channel_id: str) -> list[Experiment]:
        rows = self._all(
            "SELECT * FROM experiments WHERE channel_id = ? ORDER BY created_at, id",
            (channel_id,),
        )
        return [self._experiment(row) for row in rows]

    def update(self, experiment: Experiment, *, expected_updated_at: datetime) -> None:
        values = _experiment_row(experiment)
        for column in (
            "id",
            "channel_id",
            "content_item_id",
            "type",
            "hypothesis",
            "proposed_by_kind",
            "proposed_by_id",
            "created_at",
        ):
            del values[column]
        self._update(
            experiment.id,
            values,
            guard_column="updated_at",
            guard_value=dt(expected_updated_at),
        )

    def _experiment(self, row: sqlite3.Row) -> Experiment:
        variants = self._all(
            "SELECT key, value FROM experiment_variants WHERE experiment_id = ? "
            "ORDER BY position",
            (row["id"],),
        )
        conclusion = None
        if row["concluded_at"] is not None:
            conclusion = ExperimentConclusion(
                winner_key=row["conclusion_winner_key"],
                note=row["conclusion_note"],
                metric_snapshot_ids=tuple(
                    from_json(row["conclusion_metric_snapshot_ids_json"])
                ),
                concluded_by=actor_from(row, "concluded_by"),
                concluded_at=parse_dt(row["concluded_at"]),
            )
        return Experiment(
            id=row["id"],
            channel_id=row["channel_id"],
            content_item_id=row["content_item_id"],
            type=ExperimentType(row["type"]),
            hypothesis=row["hypothesis"],
            variants=tuple(ExperimentVariant(v["key"], v["value"]) for v in variants),
            status=ExperimentStatus(row["status"]),
            proposed_by=actor_from(row, "proposed_by"),
            created_at=parse_dt(row["created_at"]),
            updated_at=parse_dt(row["updated_at"]),
            started_by=actor_from(row, "started_by"),
            cancelled_by=actor_from(row, "cancelled_by"),
            conclusion=conclusion,
        )


def _experiment_row(experiment: Experiment) -> dict:
    conclusion = experiment.conclusion
    return {
        "id": experiment.id,
        "channel_id": experiment.channel_id,
        "content_item_id": experiment.content_item_id,
        "type": experiment.type.value,
        "hypothesis": experiment.hypothesis,
        "status": experiment.status.value,
        **actor_columns("proposed_by", experiment.proposed_by),
        **actor_columns("started_by", experiment.started_by),
        **actor_columns("cancelled_by", experiment.cancelled_by),
        "conclusion_winner_key": conclusion.winner_key if conclusion else None,
        "conclusion_note": conclusion.note if conclusion else None,
        "conclusion_metric_snapshot_ids_json": (
            to_json(list(conclusion.metric_snapshot_ids)) if conclusion else None
        ),
        **actor_columns(
            "concluded_by", conclusion.concluded_by if conclusion else None
        ),
        "concluded_at": dt(conclusion.concluded_at) if conclusion else None,
        "created_at": dt(experiment.created_at),
        "updated_at": dt(experiment.updated_at),
    }
