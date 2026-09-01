# qdrant-payload-audit

A published Qdrant benchmark found that giving 1,000 dynamic, user-defined
payload keys their own index each, instead of storing them as values inside
one fixed field, added 1.2 GB and 63 seconds to a 10,000-point collection.
Reshaping into two fixed key-value fields cut that to 24 MB and 0.2 seconds.
`get_collection()` doesn't show you when you've done this to yourself. It
only lists fields that have an explicit index, so a collection quietly
carrying 3,686 unindexed dynamic keys reports back looking exactly as clean
as one that has none.

This repo reproduces the finding at a different scale, against a real
public dataset, and ships the tool that catches it: something that samples
real stored payloads and tells you the dynamic-key-to-indexed-field ratio
`get_collection()` won't.

## The numbers, from an actual run

1,000 real GitHub repositories (`stars:>1000`, fetched via `gh api`), same
vectors, same `language` and `stars` indexes held constant across all three
collections. Only the payload shape changes. Real Qdrant server
(`qdrant/qdrant` on Docker, default settings), macOS host, measured via
Qdrant's own `/metrics` endpoint for memory and `du -sb` inside the
container for disk, after a fixed 30-second settle so WAL/segment state
isn't caught mid-flush.

| Collection | Shape | Indexed fields | Index build time | Disk | Resident memory |
|---|---|---|---|---|---|
| `repos_dynamic_keys` | one payload index per topic (`topic_svelte: true`, `topic_llm: true`, ...) | 3,687 | 69.5s | 15.6 GB | 498 MB |
| `repos_fixed_schema` | one `tags: [...]` array, single index | 3 | 0.07s | 160 MB | 172 MB |
| `repos_unindexed_topics` | same `topic_*` keys as above, stored but never indexed | 2 | 0.05s | 152 MB | 170 MB |

3,685 distinct GitHub topics across the sample. Indexing each one costs
**~98x the disk** of the fixed-schema version, for the same underlying
data. `repos_unindexed_topics` is the control that isolates why: storing
3,686 sparse dynamic keys per point costs almost nothing (152 MB, actually
slightly less than the fixed-schema collection's 160 MB). The cost is
entirely in the per-key *index*, not the data shape. That's a sharper
finding than "dynamic keys are expensive": dynamic keys are free, indexing
each one individually is what isn't.

`repos_unindexed_topics` is also the case this tool exists for.
`get_collection()` reports 2 indexed fields on it, same as a collection
with no dynamic keys at all. Running the audit tool against it:

```
Collection: repos_unindexed_topics
Sampled points: 10000
Distinct payload keys seen: 3688
Indexed fields: 2
Dynamic-key-to-indexed-field ratio: 1844.00
Unindexed keys: 3686
Lowest-frequency unindexed keys (the schema-sprawl candidates):
  topic_no_code_platform: seen in 1 of 10000 sampled points
  topic_semantic_parsing: seen in 1 of 10000 sampled points
  ...
```

Full output for all three collections is in `audit_output_*.txt`, and the
raw console output of the run that produced every number above is in
`full_run_output.txt`. `run_results.json` has the structured version.

## What's here

```
fetch_dataset.py           fetches 1000 real repos via `gh api`, checkpointed page by page
build_collections.py       builds all 3 collections against a real Qdrant server, runs the audit against each
qdrant_payload_audit.py    the tool: samples a live collection, reports the dynamic-key-to-indexed-field ratio
github_repos.json          the actual fetched dataset (1000 repos, real GitHub topics/stars/language)
run_results.json           structured output of the full run
audit_output_*.txt         audit tool's plain-text report for each of the 3 collections
full_run_output.txt        full console output of the run that produced the numbers above
requirements.txt
```

## How it works

`qdrant_payload_audit.py` does one thing: `get_collection()` gives you
`payload_schema`, the fields that have an explicit index. It says nothing
about what's actually sitting in the stored payloads. The tool pages
through up to 10,000 real points with `scroll()`, counts every payload key
it actually sees, and diffs that against `payload_schema`. What's left is
the dynamic, unindexed keys, sorted by how rarely each one shows up, since
sparse long-tail keys are exactly the shape of the anti-pattern.

```bash
python3 qdrant_payload_audit.py my_collection --url http://localhost:6333
```

`build_collections.py` builds the three comparison collections from
`github_repos.json`, restarting the Qdrant container between each so
memory metrics start from the same cold baseline, and runs the audit tool
against each one immediately after building it. This is the actual script
that produced every number in this README; running it end to end
reproduces the table above.

## Using this on your own collection

No setup beyond installing the one dependency (`qdrant-client`) and
pointing it at a real Qdrant server:

```bash
pip install -r requirements.txt
python3 qdrant_payload_audit.py your_collection_name --url http://localhost:6333
```

That's the whole tool. It's read-only (`get_collection()` + `scroll()`),
safe to run against production, and doesn't need `github_repos.json` or
`build_collections.py` at all, those only exist to reproduce this README's
benchmark numbers.

To wire it into your own script or a CI/pre-deploy check instead of running
it as a CLI, import `audit()` directly:

```python
from qdrant_client import QdrantClient
from qdrant_payload_audit import audit

client = QdrantClient(url="http://localhost:6333")
report = audit(client, collection_name="your_collection_name", sample_size=10_000)

if report["dynamic_key_to_indexed_field_ratio"] > 5:
    raise SystemExit(f"schema sprawl: {report['dynamic_key_to_indexed_field_ratio']:.1f}x")
```

`report` is a plain dict (`distinct_keys_seen`, `indexed_fields`,
`dynamic_key_to_indexed_field_ratio`, `unindexed_keys_by_frequency`), so
any threshold or alert logic can read off it directly. Two things worth
knowing before relying on it: it only works against a real Qdrant server,
payload indexes are a no-op in embedded/local-mode `QdrantClient(path=...)`
so an audit there would be meaningless; and the default 10,000-point
sample is a practical default for large collections, not a full scan, pass
`sample_size` (or `--sample-size` on the CLI) higher for an exhaustive
check on a collection small enough to afford it.

## Verified, not just written

Two real things broke while building this, both left in because they're
part of what actually happened, not smoothed over:

**Qdrant's embedded/local mode makes indexes a no-op.** The original plan
used `QdrantClient(path=...)`, no Docker required. Creating a payload index
against a local-mode client prints "Payload indexes have no effect in the
local Qdrant. Please use server Qdrant if you need payload indexes." and
just... doesn't index anything. Both collections would have measured
identically and the whole comparison would have been fabricated without
either collection actually differing. Switched to a real Qdrant server
instead, which is also what the original benchmark's numbers were measured
against.

**3,685 payload indexes exhausted open file handles on the default
segment count.** Qdrant creates a per-field index file inside every
segment; with the default multi-segment optimizer, segment count times
indexed field count file handles get opened during a bulk index build. The
first attempt at building `repos_dynamic_keys` crashed partway through
index creation with connection errors traced back to this. Fixed by
forcing `default_segment_number=1` on all three collections, which also
matches how a collection this size would actually be laid out, and keeps
the comparison isolated to the one variable that matters: indexed field
count, not segment count.

## Limitations

Numbers are from one run on one machine (macOS, Docker Desktop, default
Qdrant resource limits) against a 1,000-point sample. The original
benchmark was at 10,000 points; this repo didn't scale to that size
because building 3,685 real payload indexes at 1,000 points already took
70 seconds and 15.6 GB, and confirming the same *shape* of finding at a
different scale was the goal, not reproducing the original benchmark's
exact numbers. The audit
tool's 10,000-point sample size is also a default, not a hard limit: pass
`--sample-size` for a full scan on collections small enough to afford it.

## License

MIT, see `LICENSE`.
