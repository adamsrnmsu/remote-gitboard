"""Tests for edit.py — the spec mutations behind the TUI's card keys."""

from datetime import date

import pytest

from gitboard import edit

TODAY = date(2026, 9, 20)


def spec():
    return {
        "project": "g/p",
        "board": "dev",
        "columns": [{"name": n} for n in ("Doing", "Review", "Verify", "Done", "Failed")],
        "issues": [
            {"title": "one", "iid": 1, "labels": ["Doing", "type::bug"]},
            {"title": "two", "iid": 2, "labels": ["Verify"], "assignee": "ana"},
            {"title": "three", "iid": 3},
        ],
    }  # fmt: skip


def refused(fn, *args, match):
    with pytest.raises(edit.EditError, match=match):
        fn(*args)


def test_move_swaps_only_the_column_label_and_reports_it():
    s = spec()
    assert edit.move(s, 1, "Review") == "#1 Doing → Review"
    assert s["issues"][0]["labels"] == ["Review", "type::bug"]
    assert edit.move(s, 3, "Verify") == "#3 Backlog → Verify"  # into Verify is fine


def test_move_to_backlog_drops_the_column_label():
    s = spec()
    assert edit.move(s, 1, edit.BACKLOG) == "#1 Doing → Backlog"
    assert s["issues"][0]["labels"] == ["type::bug"]


def test_move_refusals_name_the_reason_and_change_nothing():
    s = spec()
    refused(edit.move, s, 2, "Doing", match="verified")  # out of Verify
    refused(edit.move, s, 1, "Done", match="verified")
    refused(edit.move, s, 1, "Failed", match="failed")
    refused(edit.move, s, 1, "Nowhere", match="no column")
    refused(edit.move, s, 1, "Doing", match="already")
    refused(edit.move, s, 9, "Doing", match="#9")
    assert s == spec()


def test_assign_sets_and_reports_old_to_new():
    s = spec()
    assert edit.assign(s, 2, "bob") == "#2 assignee ana → bob"
    assert edit.assign(s, 1, "ana") == "#1 assignee none → ana"
    assert s["issues"][0]["assignee"] == "ana"
    refused(edit.assign, s, 1, "ana", match="already")
    refused(edit.assign, s, 1, "  ", match="username")


def test_set_due_takes_iso_and_plus_days():
    s = spec()
    assert edit.set_due(s, 1, "2026-10-01", TODAY) == "#1 due none → 2026-10-01"
    assert edit.set_due(s, 1, "+3", TODAY) == "#1 due 2026-10-01 → 2026-09-23"
    assert s["issues"][0]["due_date"] == "2026-09-23"
    for bad in ("tomorrow", "2026-13-40", "+x", ""):
        refused(edit.set_due, s, 1, bad, TODAY, match="YYYY-MM-DD")


def test_add_note_appends_once():
    s = spec()
    assert edit.add_note(s, 1, " Q: which branch? ") == "#1 note: Q: which branch?"
    assert s["issues"][0]["notes"] == ["Q: which branch?"]
    refused(edit.add_note, s, 1, "Q: which branch?", match="already")
    refused(edit.add_note, s, 1, "   ", match="empty")


def test_new_card_appends_and_refuses_duplicates_and_verdict_columns():
    s = spec()
    assert edit.new_card(s, " fix login ", "Doing") == "(new) fix login → Doing"
    assert s["issues"][-1] == {"title": "fix login", "labels": ["Doing"]}
    assert edit.new_card(s, "later", edit.BACKLOG) == "(new) later → Backlog"
    assert s["issues"][-1] == {"title": "later"}
    refused(edit.new_card, s, "FIX LOGIN", "Doing", match="already")
    refused(edit.new_card, s, " ", "Doing", match="title")
    refused(edit.new_card, s, "x", "Done", match="verified")


def test_adopt_appends_a_missing_card_once():
    s = spec()
    live = {"title": "four", "iid": 4, "labels": ["Doing"]}
    assert edit.adopt(s, live) is live and s["issues"][-1] is live
    assert edit.adopt(s, {"title": "other", "iid": 4}) is live
    assert len(s["issues"]) == 4


def test_adopt_lists_the_cards_milestone_so_load_still_passes(tmp_path):
    """A card adopted from GitLab carries `milestone:`; without a
    `milestones:` entry the next load() refuses the whole file."""
    from gitboard import apply

    s = spec()
    edit.adopt(s, {"title": "four", "iid": 4, "milestone": "Beta"})
    edit.adopt(s, {"title": "five", "iid": 5, "milestone": "Beta"})
    assert s["milestones"] == [{"title": "Beta"}]
    path = tmp_path / "b.yaml"
    path.write_text(apply.dump(s))
    apply.load(str(path))


def test_columns_and_column_of():
    s = spec()
    assert edit.columns(s) == ["Doing", "Review", "Verify", "Done", "Failed"]
    assert edit.column_of(s, s["issues"][2]) == edit.BACKLOG


def test_a_card_without_a_number_is_found_by_title():
    s = spec()
    edit.new_card(s, "Fix login", "Doing")
    assert edit.move(s, " fix LOGIN ", "Review") == "“fix LOGIN” Doing → Review"
    assert s["issues"][-1]["labels"] == ["Review"]
    assert edit.add_note(s, "Fix login", "hi") == "“Fix login” note: hi"
    refused(edit.assign, s, "nope", "ana", match="“nope” is not on this board")
