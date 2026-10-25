"""Step runner and budget preflight shared by the animation tool.

Generalised copies of the pieces of ``tools/free_ai_film_demo.py`` (which stays
untouched): the status model, a runner in which a failed step blocks the later
ones, and the free-only budget preflight (BUDGET-01).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

PREVIOUS_STEP_FAILED = "previous step failed"


class StepStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class StepBlocked(Exception):
    """A step cannot run for a reason that is not a failure of its own work."""


@dataclass
class StepRecord:
    name: str
    status: StepStatus = StepStatus.PENDING
    started_at: str | None = None
    finished_at: str | None = None
    output: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "output": self.output,
            "detail": self.detail,
        }


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class RunLog:
    """Prints status lines and appends them to the run log file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path

    def __call__(self, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        print(line, flush=True)
        if self.path is not None:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")


class StepRunner:
    """Runs named steps in order; a failed or blocked step blocks the rest.

    A step is SUCCESS only when its function returned an existing path and
    ``validate(path)`` passed (did not raise and did not return False).
    """

    def __init__(
        self, names: Sequence[str], *, log: Callable[[str], None] = print
    ) -> None:
        self.records = {name: StepRecord(name) for name in names}
        self._log = log
        for name in names:
            self._emit(self.records[name])

    def _emit(self, record: StepRecord) -> None:
        text = f"{record.status.value:<8} {record.name}"
        if record.output:
            text += f" -> {record.output}"
        if record.detail:
            text += f" ({record.detail})"
        self._log(text)

    def _upstream_failure(self, name: str) -> str | None:
        for other in self.records.values():
            if other.name == name:
                return None
            if other.status in (StepStatus.FAILED, StepStatus.BLOCKED):
                return other.name
        return None

    def run(
        self,
        name: str,
        fn: Callable[[], Path],
        validate: Callable[[Path], object] | None = None,
        *,
        force: bool = False,
    ) -> Path | None:
        record = self.records[name]
        failed = None if force else self._upstream_failure(name)
        if failed is not None:
            record.status = StepStatus.BLOCKED
            record.detail = f"{PREVIOUS_STEP_FAILED}: {failed}"
            record.started_at = record.finished_at = now_iso()
            self._emit(record)
            return None
        record.status = StepStatus.RUNNING
        record.started_at = now_iso()
        self._emit(record)
        try:
            output = fn()
            if not isinstance(output, Path) or not output.exists():
                raise RuntimeError(f"the step produced no file ({output!r})")
            record.output = str(output)
            if validate is not None and validate(output) is False:
                raise RuntimeError("the output did not pass validation")
        except StepBlocked as exc:
            record.status = StepStatus.BLOCKED
            record.detail = str(exc)
        except Exception as exc:
            record.status = StepStatus.FAILED
            record.detail = f"{type(exc).__name__}: {exc}"
        else:
            record.status = StepStatus.SUCCESS
        record.finished_at = now_iso()
        self._emit(record)
        return output if record.status is StepStatus.SUCCESS else None

    @property
    def ok(self) -> bool:
        return all(r.status is StepStatus.SUCCESS for r in self.records.values())

    def as_list(self) -> list[dict[str, Any]]:
        return [record.as_dict() for record in self.records.values()]


# ---------------------------------------------------------------------------
# Budget check (imported lazily so a missing layer is a FAILED step, never a
# silent skip)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetLayer:
    build_container: Callable[..., Any]
    prepare_database: Callable[..., Any]
    guard_type: type
    decision_error: type[BaseException]


def import_budget_layer() -> BudgetLayer:
    # Without the budget layer these imports fail: the step fails, never skips.
    from ai_youtube_agent.bootstrap import build_container, prepare_database
    from ai_youtube_agent.content.paid_api_budget import PaidRequestDecisionError
    from ai_youtube_agent.pipeline.paid_request_guard import PaidRequestGuard

    return BudgetLayer(
        build_container, prepare_database, PaidRequestGuard, PaidRequestDecisionError
    )


def plain(value: Any) -> Any:
    """JSON-friendly copy of enums, decimals, dates and dataclasses."""
    if value is None or isinstance(value, bool | int | float | str):
        return getattr(value, "value", value)
    if hasattr(value, "value") and not callable(value.value):
        return plain(value.value)
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [plain(v) for v in value]
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: plain(getattr(value, name)) for name in value.__dataclass_fields__
        }
    return str(value)


def run_budget_check(
    settings: Any, providers: Sequence[Any], run_dir: Path, record: dict[str, Any]
) -> Path:
    """Ask the paid-API guard about the providers; only ``free`` passes.

    ``record`` receives ``mode``, ``status`` and ``declarations`` for the
    manifest.
    """
    layer = import_budget_layer()
    container = layer.build_container(settings)
    layer.prepare_database(container)
    guard = container.resolve(layer.guard_type)
    try:
        result = guard.preflight(list(providers))
    except layer.decision_error as exc:
        message = getattr(exc, "user_message", None) or str(exc)
        raise StepBlocked(f"{getattr(exc, 'code', 'budget')}: {message}") from exc
    record.update(
        mode=plain(result.mode),
        status=plain(result.status),
        declarations=plain(list(result.declarations)),
    )
    path = run_dir / "budget_check.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    if record["status"] != "free":
        raise StepBlocked(f"the budget returned '{record['status']}', not 'free'")
    return path


def validate_budget(path: Path) -> bool:
    return json.loads(path.read_text(encoding="utf-8")).get("status") == "free"
