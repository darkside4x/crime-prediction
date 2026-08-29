"""Generate a secret-free OpenAPI document for frontend type generation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from . import reka
from .app import create_app
from .settings import Settings

FORBIDDEN_OPENAPI_TEXT = (
    "REKA_API_KEY",
    "reka_api_key",
    "credential_ref",
    "endpoint_ref",
    "location_ref",
    "secret_ref",
)


def build_openapi() -> dict[str, Any]:
    document = create_app(
        provider=reka.FakeRekaProvider(), settings=Settings()
    ).openapi()
    serialized = json.dumps(document, sort_keys=True)
    leaked = [value for value in FORBIDDEN_OPENAPI_TEXT if value in serialized]
    if leaked:
        raise RuntimeError(f"OpenAPI contains restricted server fields: {leaked}")
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="Destination JSON path")
    args = parser.parse_args()
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(build_openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
