"""Recorded-video intake, Reka orchestration, review, and retention."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from src.data.contracts import validate_contract
from src.data.service import _payload_hash
from src.data.store import utc_now

from .errors import VideoPipelineError
from .reka import VisionProvider
from .store import VideoStore


ALLOWED_CATEGORIES = {"property", "violence", "public_order", "traffic_safety", "other", "unmapped"}
REVIEW_ROLES = {"reviewer", "tenant_admin", "platform_operator"}
NAMESPACE = uuid.UUID("e3978285-344f-40c8-b807-d44464a23ed3")


def _parse_utc(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise VideoPipelineError("timestamp_invalid", f"{name} must be a valid ISO 8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise VideoPipelineError("timestamp_timezone_missing", f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class LocationResolver(Protocol):
    def resolve(self, tenant_id: str, location_ref: str) -> dict[str, float]: ...


class MediaInspector(Protocol):
    def duration_seconds(self, path: Path) -> float: ...


class FfprobeMediaInspector:
    """Probe media server-side; no client-provided duration is trusted."""

    def duration_seconds(self, path: Path) -> float:
        try:
            completed = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-show_entries", "format=duration,format_name",
                    "-of", "json", str(path),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            payload = json.loads(completed.stdout)
            format_data = payload["format"]
            if "mp4" not in str(format_data["format_name"]).split(","):
                raise ValueError("not mp4")
            return float(format_data["duration"])
        except (OSError, subprocess.SubprocessError, KeyError, ValueError, json.JSONDecodeError) as error:
            raise VideoPipelineError("video_probe_failed", "Server could not validate MP4 duration") from error


@dataclass
class DictLocationResolver:
    locations: dict[tuple[str, str], dict[str, float]]

    def resolve(self, tenant_id: str, location_ref: str) -> dict[str, float]:
        value = self.locations.get((tenant_id, location_ref))
        if value is None:
            raise VideoPipelineError("location_unavailable", "Source location could not be resolved")
        return dict(value)


class VideoPipelineService:
    def __init__(
        self,
        store: VideoStore,
        provider: VisionProvider,
        location_resolver: LocationResolver,
        *,
        media_root: Path,
        max_upload_bytes: int = 500 * 1024 * 1024,
        tenant_quota_bytes: int = 2 * 1024 * 1024 * 1024,
        max_duration_seconds: int = 4 * 60 * 60,
        prompt_version: str = "candidate-v1",
        review_ttl: timedelta = timedelta(days=7),
        media_inspector: MediaInspector | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.location_resolver = location_resolver
        self.media_root = media_root.resolve()
        self.max_upload_bytes = max_upload_bytes
        self.tenant_quota_bytes = tenant_quota_bytes
        self.max_duration_seconds = max_duration_seconds
        self.prompt_version = prompt_version
        self.review_ttl = review_ttl
        self.media_inspector = media_inspector or FfprobeMediaInspector()

    def register_recorded_source(self, payload: dict[str, Any], *, authenticated_tenant_id: str) -> dict[str, Any]:
        if payload.get("tenant_id") != authenticated_tenant_id:
            raise VideoPipelineError("tenant_mismatch", "Source does not belong to authenticated tenant")
        validate_contract("camera-source.schema.json", payload)
        if payload["mode"] != "recorded_video" or payload["connection"]["transport"] != "uploaded_asset":
            raise VideoPipelineError("source_mode_invalid", "This worker accepts recorded-video sources only")
        self.store.put_source(payload)
        self.store.ingestion_store.register_camera_source(payload)
        self.store.audit(authenticated_tenant_id, "source.register", "source", payload["source_id"], "success")
        return dict(payload)

    def accept_upload(
        self,
        *,
        authenticated_tenant_id: str,
        source_id: str,
        path: Path,
        content_type: str,
        captured_start: str,
        captured_end: str,
        duration_seconds: float | None,
        consent_confirmed: bool,
        expected_sha256: str | None = None,
        received_at: str | None = None,
    ) -> dict[str, Any]:
        source = self.store.get_source(authenticated_tenant_id, source_id)
        if not consent_confirmed:
            raise VideoPipelineError("consent_required", "Lawful-use and consent confirmation is required")
        if content_type != "video/mp4" or path.suffix.lower() != ".mp4":
            raise VideoPipelineError("video_type_invalid", "Only MP4 video is accepted in this phase")
        resolved = path.resolve()
        if not resolved.is_file() or not resolved.is_relative_to(self.media_root):
            raise VideoPipelineError("video_path_invalid", "Video must be inside the restricted media directory")
        size = resolved.stat().st_size
        if size < 12 or size > self.max_upload_bytes:
            raise VideoPipelineError("video_size_invalid", "Video size is outside configured bounds")
        with resolved.open("rb") as handle:
            header = handle.read(12)
        if header[4:8] != b"ftyp":
            raise VideoPipelineError("video_corrupt", "MP4 container signature is invalid")
        probed_duration = self.media_inspector.duration_seconds(resolved)
        if not math.isfinite(probed_duration) or probed_duration <= 0 or probed_duration > self.max_duration_seconds:
            raise VideoPipelineError("video_duration_invalid", "Video duration is outside configured bounds")
        start = _parse_utc(captured_start, "captured_start")
        end = _parse_utc(captured_end, "captured_end")
        if end <= start or abs((end - start).total_seconds() - probed_duration) > 2:
            raise VideoPipelineError("video_duration_mismatch", "Capture timestamps do not match probed duration")
        if duration_seconds is not None and abs(duration_seconds - probed_duration) > 2:
            raise VideoPipelineError("video_duration_mismatch", "Declared and probed video durations do not match")
        checksum = _sha256(resolved)
        if expected_sha256 is not None and checksum != expected_sha256.lower():
            raise VideoPipelineError("checksum_mismatch", "Video checksum did not match")
        if self.store.tenant_asset_bytes(authenticated_tenant_id) + size > self.tenant_quota_bytes:
            raise VideoPipelineError("tenant_video_quota_exceeded", "Tenant video storage quota would be exceeded")
        received = _parse_utc(received_at or utc_now(), "received_at")
        retention_until = received + timedelta(days=source["retention_policy_days"])
        asset_id = str(uuid.uuid5(NAMESPACE, f"{authenticated_tenant_id}:{source_id}:{checksum}"))
        payload = {
            "schema_version": "1.0.0",
            "tenant_id": authenticated_tenant_id,
            "asset_id": asset_id,
            "source_id": source_id,
            "kind": "recorded_upload",
            "status": "ready",
            "storage_ref": f"secret://video-assets/{asset_id}",
            "content_type": content_type,
            "size_bytes": size,
            "sha256": checksum,
            "captured_start": _utc(start),
            "captured_end": _utc(end),
            "received_at": _utc(received),
            "retention_until": _utc(retention_until),
        }
        validate_contract("video-asset.schema.json", payload)
        self.store.put_asset(payload, resolved)
        self.store.audit(authenticated_tenant_id, "video.accept", "asset", asset_id, "success")
        return payload

    def process_asset(self, tenant_id: str, asset_id: str) -> list[dict[str, Any]]:
        """Run the idempotent upload/index/analyze chain once.

        Pending indexing is recorded as a retry so a scheduler can call this
        method again without duplicating the upload or candidates.
        """
        self._run_operation(tenant_id, asset_id, "upload")
        status = self._run_operation(tenant_id, asset_id, "index")
        if status != "indexed":
            return []
        return self._run_operation(tenant_id, asset_id, "analyze")

    def _run_operation(self, tenant_id: str, asset_id: str, operation: str) -> Any:
        job = self.store.enqueue(tenant_id, asset_id, operation)
        if job["state"] == "completed":
            if operation == "analyze":
                return [value for value in self.store.list_candidates(tenant_id) if value["asset_id"] == asset_id]
            if operation == "index":
                mapping = self.store.get_mapping(tenant_id, asset_id)
                return mapping["indexing_status"] if mapping else None
            return None
        if job["state"] in {"failed", "cancelled"}:
            raise VideoPipelineError("job_not_runnable", "Processing job is final")
        job = self.store.transition_job(tenant_id, job["job_id"], "running")
        try:
            result = self._execute(tenant_id, asset_id, operation)
            if operation == "index" and result in {"pending", "indexing"}:
                self.store.transition_job(tenant_id, job["job_id"], "retry", "reka_index_pending")
            else:
                self.store.transition_job(tenant_id, job["job_id"], "completed")
            self.store.audit(tenant_id, f"reka.{operation}", "asset", asset_id, "success")
            return result
        except VideoPipelineError as error:
            updated = self.store.get_job(tenant_id, job["job_id"])
            retry = error.retryable and updated["attempts"] < updated["max_attempts"]
            self.store.transition_job(tenant_id, job["job_id"], "retry" if retry else "failed", error.code)
            self.store.audit(tenant_id, f"reka.{operation}", "asset", asset_id, "failure", error.code)
            raise

    def _execute(self, tenant_id: str, asset_id: str, operation: str) -> Any:
        asset = self.store.get_asset(tenant_id, asset_id)
        mapping = self.store.get_mapping(tenant_id, asset_id)
        if operation == "upload":
            if mapping:
                return None
            video_id = self.provider.upload(
                self.store.asset_path(tenant_id, asset_id),
                video_name=f"{asset_id}.mp4",
                captured_start=asset["captured_start"],
            )
            if not isinstance(video_id, str) or not video_id:
                raise VideoPipelineError("reka_response_invalid", "Reka upload returned no video identifier")
            self.store.put_mapping(tenant_id, asset["source_id"], asset_id, video_id, "pending")
            self.store.update_asset_status(tenant_id, asset_id, "processing")
            return None
        if mapping is None:
            raise VideoPipelineError("reka_mapping_missing", "Video has not been uploaded")
        if operation == "index":
            status = self.provider.indexing_status(mapping["reka_video_id"])
            if status not in {"pending", "indexing", "indexed", "failed"}:
                raise VideoPipelineError("reka_response_invalid", "Reka returned an invalid indexing status")
            self.store.put_mapping(tenant_id, asset["source_id"], asset_id, mapping["reka_video_id"], status)
            if status == "failed":
                raise VideoPipelineError("reka_index_failed", "Reka video indexing failed")
            return status
        if operation == "analyze":
            if mapping["indexing_status"] != "indexed":
                raise VideoPipelineError("reka_index_pending", "Video indexing is not complete", retryable=True)
            proposals = self.provider.propose_candidates(mapping["reka_video_id"], prompt_version=self.prompt_version)
            if not isinstance(proposals, list) or len(proposals) > 100:
                raise VideoPipelineError("reka_output_invalid", "Reka returned malformed or excessive candidate proposals")
            candidates = [self._candidate(asset, mapping["reka_video_id"], proposal) for proposal in proposals]
            for candidate, semantic_key in candidates:
                self.store.put_candidate(candidate, semantic_key)
            self.store.update_asset_status(tenant_id, asset_id, "processed")
            return [candidate for candidate, _ in candidates]
        raise ValueError("Unsupported operation")

    def _candidate(self, asset: dict[str, Any], remote_id: str, proposal: dict[str, Any]) -> tuple[dict[str, Any], str]:
        if not isinstance(proposal, dict) or set(proposal) != {"offset_seconds", "category", "confidence"}:
            raise VideoPipelineError("reka_output_prohibited", "Candidate output contained prohibited or missing fields")
        offset = proposal["offset_seconds"]
        category = proposal["category"]
        confidence = proposal["confidence"]
        if isinstance(offset, bool) or not isinstance(offset, (int, float)) or not math.isfinite(offset) or offset < 0:
            raise VideoPipelineError("reka_output_invalid", "Candidate timestamp offset is invalid")
        if category not in ALLOWED_CATEGORIES:
            raise VideoPipelineError("reka_output_invalid", "Candidate category is invalid")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise VideoPipelineError("reka_output_invalid", "Candidate confidence is invalid")
        start = _parse_utc(asset["captured_start"], "captured_start")
        end = _parse_utc(asset["captured_end"], "captured_end")
        occurred = start + timedelta(seconds=float(offset))
        if occurred > end:
            raise VideoPipelineError("reka_output_invalid", "Candidate timestamp falls outside the video")
        occurred_text = _utc(occurred)
        semantic_key = f"{asset['tenant_id']}:{asset['asset_id']}:{remote_id}:{self.prompt_version}:{occurred_text}:{category}"
        detection_id = str(uuid.uuid5(NAMESPACE, semantic_key))
        now = datetime.now(timezone.utc)
        expires = min(now + self.review_ttl, _parse_utc(asset["retention_until"], "retention_until"))
        candidate = {
            "schema_version": "1.0.0",
            "tenant_id": asset["tenant_id"],
            "detection_id": detection_id,
            "source_id": asset["source_id"],
            "asset_id": asset["asset_id"],
            "occurred_at": occurred_text,
            "received_at": _utc(now),
            "proposed_category": category,
            "confidence": float(confidence),
            "detector_version": f"reka-vision:{self.prompt_version}",
            "evidence_ref": f"secret://candidate-evidence/{detection_id}",
            "review_status": "awaiting_review",
            "expires_at": _utc(expires),
        }
        validate_contract("candidate-detection.schema.json", candidate)
        return candidate, semantic_key

    def review_candidate(
        self,
        *,
        authenticated_tenant_id: str,
        detection_id: str,
        decision: str,
        reviewed_by: str,
        role: str,
        confirmed_category: str | None = None,
        rejection_reason: str | None = None,
        reviewed_at: str | None = None,
    ) -> dict[str, Any]:
        if role not in REVIEW_ROLES:
            raise VideoPipelineError("review_forbidden", "Role is not permitted to review candidates")
        existing = self.store.get_review_for_candidate(authenticated_tenant_id, detection_id)
        if existing:
            same = existing["decision"] == decision
            if decision == "confirmed":
                same = same and existing.get("confirmed_category") == confirmed_category
            else:
                same = same and existing.get("rejection_reason") == rejection_reason
            if same:
                return existing
            raise VideoPipelineError("review_already_final", "Candidate already has an immutable final review")
        candidate = self.store.get_candidate(authenticated_tenant_id, detection_id)
        when = _parse_utc(reviewed_at or utc_now(), "reviewed_at")
        if candidate["review_status"] == "expired" or when >= _parse_utc(candidate["expires_at"], "expires_at"):
            raise VideoPipelineError("candidate_expired", "Expired candidate cannot be reviewed")
        if decision not in {"confirmed", "rejected"}:
            raise VideoPipelineError("review_decision_invalid", "Decision must be confirmed or rejected")
        external_id = f"video-candidate:{detection_id}"
        review: dict[str, Any] = {
            "schema_version": "1.0.0",
            "tenant_id": authenticated_tenant_id,
            "review_id": str(uuid.uuid5(NAMESPACE, f"review:{authenticated_tenant_id}:{detection_id}")),
            "detection_id": detection_id,
            "decision": decision,
            "reviewed_by": reviewed_by,
            "reviewed_at": _utc(when),
        }
        if decision == "confirmed":
            if confirmed_category not in ALLOWED_CATEGORIES - {"unmapped"}:
                raise VideoPipelineError("confirmed_category_invalid", "Confirmed category is invalid")
            review.update(confirmed_category=confirmed_category, promoted_external_event_id=external_id)
            source = self.store.get_source(authenticated_tenant_id, candidate["source_id"])
            location = self.location_resolver.resolve(authenticated_tenant_id, source["location_ref"])
            event = {
                "schema_version": "1.0.0",
                "tenant_id": authenticated_tenant_id,
                "source_id": candidate["source_id"],
                "external_event_id": external_id,
                "occurred_at": candidate["occurred_at"],
                "received_at": _utc(when),
                "category": confirmed_category,
                "location": location,
                "attributes": {"reporting_channel": "reka_vision_confirmed", "source_quality": candidate["confidence"]},
            }
            validate_contract("incident-event.schema.json", event)
            self.store.ingestion_store.insert_event(event, _payload_hash(event))
        else:
            if rejection_reason not in {"false_positive", "insufficient_evidence", "duplicate", "outside_scope", "other"}:
                raise VideoPipelineError("rejection_reason_invalid", "A valid rejection reason is required")
            review["rejection_reason"] = rejection_reason
        validate_contract("candidate-review.schema.json", review)
        self.store.put_review(review)
        self.store.audit(authenticated_tenant_id, "candidate.review", "candidate", detection_id, "success")
        return review

    def record_coverage(
        self,
        *,
        tenant_id: str,
        source_id: str,
        interval_start: str,
        interval_end: str,
        connected_seconds: int,
        processable_seconds: int,
        detector_available_seconds: int,
        degraded_reason_codes: list[str] | None = None,
    ) -> dict[str, Any]:
        self.store.get_source(tenant_id, source_id)
        start = _parse_utc(interval_start, "interval_start")
        end = _parse_utc(interval_end, "interval_end")
        expected = int((end - start).total_seconds())
        if expected <= 0 or not 0 <= detector_available_seconds <= processable_seconds <= connected_seconds <= expected:
            raise VideoPipelineError("coverage_duration_invalid", "Coverage durations must be ordered within the interval")
        payload = {
            "schema_version": "1.0.0",
            "tenant_id": tenant_id,
            "source_id": source_id,
            "interval_start": _utc(start),
            "interval_end": _utc(end),
            "expected_seconds": expected,
            "connected_seconds": connected_seconds,
            "processable_seconds": processable_seconds,
            "detector_available_seconds": detector_available_seconds,
            "coverage_ratio": detector_available_seconds / expected,
            "degraded_reason_codes": sorted(set(degraded_reason_codes or [])),
            "computed_at": utc_now(),
        }
        validate_contract("coverage-snapshot.schema.json", payload)
        self.store.put_coverage(payload)
        return payload

    def expire_due_candidates(self, tenant_id: str, *, now: str | None = None) -> int:
        cutoff = _parse_utc(now or utc_now(), "now")
        count = 0
        for candidate in self.store.list_candidates(tenant_id):
            if candidate["review_status"] == "awaiting_review" and _parse_utc(candidate["expires_at"], "expires_at") <= cutoff:
                candidate["review_status"] = "expired"
                validate_contract("candidate-detection.schema.json", candidate)
                self.store.update_candidate(candidate)
                count += 1
        return count

    def enforce_retention(self, *, now: str | None = None) -> list[str]:
        deleted: list[str] = []
        for tenant_id, asset_id in self.store.expired_assets(now or utc_now()):
            job = self.store.enqueue(tenant_id, asset_id, "delete")
            if job["state"] == "completed":
                continue
            job = self.store.transition_job(tenant_id, job["job_id"], "running")
            try:
                mapping = self.store.get_mapping(tenant_id, asset_id)
                if mapping and not mapping["remote_deleted_at"]:
                    self.provider.delete(mapping["reka_video_id"])
                    self.store.mark_remote_deleted(tenant_id, asset_id)
                local_path = self.store.asset_path(tenant_id, asset_id).resolve()
                if not local_path.is_relative_to(self.media_root):
                    raise VideoPipelineError("retention_path_invalid", "Stored media path escaped restricted root")
                local_path.unlink(missing_ok=True)
                self.store.update_asset_status(tenant_id, asset_id, "deleted")
                self.store.transition_job(tenant_id, job["job_id"], "completed")
                self.store.audit(tenant_id, "reka.delete", "asset", asset_id, "success")
                deleted.append(asset_id)
            except (VideoPipelineError, OSError) as error:
                if isinstance(error, OSError):
                    error = VideoPipelineError("local_retention_failed", "Local transient deletion failed", retryable=True)
                updated = self.store.get_job(tenant_id, job["job_id"])
                state = "retry" if error.retryable and updated["attempts"] < updated["max_attempts"] else "failed"
                self.store.transition_job(tenant_id, job["job_id"], state, error.code)
                self.store.audit(tenant_id, "reka.delete", "asset", asset_id, "failure", error.code)
        return deleted
