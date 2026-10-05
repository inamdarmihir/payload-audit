# Qdrant Payload Audit

**Find payload-key sprawl that collection metadata alone cannot show you.**

A collection can have thousands of stored payload keys and only two indexed fields. `payload_schema` describes indexes, not every key in your data. This read-only audit scrolls stored points, counts the keys it sees and compares them with the indexed schema.

The companion experiment measures the storage cost of indexing every GitHub topic as a separate field. Across 1,000 repositories and 3,685 distinct topics, that shape used about 98 times the disk of a fixed `tags` array in the committed run.

[Quickstart](#quickstart) · [Results](#results) · [Reproduce](#reproduce-the-storage-experiment) · [Limits](#limits) · [Article](https://aihive.hashnode.dev/qdrant-payload-index-per-key-98x-disk)

## Quickstart

Python 3.9+ and a running Qdrant server are required. Install from the repository; this README does not assume a published PyPI package.

```bash
git clone https://github.com/inamdarmihir/payload-audit.git
cd payload-audit
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
qdrant-payload-audit your_collection --url http://localhost:6333
```

For an authenticated server, set `QDRANT_API_KEY` in your environment. Do not put credentials in committed files.

```bash
qdrant-payload-audit your_collection --sample-size 10000 --json
qdrant-payload-audit your_collection --max-ratio 5
```

`--max-ratio` exits non-zero when the observed key/index ratio exceeds your threshold. Five is an example policy, not a universal safe limit. The audit only calls `get_collection()` and `scroll()`; it does not create indexes or change points. Large scans still put read load on the server.

## How it works

```text
collection metadata -> indexed field names
stored point scroll -> observed top-level payload keys
                     |
                compare and report
                     |
       key/index ratio + unindexed-key frequencies
```

The ratio is **all distinct observed payload keys / indexed fields**, not just unindexed keys. With no indexed fields it is infinite. A high ratio is a signal to inspect the schema, not proof that every key should be indexed.

Python use:

```python
from qdrant_client import QdrantClient
from qdrant_payload_audit import audit

client = QdrantClient(url="http://localhost:6333")
report = audit(client, collection_name="your_collection", sample_size=10_000)
print(report["unindexed_keys_by_frequency"])
```

## Results

Same 1,000 GitHub repositories and seeded random vector fixtures in each collection. `language` and `stars` indexes stay constant; topic payload shape and topic indexing change. These are **storage fixtures, not semantic embeddings**, and this is not a search-quality benchmark.

| Topic representation | Indexed fields | Index build | Disk bytes | Resident memory bytes |
| --- | ---: | ---: | ---: | ---: |
| Separate indexed `topic_*` keys | 3,687 | 69.5 s | 15,609,424,528 | 521,928,704 |
| Indexed `tags` array | 3 | 0.07 s | 159,572,083 | 180,486,144 |
| Separate unindexed `topic_*` keys | 2 | 0.05 s | 152,198,670 | 178,061,312 |

Source: [`run_results.json`](run_results.json). Disk ratio, indexed dynamic keys vs fixed schema: approximately **97.8x**. The control suggests that per-key indexing dominates the storage difference in this setup. It does **not** show that dynamic keys are free in all workloads.

The 3,685 topic names are distinct across the dataset, not present on every repository. The fixed-schema sample-topic query returned 39 matches, matching the expected count in the committed result.

Measurements used a real Qdrant Docker server on a macOS host, one configured segment, container restarts between conditions and a 30-second settle before disk measurement. Disk comes from `du -sb` in the container; memory comes from the server's `/metrics` endpoint. The server image is unpinned, so a new run may differ.

## Reproduce the storage experiment

**Use a disposable server.** This path creates and deletes benchmark collections, restarts its named Docker container and can consume more than 15 GB of disk. It is separate from the read-only audit.

```bash
# From the checkout, with the virtual environment active:
docker run -d --name qdrant-audit-test -p 127.0.0.1:6333:6333 qdrant/qdrant
python3 build_collections.py
```

The committed [`github_repos.json`](github_repos.json) is the input snapshot. No GitHub authentication is needed to use it. Optional refetching with `python3 fetch_dataset.py fetch` requires an authenticated `gh` CLI and changes the dataset.

## Inspect the evidence

| File | Contents |
| --- | --- |
| `run_results.json` | Build times, memory, disk and query check |
| `full_run_output.txt` | Console output of the experiment |
| `audit_output_*.txt` | Audit report for each collection |
| `github_repos.json` | Input repositories and their topic lists |
| `qdrant_payload_audit/` | Audit library and CLI |
| `tests/test_audit.py` | Unit tests using a fake client, not the storage experiment |

## Limits

- One dataset and one measured run. Exact disk and timing figures depend on server version, segments, storage and host.
- The audit scans the first points returned by scrolling up to the requested limit; it is not a random sample and can miss rare keys.
- **Reporting caveat:** `sample_size` and the CLI's "Sampled points" line currently echo the requested limit, not the actual number read. For a smaller collection, do not interpret that value as an observed count or use it as a frequency denominator.
- Only top-level payload keys are counted. Nested payload paths are not exhaustively audited.
- Embedded/local Qdrant does not provide the server payload-index behavior this experiment requires.
- Thousands of per-field indexes exhausted file handles during development. The experiment forces one segment in all conditions rather than measuring the default multi-segment layout.

## Contributing

A useful first fix is reporting the actual scanned-point count. Other changes should include tests for missing indexes, small collections and CLI exit status. Keep storage experiments on real servers, separate from fake-client unit tests.

## License

[MIT](LICENSE).
