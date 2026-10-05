"""
Unit tests for the audit() logic, using a fake in-process stand-in for
QdrantClient instead of a live server. These check the ratio/diff math
and CLI plumbing only; they intentionally don't touch the real benchmark
numbers in run_results.json or the audit_output_*.txt files, which come
from an actual run against a real Qdrant server and aren't reproduced by
a unit test. See build_collections.py / README.md's "Reproduce this
README's numbers" section for that.

Run with: pytest
"""

from types import SimpleNamespace

import qdrant_payload_audit.cli as cli_module
from qdrant_payload_audit import audit
from qdrant_payload_audit.cli import build_parser, format_report


class FakeQdrantClient:
    """Minimal stand-in for QdrantClient exposing only what audit() calls:
    get_collection() (for payload_schema) and scroll() (for sampled
    points). Good enough to test the audit's diff/ratio logic without a
    live server."""

    def __init__(self, indexed_fields, points):
        self._indexed_fields = indexed_fields
        self._points = points

    def get_collection(self, collection_name):
        return SimpleNamespace(payload_schema=dict.fromkeys(self._indexed_fields, object()))

    def scroll(self, collection_name, limit, offset, with_payload, with_vectors):
        offset = offset or 0
        page = self._points[offset : offset + limit]
        records = [SimpleNamespace(payload=p) for p in page]
        next_offset = offset + limit if offset + limit < len(self._points) else None
        return records, next_offset


def test_no_dynamic_keys_gives_ratio_of_one():
    points = [{"language": "python", "stars": 10} for _ in range(5)]
    client = FakeQdrantClient(indexed_fields={"language", "stars"}, points=points)

    report = audit(client, "clean_collection", sample_size=100)

    assert report["distinct_keys_seen"] == 2
    assert report["dynamic_key_to_indexed_field_ratio"] == 1.0
    assert report["unindexed_keys_by_frequency"] == []


def test_unindexed_dynamic_keys_are_flagged_by_frequency():
    points = [
        {"language": "python", "topic_rare": True},
        {"language": "python", "topic_common": True},
        {"language": "python", "topic_common": True},
    ]
    client = FakeQdrantClient(indexed_fields={"language"}, points=points)

    report = audit(client, "sprawling_collection", sample_size=100)

    assert report["distinct_keys_seen"] == 3  # language, topic_rare, topic_common
    assert report["dynamic_key_to_indexed_field_ratio"] == 3.0
    keys_in_order = [key for key, _count in report["unindexed_keys_by_frequency"]]
    assert keys_in_order == ["topic_rare", "topic_common"]  # rarest first


def test_no_indexed_fields_gives_infinite_ratio():
    points = [{"topic_a": True}]
    client = FakeQdrantClient(indexed_fields=set(), points=points)

    report = audit(client, "unindexed_collection", sample_size=100)

    assert report["dynamic_key_to_indexed_field_ratio"] == float("inf")


def test_sample_size_caps_points_scanned():
    points = [{"language": "python"} for _ in range(50)]
    client = FakeQdrantClient(indexed_fields={"language"}, points=points)

    report = audit(client, "big_collection", sample_size=10)

    assert report["sample_size"] == 10
    assert report["points_scanned"] == 10


def test_points_scanned_reports_actual_count_for_small_collections():
    points = [{"language": "python"} for _ in range(3)]
    client = FakeQdrantClient(indexed_fields={"language"}, points=points)

    report = audit(client, "small_collection", sample_size=10_000)

    assert report["sample_size"] == 10_000
    assert report["points_scanned"] == 3


def test_cli_parser_accepts_max_ratio_and_json_flags():
    parser = build_parser()
    args = parser.parse_args(["my_collection", "--url", "http://qdrant:6333", "--max-ratio", "5", "--json"])

    assert args.collection_name == "my_collection"
    assert args.url == "http://qdrant:6333"
    assert args.max_ratio == 5.0
    assert args.json is True


def test_format_report_is_human_readable():
    report = {
        "collection_name": "demo",
        "sample_size": 100,
        "points_scanned": 40,
        "distinct_keys_seen": 2,
        "indexed_fields": ["language"],
        "dynamic_key_to_indexed_field_ratio": 2.0,
        "unindexed_keys_by_frequency": [("topic_x", 1)],
    }

    text = format_report(report)

    assert "Collection: demo" in text
    assert "Dynamic-key-to-indexed-field ratio: 2.00" in text
    assert "topic_x: seen in 1 of 40 scanned points" in text
    assert "Points scanned: 40 (limit 100)" in text


def test_main_exits_nonzero_when_ratio_exceeds_max_ratio(monkeypatch, capsys):
    points = [{"language": "python", "topic_a": True, "topic_b": True}]
    fake_client = FakeQdrantClient(indexed_fields={"language"}, points=points)
    monkeypatch.setattr(cli_module, "QdrantClient", lambda url, api_key=None: fake_client)

    exit_code = cli_module.main(["schema_sprawl_demo", "--max-ratio", "1"])

    assert exit_code == 1
    assert "FAILED" in capsys.readouterr().err


def test_main_exits_zero_when_ratio_within_max_ratio(monkeypatch, capsys):
    points = [{"language": "python"}]
    fake_client = FakeQdrantClient(indexed_fields={"language"}, points=points)
    monkeypatch.setattr(cli_module, "QdrantClient", lambda url, api_key=None: fake_client)

    exit_code = cli_module.main(["clean_demo", "--max-ratio", "1"])

    assert exit_code == 0
