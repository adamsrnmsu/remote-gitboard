"""Tests for mail.py — Outlook-safe HTML from the stats dicts."""

import re

from test_stats import COLUMNS, END, NOW, START, TIGHT, _verified, history

from gitboard import mail, stats

NO_BG = re.compile(r"<td(?![^>]*bgcolor=)")


def summary():
    return stats.summarise(history(), COLUMNS, START, END, NOW)


def series():
    return stats.daily_series(history(), COLUMNS, START, END)


WEEK = {
    "period_end": "2026-09-07T12:00:00+00:00",
    "done": 9,
    "open": 41,
    "verify_queue": 3,
    "verify_median": 1.4,
}


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


def test_sparkline_row_pads_left_without_labels():
    html = mail.sparkline_row("x", [("09-01", 3), ("09-08", 5), ("09-15", 4)], "#fff")
    assert html.count('valign="bottom"') == 8
    tops = html.split("<tr>")[2]  # the value-label row of the chart
    first = tops.split("</td>")[0]
    assert ">3<" not in first and "&nbsp;" in first  # padded column: blank
    assert ">4</span></td></tr>" in html  # last value in the right cell
    assert "–" in mail.sparkline_row("x", [], "#fff")


def test_trend8_needs_two_rows():
    assert "first week logged" in mail._trend8([WEEK])
    html = mail._trend8([WEEK, {**WEEK, "period_end": "2026-09-14T12:00:00+00:00"}])
    assert "09-07" in html and "09-14" in html and "done / week" in html


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


def test_zones_you_before_team_and_team_only_on_team_page():
    s = summary()
    person = stats.for_person(s, history(), "alice", NOW)
    html = mail.render_person_html(person, s, "alice", series())
    assert html.index(">YOU<") < html.index(">TEAM<")
    team = mail.render_team_html(s, series())
    assert ">YOU<" not in team and ">TEAM<" in team
    assert 'href="team.html"' in html


def test_rule_count_grows_with_the_trend():
    s = summary()
    person = stats.for_person(s, history(), "alice", NOW)
    without = mail.render_person_html(person, s, "alice", series())
    with_ = mail.render_person_html(person, s, "alice", series(), weekly=[WEEK, WEEK])
    assert without.count(mail.rule()) == 4
    assert with_.count(mail.rule()) == 5


def test_glance_chips_and_person_shorter_than_team():
    s = summary()
    person = stats.for_person(s, history(), "alice", NOW)
    html = mail.render_person_html(person, s, "alice", series(), weekly=[WEEK])
    assert "moves</span>" in html and "open, team</span>" in html
    assert len(html) < len(mail.render_team_html(s, series(), weekly=[WEEK]))


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


def test_weak_verdicts_name_the_verifier_on_team_and_not_on_their_own_page():
    h = [_verified(1, tasks=(0, 2))]
    s = stats.summarise(h, COLUMNS, START, END, NOW)
    team = mail.render_team_html(s, stats.daily_series(h, COLUMNS, START, END))
    assert "bob · steps 0/2" in team and NO_BG.findall(team) == []
    bob = mail._yours(stats.for_person(s, h, "bob", NOW))
    assert "steps 0/2" in bob and "bob ·" not in bob and NO_BG.findall(bob) == []
    assert "unticked" not in mail._yours(stats.for_person(s, h, "ana", NOW))


def test_tight_dates_show_on_team_and_on_the_owner_only():
    s = summary()
    s["flow"]["tight"] = [TIGHT]
    team = mail.render_team_html(s, series())
    assert "bob · due 2026-09-17 · likely 2026-09-19" in team
    assert NO_BG.findall(team) == []
    bob = mail._yours(stats.for_person(s, history(), "bob", NOW))
    assert "due 2026-09-17 · likely 2026-09-19" in bob and NO_BG.findall(bob) == []
    assert "likely" not in mail._yours(stats.for_person(s, history(), "alice", NOW))
