from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from src.api.forecasting import (
    ForecastOrchestrator,
    InMemoryForecastRepository,
    ScheduledForecastRunner,
)
from src.models.contracts import REPOSITORY_ROOT
from src.models.errors import DataContractError
from src.models.operational import ForecastService


TENANT = "00000000-0000-4000-8000-000000000001"


def _row(cell: str = "8861892581fffff") -> dict:
    row = json.loads(
        (REPOSITORY_ROOT / "contracts/fixtures/forecast-feature-row.json").read_text()
    )
    row.update(
        tenant_id=TENANT,
        cell_id=cell,
        interval_start="2099-08-30T00:00:00Z",
        data_as_of="2099-08-29T23:59:59Z",
        coverage_ratio=0.9,
    )
    return row


def test_scheduled_publication_is_idempotent_and_tenant_scoped() -> None:
    repository = InMemoryForecastRepository()
    orchestrator = ForecastOrchestrator(ForecastService(), repository)
    now = datetime(2099, 8, 29, 23, 55, tzinfo=timezone.utc)
    first = orchestrator.publish_future_rows(TENANT, [_row()], generated_at=now)
    second = orchestrator.publish_future_rows(TENANT, [_row()], generated_at=now)
    assert first == second
    assert repository.get(TENANT, first[0]["forecast_id"]) == first[0]
    assert repository.get("00000000-0000-4000-8000-000000000002", first[0]["forecast_id"]) is None


def test_scheduled_publication_rejects_duplicate_prediction_keys_and_labels() -> None:
    orchestrator = ForecastOrchestrator(ForecastService(), InMemoryForecastRepository())
    now = datetime(2099, 8, 29, 23, 55, tzinfo=timezone.utc)
    with pytest.raises(DataContractError, match="duplicate prediction key"):
        orchestrator.publish_future_rows(TENANT, [_row(), _row()], generated_at=now)
    labelled = _row()
    labelled["event_count"] = 1
    with pytest.raises(DataContractError):
        orchestrator.publish_future_rows(TENANT, [labelled], generated_at=now)


def test_external_scheduler_runs_each_tenant_generator_once() -> None:
    class Generator:
        def run(self, now):
            return [_row()]

    runner = ScheduledForecastRunner(
        ForecastOrchestrator(ForecastService(), InMemoryForecastRepository()),
        {TENANT: Generator()},
    )
    result = runner.run_once(datetime(2099, 8, 29, 23, 55, tzinfo=timezone.utc))
    assert result == {TENANT: 1}
