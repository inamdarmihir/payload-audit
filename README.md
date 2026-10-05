<p align="center">
  <a href="https://github.com/inamdarmihir/payload-audit">
    <img src="docs/assets/banner.svg" width="800px" alt="Qdrant Payload Audit: find payload-key sprawl that collection metadata cannot show">
  </a>
</p>

<p align="center">
  <a href="#quickstart">Quickstart</a>
  ·
  <a href="#results">Results</a>
  ·
  <a href="#reproduce-the-storage-experiment">Reproduce</a>
  ·
  <a href="#limits">Limits</a>
  ·
  <a href="https://aihive.hashnode.dev/qdrant-payload-index-per-key-98x-disk">Article</a>
</p>

<p align="center">
  <a href="https://github.com/inamdarmihir/payload-audit/actions/workflows/ci.yml">
    <img src="https://github.com/inamdarmihir/payload-audit/actions/workflows/ci.yml/badge.svg" alt="CI">
  </a>
  <a href="pyproject.toml">
    <img src="https://img.shields.io/badge/python-3.9%2B-blue.svg" alt="Python 3.9+">
  </a>
  <a href="https://qdrant.tech">
    <img src="https://img.shields.io/badge/built%20for-Qdrant-dc244c.svg" alt="Built for Qdrant">
  </a>
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT">
  </a>
  <a href="https://github.com/inamdarmihir/payload-audit/commits/main">
    <img src="https://img.shields.io/github/last-commit/inamdarmihir/payload-audit" alt="Last commit">
  </a>
</p>

# Qdrant Payload Audit

A collection can hold thousands of stored payload keys and only two indexed fields. `payload_schema` describes indexes, not the keys in your data, so `get_collection()` cannot show the sprawl. **Payload Audit scrolls stored points, counts the keys it finds and compares them with the indexed schema.** It only reads, so it is safe to point at a live collection.

This repo also ships the experiment behind the headline number. Indexing every GitHub topic as its own field (1,000 repositories, 3,685 distinct topics) used **about 98x the disk** of a fixed `tags` array.

| | |
| --- | --- |
| **Read-only** | Only calls `get_collection()` and `scroll()`. Never creates indexes or modifies points. |
| **CI-ready** | `--max-ratio` returns a non-zero exit code so a pipeline can fail on schema sprawl. |
| **Scriptable** | Plain-dict Python API and `--json` output. |
| **Measured** | The 98x claim comes from a committed run on a real Qdrant server, with raw outputs included. |

## Contents

- [Quickstart](#quickstart)
- [How it works](#how-it-works)
- [Usage](#usage)
- [Data and ground truth](#data-and-ground-truth)
- [Results](#results)
- [Reproduce the storage experiment](#reproduce-the-storage-experiment)
- [Repository layout](#repository-layout)
- [Limits](#limits)
- [Development](#development)
- [License](#license)

## Quickstart

Requires Python 3.9+ and a running Qdrant server. Install from the repository; no PyPI package is assumed.

```bash
git clone https://github.com/inamdarmihir/payload-audit.git
cd payload-audit
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

qdrant-payload-audit your_collection --url http://localhost:6333
```

Excerpt from a real run on the committed experiment (`repos_unindexed_topics`, full report in [`audit_output_unindexed_topics.txt`](audit_output_unindexed_topics.txt)):

```text
Collection: repos_unindexed_topics
...
Distinct payload keys seen: 3688
Indexed fields: 2
Dynamic-key-to-indexed-field ratio: 1844.00
Unindexed keys: 3686
Lowest-frequency unindexed keys (the schema-sprawl candidates):
  topic_no_code_platform: seen in 1 of 1000 scanned points
  topic_semantic_parsing: seen in 1 of 1000 scanned points
```

For an authenticated server, set `QDRANT_API_KEY` (or `QDRANT_URL`) in your environment. Do not put credentials in committed files.

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

## Usage

### Command line

```bash
qdrant-payload-audit your_collection --sample-size 10000 --json
qdrant-payload-audit your_collection --max-ratio 5
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `collection_name` | required | Collection to audit |
| `--url` | `$QDRANT_URL` or `http://localhost:6333` | Qdrant server |
| `--api-key` | `$QDRANT_API_KEY` | API key, for example for Qdrant Cloud |
| `--sample-size` | `10000` | Maximum number of points to scroll |
| `--max-ratio` | off | Exit with status 1 when the key/index ratio exceeds this value |
| `--json` | off | Print the raw report instead of the summary |

`--max-ratio 5` is an example policy, not a universal safe limit. Large scans still put read load on the server. You can also run the tool without installing it, via `python3 -m qdrant_payload_audit`.

### Python

```python
from qdrant_client import QdrantClient
from qdrant_payload_audit import audit

client = QdrantClient(url="http://localhost:6333")
report = audit(client, collection_name="your_collection", sample_size=10_000)

print(report["points_scanned"])                # points actually read
print(report["unindexed_keys_by_frequency"])   # [(key, count), ...] rarest first
```

Report fields: `collection_name`, `sample_size` (the requested limit), `points_scanned` (points actually read), `distinct_keys_seen`, `indexed_fields`, `dynamic_key_to_indexed_field_ratio`, `unindexed_keys_by_frequency`.

### As a CI gate

The [CI workflow](.github/workflows/ci.yml) seeds a small collection with unindexed dynamic keys, asserts that the audit fails on it, indexes the keys, then asserts that it passes. Copy that job to gate your own deploys.

## Data and ground truth

The experiment uses real repository metadata, and everything it reports is a direct measurement.

| Item | Source | Notes |
| --- | --- | --- |
| Input records (1,000 repositories, 3,685 distinct topics) | GitHub [REST search API](https://docs.github.com/en/rest/search/search#search-repositories), query `stars:>1000`, sorted by stars, 10 pages x 100 | Fetched by [`fetch_dataset.py`](fetch_dataset.py) and committed as [`github_repos.json`](github_repos.json) (snapshot first committed 2026-09-01). Topics are user-assigned tags, so the long-tail shape is organic. GitHub's search API caps results at 1,000, which is why the dataset is exactly that size. |
| Ground truth for the 98x claim | Measured on a real Qdrant server | Disk from `du -sb` inside the container, memory from the server's `/metrics` endpoint, build time from the run. Raw output: [`run_results.json`](run_results.json), [`full_run_output.txt`](full_run_output.txt). |
| Ground truth for the audit tool | The collection itself | The audit reads stored payloads and the server's own `payload_schema`. There is no external label set. |

**What we transform, and what is not real.**
- Each repository's topic list is reshaped into one boolean payload key per topic (`topic_<name>: true`), to reproduce the "one index per dynamic key" shape. This transform is ours and is disclosed in `fetch_dataset.py`.
- The **vectors are seeded random fixtures**, not embeddings. The experiment measures storage cost, not search quality, and makes no retrieval-quality claim.
- The snapshot is a point in time. Refetching (`python3 fetch_dataset.py fetch`) returns a different set of repositories and different numbers.

## Results

Same 1,000 GitHub repositories and seeded random vector fixtures in each collection. The `language` and `stars` indexes stay constant; topic payload shape and topic indexing change. These are **storage fixtures, not semantic embeddings**, and this is not a search-quality benchmark.

| Topic representation | Indexed fields | Index build | Disk bytes | Resident memory bytes |
| --- | ---: | ---: | ---: | ---: |
| Separate indexed `topic_*` keys | 3,687 | 69.5 s | 15,609,424,528 | 521,928,704 |
| Indexed `tags` array | 3 | 0.07 s | 159,572,083 | 180,486,144 |
| Separate unindexed `topic_*` keys | 2 | 0.05 s | 152,198,670 | 178,061,312 |

Source: [`run_results.json`](run_results.json). Disk ratio, indexed dynamic keys vs fixed schema: approximately **97.8x**. The control suggests that per-key indexing dominates the storage difference in this setup. It does **not** show that dynamic keys are free in all workloads.

The 3,685 topic names are distinct across the dataset, not present on every repository. The fixed-schema sample-topic query returned 39 matches, matching the expected count in the committed result.

Measurements used a real Qdrant Docker server on a macOS host, one configured segment, container restarts between conditions and a 30-second settle before disk measurement. Disk comes from `du -sb` in the container; memory comes from the server's `/metrics` endpoint. The server image is unpinned, so a new run may differ.

## Reproduce the storage experiment

> [!WARNING]
> **Use a disposable server.** This path creates and deletes benchmark collections, restarts its named Docker container and can consume more than 15 GB of disk. It is separate from the read-only audit.

```bash
# From the checkout, with the virtual environment active:
docker run -d --name qdrant-audit-test -p 127.0.0.1:6333:6333 qdrant/qdrant
python3 build_collections.py
```

The committed [`github_repos.json`](github_repos.json) is the input snapshot. No GitHub authentication is needed to use it. Optional refetching with `python3 fetch_dataset.py fetch` requires an authenticated `gh` CLI and changes the dataset.

## Repository layout

| Path | Contents |
| --- | --- |
| `qdrant_payload_audit/` | Audit library (`__init__.py`) and CLI (`cli.py`) |
| `tests/test_audit.py` | Unit tests using a fake client, not the storage experiment |
| `build_collections.py`, `fetch_dataset.py` | Storage experiment and its dataset fetcher |
| `github_repos.json` | Input repositories and their topic lists |
| `run_results.json` | Build times, memory, disk and query check |
| `full_run_output.txt`, `audit_output_*.txt` | Console output and the audit report for each collection |

## Limits

- One dataset and one measured run. Exact disk and timing figures depend on server version, segments, storage and host.
- The audit scans the first points returned by scrolling, up to the requested limit. It is not a random sample and can miss rare keys.
- Only top-level payload keys are counted. Nested payload paths are not exhaustively audited.
- Embedded/local Qdrant does not provide the server payload-index behavior this experiment requires.
- Thousands of per-field indexes exhausted file handles during development. The experiment forces one segment in all conditions rather than measuring the default multi-segment layout.
- The committed `audit_output_*.txt` files were produced before the `points_scanned` field existed, so their "Sampled points" line shows the requested limit (10,000) rather than the 1,000 points actually scanned.

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

Unit tests use a fake client and need no server. Keep storage experiments on real servers, separate from those tests. Changes should include tests for missing indexes, small collections and CLI exit status.

## License

[MIT](LICENSE). Not affiliated with or endorsed by Qdrant.
