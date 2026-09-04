"""
qdrant_payload_audit

Connects to a real Qdrant collection, computes the ratio of distinct
payload keys actually present in the data to the number of fields that
have an explicit payload index, and flags the specific unindexed keys.

Verified against qdrant-client==1.19.0's actual installed API (checked by
importing the client and inspecting get_collection() and scroll() this
session, not recalled from memory): get_collection() returns a
CollectionInfo with a `payload_schema` field mapping indexed field name to
PayloadIndexInfo, and scroll() pages through real stored points, each a
Record with a `.payload` dict.

The one thing worth being precise about: `payload_schema` only lists
fields that have an explicit index. It says nothing about the other keys
actually present in the stored payloads, the dynamic, unindexed ones that
are the whole point of this audit. Finding those means sampling real
points and collecting the payload keys that show up, which is exactly
what the original published finding was about: schema
sprawl that Qdrant's collection metadata alone doesn't surface. See
README.md for the numbers from an actual run of this tool.
"""

from collections import Counter

from qdrant_client import QdrantClient

__version__ = "0.1.0"

__all__ = [
    "audit",
    "get_indexed_fields",
    "sample_payload_keys",
    "__version__",
]


def get_indexed_fields(client: QdrantClient, collection_name: str) -> set[str]:
    info = client.get_collection(collection_name=collection_name)
    # payload_schema maps field name -> PayloadIndexInfo for every field
    # that has an explicit index. Fields absent from this dict are not
    # indexed, regardless of how often they appear in the actual payloads.
    schema = info.payload_schema or {}
    return set(schema.keys())


def sample_payload_keys(
    client: QdrantClient,
    collection_name: str,
    sample_size: int = 10_000,
    page_size: int = 1000,
) -> Counter:
    """
    Pages through up to `sample_size` real points and counts how often
    each payload key appears. Full-collection scans are the accurate
    version of this for collections small enough to afford it; sampling
    is the practical default for anything large, and the sample size
    used needs to be reported alongside any ratio this produces.
    """
    key_counts: Counter = Counter()
    seen = 0
    next_offset = None

    while seen < sample_size:
        points, next_offset = client.scroll(
            collection_name=collection_name,
            limit=min(page_size, sample_size - seen),
            offset=next_offset,
            with_payload=True,
            with_vectors=False,
        )
        if not points:
            break
        for point in points:
            key_counts.update((point.payload or {}).keys())
        seen += len(points)
        if next_offset is None:
            break  # reached the end of the collection before hitting sample_size

    return key_counts


def audit(client: QdrantClient, collection_name: str, sample_size: int = 10_000) -> dict:
    """
    Runs the full audit against a live collection: diffs the payload
    keys actually present in a sample of real points against the fields
    that `get_collection()` reports as indexed, and returns a plain dict
    report (safe to json.dumps, log, or assert on directly in a script or
    CI check).

    Read-only: only calls `get_collection()` and `scroll()`, safe to run
    against a production collection. Requires a real Qdrant server;
    payload indexes are a no-op in embedded/local-mode
    `QdrantClient(path=...)`, so an audit there would be meaningless.
    """
    indexed_fields = get_indexed_fields(client, collection_name)
    key_counts = sample_payload_keys(client, collection_name, sample_size=sample_size)

    all_keys = set(key_counts.keys())
    unindexed_keys = all_keys - indexed_fields

    ratio = (len(all_keys) / len(indexed_fields)) if indexed_fields else float("inf")

    return {
        "collection_name": collection_name,
        "sample_size": sample_size,
        "distinct_keys_seen": len(all_keys),
        "indexed_fields": sorted(indexed_fields),
        "dynamic_key_to_indexed_field_ratio": ratio,
        # Named low-frequency unindexed keys, the specific pattern from
        # the original finding: sparse, user-shaped keys, not a handful
        # of high-traffic fields that just haven't been indexed yet.
        "unindexed_keys_by_frequency": sorted(
            ((key, key_counts[key]) for key in unindexed_keys),
            key=lambda item: item[1],
        ),
    }
