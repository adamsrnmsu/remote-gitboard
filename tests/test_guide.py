"""Tests for guide.py — no key ships without its guide text."""

from gitboard import guide


def keybar_keys():
    rows = guide.rows(False, True) + guide.rows(True, True)
    return {key for row in rows for key, _ in row}


def test_every_keybar_key_has_a_guide_text():
    missing = keybar_keys() - set(guide.GUIDE) - guide.NO_GUIDE
    assert missing == set()
    for title, lines in guide.GUIDE.values():
        assert title and len(lines) >= 2


def test_keys_that_prompt_carry_a_worked_example():
    for key in guide.PROMPTS:
        assert any("Example" in x for x in guide.GUIDE[key][1]), key


def test_rows_offline_drop_the_gitlab_keys_and_plan_needs_a_yaml():
    offline = {k for k, _ in guide.rows(True, True)[0]}
    assert not offline & set("bsmayf") and {"r", "e", "p", "g"} <= offline
    assert "a" not in {k for k, _ in guide.rows(False, False)[0]}
    assert guide.rows(False, False)[1] == guide.CARD_KEYS


def test_panel_is_none_for_keys_without_a_text():
    assert guide.panel("q") is None and guide.panel("v") is not None
