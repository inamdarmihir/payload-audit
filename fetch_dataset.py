"""
fetch_dataset.py

Fetches real repository data from GitHub's search API (via the authenticated
`gh api` CLI) and reshapes it into two payload structures over the same
underlying data, to reproduce a published dynamic-payload-key finding:

  https://qdrant.tech found that a 10,000-point
  collection with 1,000 dynamic, user-defined payload keys, each given its
  own payload index, added 1.2 GB and took 63 seconds to build. Reshaping
  into two fixed key-value fields with a nested filter cut that to 24 MB
  and 0.2 seconds.

Where the data comes from: GitHub's public "topics" array on each repo is a
real, sparse, long-tail, user-assigned tag set, genuinely the kind of
end-user-shaped metadata the finding is about. It is not fabricated data.
It IS reshaped deliberately: this script turns each repo's topic list into
one boolean payload key per topic (`topic_<name>: true`) to reproduce the
specific "one index per dynamic key" shape the audit tool needs to catch,
as opposed to "one field with many values." That transform is disclosed
here and in the README, not hidden.

Run as a script to fetch and checkpoint the dataset:
    python3 fetch_dataset.py fetch

Then build the three comparison collections against a real Qdrant server:
    python3 build_collections.py
"""

import json
import subprocess
import sys
import time
from pathlib import Path

DATA_FILE = Path(__file__).parent / "github_repos.json"
PAGES_TARGET = 10          # GitHub search API caps results at 1000 (10 pages x 100)
PER_PAGE = 100
QUERY = "stars:>1000"


def fetch_page(page: int) -> dict:
    """One page via the authenticated `gh api` CLI (higher rate limit than raw curl)."""
    cmd = [
        "gh", "api",
        f"/search/repositories?q={QUERY}&sort=stars&order=desc&per_page={PER_PAGE}&page={page}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"gh api failed on page {page}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def load_checkpoint() -> list[dict]:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return []


def save_checkpoint(repos: list[dict]) -> None:
    DATA_FILE.write_text(json.dumps(repos, indent=2))


def fetch_dataset() -> list[dict]:
    """
    Fetches repos page by page, checkpointing to github_repos.json after
    every page so an interruption mid-fetch loses at most one page, not
    the whole run.
    """
    repos = load_checkpoint()
    fetched_pages = len(repos) // PER_PAGE
    print(f"Resuming from checkpoint: {len(repos)} repos already fetched ({fetched_pages} pages)")

    for page in range(fetched_pages + 1, PAGES_TARGET + 1):
        print(f"Fetching page {page}/{PAGES_TARGET}...")
        try:
            data = fetch_page(page)
        except RuntimeError as e:
            print(f"  stopped early: {e}")
            break

        items = data.get("items", [])
        if not items:
            print("  no more items returned, stopping")
            break

        for item in items:
            repos.append({
                "full_name": item["full_name"],
                "language": item.get("language"),
                "stargazers_count": item.get("stargazers_count", 0),
                "topics": item.get("topics", []),
            })

        save_checkpoint(repos)
        print(f"  got {len(items)} repos (total: {len(repos)})")
        time.sleep(1)  # be polite even though gh api has a generous authenticated quota

    print(f"Done. {len(repos)} repos saved to {DATA_FILE}")
    return repos


def to_dynamic_key_payload(repo: dict) -> dict:
    """The anti-pattern shape: one payload key per topic."""
    payload = {
        "repo": repo["full_name"],
        "language": repo["language"],
        "stars": repo["stargazers_count"],
    }
    for topic in repo["topics"]:
        key = f"topic_{topic.replace('-', '_')}"
        payload[key] = True
    return payload


def to_fixed_schema_payload(repo: dict) -> dict:
    """The fix: same data, one fixed `tags` field holding the topic list."""
    return {
        "repo": repo["full_name"],
        "language": repo["language"],
        "stars": repo["stargazers_count"],
        "tags": repo["topics"],
    }


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "fetch":
        print("Usage: python3 fetch_dataset.py fetch")
        sys.exit(1)
    fetch_dataset()
