"""ingest.py — tasks.md in, board issues out. Pure, no files.

The rule under test: a person's `verified` / `failed` comment moves a task;
a `[x]` in the file never does.
"""

from copy import deepcopy

from gitboard import ingest

SAMPLE = """\
commit: abc123
mr: 41
session: run-7

# Verification round 3

Some prose the agent wrapped around it.

## Alice
- [ ] Check the login flow · id: T-login
  Open /login, sign in as a viewer, expect the dashboard.
  - Then sign out.
  evidence: screenshots/login.png
- [x] Confirm the migration ran
  `make migrate` prints nothing.
  **Feedback**
  Ran twice, second run printed a warning about a stale lock.

```
- [ ] Not a task, this is a code sample
```

## Bob Lee
- [ ] Review the API docs (id: T-docs)
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
SRC, DAY = "proj-x/tasks.md", "2026-09-15"


def fresh():
    return deepcopy(SPEC)


def seeded():
    spec = fresh()
    ingest.merge(spec, ingest.parse(SAMPLE), SRC, DAY)
    return spec


def say(entry, body, at="2026-09-16"):
    entry.setdefault("discussion", []).append({"by": "alice", "at": at, "body": body})


# --- parse -----------------------------------------------------------------


def test_parse_header_meta_people_ids_evidence_and_fences():
    tasks = ingest.parse(SAMPLE)
    assert [t["title"] for t in tasks] == [
        "Check the login flow",
        "Confirm the migration ran",
        "Review the API docs",
    ]
    login, migration, docs = tasks
    assert login["meta"] == {"commit": "abc123", "mr": "41", "session": "run-7"}
    assert login["person"] == "Alice" and login["id"] == "T-login"
    assert not login["done"] and migration["done"]
    assert login["verify"] == (
        "Open /login, sign in as a viewer, expect the dashboard.\n- Then sign out."
    )
    assert login["evidence"] == "screenshots/login.png" and login["feedback"] == ""
    assert migration["id"] is None and migration["feedback"].startswith("Ran twice")
    assert docs["person"] == "Bob Lee" and docs["id"] == "T-docs"
    assert docs["feedback"] == "Looks fine, one typo on page 2."


def test_level_one_heading_is_the_document_not_a_person():
    tasks = ingest.parse("# Round 1\n- [ ] Orphan task\n## Cy\n- [ ] Owned [T-1]\n")
    assert [(t["person"], t["id"]) for t in tasks] == [(None, None), ("Cy", "T-1")]


# --- footer -----------------------------------------------------------------


def test_footer_round_trips():
    meta = {"commit": "abc123", "mr": "41", "session": "run-7", "branch": "x"}
    line = ingest.footer_line(SRC, "Alice", DAY, "T-login", meta)
    assert line == (
        "Source: proj-x/tasks.md · Alice · 2026-09-15 · id: T-login · "
        "commit: abc123 · mr: !41 · session: run-7"
    )
    assert ingest.parse_footer(f"steps\n\n{line}") == {
        "source": SRC,
        "person": "Alice",
        "date": DAY,
        "id": "T-login",
        "commit": "abc123",
        "mr": "!41",
        "session": "run-7",
    }
    assert ingest.parse_footer("no footer here") == {}
    assert ingest.footer_line(SRC, None, DAY) == f"Source: {SRC} · unattributed · {DAY}"


# --- merge: adding ----------------------------------------------------------


def test_merge_adds_every_task_to_verify_as_a_task_list_with_footer():
    spec = fresh()
    out = ingest.merge(spec, ingest.parse(SAMPLE), SRC, DAY)
    assert out["added"] == 3 and out["moved"] == 0 and out["notes"] == 2
    assert out["unmapped"] == ["Bob Lee"] and out["changed"]
    assert [c["name"] for c in spec["columns"]] == ["Doing", "Verify"]
    login, migration, docs = spec["issues"]
    assert (
        login["labels"] == ["Verify", "type::verify"] and login["assignee"] == "alice"
    )
    assert login["description"] == (
        "- [ ] Open /login, sign in as a viewer, expect the dashboard.\n"
        "- [ ] Then sign out.\n\n"
        "Evidence: screenshots/login.png\n\n"
        "Source: proj-x/tasks.md · Alice · 2026-09-15 · id: T-login · "
        "commit: abc123 · mr: !41 · session: run-7"
    )
    assert "assignee" not in docs
    assert docs["notes"] == [
        "*feedback from Bob Lee via proj-x/tasks.md:*\n\nLooks fine, one typo on page 2."
    ]
    assert migration["notes"][0].startswith(
        "*feedback from @alice via proj-x/tasks.md:*"
    )
    # [x] without a verdict stays put and is reported
    assert out["unverified"] == ["Confirm the migration ran"]
    assert migration["labels"] == ["Verify", "type::verify"]


def test_verdict_moves_regardless_of_the_checkbox():
    spec = seeded()
    login, migration, _ = spec["issues"]
    say(login, "Verified: works on staging too")  # file says [ ]
    say(migration, "looks good")  # not a verdict
    say(migration, "FAILED\n\nsecond run still warns")  # file says [x]
    out = ingest.merge(spec, ingest.parse(SAMPLE), SRC, "2026-09-16")
    assert out["moved"] == 2 and out["unverified"] == []
    assert login["labels"] == ["type::verify", "Done"] and migration["labels"] == [
        "type::verify",
        "Failed",
    ]
    assert {c["name"]: c.get("color") for c in spec["columns"]} == {
        "Doing": None,
        "Verify": "carrot orange",
        "Done": "medium sea green",
        "Failed": "crimson",
    }
    say(migration, "verified, after !42", at="2026-09-17")
    ingest.merge(spec, ingest.parse(SAMPLE), SRC, "2026-09-17")
    assert migration["labels"] == ["type::verify", "Done"]


def test_reingest_is_a_no_op():
    spec = seeded()
    before = deepcopy(spec)
    out = ingest.merge(spec, ingest.parse(SAMPLE), SRC, "2026-09-16")
    assert spec == before and not out["changed"]
    assert out["added"] == out["moved"] == out["notes"] == out["stale"] == 0


# --- merge: identity ---------------------------------------------------------


def test_id_match_keeps_the_board_title_and_reports_the_rename():
    spec = seeded()
    renamed = SAMPLE.replace(
        "Check the login flow · id: T-login", "Login works · id: T-login"
    )
    out = ingest.merge(spec, ingest.parse(renamed), SRC, "2026-09-16")
    assert out["added"] == 0 and out["retitled"] == [
        ("Check the login flow", "Login works")
    ]
    assert spec["issues"][0]["title"] == "Check the login flow"
    assert "stale" not in spec["issues"][0]["labels"]


def test_near_duplicate_reported_not_added_exact_match_ignores_case():
    spec = seeded()
    near = SAMPLE.replace(
        "- [x] Confirm the migration ran", "- [ ] Confirm the migration runs"
    )
    out = ingest.merge(spec, ingest.parse(near), SRC, "2026-09-16")
    assert out["added"] == 0 and len(spec["issues"]) == 3
    new, old, ratio = out["similar"][0]
    assert (new, old) == ("Confirm the migration runs", "Confirm the migration ran")
    assert ratio >= 0.85
    assert "stale" not in spec["issues"][1].get("labels", [])
    # exact match ignores case and whitespace: matched, not "similar"
    spec = seeded()
    shouty = SAMPLE.replace("Review the API docs (id: T-docs)", "REVIEW  the api docs")
    out = ingest.merge(spec, ingest.parse(shouty), SRC, "2026-09-16")
    assert out["added"] == 0 and out["similar"] == [] and len(spec["issues"]) == 3


# --- merge: lineage ----------------------------------------------------------


def test_task_gone_from_the_file_is_stale_until_it_returns():
    spec = seeded()
    spec["issues"].append({"title": "Hand-made", "labels": ["Doing"]})
    without = SAMPLE.replace("- [ ] Review the API docs (id: T-docs)\n", "")
    out = ingest.merge(spec, ingest.parse(without), SRC, "2026-09-16")
    assert out["stale"] == 1 and len(spec["issues"]) == 4
    assert spec["issues"][2]["labels"] == ["Verify", "type::verify", "stale"]
    assert spec["issues"][3] == {"title": "Hand-made", "labels": ["Doing"]}  # not ours
    assert ingest.merge(spec, ingest.parse(without), SRC, "2026-09-17")["stale"] == 0
    ingest.merge(spec, ingest.parse(SAMPLE), SRC, "2026-09-18")
    assert spec["issues"][2]["labels"] == ["Verify", "type::verify"]


def test_commit_change_flags_reverify_until_a_newer_verdict():
    spec = seeded()
    login = spec["issues"][0]
    say(login, "verified", at="2026-09-15")
    ingest.merge(spec, ingest.parse(SAMPLE), SRC, "2026-09-15")
    assert login["labels"] == ["type::verify", "Done"]

    bumped = SAMPLE.replace("commit: abc123", "commit: def456")
    out = ingest.merge(spec, ingest.parse(bumped), SRC, "2026-09-17")
    assert out["reverify"] == 3  # the header commit covers every task
    assert login["labels"] == ["type::verify", "Done", "re-verify"]
    footer = ingest.parse_footer(login["description"])
    assert footer["commit"] == "def456" and footer["date"] == "2026-09-17"
    assert login["description"].startswith("- [ ] Open /login")  # body untouched

    ingest.merge(spec, ingest.parse(bumped), SRC, "2026-09-18")
    assert "re-verify" in login["labels"]  # old verdict does not clear it
    say(login, "verified again", at="2026-09-18")
    ingest.merge(spec, ingest.parse(bumped), SRC, "2026-09-18")
    assert login["labels"] == ["type::verify", "Done"]


# --- merge: feedback ---------------------------------------------------------


def test_feedback_already_in_the_pulled_discussion_is_not_staged_again():
    spec = seeded()
    body = spec["issues"][1].pop("notes")[0]
    say(spec["issues"][1], body)
    out = ingest.merge(spec, ingest.parse(SAMPLE), SRC, "2026-09-16")
    assert out["notes"] == 0 and "notes" not in spec["issues"][1]


def test_a_verdict_moves_the_card_out_of_every_column():
    spec = {
        **SPEC,
        "columns": [{"name": "Doing"}, {"name": "Review"}],
        "issues": [
            {
                "title": "Check the login flow",
                "labels": ["Review", "priority::high"],
                "discussion": [
                    {"by": "alice", "at": "2026-09-16", "body": "verified: ok"}
                ],
            }
        ],
    }
    ingest.merge(spec, ingest.parse(SAMPLE), "src", "2026-09-16")
    assert spec["issues"][0]["labels"] == ["priority::high", "Done"]
