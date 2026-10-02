#!/usr/bin/env python3
"""Generate five busy demo boards and load them through the CLI.

    scripts/bulk_demo.py                 # 5 boards x 30 issues, comments on payments
    scripts/bulk_demo.py --issues 60     # bigger boards
    scripts/bulk_demo.py --seed 7        # a different (but still reproducible) mix

Stress data for the TUI and migrate-comments: many columns, extra category
labels, overdue work, absurdly long titles, issues straddling two columns,
and a handful of commented issues in test/payments so `m` has something to
copy. Deterministic for a given --seed, and everything goes through
`gitboard push`, so re-running writes only drift (comment seeding skips
issues that already have notes).

Only for the throwaway instance in docker-compose.yml — the specs say
`create_project: true` and assign root. Plain python3, no dependencies of
its own; like seed.py it calls .venv's python with PYTHONPATH=src (see the
Makefile header for why). Run `make install` and `make up wait` first.
"""

import argparse
import os
import random
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.path.join(ROOT, ".venv", "bin", "python")
COMMENTED = "test/payments"

BOARDS = {
    "payments": (
        "Payments",
        ["Triage", "Doing", "Review", "Blocked", "Shipped"],
        ["bug", "feature", "security", "urgent", "tech-debt"],
    ),
    "mobile-app": (
        "Mobile",
        ["Icebox", "Design", "Build", "QA", "Release", "Done"],
        ["ios", "android", "ux", "crash", "perf"],
    ),
    "infra": (
        "Infra",
        ["Backlog2", "Planned", "In Progress", "Waiting", "Verify"],
        ["terraform", "k8s", "oncall", "cost", "upgrade"],
    ),
    "website": (
        "Website",
        ["Ideas", "Writing", "Editing", "Published"],
        ["seo", "blog", "landing", "a11y", "copy"],
    ),
    "ml-pipeline": (
        "ML Pipeline",
        ["Data", "Training", "Eval", "Deploy", "Monitor", "Parked"],
        ["dataset", "gpu", "drift", "experiment", "paper"],
    ),
}
COLORS = [
    "crimson", "gitlab blue", "teal", "carrot orange", "dark violet",
    "medium sea green", "charcoal", "rose red", "blue gray", "aztec gold",
]
VERBS = [
    "Fix", "Investigate", "Refactor", "Document", "Migrate", "Automate",
    "Benchmark", "Redesign", "Remove", "Upgrade", "Audit", "Prototype",
]
THINGS = [
    "the retry queue", "webhook signatures", "session storage",
    "the nightly job", "rate limiting", "the onboarding flow",
    "stale cache invalidation", "flaky CI on darwin", "the metrics exporter",
    "TLS cert rotation", "the search indexer", "duplicate event delivery",
    "the admin audit log", "cold-start latency", "the feature-flag service",
]
LONG = (
    " across every region we deploy to, including the two legacy "
    "environments nobody wants to touch"
)
DUES = [None, None, None, "2026-08-10", "2026-08-19", "2026-08-25",
        "2026-09-05", "2026-09-30"]
SNIPPETS = [
    "Repro'd on staging — only happens when the retry lands on a cold shard.",
    "Blocked on the vendor ticket, ETA next week.",
    "Half the fix is in !47, the config half still needs review.",
    "@root can you confirm the alert threshold before we close this?",
    "Downgrading urgency — the duplicate rows turned out to be the load test.",
    "Postmortem doc drafted, linking here for the follow-ups.",
    "This regressed again after the 8/15 deploy, reopening the investigation.",
]


def write_spec(slug, board, cols, tags, issues):
    lines = [
        f"project: test/{slug}", f"board: {board}", "create_project: true",
        "", "columns:",
    ]
    for i, col in enumerate(cols):
        lines += [f"  - name: {col}", f"    color: {COLORS[i % len(COLORS)]}"]
    lines += ["", "issues:"]
    for n in range(issues):
        title = f"{random.choice(VERBS)} {random.choice(THINGS)}"
        if n % 9 == 0:
            title += LONG
        title += f" ({slug} #{n + 1})"
        labels = []
        if n % 5 != 0:  # every 5th issue -> Backlog
            labels.append(random.choice(cols))
            if n % 7 == 0:  # a few straddle two columns
                labels.append(random.choice([c for c in cols if c not in labels]))
        labels += random.sample(tags, random.choice([0, 1, 1, 2]))
        lines.append(f"  - title: {title!r}")
        if labels:
            lines.append(f"    labels: {labels!r}")
        if due := random.choice(DUES):
            lines.append(f"    due_date: {due!r}")
        if n % 3 == 0:
            lines.append("    assignee: root")
        if n % 4 == 0:
            lines += [
                "    description: |",
                f"      Notes for {title.lower()}.",
                "      - one concrete step",
                "      - another",
            ]
    path = os.path.join(ROOT, "boards", f"demo-{slug}.yaml")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


COMMENT_SNIPPET = """
import random, sys
from gitboard import client
random.seed(int(sys.argv[1]))
snippets = {snippets!r}
proj = client.gitlab(write=True).projects.get({project!r})
issues = proj.issues.list(state="opened", order_by="created_at", sort="asc",
                          per_page=8, get_all=False)
for issue in issues:
    if issue.notes.list(per_page=1, get_all=False):
        continue  # already commented — keep re-runs idempotent
    for text in random.sample(snippets, random.choice([3, 4, 5])):
        issue.notes.create({{"body": text}})
    print(f"  #{{issue.iid}} commented")
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--issues", type=int, default=30, help="issues per board")
    ap.add_argument("--seed", type=int, default=42, help="RNG seed")
    args = ap.parse_args()

    if not os.path.exists(PYTHON):
        sys.exit("no .venv — run `make install` first")
    random.seed(args.seed)
    env = {**os.environ, "PYTHONPATH": "src"}

    for slug, (board, cols, tags) in BOARDS.items():
        path = write_spec(slug, board, cols, tags, args.issues)
        print(f"applying {os.path.relpath(path, ROOT)}…")
        subprocess.run(
            [PYTHON, "-m", "gitboard.cli", "push", path, "--yes"],
            cwd=ROOT, env=env, check=True,
        )

    print(f"commenting the first issues of {COMMENTED}…")
    snippet = COMMENT_SNIPPET.format(snippets=SNIPPETS, project=COMMENTED)
    subprocess.run(
        [PYTHON, "-c", snippet, str(args.seed)], cwd=ROOT, env=env, check=True
    )
    print("done — try: make tui PROJECT=test/payments")


if __name__ == "__main__":
    main()
