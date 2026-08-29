"""Measured source coverage from capture and detector telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from src.data.postgres import TenantPostgres

from .errors import VideoPipelineError


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Coverage timestamp requires a timezone")
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class CoverageObservation:
    tenant_id: str
    source_id: str
    observed_at: str
    sample_seconds: int
    connected: bool
    frame_processable: bool
    detector_available: bool
    capture_failure: bool = False
    frame_gap: bool = False
    reka_available: bool = True
    processing_latency_ms: int | None = None

    def validate(self) -> None:
        _parse(self.observed_at)
        if self.sample_seconds < 1:
            raise ValueError("sample_seconds must be positive")
        if self.detector_available and not self.frame_processable:
            raise ValueError("Detector availability requires a processable frame")
        if self.frame_processable and not self.connected:
            raise ValueError("Processable frames require a connected source")
        if self.processing_latency_ms is not None and self.processing_latency_ms < 0:
            raise ValueError("processing latency cannot be negative")


class CoverageTelemetry(Protocol):
    def record(self, observation: CoverageObservation) -> None: ...
    def snapshot(self, tenant_id: str, source_id: str, interval_start: str, interval_end: str) -> dict[str, Any]: ...


class PostgresCoverageTelemetry:
    def __init__(self, database: TenantPostgres) -> None:
        self.database = database

    def record(self, observation: CoverageObservation) -> None:
        observation.validate()
        with self.database.transaction(observation.tenant_id) as cursor:
            cursor.execute(
                """INSERT INTO source_coverage_telemetry
                   (tenant_id, source_id, observed_at, sample_seconds, connected,
                    frame_processable, detector_available, capture_failure, frame_gap,
                    reka_available, processing_latency_ms)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (tenant_id, source_id, observed_at) DO UPDATE SET
                     sample_seconds=excluded.sample_seconds,
                     connected=excluded.connected,
                     frame_processable=excluded.frame_processable,
                     detector_available=excluded.detector_available,
                     capture_failure=excluded.capture_failure,
                     frame_gap=excluded.frame_gap,
                     reka_available=excluded.reka_available,
                     processing_latency_ms=excluded.processing_latency_ms""",
                (
                    observation.tenant_id,
                    observation.source_id,
                    observation.observed_at,
                    observation.sample_seconds,
                    observation.connected,
                    observation.frame_processable,
                    observation.detector_available,
                    observation.capture_failure,
                    observation.frame_gap,
                    observation.reka_available,
                    observation.processing_latency_ms,
                ),
            )

    def snapshot(
        self, tenant_id: str, source_id: str, interval_start: str, interval_end: str
    ) -> dict[str, Any]:
        start, end = _parse(interval_start), _parse(interval_end)
        expected = int((end - start).total_seconds())
        if expected <= 0:
            raise ValueError("Coverage interval must be positive")
        with self.database.transaction(tenant_id) as cursor:
            cursor.execute(
                """SELECT
                     COALESCE(sum(sample_seconds) FILTER (WHERE connected), 0) AS connected,
                     COALESCE(sum(sample_seconds) FILTER (WHERE frame_processable), 0) AS processable,
                     COALESCE(sum(sample_seconds) FILTER (WHERE detector_available), 0) AS detector,
                     COALESCE(bool_or(capture_failure), false) AS capture_failure,
                     COALESCE(bool_or(frame_gap), false) AS frame_gap,
                     COALESCE(bool_and(reka_available), true) AS reka_available
                   FROM source_coverage_telemetry
                   WHERE tenant_id=%s AND source_id=%s
                     AND observed_at >= %s AND observed_at < %s""",
                (tenant_id, source_id, interval_start, interval_end),
            )
            row = cursor.fetchone()
        connected = min(int(row["connected"]), expected)
        processable = min(int(row["processable"]), connected)
        detector = min(int(row["detector"]), processable)
        reasons: list[str] = []
        if row["capture_failure"]:
            reasons.append("capture_failure")
        if row["frame_gap"]:
            reasons.append("frame_gap")
        if not row["reka_available"]:
            reasons.append("reka_unavailable")
        if detector < expected and not reasons:
            reasons.append("detector_gap")
        return {
            "connected_seconds": connected,
            "processable_seconds": processable,
            "detector_available_seconds": detector,
            "degraded_reason_codes": reasons,
        }


class InMemoryCoverageTelemetry:
    """Deterministic test adapter with the same measurement formula."""

    def __init__(self) -> None:
        self.observations: list[CoverageObservation] = []

    def record(self, observation: CoverageObservation) -> None:
        observation.validate()
        self.observations.append(observation)

    def snapshot(
        self, tenant_id: str, source_id: str, interval_start: str, interval_end: str
    ) -> dict[str, Any]:
        start, end = _parse(interval_start), _parse(interval_end)
        expected = int((end - start).total_seconds())
        selected = [
            item
            for item in self.observations
            if item.tenant_id == tenant_id
            and item.source_id == source_id
            and start <= _parse(item.observed_at) < end
        ]
        connected = min(sum(item.sample_seconds for item in selected if item.connected), expected)
        processable = min(
            sum(item.sample_seconds for item in selected if item.frame_processable), connected
        )
        detector = min(
            sum(item.sample_seconds for item in selected if item.detector_available), processable
        )
        reasons = sorted(
            {
                reason
                for item in selected
                for reason, active in (
                    ("capture_failure", item.capture_failure),
                    ("frame_gap", item.frame_gap),
                    ("reka_unavailable", not item.reka_available),
                )
                if active
            }
        )
        if detector < expected and not reasons:
            reasons.append("detector_gap")
        return {
            "connected_seconds": connected,
            "processable_seconds": processable,
            "detector_available_seconds": detector,
            "degraded_reason_codes": reasons,
        }


def persist_measured_snapshot(
    telemetry: CoverageTelemetry,
    service: Any,
    *,
    tenant_id: str,
    source_id: str,
    interval_start: str,
    interval_end: str,
) -> dict[str, Any]:
    values = telemetry.snapshot(tenant_id, source_id, interval_start, interval_end)
    return service.record_coverage(
        tenant_id=tenant_id,
        source_id=source_id,
        interval_start=interval_start,
        interval_end=interval_end,
        **values,
    )


class StoreCoverageProvider:
    """FastAPI adapter: measured coverage when present, fail-closed zero otherwise."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def __call__(self, tenant_id: str, before: str) -> float:
        try:
            return float(self.store.latest_tenant_coverage_ratio(tenant_id, before))
        except (ValueError, VideoPipelineError):
            return 0.0
