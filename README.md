# qdrant-payload-audit

**Audit a live Qdrant collection for dynamic-payload-key schema sprawl that `get_collection()` alone won't show you.**

`get_collection()` only reports fields that have an explicit payload index. A
collection quietly carrying thousands of dynamic, unindexed payload keys
reports back looking exactly as clean as one that has none. This CLI samples
real stored payloads, diffs them against the indexed schema, and tells you
the dynamic-key-to-indexed-field ratio before it costs you disk and memory.

```
$ qdrant-payload-audit repos_unindexed_topics
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

That's `get_collection()` reporting **2** indexed fields on this collection —
same as a collection with no dynamic keys at all — while **3,686** other keys
sit in every payload, invisible to that metadata call. This is the exact
case the tool exists to catch.

## The problem

A published Qdrant benchmark found that giving 1,000 dynamic, user-defined
payload keys their own index each, instead of storing them as values inside
one fixed field, added 1.2 GB and 63 seconds to a 10,000-point collection.
Reshaping into two fixed key-value fields cut that to 24 MB and 0.2 seconds.
Qdrant's own collection metadata doesn't surface this: `payload_schema` only
lists fields with an explicit index, so the sprawling, expensive shape and
the clean, cheap shape can report back looking identical.

This repo reproduces that finding at a different scale, against a real
public dataset, and ships the tool that catches it before it happens to your
own collection.

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
entirely in the per-key *index*, not the data shape.

**Dynamic keys are free. Indexing each one individually is what isn't.**

Full output for all three collections is in `audit_output_*.txt`, and the
raw console output of the run that produced every number above is in
`full_run_output.txt`. `run_results.json` has the structured version. See
["Reading the committed results"](#reading-the-committed-results) below for
how those files map to the table.

## Quickstart

Requires Python 3.9+ and a real Qdrant server (not embedded/local mode —
see [Limitations](#limitations)).

```bash
git clone https://github.com/inamdarmihir/payload-audit.git
cd payload-audit
pip install -e .

qdrant-payload-audit your_collection_name --url http://localhost:6333
```

That installs the `qdrant-payload-audit` console script (entry point defined
in `pyproject.toml`, `qdrant-client` pinned to `1.19.0`). No Docker, no
dataset download, no build step needed for this — it's a read-only CLI that
talks to whatever Qdrant server you point it at.

Prefer not to install anything? It runs the same way straight from a
checkout:

```bash
python3 -m qdrant_payload_audit your_collection_name --url http://localhost:6333
```

## Audit your own collection vs. reproduce this README's numbers

These are two different things. Keep them separate:

### Audit your own collection

This is the actual tool. It's read-only (`get_collection()` + `scroll()`
only), safe to run against production, and needs nothing from this repo
beyond the installed package:

```bash
qdrant-payload-audit your_collection_name --url http://localhost:6333
```

Wire it into a script or a CI/pre-deploy check by importing `audit()`
directly instead of shelling out:

```python
from qdrant_client import QdrantClient
from qdrant_payload_audit import audit

client = QdrantClient(url="http://localhost:6333")
report = audit(client, collection_name="your_collection_name", sample_size=10_000)

if report["dynamic_key_to_indexed_field_ratio"] > 5:
    raise SystemExit(f"schema sprawl: {report['dynamic_key_to_indexed_field_ratio']:.1f}x")
```

`report` is a plain dict (`distinct_keys_seen`, `indexed_fields`,
`dynamic_key_to_indexed_field_ratio`, `unindexed_keys_by_frequency`), so any
threshold or alert logic can read off it directly.

Two things worth knowing before relying on it:

- **It only works against a real Qdrant server.** Payload indexes are a
  no-op in embedded/local-mode `QdrantClient(path=...)`, so an audit there
  would be meaningless.
- **The default 10,000-point sample is a practical default, not a full
  scan.** Pass `--sample-size` (or `sample_size=` on `audit()`) higher for
  an exhaustive check on a collection small enough to afford it.

#### Failing CI when schema sprawl crosses a threshold

The CLI's `--max-ratio` flag turns the check into a pass/fail gate: it
exits non-zero if the dynamic-key-to-indexed-field ratio comes back above
the threshold you set, so a pipeline step fails loudly instead of a
collection quietly accumulating unindexed keys for months.

```yaml
# .github/workflows/schema-sprawl-check.yml
- name: Fail if payload schema sprawl exceeds threshold
  run: |
    pip install qdrant-payload-audit  # or: pip install -e . from a checkout
    qdrant-payload-audit my_collection --url "$QDRANT_URL" --max-ratio 5
```

This repo's own CI (`.github/workflows/ci.yml`) runs exactly this pattern as
a smoke test: it builds a small collection with unindexed dynamic keys
against a real `qdrant/qdrant` service container, asserts the audit fails
above the threshold, indexes the keys, and asserts it then passes. That job
is a cheap correctness check on the CLI's exit code, not a re-run of the
benchmark in the table above — see the next section for why those numbers
aren't and shouldn't be regenerated by CI.

### Reproduce this README's numbers

This is a separate, much more expensive path that only exists to regenerate
the table above from scratch. It needs the rest of this repo
(`github_repos.json`, `build_collections.py`, `fetch_dataset.py`) and a
local Docker daemon, and building 3,685 real payload indexes takes on the
order of a minute and multiple gigabytes of disk on its own:

```bash
pip install -e .
docker run -d --name qdrant-audit-test -p 6333:6333 qdrant/qdrant

python3 build_collections.py
```

`build_collections.py` builds all three comparison collections from
`github_repos.json` (the actual fetched dataset — 1,000 real repos with
real GitHub topics/stars/language, committed so this is reproducible without
re-fetching), restarting the Qdrant container between each so memory metrics
start from the same cold baseline, and runs the audit tool against each one
immediately after building it. This is the actual script that produced
every number in the table above, `full_run_output.txt`, `run_results.json`,
and the `audit_output_*.txt` files. Re-fetching the dataset itself (optional
— `github_repos.json` is already committed) needs a `gh api`-authenticated
GitHub CLI:

```bash
python3 fetch_dataset.py fetch
```

## Reading the committed results

| File | What it is |
|---|---|
| `run_results.json` | Structured build metrics per collection: upsert time, index build time, resident/allocated memory, disk bytes. This is where every number in the results table above comes from. |
| `audit_output_dynamic_keys.txt`, `audit_output_fixed_schema.txt`, `audit_output_unindexed_topics.txt` | The CLI's own plain-text report (`qdrant-payload-audit`'s output) against each of the three built collections, generated by `build_collections.py` right after building each one. |
| `full_run_output.txt` | Full console output of the one end-to-end run of `build_collections.py` that produced everything above, unedited. |
| `github_repos.json` | The raw input dataset: 1,000 real GitHub repos (`full_name`, `language`, `stargazers_count`, `topics`) fetched via `gh api`. |

None of these files are regenerated by anything except `build_collections.py`
(and `fetch_dataset.py` for the input dataset) run against a real Qdrant
server. They are not touched by the CI smoke test, and no number in this
README is invented or backfilled — every figure traces back to one of these
files.

## How it works

`qdrant_payload_audit` does one thing: `get_collection()` gives you
`payload_schema`, the fields that have an explicit index. It says nothing
about what's actually sitting in the stored payloads. The tool pages
through up to 10,000 real points with `scroll()`, counts every payload key
it actually sees, and diffs that against `payload_schema`. What's left is
the dynamic, unindexed keys, sorted by how rarely each one shows up, since
sparse long-tail keys are exactly the shape of the anti-pattern.

```
qdrant_payload_audit/
  __init__.py     the audit logic: get_indexed_fields(), sample_payload_keys(), audit()
  cli.py          argparse CLI, the `qdrant-payload-audit` console script
  __main__.py     lets `python3 -m qdrant_payload_audit` work without installing
tests/
  test_audit.py   unit tests against a fake in-process client (no live server needed)
pyproject.toml    packaging + console_scripts entry point, qdrant-client pinned
fetch_dataset.py       fetches 1000 real repos via `gh api`, checkpointed page by page
build_collections.py   builds all 3 collections against a real Qdrant server, runs the audit against each
github_repos.json       the actual fetched dataset (1000 repos, real GitHub topics/stars/language)
run_results.json        structured output of the full run
audit_output_*.txt       audit tool's plain-text report for each of the 3 collections
full_run_output.txt      full console output of the run that produced the numbers above
```

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

- **One run, one machine.** Numbers are from a single run (macOS, Docker
  Desktop, default Qdrant resource limits) against a 1,000-point sample.
  The original benchmark was at 10,000 points; this repo didn't scale to
  that size because building 3,685 real payload indexes at 1,000 points
  already took 70 seconds and 15.6 GB, and confirming the same *shape* of
  finding at a different scale was the goal, not reproducing the original
  benchmark's exact numbers.
- **Real server required.** The audit tool needs a real Qdrant server;
  payload indexes (and therefore this whole audit) are meaningless against
  embedded/local-mode `QdrantClient(path=...)`.
- **Sampling, not a full scan by default.** The CLI's 10,000-point sample
  size is a practical default, not a hard limit — pass `--sample-size` for
  a full scan on collections small enough to afford it, and treat the ratio
  from a sampled run as an estimate on very large collections.
- **One dataset shape.** The benchmark dataset uses boolean `topic_<name>:
  true` keys as the dynamic-key shape (matching the original finding this
  reproduces). Real-world dynamic-key sprawl (e.g. per-tenant or per-user
  fields with mixed types) will differ in exact numbers, though the
  underlying mechanism — per-field index overhead scaling with distinct key
  count — is the same.

## License

MIT, see `LICENSE`.
