"""`status` and `report` under PI_BLOCKS=1: blocks that match the text."""

import json
from datetime import UTC, datetime, timedelta

from test_blocks import check
from test_cli import SPEC, edited, write_spec
from typer.testing import CliRunner

from gitboard.cli import app


def blocks(r):
    assert r.exit_code == 0, r.output
    found = [json.loads(line) for line in r.stdout.splitlines()]
    for b in found:
        check(b)
    return found


def test_status_emits_one_table_with_the_text_columns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_spec(tmp_path, spec=edited(), base=False)
    text = CliRunner().invoke(app, ["status"]).stdout
    monkeypatch.setenv("PI_BLOCKS", "1")
    (b,) = blocks(CliRunner().invoke(app, ["status"]))
    assert b["block"] == "table"
    assert b["columns"] == [
        "board",
        "pulled",
        "staged",
        "notes",
        "questions",
        "verify",
        "overdue",
        "snapshot",
    ]
    name = f"{SPEC['project']} · {SPEC['board']}"
    assert b["rows"] == [[name, "never", "-", "1", "-", "-", "1", "never"]]
    assert "questions" in text and "{" not in text


def test_report_emits_figures_movement_tally_in_name_order_and_stuck(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    now = datetime.now(UTC)
    base = {"project": "grp/proj", "board": "Dev Board"}

    def rec(iid, who, col, days):
        ts = (now - timedelta(days=days)).isoformat()
        return {
            **base,
            "iid": iid,
            "title": f"t{iid}",
            "ts": ts,
            "assignee": who,
            "columns": [col],
        }

    lines = [
        rec(1, "zed", "Doing", 4),
        rec(2, "amy", "Doing", 4),
        rec(1, "zed", "Verify", 0),
        rec(2, "amy", "Doing", 0),
        rec(3, "zed", "Doing", 0),
        rec(4, "amy", "Doing", 0),
    ]
    (tmp_path / "snapshots.jsonl").write_text(
        "".join(json.dumps(x) + "\n" for x in lines)
    )
    plain = CliRunner().invoke(app, ["report", "grp/proj"]).stdout
    monkeypatch.setenv("PI_BLOCKS", "1")
    bs = blocks(CliRunner().invoke(app, ["report", "grp/proj"]))
    fig = next(b for b in bs if b["block"] == "figures")
    assert {f["label"]: f["value"] for f in fig["items"]} == {
        "moved": "1",
        "new": "2",
        "closed": "0",
        "unchanged": "1",
    }
    moves, tally = [b for b in bs if b["block"] == "table"]
    assert [r[0] for r in moves["rows"]] == ["#1", "#3", "#4"]
    # zed did more than amy but sorts after her: name order, never a ranking
    assert [r[0] for r in tally["rows"]] == ["amy", "zed"]
    assert bs[-1]["block"] == "list" and bs[-1]["items"] == ["#2 t2  Doing for 4d"]
    # text path unchanged: still rich text, no JSON
    assert "snapshots within 7 day(s)" in plain and "stuck" in plain
    assert not plain.startswith("{")
