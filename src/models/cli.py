"""Command-line interface for reproducible model evaluation."""

from __future__ import annotations

import argparse
import json
import sys

from .errors import ModelingError
from .pipeline import run_evaluation


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    evaluate = subparsers.add_parser("evaluate", help="Train, compare, and export tenant artifacts")
    evaluate.add_argument("--config", required=True)
    evaluate.add_argument(
        "--feature-manifest",
        action="append",
        required=True,
        help="Tenant feature-table manifest; repeat once per tenant",
    )
    evaluate.add_argument("--output-root", default="artifacts")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "evaluate":
            results = run_evaluation(
                config_path=args.config,
                feature_manifest_paths=args.feature_manifest,
                output_root=args.output_root,
            )
            print(json.dumps({"status": "completed", "tenants": results}, indent=2))
            return 0
    except (ModelingError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
