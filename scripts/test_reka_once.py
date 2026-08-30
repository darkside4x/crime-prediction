#!/usr/bin/env python3
"""Run one bounded Reka Vision smoke test and always delete the remote video."""

from __future__ import annotations

import argparse
import time
from datetime import UTC, datetime
from pathlib import Path

from src.api.settings import Settings
from src.data.video.reka import RekaVisionProvider


ALLOWED_CATEGORIES = {
    "property",
    "violence",
    "public_order",
    "traffic_safety",
    "other",
    "unmapped",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=240)
    args = parser.parse_args()
    if not args.video.is_file() or args.video.stat().st_size > 5 * 1024 * 1024:
        raise SystemExit("Smoke-test video must exist and be no larger than 5 MiB")

    settings = Settings.from_environment()
    if not settings.reka_configured:
        raise SystemExit("REKA_API_KEY is not configured")
    provider = RekaVisionProvider(
        settings.reka_api_key,
        base_url=settings.reka_vision_base_url,
        timeout_seconds=min(settings.reka_timeout_seconds, 30),
    )
    video_id: str | None = None
    try:
        print("reka smoke: uploading bounded synthetic clip", flush=True)
        video_id = provider.upload(
            args.video,
            video_name="aggregate-forecast-synthetic-smoke.mp4",
            captured_start=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
        deadline = time.monotonic() + args.timeout_seconds
        while True:
            status = provider.indexing_status(video_id)
            print(f"reka smoke: indexing status={status}", flush=True)
            if status == "indexed":
                break
            if status == "failed":
                raise RuntimeError("Reka indexing failed")
            if time.monotonic() >= deadline:
                raise TimeoutError("Reka indexing did not finish within the smoke-test bound")
            time.sleep(4)

        proposals = provider.propose_candidates(video_id, prompt_version="smoke-v1")
        for proposal in proposals:
            if set(proposal) != {"offset_seconds", "category", "confidence"}:
                raise ValueError("Reka proposal crossed the allowlisted output boundary")
            if proposal["category"] not in ALLOWED_CATEGORIES:
                raise ValueError("Reka proposal contained a prohibited category")
            if not isinstance(proposal["offset_seconds"], (int, float)) or proposal["offset_seconds"] < 0:
                raise ValueError("Reka proposal contained an invalid offset")
            if not isinstance(proposal["confidence"], (int, float)) or not 0 <= proposal["confidence"] <= 1:
                raise ValueError("Reka proposal contained an invalid confidence")
        categories = sorted({proposal["category"] for proposal in proposals})
        print(
            f"reka smoke: validated proposals={len(proposals)} categories={categories}",
            flush=True,
        )
    finally:
        if video_id is not None:
            provider.delete(video_id)
            print("reka smoke: remote video deleted", flush=True)


if __name__ == "__main__":
    main()
