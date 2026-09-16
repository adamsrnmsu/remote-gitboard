"""ingest.py — tasks.md in, board issues out. Pure, no files."""

from gitboard import ingest

SAMPLE = """\
# Verification round 3

## Alice
- [ ] Check the login flow
  Open /login, sign in as a viewer, expect the dashboard.
  Then sign out.
- [x] Confirm the migration ran
  `make migrate` prints nothing.
  **Feedback**
  Ran twice, second run printed a warning about a stale lock.

## Bob Lee
- [ ] Review the API docs
Feedback:
Looks fine, one typo on page 2.
"""

SPEC = {
    "project": "grp/proj",
    "board": "Dev Board",
    "columns": [{"name": "Doing"}],
    "people": {"Alice": "alice"},
    "issues": [],
}


def test_parse_groups_by_person_and_splits_verify_from_feedback():
    tasks = ingest.parse(SAMPLE)
    assert [t["title"] for t in tasks] == [
        "Check the login flow",
        "Confirm the migration ran",
        "Review the API docs",
    ]
    login, migration, docs = tasks
    assert login["person"] == "Alice" and not login["done"]
    assert login["verify"] == (
        "Open /login, sign in as a viewer, expect the dashboard.\nThen sign out."
    )
    assert login["feedback"] == ""
    assert migration["done"]
    assert migration["verify"] == "`make migrate` prints nothing."
    assert migration["feedback"].startswith("Ran twice")
    assert (
        docs["person"] == "Bob Lee"
        and docs["feedback"] == "Looks fine, one typo on page 2."
    )


def fresh():
    return {**SPEC, "columns": [dict(c) for c in SPEC["columns"]], "issues": []}


def test_merge_adds_issues_in_verify_or_done_with_a_source_footer():
    spec = fresh()
    out = ingest.merge(spec, ingest.parse(SAMPLE), "proj-x/tasks.md", "2026-09-15")
    assert out["added"] == 3 and out["notes"] == 2 and out["unmapped"] == ["Bob Lee"]
    assert [c["name"] for c in spec["columns"]] == ["Doing", "Verify", "Done"]
    login, migration, docs = spec["issues"]
    assert login["labels"] == ["Verify"] and login["assignee"] == "alice"
    assert login["description"].endswith("Source: proj-x/tasks.md · Alice · 2026-09-15")
    assert migration["labels"] == ["Done"]
    assert migration["notes"][0].startswith(
        "*feedback from Alice via proj-x/tasks.md:*"
    )
    assert "assignee" not in docs


def test_merge_is_idempotent_and_moves_a_checked_task_to_done():
    spec = fresh()
    tasks = ingest.parse(SAMPLE)
    ingest.merge(spec, tasks, "src", "2026-09-15")
    again = ingest.merge(spec, tasks, "src", "2026-09-16")
    assert again == {"added": 0, "moved": 0, "notes": 0, "unmapped": ["Bob Lee"]}
    assert len(spec["issues"]) == 3

    checked = ingest.parse(
        SAMPLE.replace("- [ ] Check the login flow", "- [x] Check the login flow")
    )
    out = ingest.merge(spec, checked, "src", "2026-09-17")
    assert out["moved"] == 1 and spec["issues"][0]["labels"] == ["Done"]


def test_feedback_already_in_the_pulled_discussion_is_not_staged_again():
    spec = fresh()
    ingest.merge(spec, ingest.parse(SAMPLE), "src", "2026-09-15")
    body = spec["issues"][1].pop("notes")[0]
    spec["issues"][1]["discussion"] = [{"by": "me", "at": "2026-09-15", "body": body}]
    out = ingest.merge(spec, ingest.parse(SAMPLE), "src", "2026-09-16")
    assert out["notes"] == 0 and "notes" not in spec["issues"][1]
