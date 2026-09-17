"""Tests for mail.py — Outlook-safe HTML from the stats dicts."""

import re

from test_stats import COLUMNS, END, NOW, START, history

from gitboard import mail, stats

NO_BG = re.compile(r"<td(?![^>]*bgcolor=)")


def summary():
    return stats.summarise(history(), COLUMNS, START, END, NOW)


def series():
    return stats.daily_series(history(), COLUMNS, START, END)


# --- primitives ---------------------------------------------------------------


def test_bar_row_widths_and_zero():
    tiny = mail.bar_row("a", 1, 100, "#fff")
    half = mail.bar_row("b", 50, 100, "#fff")
    zero = mail.bar_row("c", 0, 100, "#fff")
    assert 'width="4"' in tiny
    assert 'width="190"' in half
    assert zero.count("<td") == 2  # label + value, no bar cell


def test_column_chart_all_zero_has_no_filled_cells():
    s = [{"date": f"2026-09-{d:02d}", "open": 0} for d in range(10, 17)]
    html = mail.column_chart(s, "open", "#3987e5")
    assert "#3987e5" not in html
    assert "Th" in html  # weekday labels still render


def test_column_chart_scales_to_the_peak():
    s = [{"date": "2026-09-14", "open": 2}, {"date": "2026-09-15", "open": 4}]
    html = mail.column_chart(s, "open", "#3987e5", height=80)
    assert 'height="40"' in html and 'height="80"' in html


def test_moves_block_empty_text():
    assert "Inbox zero" in mail.moves_block([])


def test_lists_are_capped_with_more():
    items = [{"iid": n, "title": f"t{n}", "days": 1.0} for n in range(8)]
    html = mail._rows(items, "x", mail._days)
    assert "+3 more" in html and "t7" not in html


# --- pages --------------------------------------------------------------------


def test_every_td_has_bgcolor_in_both_renderers():
    s = summary()
    person = stats.for_person(s, history(), "alice", NOW)
    for html in (
        mail.render_person_html(person, s, "alice", series()),
        mail.render_team_html(s, series()),
        mail.render_index_html([{"name": "alice", "files": {"md": "alice.md"}}]),
    ):
        assert NO_BG.findall(html) == []
        assert 'name="color-scheme" content="dark"' in html
        assert '<body bgcolor="#14171c"' in html


def test_svg_only_when_asked():
    s = summary()
    assert "<svg" not in mail.render_team_html(s, series())
    assert "<svg" in mail.render_team_html(s, series(), svg=True)


def test_headers_panel_shows_in_preview():
    html = mail.render_team_html(
        summary(), series(), headers={"To": "a@x", "Subject": "hi"}
    )
    assert "a@x" in html and "Subject" in html


def test_person_page_leads_with_moves_and_links_cards():
    s = summary()
    person = stats.for_person(s, history(), "alice", NOW)
    html = mail.render_person_html(person, s, "alice", series())
    assert html.index("Your 3 moves") < html.index("Team burndown")
    assert 'href="http://x/4"' in html


def test_index_lists_every_file():
    html = mail.render_index_html(
        [
            {
                "name": "alice",
                "to": "a@x",
                "subject": "s",
                "files": {"md": "alice.md", "eml": "alice.eml"},
            }
        ]
    )
    assert 'href="alice.md"' in html and 'href="alice.eml"' in html


def test_text_is_escaped():
    html = mail.moves_block(
        [{"verb": "Verify", "iid": 1, "title": "<b>x</b>", "age": "1 d", "url": "u"}]
    )
    assert "&lt;b&gt;x&lt;/b&gt;" in html and "<b>x</b>" not in html
