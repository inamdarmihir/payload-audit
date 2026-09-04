"""
Command-line entry point for qdrant-payload-audit.

    qdrant-payload-audit my_collection --url http://localhost:6333

Installed as a console script by pyproject.toml (`pip install -e .`), and
also runnable without installing via `python3 -m qdrant_payload_audit`.
"""

import argparse
import json
import os
import sys

from qdrant_client import QdrantClient

from . import __version__, audit


def format_report(report: dict, top_n: int = 20) -> str:
    lines = [
        f"Collection: {report['collection_name']}",
        f"Sampled points: {report['sample_size']}",
        f"Distinct payload keys seen: {report['distinct_keys_seen']}",
        f"Indexed fields: {len(report['indexed_fields'])}",
        f"Dynamic-key-to-indexed-field ratio: {report['dynamic_key_to_indexed_field_ratio']:.2f}",
        f"Unindexed keys: {len(report['unindexed_keys_by_frequency'])}",
        "Lowest-frequency unindexed keys (the schema-sprawl candidates):",
    ]
    for key, count in report["unindexed_keys_by_frequency"][:top_n]:
        lines.append(f"  {key}: seen in {count} of {report['sample_size']} sampled points")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qdrant-payload-audit",
        description=(
            "Sample a live Qdrant collection and report the dynamic-key-to-"
            "indexed-field ratio that get_collection() alone won't show you."
        ),
    )
    parser.add_argument("collection_name", help="Name of the Qdrant collection to audit")
    parser.add_argument(
        "--url",
        default=os.environ.get("QDRANT_URL", "http://localhost:6333"),
        help="Qdrant server URL (default: $QDRANT_URL or http://localhost:6333)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("QDRANT_API_KEY"),
        help="Qdrant API key, e.g. for Qdrant Cloud (default: $QDRANT_API_KEY)",
    )
    parser.add_argument("--sample-size", type=int, default=10_000, help="Points to scroll through (default: 10000)")
    parser.add_argument(
        "--max-ratio",
        type=float,
        default=None,
        help=(
            "Exit with a non-zero status if the dynamic-key-to-indexed-field "
            "ratio exceeds this value. Intended for CI / pre-deploy checks."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Print the raw report as JSON instead of the summary")
    parser.add_argument("--version", action="version", version=f"qdrant-payload-audit {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    client = QdrantClient(url=args.url, api_key=args.api_key)
    report = audit(client, collection_name=args.collection_name, sample_size=args.sample_size)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_report(report))

    if args.max_ratio is not None and report["dynamic_key_to_indexed_field_ratio"] > args.max_ratio:
        print(
            f"\nFAILED: ratio {report['dynamic_key_to_indexed_field_ratio']:.2f} "
            f"exceeds --max-ratio {args.max_ratio:.2f}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
