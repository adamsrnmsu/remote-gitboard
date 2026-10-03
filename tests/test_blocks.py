"""blocks.py and the team report built from it (contract: perch's
docs/superpowers/specs/2026-10-02-tui-blocks-design.md)."""

import io
import json
from pathlib import Path

import pytest
from team_cases import cases

from gitboard import blocks, stats

GOLDEN = json.loads((Path(__file__).parent / "golden_team_md.json").read_text())
TONES = (None, "good", "warn", "bad", "dim")


def _strs(v, n=None):
    return (
        isinstance(v, list)
        and all(isinstance(x, str) for x in v)
        and (n is None or len(v) == n)
    )


def check(b):
    """The spec's field table; raises AssertionError on a violation."""
    assert b["pi"] == 1 and isinstance(b.get("md", ""), str)
    assert b.get("title") is None or isinstance(b["title"], str)
    k = b["block"]
    if k == "heading":
        assert b["level"] in (1, 2, 3) and isinstance(b["text"], str)
    elif k == "text":
        assert isinstance(b["text"], str) and b.get("tone") in TONES
    elif k == "figures":
        assert b["items"]
        for f in b["items"]:
            assert isinstance(f["label"], str) and isinstance(f["value"], str)
            assert isinstance(f.get("note", ""), str) and f.get("tone") in TONES
    elif k == "table":
        assert _strs(b["columns"]) and all(
            _strs(r, len(b["columns"])) for r in b["rows"]
        )
        assert b.get("align") is None or _strs(b["align"], len(b["columns"]))
    elif k == "bars":
        for label, n in b["items"]:
            assert isinstance(label, str)
            assert isinstance(n, int | float) and not isinstance(n, bool)
    elif k == "list":
        assert _strs(b["items"])
    else:
        pytest.fail(f"unknown block {k}")


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_render_team_md_is_byte_identical_to_pre_blocks_output(name):
    summary, weekly = cases()[name]
    assert stats.render_team_md(summary, weekly=weekly) == GOLDEN[name]


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_team_blocks_satisfy_the_contract(name):
    summary, weekly = cases()[name]
    bl = stats.team_blocks(summary, weekly)
    assert bl[0]["block"] == "heading" and bl[1]["block"] == "figures"
    for b in bl:
        check(b)
        json.dumps(b, ensure_ascii=False)


def test_team_blocks_use_bars_for_counts_and_tables_for_the_rest():
    summary, _ = cases()["plain"]
    kinds = [b["block"] for b in stats.team_blocks(summary)]
    assert kinds.count("bars") == 10 and "table" in kinds and "text" in kinds


def test_constructors_drop_none_and_keep_md():
    assert blocks.heading("Open") == {
        "pi": 1, "block": "heading", "level": 2, "text": "Open",
    }  # fmt: skip
    assert blocks.text("x", "dim", md="X")["md"] == "X"
    assert blocks.figure("A", "1") == {"label": "A", "value": "1"}
    assert blocks.figures([blocks.figure("A", "1")])["items"][0]["label"] == "A"
    t = blocks.table(["a"], [["1"]], title="T", align=["r"])
    assert (t["title"], t["align"]) == ("T", ["r"])
    assert blocks.bars([("a", 2)])["items"] == [["a", 2]]
    assert blocks.bullets(["x"])["block"] == "list"


def test_wanted_reads_env(monkeypatch):
    monkeypatch.delenv("PI_BLOCKS", raising=False)
    assert not blocks.wanted()
    monkeypatch.setenv("PI_BLOCKS", "1")
    assert blocks.wanted()


def test_emit_one_line_per_block_keeping_unicode():
    out = io.StringIO()
    blocks.emit([blocks.heading("Team — x"), blocks.bullets(["a"])], out)
    lines = out.getvalue().splitlines()
    assert len(lines) == 2 and "—" in lines[0]
    assert json.loads(lines[1])["items"] == ["a"]


def test_emit_refuses_non_finite_numbers_and_writes_nothing():
    out = io.StringIO()
    with pytest.raises(ValueError):
        blocks.emit([blocks.heading("x"), blocks.bars([("a", float("nan"))])], out)
    assert out.getvalue() == ""


def test_to_md_each_block_type():
    md = blocks.to_md(
        [
            blocks.heading("Top", 1),
            blocks.text("hello"),
            blocks.figures([blocks.figure("A", "1"), blocks.figure("B", "2")]),
            blocks.figures([blocks.figure("A", "1")], md="**custom**"),
            blocks.heading("Counts", 3),
            blocks.bars([("x", 2), ("y", 1)]),
            blocks.heading("T", 3),
            blocks.table(["a", "b"], [["1", "2"]]),
            blocks.table(["a"], []),
            blocks.bullets(["p", "q"]),
        ]
    )
    assert md == (
        "# Top\n\nhello\n\nA 1 · B 2\n\n**custom**\n\n### Counts\n"
        "| name | n |\n|---|---|\n| x | 2 |\n| y | 1 |\n\n### T\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n\n_none_\n\n- p\n- q"
    )


def test_stats_cli_emits_only_valid_blocks_with_pi_blocks(tmp_path, monkeypatch):
    from test_cli import history_file
    from typer.testing import CliRunner

    from gitboard.cli import app

    monkeypatch.chdir(tmp_path)
    path, _ = history_file(tmp_path)
    monkeypatch.setenv("PI_BLOCKS", "1")
    r = CliRunner().invoke(app, ["stats", "--from", path])
    assert r.exit_code == 0, r.output
    lines = r.stdout.splitlines()
    assert lines and all(line.startswith("{") for line in lines)
    for line in lines:
        check(json.loads(line))
    monkeypatch.delenv("PI_BLOCKS")
    plain = CliRunner().invoke(app, ["stats", "--from", path])
    assert plain.stdout.startswith("# Team — 7 days to 2026-09-14")
