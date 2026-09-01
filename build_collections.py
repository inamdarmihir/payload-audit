"""
build_collections.py

Builds two collections from the same real GitHub dataset (github_repos.json,
produced by fetch_dataset.py) against a real local Qdrant server, to measure
the actual before/after cost of the dynamic-payload-key
anti-pattern rather than assume the original numbers hold at a different scale.

Deviation from the original plan, disclosed here: the task called for
Qdrant's embedded/local mode (QdrantClient(path=...) or :memory:), no
Docker required. Local mode was tried first and rejected: creating a
payload index against a local-mode client prints
"Payload indexes have no effect in the local Qdrant. Please use server
Qdrant if you need payload indexes." Local mode's indexes are a no-op, so
both collections would measure identically and the whole comparison would
be fabricated. A real Qdrant server (`docker run qdrant/qdrant`) is used
instead, which is also what the original benchmark's numbers were measured
against.

Two collections, same 1000 real repos, same vectors, same `language` and
`stars` indexes in both (held constant so they don't bias the comparison):

  repos_dynamic_keys  - one payload index per distinct topic (the anti-pattern)
  repos_fixed_schema  - one payload index on a single `tags` array field

Memory is read from Qdrant's own /metrics endpoint (jemalloc stats Qdrant
exposes directly: memory_allocated_bytes, memory_resident_bytes), not
inferred. Disk size is read with `du -sb` inside the container against the
actual collection's storage directory. The container is restarted between
builds so each measurement starts from the same cold baseline rather than
carrying over the other collection's allocations.
"""

import json
import subprocess
import time
import urllib.request
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    KeywordIndexParams,
    KeywordIndexType,
    OptimizersConfigDiff,
    PayloadSchemaType,
    VectorParams,
)

DATA_FILE = Path(__file__).parent / "github_repos.json"
RESULTS_FILE = Path(__file__).parent / "run_results.json"
QDRANT_URL = "http://localhost:6333"
CONTAINER_NAME = "qdrant-audit-test"
VECTOR_SIZE = 8
SETTLE_SECONDS = 30  # fixed wait applied identically to both collections before
                      # measuring disk, since Qdrant preallocates WAL segments
                      # and raw post-write disk usage is noisy without it

client = QdrantClient(url=QDRANT_URL)


def load_repos() -> list[dict]:
    return json.loads(DATA_FILE.read_text())


def fetch_metrics() -> dict:
    """Reads Qdrant's own Prometheus /metrics endpoint. Real numbers the
    server reports about itself, not derived or estimated."""
    with urllib.request.urlopen(f"{QDRANT_URL}/metrics", timeout=10) as resp:
        text = resp.read().decode()
    values = {}
    for line in text.splitlines():
        if line.startswith("memory_") and " " in line:
            name, val = line.rsplit(" ", 1)
            try:
                values[name] = int(val)
            except ValueError:
                pass
    return values


def container_du_bytes(collection_name: str) -> int:
    path = f"/qdrant/storage/collections/{collection_name}"
    result = subprocess.run(
        ["docker", "exec", CONTAINER_NAME, "du", "-sb", path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return -1
    return int(result.stdout.split()[0])


def restart_container(wait_s: float = 3.0) -> None:
    print(f"Restarting {CONTAINER_NAME} to reset memory baseline...")
    subprocess.run(["docker", "restart", CONTAINER_NAME], capture_output=True, text=True, check=True)
    for _ in range(30):
        try:
            urllib.request.urlopen(f"{QDRANT_URL}/collections", timeout=2)
            time.sleep(wait_s)
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("Qdrant did not come back up after restart")


def ensure_collection(name: str) -> None:
    if client.collection_exists(name):
        client.delete_collection(name)
    client.create_collection(
        collection_name=name,
        vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        # Force a single segment. Qdrant creates a per-field index file
        # inside every segment; with the default multi-segment optimizer,
        # (segment count x indexed field count) file handles get opened
        # during a bulk index build. At ~3700 dynamic keys the default
        # segmentation genuinely exhausted open file handles mid-build
        # (a real failure hit during this run, logged in the README) -
        # forcing one segment keeps the comparison to the variable that
        # actually matters (indexed field count) and matches how a small
        # collection like this would actually be laid out in practice.
        optimizers_config=OptimizersConfigDiff(default_segment_number=1),
    )


def upsert_repos(name: str, repos: list[dict], payload_fn) -> None:
    import random
    random.seed(42)
    batch = []
    for i, repo in enumerate(repos):
        vector = [random.random() for _ in range(VECTOR_SIZE)]
        batch.append({"id": i, "vector": vector, "payload": payload_fn(repo)})
        if len(batch) >= 200:
            client.upsert(collection_name=name, points=batch)
            batch = []
    if batch:
        client.upsert(collection_name=name, points=batch)


def build_dynamic_keys_collection(repos: list[dict]) -> dict:
    from fetch_dataset import to_dynamic_key_payload

    name = "repos_dynamic_keys"
    print(f"\n=== Building {name} (one payload index per topic key) ===")
    ensure_collection(name)

    t0 = time.monotonic()
    upsert_repos(name, repos, to_dynamic_key_payload)
    upsert_done = time.monotonic()
    print(f"Upserted {len(repos)} points in {upsert_done - t0:.2f}s")

    client.create_payload_index(name, field_name="language", field_schema=PayloadSchemaType.KEYWORD)
    client.create_payload_index(name, field_name="stars", field_schema=PayloadSchemaType.INTEGER)

    topic_keys = sorted({
        key for repo in repos
        for key in [f"topic_{t.replace('-', '_')}" for t in repo["topics"]]
    })
    print(f"Creating {len(topic_keys)} individual payload indexes...")
    already_indexed = set(client.get_collection(name).payload_schema or {})
    created = 0
    for key in topic_keys:
        if key in already_indexed:
            continue
        client.create_payload_index(name, field_name=key, field_schema=PayloadSchemaType.BOOL)
        created += 1
        if created % 500 == 0:
            print(f"  {created}/{len(topic_keys)} indexes created...")
    index_done = time.monotonic()

    print(f"Waiting {SETTLE_SECONDS}s for WAL/segment state to settle before measuring disk...")
    time.sleep(SETTLE_SECONDS)
    metrics = fetch_metrics()
    disk_bytes = container_du_bytes(name)
    result = {
        "collection": name,
        "points": len(repos),
        "distinct_topic_keys": len(topic_keys),
        "indexed_fields_total": len(topic_keys) + 2,
        "upsert_seconds": round(upsert_done - t0, 2),
        "index_build_seconds": round(index_done - upsert_done, 2),
        "total_build_seconds": round(index_done - t0, 2),
        "memory_allocated_bytes": metrics.get("memory_allocated_bytes"),
        "memory_resident_bytes": metrics.get("memory_resident_bytes"),
        "disk_bytes": disk_bytes,
    }
    print(json.dumps(result, indent=2))
    return result


def build_fixed_schema_collection(repos: list[dict]) -> dict:
    from fetch_dataset import to_fixed_schema_payload

    name = "repos_fixed_schema"
    print(f"\n=== Building {name} (single indexed `tags` field) ===")
    ensure_collection(name)

    t0 = time.monotonic()
    upsert_repos(name, repos, to_fixed_schema_payload)
    upsert_done = time.monotonic()
    print(f"Upserted {len(repos)} points in {upsert_done - t0:.2f}s")

    client.create_payload_index(name, field_name="language", field_schema=PayloadSchemaType.KEYWORD)
    client.create_payload_index(name, field_name="stars", field_schema=PayloadSchemaType.INTEGER)
    client.create_payload_index(
        name,
        field_name="tags",
        field_schema=KeywordIndexParams(type=KeywordIndexType.KEYWORD, is_tenant=False),
    )
    index_done = time.monotonic()

    print(f"Waiting {SETTLE_SECONDS}s for WAL/segment state to settle before measuring disk...")
    time.sleep(SETTLE_SECONDS)
    metrics = fetch_metrics()
    disk_bytes = container_du_bytes(name)
    result = {
        "collection": name,
        "points": len(repos),
        "distinct_topic_keys": None,
        "indexed_fields_total": 3,
        "upsert_seconds": round(upsert_done - t0, 2),
        "index_build_seconds": round(index_done - upsert_done, 2),
        "total_build_seconds": round(index_done - t0, 2),
        "memory_allocated_bytes": metrics.get("memory_allocated_bytes"),
        "memory_resident_bytes": metrics.get("memory_resident_bytes"),
        "disk_bytes": disk_bytes,
    }
    print(json.dumps(result, indent=2))
    return result


def build_unindexed_topics_collection(repos: list[dict]) -> dict:
    """Third scenario: the dynamic topic_* keys are stored in every payload
    (same shape as repos_dynamic_keys) but never given their own index, only
    `language` and `stars` are indexed. This is the case the audit tool
    exists to catch: get_collection().payload_schema looks fine (2 indexed
    fields, nothing alarming), but 3686 other keys are sitting in every
    payload unindexed and invisible to that metadata call."""
    from fetch_dataset import to_dynamic_key_payload

    name = "repos_unindexed_topics"
    print(f"\n=== Building {name} (dynamic keys present, none indexed) ===")
    ensure_collection(name)

    t0 = time.monotonic()
    upsert_repos(name, repos, to_dynamic_key_payload)
    upsert_done = time.monotonic()
    print(f"Upserted {len(repos)} points in {upsert_done - t0:.2f}s")

    client.create_payload_index(name, field_name="language", field_schema=PayloadSchemaType.KEYWORD)
    client.create_payload_index(name, field_name="stars", field_schema=PayloadSchemaType.INTEGER)
    index_done = time.monotonic()

    print(f"Waiting {SETTLE_SECONDS}s for WAL/segment state to settle before measuring disk...")
    time.sleep(SETTLE_SECONDS)
    metrics = fetch_metrics()
    disk_bytes = container_du_bytes(name)
    result = {
        "collection": name,
        "points": len(repos),
        "distinct_topic_keys": None,
        "indexed_fields_total": 2,
        "upsert_seconds": round(upsert_done - t0, 2),
        "index_build_seconds": round(index_done - upsert_done, 2),
        "total_build_seconds": round(index_done - t0, 2),
        "memory_allocated_bytes": metrics.get("memory_allocated_bytes"),
        "memory_resident_bytes": metrics.get("memory_resident_bytes"),
        "disk_bytes": disk_bytes,
    }
    print(json.dumps(result, indent=2))
    return result


def verify_fixed_schema_query(repos: list[dict]) -> dict:
    """Confirms the reshaped collection actually answers the same question
    (find repos tagged with a given topic) via MatchAny instead of a
    per-topic index."""
    from qdrant_client.models import Filter, FieldCondition, MatchAny

    sample_topic = next(t for r in repos for t in r["topics"])
    hits = client.scroll(
        collection_name="repos_fixed_schema",
        scroll_filter=Filter(must=[FieldCondition(key="tags", match=MatchAny(any=[sample_topic]))]),
        limit=1000,
        with_payload=False,
    )[0]
    expected = sum(1 for r in repos if sample_topic in r["topics"])
    return {"sample_topic": sample_topic, "matches_found": len(hits), "matches_expected": expected}


def run_audit_and_save(name: str) -> None:
    """Runs qdrant_payload_audit.py's own audit() against a just-built
    collection and writes the same plain-text report the CLI would print,
    so the committed audit_output_*.txt files are reproduced by this
    script rather than being a separate, undocumented step."""
    from qdrant_payload_audit import audit

    report = audit(client, collection_name=name, sample_size=10_000)
    lines = [
        f"Collection: {report['collection_name']}",
        f"Sampled points: {report['sample_size']}",
        f"Distinct payload keys seen: {report['distinct_keys_seen']}",
        f"Indexed fields: {len(report['indexed_fields'])}",
        f"Dynamic-key-to-indexed-field ratio: {report['dynamic_key_to_indexed_field_ratio']:.2f}",
        f"Unindexed keys: {len(report['unindexed_keys_by_frequency'])}",
        "Lowest-frequency unindexed keys (the schema-sprawl candidates):",
    ]
    for key, count in report["unindexed_keys_by_frequency"][:20]:
        lines.append(f"  {key}: seen in {count} of {report['sample_size']} sampled points")
    text = "\n".join(lines) + "\n"
    (Path(__file__).parent / f"audit_output_{name.removeprefix('repos_')}.txt").write_text(text)
    print(text)


if __name__ == "__main__":
    repos = load_repos()
    print(f"Loaded {len(repos)} real repos from {DATA_FILE.name}")

    restart_container()
    result_a = build_dynamic_keys_collection(repos)
    run_audit_and_save("repos_dynamic_keys")
    client.delete_collection("repos_dynamic_keys")

    restart_container()
    result_b = build_fixed_schema_collection(repos)
    run_audit_and_save("repos_fixed_schema")
    query_check = verify_fixed_schema_query(repos)
    print(f"\nQuery correctness check (MatchAny on `tags`): {query_check}")
    client.delete_collection("repos_fixed_schema")

    restart_container()
    result_c = build_unindexed_topics_collection(repos)
    run_audit_and_save("repos_unindexed_topics")
    client.delete_collection("repos_unindexed_topics")

    RESULTS_FILE.write_text(json.dumps({
        "dynamic_keys": result_a,
        "fixed_schema": result_b,
        "unindexed_topics": result_c,
        "query_check": query_check,
    }, indent=2))
    print(f"\nResults written to {RESULTS_FILE}")
