"""Tests for cli.py: the drift guard, pull's guards, status, sync, --all.

No network — `board_mod.fetch`, `board_mod.board_columns`, `apply_mod.*`
and `client.gitlab` are patched. Each test runs in an empty cwd with the
config singleton reset, like tests/test_config.py's fixture, so the repo's
own .env and boards/ never leak in.
"""

import io
import json
import os
import re
import shutil
import subprocess
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from gitboard import apply as apply_mod
from gitboard import board as board_mod
from gitboard import cli, client, config
from gitboard import graph as graph_mod
from gitboard.cli import SIGN, STYLE, _changes_table, app
from gitboard.log import THEME

runner = CliRunner()

SPEC = {
    "project": "grp/proj",
    "board": "Dev Board",
    "columns": [{"name": "Doing", "color": "#428bca"}, {"name": "Verify"}],
    "issues": [
        {"title": "one", "iid": 1, "labels": ["Doing"]},
        {"title": "two", "iid": 2, "labels": ["Verify"], "due_date": "2020-01-01"},
    ],
}


class FakeIssue:
    def __init__(self, iid, title, labels):
        self.iid, self.title, self.labels = iid, title, labels
        self.assignee = self.due_date = self.web_url = None


def fake_board(project="grp/proj", name="Dev Board"):
    milestones = types.SimpleNamespace(list=lambda **k: [])
    proj = types.SimpleNamespace(path_with_namespace=project, milestones=milestones)
    return proj, types.SimpleNamespace(name=name)


COLUMNS = [
    ("Backlog", []),
    ("Doing", [FakeIssue(1, "one", ["Doing"])]),
    ("Verify", [FakeIssue(2, "two", ["Verify"])]),
]


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for var in ("GITBOARD_CONFIG", "GITLAB_URL", "GITLAB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GITLAB_READ_TOKEN", "read-tok")
    monkeypatch.setenv("GITLAB_WRITE_TOKEN", "write-tok")
    monkeypatch.chdir(tmp_path)
    # the cached stdout console is 80 wide off a tty; status needs more
    monkeypatch.setattr(cli, "out", lambda: Console(theme=THEME, width=120))
    config.reset()
    yield
    config.reset()


@pytest.fixture
def gl(monkeypatch):
    """The API surface, faked. `calls` records what the writer was asked."""
    calls = {"fetch": [], "apply": [], "plan": []}
    pending = {"plan": []}

    def fetch(path, board_name=None):
        calls["fetch"].append(path)
        return fake_board(path)

    def plan(_gl, spec, base=None):
        calls["plan"].append({"base": base})
        return pending["plan"]

    def apply(_gl, spec, base=None, force=False, on_change=None):
        calls["apply"].append({"base": base, "force": force})
        return [c for c in pending["plan"] if c[0] != "skipped"]

    monkeypatch.setattr(client, "gitlab", lambda write=False: object())
    monkeypatch.setattr(board_mod, "fetch", fetch)
    monkeypatch.setattr(board_mod, "board_columns", lambda p, b: COLUMNS)
    monkeypatch.setattr(apply_mod, "plan", plan)
    monkeypatch.setattr(apply_mod, "apply", apply)
    monkeypatch.setattr(
        apply_mod, "spec_from_board", lambda p, b, c, notes=False: dict(SPEC)
    )
    calls["pending"] = pending
    return calls


def write_spec(tmp_path, name="boards/x.yaml", spec=SPEC, base=True):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(apply_mod.dump(spec))
    if base:
        path.with_name(path.name + ".base").write_text(apply_mod.dump(SPEC))
    return name  # relative: the cwd is tmp_path, and messages stay unwrapped


def edited():
    """SPEC with one staged edit and one staged note."""
    spec = json.loads(json.dumps(SPEC))
    spec["issues"][0]["labels"] = ["Verify"]
    spec["issues"][1]["notes"] = ["looks good"]
    return spec


def snapshot_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def cells(stdout):
    """The grp/proj row of a rich table, split on the column gutters."""
    row = next(line for line in stdout.splitlines() if "grp/proj" in line)
    return re.split(r"\s{2,}", row.strip())


# --- the change table ------------------------------------------------------


def test_sign_and_style_cover_every_kind_and_unknown_kind_survives():
    assert set(SIGN) == set(STYLE) == {"added", "changed", "skipped", "drift", "oneway"}
    assert SIGN["oneway"] == SIGN["drift"] == "!"  # both mean: look before you leap
    assert _changes_table([("weird", "issue", "x")], "t").row_count == 1


# --- pull ------------------------------------------------------------------


def test_pull_base_writes_both_and_a_snapshot(gl, tmp_path):
    r = runner.invoke(app, ["pull", "grp/proj", "--base"])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "boards/proj.yaml").exists()
    assert (tmp_path / "boards/proj.yaml.base").exists()
    recs = snapshot_lines(tmp_path / "snapshots.jsonl")
    assert {rec["iid"] for rec in recs} == {1, 2}


def test_pull_no_snapshot_skips_the_log(gl, tmp_path):
    r = runner.invoke(app, ["pull", "grp/proj", "--no-snapshot"])
    assert r.exit_code == 0, r.output
    assert not (tmp_path / "snapshots.jsonl").exists()


def test_pull_force_refuses_pending_edits(gl, tmp_path):
    path = write_spec(tmp_path, spec=edited())
    r = runner.invoke(app, ["pull", "grp/proj", "--force", "-o", path])
    assert r.exit_code == 1
    assert "has edits not in boards/x.yaml.base" in r.output
    assert apply_mod.load(path) == edited()  # untouched


def test_pull_discard_edits_overwrites_and_rotates_the_base(gl, tmp_path):
    path = write_spec(tmp_path, spec=edited())
    (tmp_path / "boards/x.yaml.base").write_text("project: grp/proj\nboard: old\n")
    args = ["pull", "grp/proj", "--force", "--discard-edits", "--base", "-o", path]
    r = runner.invoke(app, args)
    assert r.exit_code == 0, r.output
    assert apply_mod.load(path) == SPEC
    assert (tmp_path / "boards/x.yaml.base.old").read_text().endswith("board: old\n")
    assert apply_mod.load(path + ".base") == SPEC


def test_pull_force_is_fine_when_the_edits_were_applied(gl, tmp_path):
    """Spec == base means nothing is staged, so a refresh loses nothing."""
    path = write_spec(tmp_path)
    r = runner.invoke(app, ["pull", "grp/proj", "--force", "-o", path])
    assert r.exit_code == 0, r.output


def test_pull_refuses_another_projects_file_regardless_of_flags(gl, tmp_path):
    path = write_spec(tmp_path, spec={**SPEC, "project": "other/proj"}, base=False)
    args = ["pull", "grp/proj", "--force", "--discard-edits", "-o", path]
    r = runner.invoke(app, args)
    assert r.exit_code == 1
    assert "other/proj" in r.output and "not grp/proj" in r.output


# --- push: the drift guard ------------------------------------------------


def test_push_refuses_on_drift(gl, tmp_path):
    path = write_spec(tmp_path, spec=edited())
    gl["pending"]["plan"] = [("drift", "issue", "one: labels")]
    r = runner.invoke(app, ["push", path, "--yes"])
    assert r.exit_code == 1
    assert "1 field(s) changed on GitLab since the pull" in r.output
    assert "--ignore-drift" in r.output
    assert gl["apply"] == []
    assert gl["plan"][0]["base"] == SPEC  # the .base was passed


def test_push_ignore_drift_forces_and_snapshots(gl, tmp_path):
    path = write_spec(tmp_path, spec=edited())
    gl["pending"]["plan"] = [("drift", "issue", "one: labels")]
    r = runner.invoke(app, ["push", path, "--yes", "--ignore-drift"])
    assert r.exit_code == 0, r.output
    assert gl["apply"] == [{"base": SPEC, "force": True}]
    assert (tmp_path / "snapshots.jsonl").exists()


def test_push_without_a_base_passes_none(gl, tmp_path):
    path = write_spec(tmp_path, spec=edited(), base=False)
    gl["pending"]["plan"] = [("changed", "issue", "one: labels")]
    r = runner.invoke(app, ["push", path, "--yes"])
    assert r.exit_code == 0, r.output
    assert gl["apply"] == [{"base": None, "force": False}]


def test_push_with_only_skipped_rows_writes_nothing(gl, tmp_path):
    path = write_spec(tmp_path)
    gl["pending"]["plan"] = [("skipped", "issue", "one: labels")]
    r = runner.invoke(app, ["push", path, "--yes"])
    assert r.exit_code == 0, r.output
    assert gl["apply"] == []
    assert not (tmp_path / "snapshots.jsonl").exists()


def test_plan_base_option_passes_the_base(gl, tmp_path):
    path = write_spec(tmp_path, spec=edited(), base=False)
    other = tmp_path / "elsewhere.base"
    other.write_text(apply_mod.dump(SPEC))
    r = runner.invoke(app, ["plan", path, "--base", str(other)])
    assert r.exit_code == 0, r.output
    assert gl["plan"] == [{"base": SPEC}]


# --- status ----------------------------------------------------------------


def test_status_renders_offline(gl, tmp_path, monkeypatch):
    write_spec(tmp_path, spec=edited())
    monkeypatch.setattr(
        board_mod, "fetch", lambda *a: pytest.fail("status touched the network")
    )
    old = (datetime.now(UTC) - timedelta(days=3)).isoformat(timespec="seconds")
    rec = {"project": "grp/proj", "board": "Dev Board", "iid": 2, "title": "two"}
    (tmp_path / "snapshots.jsonl").write_text(
        json.dumps({**rec, "ts": old, "columns": ["Verify"]}) + "\n"
    )
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0, r.output
    assert cells(r.stdout) == [
        "grp/proj · Dev Board",
        "0m ago",
        "1",
        "1",
        "-",
        "3d",
        "1",
        "3d ago",
    ]


def test_status_without_a_base_shows_dashes(gl, tmp_path):
    write_spec(tmp_path, base=False)
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0, r.output
    assert cells(r.stdout) == [
        "grp/proj · Dev Board",
        "never",
        "-",
        "0",
        "-",
        "-",
        "1",
        "never",
    ]


def talk(by, body, at="2026-09-10"):
    return {"by": by, "at": at, "body": body}


def test_status_counts_questions_nobody_else_answered(gl, tmp_path):
    spec = json.loads(json.dumps(SPEC))
    # answered by someone else: not waiting
    spec["issues"][0]["discussion"] = [talk("bob", "Q: which env?"), talk("me", "prod")]
    # asked, then only the asker again: still waiting
    spec["issues"][1]["discussion"] = [talk("bob", "Q: ok to close?"), talk("bob", "?")]
    write_spec(tmp_path, spec=spec, base=False)
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 0, r.output
    assert cells(r.stdout)[4] == "1"
    # a staged reply answers it; a staged question (marker or not) waits
    spec["issues"][1]["notes"] = ["yes, close it"]
    spec["issues"][0]["notes"] = [f"{apply_mod.MARKER}\n\nQ: and staging?"]
    write_spec(tmp_path, spec=spec, base=False)
    assert cells(runner.invoke(app, ["status"]).stdout)[4] == "1"
    assert cli.waiting_questions(spec["issues"][1]) == 0
    assert cli.waiting_questions(spec["issues"][0]) == 1


# --- sync ------------------------------------------------------------------


def test_sync_yes_happy_path(gl, tmp_path):
    path = write_spec(tmp_path, spec=edited())
    (tmp_path / "boards/issues.jsonl").write_text("")
    gl["pending"]["plan"] = [("changed", "issue", "one: labels")]
    r = runner.invoke(app, ["sync", path, "--yes"])
    assert r.exit_code == 0, r.output
    assert gl["apply"] == [{"base": SPEC, "force": False}]
    # both files move forward to the live board: nothing is staged after a sync
    assert apply_mod.load(path) == SPEC
    assert apply_mod.load(path + ".base") == SPEC
    assert (tmp_path / "boards/x.yaml.base.old").exists()
    assert snapshot_lines(tmp_path / "snapshots.jsonl")
    assert "bd import" in r.output


def test_sync_refuses_drift_and_keeps_the_base(gl, tmp_path):
    path = write_spec(tmp_path, spec=edited())
    gl["pending"]["plan"] = [("drift", "issue", "one: labels")]
    r = runner.invoke(app, ["sync", path, "--yes"])
    assert r.exit_code == 1
    assert not (tmp_path / "boards/x.yaml.base.old").exists()


# --- migrate: the one-way ops a person runs --------------------------------


MIG = {"project": "grp/proj", "board": "Dev Board", "ops": [{"rename_label": {}}]}


@pytest.fixture
def migrate(gl, monkeypatch):
    """A fake gitboard.migrate bound into cli, so these tests cover the
    command, not the ops. `calls["apply"]` is what ran."""
    calls = {"apply": [], "plan": []}
    pending = {"plan": []}

    def plan(_gl, mig):
        calls["plan"].append(mig)
        return pending["plan"]

    def apply(_gl, mig, record):
        ran = [c for c in pending["plan"] if c[0] != "skipped"]
        for c in ran:
            record(*c)
        calls["apply"].append(mig)
        return ran

    fake = types.SimpleNamespace(
        load=lambda path: {**MIG, "file": path},
        plan=plan,
        apply=apply,
        touched=lambda pending: sum(1 for c in pending if c[0] == "oneway"),
    )
    monkeypatch.setattr(cli, "migrate_mod", fake)
    calls["pending"] = pending
    return calls


ONEWAY = [
    ("changed", "rename", "bug -> type::bug"),
    ("oneway", "merge", "p1, urgent -> priority::high (3 cards)"),
    ("skipped", "order", "columns already in order"),
]


def test_migrate_prints_the_plan_and_refuses_without_yes(migrate, tmp_path):
    migrate["pending"]["plan"] = ONEWAY
    r = runner.invoke(app, ["migrate", "boards/x.migration.yaml"], input="n\n")
    assert r.exit_code != 0
    assert "migration boards/x.migration.yaml" in r.output
    assert "priority::high (3 cards)" in r.output
    assert "run 2 op(s), 1 card(s) touched by one-way ops?" in r.output
    assert migrate["apply"] == []
    assert not (tmp_path / "snapshots.jsonl").exists()


def test_migrate_yes_applies_and_snapshots(migrate, gl, tmp_path):
    migrate["pending"]["plan"] = ONEWAY
    r = runner.invoke(app, ["migrate", "boards/x.migration.yaml", "--yes"])
    assert r.exit_code == 0, r.output
    assert migrate["apply"] == [{**MIG, "file": "boards/x.migration.yaml"}]
    assert "2 op(s) run" in r.output
    assert gl["fetch"] == ["grp/proj"]
    assert snapshot_lines(tmp_path / "snapshots.jsonl")


def test_migrate_with_nothing_pending_says_so(migrate, tmp_path):
    migrate["pending"]["plan"] = [("skipped", "rename", "already applied")]
    r = runner.invoke(app, ["migrate", "boards/x.migration.yaml", "--yes"])
    assert r.exit_code == 0, r.output
    assert "nothing to migrate" in r.output
    assert migrate["apply"] == []
    assert not (tmp_path / "snapshots.jsonl").exists()


# --- --all -----------------------------------------------------------------


def test_snapshot_all_iterates_local_specs(gl, tmp_path):
    write_spec(tmp_path, "boards/a.yaml", base=False)
    write_spec(tmp_path, "boards/b.yaml", {**SPEC, "project": "grp/b"}, base=False)
    write_spec(tmp_path, "boards/dup.yaml", base=False)  # same (project, board)
    r = runner.invoke(app, ["snapshot", "--all"])
    assert r.exit_code == 0, r.output
    assert sorted(gl["fetch"]) == ["grp/b", "grp/proj"]


def test_pull_all_refreshes_each_file_in_place(gl, tmp_path):
    write_spec(tmp_path, "boards/a.yaml", edited())
    write_spec(tmp_path, "boards/b.yaml", {**SPEC, "project": "grp/b"}, base=False)
    r = runner.invoke(app, ["pull", "--all"])
    assert r.exit_code == 1  # a.yaml has edits vs its base: refused, b still done
    assert "boards/a.yaml has edits" in r.output
    assert apply_mod.load(str(tmp_path / "boards/b.yaml")) == SPEC


def test_report_all_reads_only_the_log(gl, tmp_path, monkeypatch):
    write_spec(tmp_path, base=False)
    monkeypatch.setattr(board_mod, "fetch", lambda *a: pytest.fail("network"))
    r = runner.invoke(app, ["report", "--all"])
    assert r.exit_code == 0, r.output
    assert "no snapshot of grp/proj" in r.output


def test_report_history_credits_verifiers(gl, tmp_path, monkeypatch):
    """--history adds a verified column from the dump's verdicts; a verifier
    who is not an assignee is listed like an unmatched git author."""
    monkeypatch.setattr(board_mod, "fetch", lambda *a: pytest.fail("network"))
    path, data = history_file(tmp_path)
    data["history"][0]["verdicts"].append(["2026-09-13T00:00:00Z", "carol", "verified"])
    (tmp_path / "h.json").write_text(json.dumps(data))
    now = datetime.now(UTC)
    rec = {"project": "grp/proj", "board": "Dev Board", "iid": 1, "title": "one"}
    lines = [
        {
            **rec,
            "ts": (now - timedelta(days=1)).isoformat(),
            "columns": ["Doing"],
            "assignee": "alice",
        },
        {**rec, "ts": now.isoformat(), "columns": ["Verify"], "assignee": "alice"},
        {
            **rec,
            "iid": 2,
            "title": "two",
            "ts": now.isoformat(),
            "columns": ["Doing"],
            "assignee": "bob",
        },
    ]
    (tmp_path / "snapshots.jsonl").write_text(
        "".join(json.dumps(ln) + "\n" for ln in lines)
    )
    plain = runner.invoke(app, ["report", "grp/proj"])
    assert plain.exit_code == 0, plain.output
    assert "verified" not in plain.stdout
    r = runner.invoke(app, ["report", "grp/proj", "--history", path])
    assert r.exit_code == 0, r.output
    assert "verified" in r.stdout
    assert re.search(r"bob\s+1\s+1\s*\n", r.stdout), r.stdout
    assert "1 verdict(s) by carol matched no assignee" in r.stdout


# --- stats / digest --------------------------------------------------------


def history_file(tmp_path):
    """A tiny --dump: alice owns an open Verify card, bob closed one and
    verified alice's; fetched_at is fixed so the numbers are stable."""
    now = "2026-09-14T07:00:00+00:00"
    data = {
        "project": "grp/proj",
        "board": "Dev Board",
        "columns": ["Doing", "Verify", "Done"],
        "fetched_at": now,
        "history": [
            {
                "iid": 1,
                "title": "one",
                "state": "opened",
                "created_at": "2026-09-08T00:00:00Z",
                "closed_at": None,
                "updated_at": now,
                "assignee": "alice",
                "labels": ["Verify", "epic::auth"],
                "milestone": None,
                "due_date": None,
                "web_url": "http://gl/1",
                "transitions": [["2026-09-10T00:00:00Z", "add", "Verify"]],
                "verdicts": [["2026-09-12T00:00:00Z", "bob", "failed"]],
                "notes": [["2026-09-12T00:00:00Z", "bob", "failed: nope"]],
            },
            {
                "iid": 2,
                "title": "two",
                "state": "closed",
                "created_at": "2026-09-01T00:00:00Z",
                "closed_at": "2026-09-11T00:00:00Z",
                "updated_at": now,
                "assignee": "bob",
                "labels": ["Done"],
                "milestone": "M1",
                "due_date": None,
                "web_url": "http://gl/2",
                "transitions": [],
                "verdicts": [],
                "notes": [],
            },
        ],
    }
    path = tmp_path / "h.json"
    path.write_text(json.dumps(data))
    return str(path), data


def test_stats_from_renders_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(
        board_mod, "fetch_history", lambda *a, **k: pytest.fail("network")
    )
    monkeypatch.setattr(board_mod, "fetch", lambda *a: pytest.fail("network"))
    path, _ = history_file(tmp_path)
    r = runner.invoke(app, ["stats", "--from", path])
    assert r.exit_code == 0, r.output
    assert r.stdout.startswith("# Team — 7 days to 2026-09-14")
    assert "| alice | 1 |" in r.stdout
    j = runner.invoke(app, ["stats", "--from", path, "--json"])
    assert json.loads(j.stdout)["throughput"]["done"] == 1


def test_stats_dump_round_trips_through_from(gl, tmp_path, monkeypatch):
    _, data = history_file(tmp_path)
    seen = []

    def fake(p, b, since):
        seen.append(since.isoformat())
        return data["history"], data["columns"]

    monkeypatch.setattr(board_mod, "fetch_history", fake)
    r = runner.invoke(app, ["stats", "grp/proj", "--dump", "h2.json"])
    assert r.exit_code == 0, r.output
    dumped = json.loads((tmp_path / "h2.json").read_text())
    assert set(dumped) == {
        "project",
        "board",
        "columns",
        "fetched_at",
        "since",
        "milestones",
        "history",
    }
    assert dumped["since"] == seen[0]
    assert dumped["history"] == data["history"]
    again = runner.invoke(app, ["stats", "--from", "h2.json"])
    assert again.exit_code == 0, again.output
    assert again.stdout == r.stdout


def test_stats_history_days_sets_only_the_dump_span(gl, tmp_path, monkeypatch):
    _, data = history_file(tmp_path)
    monkeypatch.setattr(
        board_mod,
        "fetch_history",
        lambda p, b, since: (data["history"], data["columns"]),
    )
    r = runner.invoke(
        app, ["stats", "grp/proj", "--dump", "h3.json", "--history-days", "276"]
    )
    assert r.exit_code == 0, r.output
    dumped = json.loads((tmp_path / "h3.json").read_text())
    span = datetime.fromisoformat(dumped["fetched_at"]) - datetime.fromisoformat(
        dumped["since"]
    )
    assert span.days == 276
    assert r.stdout.startswith("# Team — 7 days to")
    row = json.loads((tmp_path / "reports/stats.jsonl").read_text().splitlines()[-1])
    assert row["days"] == 7


def test_stats_counts_a_verdict_in_the_fetchs_own_second(gl, tmp_path, monkeypatch):
    """gb-6xo: fetched_at kept whole seconds, so 18:30:00.103 fell outside the
    half-open window ending 18:30:00; the dump now keeps the fraction."""
    _, data = history_file(tmp_path)
    card = data["history"][0]
    card.update(
        tasks=[0, 3], verdicts=[["2026-09-14T18:30:00.103Z", "bob", "verified"]]
    )
    card["transitions"] = [["2026-09-14T00:00:00Z", "add", "Verify"]]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 14, 18, 30, 0, 250000, tzinfo=tz)

    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(
        board_mod,
        "fetch_history",
        lambda p, b, since: (data["history"], data["columns"]),
    )
    r = runner.invoke(app, ["stats", "grp/proj", "--dump", "h3.json", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["verify"]["weak"], "verdict in the fetch's second"
    assert "18:30:00.25" in json.loads((tmp_path / "h3.json").read_text())["fetched_at"]


def test_stats_logs_one_row_per_board_week_and_weeks_reads_it_offline(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        board_mod, "fetch_history", lambda *a, **k: pytest.fail("network")
    )
    monkeypatch.setattr(board_mod, "fetch", lambda *a: pytest.fail("network"))
    path, _ = history_file(tmp_path)
    runner.invoke(app, ["stats", "--from", path])
    runner.invoke(app, ["stats", "--from", path])  # same week: no second row
    log = tmp_path / "reports/stats.jsonl"
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["project"] == "grp/proj"
    assert rows[0]["done"] == 1
    r = runner.invoke(app, ["stats", "grp/proj", "--weeks", "8"])
    assert r.exit_code == 0, r.output
    assert r.stdout.startswith("# grp/proj — last 8 weeks")
    assert "| week |" in r.stdout and "2026-09-14" in r.stdout


def test_digest_writes_md_for_everyone_and_eml_where_there_is_an_address(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(board_mod, "fetch", lambda *a: pytest.fail("network"))
    write_spec(tmp_path, spec={**SPEC, "emails": {"alice": "a@x"}}, base=False)
    path, _ = history_file(tmp_path)
    r = runner.invoke(
        app, ["digest", "--from", path, "--sender", "lead@x", "--md-only"]
    )
    assert r.exit_code == 0, r.output
    folder = tmp_path / "reports/2026-09-14/grp-proj"
    assert sorted(p.name for p in folder.iterdir()) == [
        "alice.eml",
        "alice.md",
        "bob.md",
        "team.md",
    ]
    assert (folder / "alice.md").read_text().startswith("# alice")
    eml = (folder / "alice.eml").read_text()
    assert "To: a@x" in eml and "From: lead@x" in eml
    assert "Subject: [grp/proj] week of 2026-09-07 =?utf-8?b?4oCU?= alice" in eml
    assert "reports/2026-09-14/grp-proj/team.md" in r.output


def test_digest_writes_graph_for_milestones_without_edges(tmp_path, monkeypatch):
    monkeypatch.setattr(board_mod, "fetch", lambda *a: pytest.fail("network"))
    write_spec(tmp_path, spec=SPEC, base=False)
    path, data = history_file(tmp_path)
    for c in data["history"]:
        c["milestone"] = None
    data["milestones"] = [{"title": "Later", "due_date": None}]
    with open(path, "w") as f:
        json.dump(data, f)
    r = runner.invoke(app, ["digest", "--from", path])
    assert r.exit_code == 0, r.output
    page = (tmp_path / "reports/2026-09-14/grp-proj/graph.html").read_text()
    assert 'data-key="m:Later"' in page


def test_digest_writes_html_previews_and_a_multipart_eml(tmp_path, monkeypatch):
    import email
    import email.policy

    monkeypatch.setattr(board_mod, "fetch", lambda *a: pytest.fail("network"))
    write_spec(tmp_path, spec={**SPEC, "emails": {"alice": "a@x"}}, base=False)
    path, _ = history_file(tmp_path)
    r = runner.invoke(app, ["digest", "--from", path])
    assert r.exit_code == 0, r.output
    folder = tmp_path / "reports/2026-09-14/grp-proj"
    names = sorted(p.name for p in folder.iterdir())
    assert names == [
        "alice.eml",
        "alice.html",
        "alice.md",
        "bob.html",
        "bob.md",
        "gantt.html",
        "graph.html",  # #2 carries milestone M1, so there is a graph
        "index.html",
        "team.html",
        "team.md",
    ]
    msg = email.message_from_string(
        (folder / "alice.eml").read_text(), policy=email.policy.default
    )
    assert msg.is_multipart()
    assert [p.get_content_type() for p in msg.iter_parts()] == [
        "text/plain",
        "text/html",
    ]
    html = msg.get_body(("html",)).get_content()
    assert "<svg" not in html and "Your 3 moves" in html
    assert "$ gantt --who alice" in html  # the mail itself, not only the preview
    text = msg.get_body(("plain",)).get_content()
    assert "## Timeline" in text and "$ gantt --who alice" in text
    assert "## Timeline" in (folder / "alice.md").read_text()
    preview = (folder / "alice.html").read_text()
    assert "<svg" in preview and "a@x" in preview  # browser copy: chart + headers
    index = (folder / "index.html").read_text()
    assert 'href="alice.eml"' in index and 'href="bob.html"' in index
    assert 'href="graph.html"' in index and 'href="gantt.html"' in index
    assert "$ gantt --project" in (folder / "team.html").read_text()
    assert (folder / "graph.html").read_text().count("<svg") == 1
    assert "index.html" in r.output


# --- config ----------------------------------------------------------------


def test_config_command_survives_a_missing_keychain(monkeypatch):
    monkeypatch.delenv("GITLAB_READ_TOKEN", raising=False)
    monkeypatch.delenv("GITLAB_WRITE_TOKEN", raising=False)

    def no_security(*a, **k):
        raise FileNotFoundError("security")

    monkeypatch.setattr(config.subprocess, "run", no_security)
    r = runner.invoke(app, ["config"])
    # No read token: the table still prints, and the exit code says so, so
    # callers (perch doctor) can turn it into a FIX line.
    assert r.exit_code == 1, r.output
    assert "not found" in r.output
    assert r.exception is None or isinstance(r.exception, SystemExit)


def _estimate_setup(tmp_path, monkeypatch, estimates=""):
    """A spec with one undated card and a dump where alice finished 5 cards."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "boards").mkdir()
    spec = tmp_path / "boards" / "b.yaml"
    spec.write_text(
        "project: g/p\nboard: dev\n"
        + estimates
        + "issues:\n  - title: next\n    assignee: alice\n    labels: [Doing]\n"
    )
    done = [
        {"iid": n, "title": f"d{n}", "state": "closed", "assignee": "alice",
         "created_at": "2026-09-01T00:00:00Z", "closed_at": "2026-09-03T00:00:00Z",
         "updated_at": None, "labels": [], "milestone": None, "due_date": None,
         "web_url": "u", "transitions": [], "verdicts": [], "notes": []}
        for n in range(1, 6)
    ]  # fmt: skip
    dump = tmp_path / "h.json"
    dump.write_text(json.dumps({
        "project": "g/p", "board": "dev", "columns": ["Doing"],
        "fetched_at": "2026-09-14T12:00:00+00:00", "history": done,
    }))  # fmt: skip
    return spec, dump


def test_estimate_stages_a_due_date_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(board_mod, "fetch", lambda *a: pytest.fail("network"))
    spec, dump = _estimate_setup(tmp_path, monkeypatch)
    r = runner.invoke(app, ["estimate", str(spec), "--history", str(dump)])
    assert r.exit_code == 0, r.output
    assert "p85 of 5 cards: alice" in r.output
    due = apply_mod.load(str(spec))["issues"][0]["due_date"]
    assert due == (datetime.now(UTC).date() + timedelta(days=2)).isoformat()


def test_estimate_knob_off_prints_and_leaves_the_file_alone(tmp_path, monkeypatch):
    spec, dump = _estimate_setup(
        tmp_path, monkeypatch, "estimates:\n  suggest_due: false\n"
    )
    before = spec.read_text()
    r = runner.invoke(app, ["estimate", str(spec), "--history", str(dump)])
    assert r.exit_code == 0 and "p85 of 5 cards: alice" in r.output
    assert spec.read_text() == before


def test_split_keys_names_arrows_and_keeps_everything_else_per_char():
    assert cli._split_keys("\x1b[A") == ["up"]
    assert cli._split_keys("\x1bOB\x1b[C\x1b[D") == ["down", "right", "left"]
    assert cli._split_keys("12\r") == ["1", "2", "\r"]
    assert cli._split_keys("\x1b") == ["\x1b"]  # a lone esc still cancels
    assert cli._split_keys("é\x1b[Bq") == ["é", "down", "q"]


def test_move_cursor_walks_cards_and_skips_empty_columns():
    sizes = [2, 0, 3]  # shown cards per column
    assert cli._move_cursor(None, "down", sizes) == (0, 0)
    assert cli._move_cursor((0, 0), "down", sizes) == (0, 1)
    assert cli._move_cursor((0, 1), "down", sizes) == (2, 0)  # over the empty one
    assert cli._move_cursor((2, 0), "up", sizes) == (0, 1)
    assert cli._move_cursor((0, 0), "up", sizes) == (2, 2)  # wraps
    assert cli._move_cursor((2, 2), "left", sizes) == (0, 1)  # row clamped
    assert cli._move_cursor((0, 1), "right", sizes) == (2, 1)
    assert cli._move_cursor((1, 5), "down", sizes) == (0, 0)  # stale cursor resets
    assert cli._move_cursor(None, "down", [0, 0]) is None


def test_find_card_follows_a_resorted_card_and_prefers_its_column():
    def card(iid, title="t"):
        return types.SimpleNamespace(iid=iid, title=title)

    two = card(2)
    columns = [("Doing", [card(1), two]), ("Blocked", [card(2)]), ("Review", [])]
    assert cli._find_card(columns, two, 5, prefer=1) == (1, 0)  # on screen twice
    assert cli._find_card(columns, two, 5, prefer=0) == (0, 1)
    assert cli._find_card(columns, two, 1, prefer=0) == (1, 0)  # hidden in Doing
    assert cli._find_card(columns, card(9), 5) is None
    new = card(None, "fresh")
    assert cli._find_card([("Doing", [card(None, "other"), new])], new, 5) == (0, 1)


# --- graph -----------------------------------------------------------------

GRAPH_SPEC = {
    "project": "grp/proj",
    "board": "Dev Board",
    "columns": [{"name": "Doing"}, {"name": "Blocked"}, {"name": "Verify"}],
    "milestones": [
        {"title": "Beta", "due_date": "2026-11-01"},
        {"title": "GA", "due_date": "2026-12-01"},
    ],
    "issues": [
        {"title": "Token rotation", "iid": 1, "labels": ["Doing", "priority::3"]},
        {
            "title": "Board reader",
            "iid": 2,
            "labels": ["Doing", "priority::1"],
            "milestone": "Beta",
            "blocked_by": [1],
        },
        {
            "title": '<script>alert("x")</script>',
            "iid": 3,
            "labels": ["Blocked"],
            "milestone": "GA",
            "blocked_by": [2, "infra/platform#4"],
        },
        {"title": "Docs", "iid": 4, "labels": ["Doing"], "milestone": "Beta"},
        {"title": "New thing", "milestone": "GA", "blocked_by": ["Docs"]},
    ],
}


def graph_spec(tmp_path, spec=GRAPH_SPEC):
    return write_spec(tmp_path, "boards/g.yaml", spec=spec, base=False)


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(board_mod, "fetch", lambda *a: pytest.fail("network"))
    monkeypatch.setattr(
        board_mod, "fetch_history", lambda *a, **k: pytest.fail("network")
    )


def test_graph_from_file_prints_tree(tmp_path, offline):
    r = runner.invoke(app, ["graph", "--from", graph_spec(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "◆ Beta" in r.stdout and "◆ GA" in r.stdout
    assert r.stdout.index("◆ Beta") < r.stdout.index("◆ GA")  # soonest first
    assert "#1 Token rotation" in r.stdout and "infra/platform#4" in r.stdout
    assert "(see #2 above)" in r.stdout  # #2 is under Beta and blocks #3 in GA
    assert "⚑" in r.stdout  # #1 (P3) blocks #2 (P1): a priority inversion
    # -M narrows the tree to one milestone
    r = runner.invoke(app, ["graph", "--from", graph_spec(tmp_path), "-M", "Beta"])
    assert r.exit_code == 0, r.output
    assert "◆ Beta" in r.stdout and "◆ GA" not in r.stdout


def test_graph_tree_marks_every_flag_kind(tmp_path, offline):
    """⚑ is "the same flags stats lists": a Blocked card whose only blocker
    is closed (blocked_stale) is marked, not just blocker/card pairs."""
    spec = {
        **GRAPH_SPEC,
        "issues": [
            {
                "title": "Stale",
                "iid": 5,
                "labels": ["Blocked"],
                "assignee": "ana",
                "milestone": "Beta",
                "blocked_by": [9],
            }
        ],
    }
    r = runner.invoke(app, ["graph", "--from", graph_spec(tmp_path, spec)])
    assert r.exit_code == 0, r.output
    assert "#5 Stale  @ana ★  ⚑" in r.stdout


def test_graph_mermaid_to_stdout(tmp_path, offline):
    r = runner.invoke(app, ["graph", "--from", graph_spec(tmp_path), "--mermaid"])
    assert r.exit_code == 0, r.output
    assert r.stdout.startswith("flowchart LR\n")
    assert "  i1 --> i2" in r.stdout
    assert "<script>" not in r.stdout  # the hostile title is escaped


def test_graph_html_writes_file(tmp_path, offline):
    r = runner.invoke(
        app, ["graph", "--from", graph_spec(tmp_path), "--html", "g.html"]
    )
    assert r.exit_code == 0, r.output
    assert r.stdout == ""  # the page goes to the file, the path to stderr
    assert "wrote g.html" in r.output
    page = (tmp_path / "g.html").read_text()
    assert page.count("<svg") == 1 and "<script>alert" not in page
    assert "node flag" in page  # the inversion is drawn


def test_graph_unknown_milestone_errors(tmp_path, offline):
    r = runner.invoke(app, ["graph", "--from", graph_spec(tmp_path), "-M", "Nope"])
    assert r.exit_code == 1
    assert "no milestone 'Nope'" in r.output and "Beta, GA" in r.output


def test_graph_empty_board_says_so(tmp_path, offline):
    r = runner.invoke(app, ["graph", "--from", graph_spec(tmp_path, SPEC)])
    assert r.exit_code == 0, r.output
    assert r.stdout == ""
    assert "no blockers or milestones on this board" in r.output


def test_graph_only_empty_milestones_is_drawn(tmp_path, offline):
    spec = {**SPEC, "milestones": [{"title": "Later"}]}
    path = graph_spec(tmp_path, spec)
    r = runner.invoke(app, ["graph", "--from", path])
    assert r.exit_code == 0, r.output
    assert "◆ Later" in r.stdout and "no cards yet" in r.stdout
    r = runner.invoke(app, ["graph", "--from", path, "-M", "Later"])
    assert r.exit_code == 0 and "◆ Later" in r.stdout


def test_graph_live_fetches_history(tmp_path, monkeypatch):
    seen = {}

    def fetch_history(proj, board, since):
        seen["since"] = since
        return graph_mod.cards_from_spec(GRAPH_SPEC, "http://gl"), ["Doing"]

    monkeypatch.setattr(board_mod, "fetch", lambda *a: fake_board())
    monkeypatch.setattr(board_mod, "fetch_history", fetch_history)
    r = runner.invoke(app, ["graph", "grp/proj", "--mermaid"])
    assert r.exit_code == 0, r.output
    assert "  i1 --> i2" in r.stdout
    assert datetime.now(UTC) - seen["since"] > timedelta(days=29)


def test_graph_live_passes_the_late_forecast_to_the_root(tmp_path, monkeypatch):
    monkeypatch.setattr(board_mod, "fetch", lambda *a: fake_board())
    monkeypatch.setattr(
        board_mod,
        "fetch_history",
        lambda *a, **k: (graph_mod.cards_from_spec(GRAPH_SPEC, "http://gl"), ["Doing"]),
    )
    monkeypatch.setattr(
        cli.estimate_mod,
        "late_milestones",
        lambda *a: [{"milestone": "Beta", "days_late": 12}],
    )
    r = runner.invoke(app, ["graph", "grp/proj"])
    assert r.exit_code == 0, r.output
    assert "forecast 12 days late" in r.stdout
    assert r.stdout.count("forecast") == 1  # GA is not late


def test_changes_table_puts_link_and_order_rows_first():
    pending = [
        ("changed", "issue", "a: labels [Doing] -> [Verify]"),
        ("added", "note", "a: hi"),
        ("changed", "order", "#3 after #1"),
        ("changed", "issue", "b: blocked_by [#1] -> []"),
        ("added", "label", "x"),
        ("changed", "link", "c: removed #9"),
        ("added", "note", "b: bye"),
    ]
    table = _changes_table(pending, "t")
    assert [str(c) for c in table.columns[2].cells] == [
        "a: hi",
        "b: bye",
        "#3 after #1",
        "b: blocked_by [#1] -> []",
        "c: removed #9",
        "a: labels [Doing] -> [Verify]",
        "x",
    ]


def test_changes_table_prints_bracketed_values_verbatim():
    """`[#11]` is a rich colour tag; as markup the old blocked_by vanished."""
    from rich.console import Console

    from gitboard.log import THEME

    console = Console(record=True, width=120, color_system=None, theme=THEME)
    console.print(
        _changes_table([("changed", "issue", "d: blocked_by [#11] -> []")], "t")
    )
    assert "d: blocked_by [#11] -> []" in console.export_text()


# --- tui: plan/push carry the .base (gb-0yy) --------------------------------


def _tui_files(tmp_path, base_iids, spec_iids):
    from test_apply import order_spec

    path = tmp_path / "b.yaml"
    path.write_text(apply_mod.dump(order_spec(spec_iids)))
    (tmp_path / "b.yaml.base").write_text(apply_mod.dump(order_spec(base_iids)))
    return apply_mod.load(str(path)), str(path)


def test_tui_plan_with_a_base_shows_the_order_change(monkeypatch, tmp_path):
    from test_apply import ordered_project, use_project

    parsed, path = _tui_files(tmp_path, [1, 2, 3, 4], [4, 1, 2, 3])
    gl = use_project(monkeypatch, ordered_project([1, 2, 3, 4]))
    monkeypatch.setattr(client, "gitlab", lambda write=False: gl)
    pending = cli._staged(parsed, path)
    assert pending == [("changed", "order", "#4 to the top")]
    assert cli._drift_refusal(pending) is None


def test_tui_refuses_on_order_drift(monkeypatch, tmp_path):
    from test_apply import ordered_project, use_project

    parsed, path = _tui_files(tmp_path, [1, 2, 3], [3, 1, 2])
    gl = use_project(monkeypatch, ordered_project([2, 1, 3]))
    monkeypatch.setattr(client, "gitlab", lambda write=False: gl)
    pending = cli._staged(parsed, path)
    assert [c[0] for c in pending] == ["drift"]
    assert "1 field(s) changed on GitLab" in cli._drift_refusal(pending)


def test_tui_push_gets_the_same_base(monkeypatch, tmp_path):
    """The write half of _staged's wiring: apply_mod.plan sees the .base."""
    parsed, path = _tui_files(tmp_path, [1, 2], [2, 1])
    seen = {}
    monkeypatch.setattr(client, "gitlab", lambda write=False: object())
    monkeypatch.setattr(
        apply_mod, "plan", lambda _gl, spec, base=None: seen.update(base=base) or []
    )
    cli._staged(parsed, path, write=True)
    assert seen["base"]["issues"][0]["iid"] == 1


# --- gb-yl3: command branches the happy-path tests skip ----------------------


def test_main_callback_reports_a_bad_config_file_and_exits_1(tmp_path):
    r = runner.invoke(app, ["--config", str(tmp_path / "nope.toml"), "config"])
    assert r.exit_code == 1
    assert "error" in r.output


def test_commands_without_a_project_say_how_to_supply_one(gl):
    for argv in (["show"], ["snapshot"], ["pull"], ["report"], ["graph"]):
        r = runner.invoke(app, argv)
        assert r.exit_code == 1, argv
        assert "no project given" in r.output, argv
        assert "gitboard show group/project" in r.output


def test_commands_without_a_spec_say_how_to_supply_one(gl):
    for argv in (["plan"], ["push"], ["sync"], ["estimate"]):
        r = runner.invoke(app, argv)
        assert r.exit_code == 1, argv
        assert "no spec file given" in r.output, argv


def test_config_default_spec_stands_in_for_the_argument(gl, tmp_path):
    path = write_spec(tmp_path)
    (tmp_path / "gitboard.toml").write_text(f'spec = "{path}"\n')
    r = runner.invoke(app, ["plan"])
    assert r.exit_code == 0, r.output
    assert gl["plan"] == [{"base": SPEC}]


# --- show --------------------------------------------------------------------


def test_show_live_markdown_and_rich(gl, tmp_path):
    md = runner.invoke(app, ["show", "grp/proj", "-m"])
    assert md.exit_code == 0, md.output
    assert gl["fetch"] == ["grp/proj"]
    assert "Doing" in md.stdout and "one" in md.stdout
    write_spec(tmp_path, base=False)
    rich = runner.invoke(app, ["show", "grp/proj", "-n", "1"])
    assert rich.exit_code == 0, rich.output
    assert "Verify (1)" in rich.output
    assert "boards/x.yaml" in rich.output  # the where-to-edit footer


def test_show_from_file_never_fetches(gl, tmp_path, monkeypatch):
    monkeypatch.setattr(board_mod, "fetch", lambda *a: pytest.fail("network"))
    path = write_spec(tmp_path, base=False)
    r = runner.invoke(app, ["show", "--from", path, "-m"])
    assert r.exit_code == 0, r.output
    assert "one" in r.stdout and "two" in r.stdout


def test_show_reports_a_gitlab_problem_as_one_line(monkeypatch):
    def boom(*a):
        raise client.GitlabProblem("no such project: grp/gone")

    monkeypatch.setattr(board_mod, "fetch", boom)
    r = runner.invoke(app, ["show", "grp/gone"])
    assert r.exit_code == 1
    assert "no such project: grp/gone" in r.output


# --- plan --------------------------------------------------------------------


def test_plan_against_diffs_two_files_without_the_network(gl, tmp_path, monkeypatch):
    monkeypatch.setattr(client, "gitlab", lambda write=False: pytest.fail("network"))
    path = write_spec(tmp_path, spec=edited(), base=False)
    other = write_spec(tmp_path, "boards/o.yaml", base=False)
    r = runner.invoke(app, ["plan", path, "--against", other])
    assert r.exit_code == 0, r.output
    assert f"pending against {other}" in r.output
    assert "labels" in r.output


def test_plan_names_the_base_in_the_title_and_passes_it(gl, tmp_path):
    path = write_spec(tmp_path, spec=edited())
    gl["pending"]["plan"] = [("changed", "issue", "one: labels")]
    r = runner.invoke(app, ["plan", path])
    assert r.exit_code == 0, r.output
    assert f"driftiswhatmovedsince{path}.base" in "".join(r.output.split())
    assert gl["plan"] == [{"base": SPEC}]


def test_plan_missing_file_is_a_clean_error(gl):
    r = runner.invoke(app, ["plan", "boards/none.yaml"])
    assert r.exit_code == 1
    assert "no such board file: boards/none.yaml" in r.output
    assert r.exception is None or isinstance(r.exception, SystemExit)


# --- push / sync: the refusals other than drift ------------------------------


def test_push_declined_prompt_aborts_and_writes_nothing(gl, tmp_path):
    path = write_spec(tmp_path, spec=edited())
    gl["pending"]["plan"] = [("changed", "issue", "one: labels")]
    r = runner.invoke(app, ["push", path], input="n\n")
    assert r.exit_code != 0
    assert "push 1 change(s)?" in r.output
    assert gl["apply"] == []
    assert not (tmp_path / "snapshots.jsonl").exists()


def test_push_confirmed_prompt_writes(gl, tmp_path):
    path = write_spec(tmp_path, spec=edited())
    gl["pending"]["plan"] = [("changed", "issue", "one: labels")]
    r = runner.invoke(app, ["push", path], input="y\n")
    assert r.exit_code == 0, r.output
    assert len(gl["apply"]) == 1
    assert "1 change(s) written" in r.output


def test_push_missing_file_exits_1_before_any_connection(monkeypatch):
    monkeypatch.setattr(client, "gitlab", lambda write=False: pytest.fail("network"))
    r = runner.invoke(app, ["push", "boards/none.yaml", "--yes"])
    assert r.exit_code == 1
    assert "no such board file" in r.output


def test_push_gitlab_problem_while_planning_is_one_line(gl, tmp_path, monkeypatch):
    path = write_spec(tmp_path)

    def boom(_gl, spec, base=None):
        raise client.GitlabProblem("cannot reach gitlab")

    monkeypatch.setattr(apply_mod, "plan", boom)
    r = runner.invoke(app, ["push", path, "--yes"])
    assert r.exit_code == 1
    assert "cannot reach gitlab" in r.output
    assert gl["apply"] == []


def test_push_a_refused_write_explains_the_token_scope(gl, tmp_path, monkeypatch):
    import gitlab as gitlab_pkg

    path = write_spec(tmp_path, spec=edited())
    gl["pending"]["plan"] = [("changed", "issue", "one: labels")]

    def forbidden(*a, **k):
        raise gitlab_pkg.exceptions.GitlabAuthenticationError("403", response_code=403)

    monkeypatch.setattr(apply_mod, "apply", forbidden)
    r = runner.invoke(app, ["push", path, "--yes"])
    assert r.exit_code == 1
    assert "needs `api` scope" in r.output


def test_push_with_nothing_pending_prints_nothing_to_write_only_for_skips(gl, tmp_path):
    path = write_spec(tmp_path)
    r = runner.invoke(app, ["push", path, "--yes"])  # an empty plan
    assert r.exit_code == 0, r.output
    assert gl["apply"] == []
    assert "nothing to write" not in r.output


def test_sync_declined_prompt_aborts_before_touching_the_files(gl, tmp_path):
    path = write_spec(tmp_path, spec=edited())
    gl["pending"]["plan"] = [("changed", "issue", "one: labels")]
    r = runner.invoke(app, ["sync", path], input="n\n")
    assert r.exit_code != 0
    assert gl["apply"] == []
    assert apply_mod.load(path)["issues"][0]["labels"] == ["Verify"]  # edit kept
    assert not (tmp_path / "boards/x.yaml.base.old").exists()


def test_sync_with_nothing_to_write_still_refreshes_from_the_live_board(gl, tmp_path):
    path = write_spec(tmp_path, spec=edited())  # plan is empty: fetched here
    r = runner.invoke(app, ["sync", path, "--yes"])
    assert r.exit_code == 0, r.output
    assert gl["apply"] == []
    assert gl["fetch"] == ["grp/proj"]
    assert apply_mod.load(path) == SPEC
    assert "refreshed boards/x.yaml and its .base" in r.output
    assert "bd import" not in r.output  # no issues.jsonl beside it


def test_sync_keeps_discussion_when_the_spec_carried_notes(gl, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        apply_mod,
        "spec_from_board",
        lambda p, b, c, notes=False: seen.append(notes) or dict(SPEC),
    )
    spec = json.loads(json.dumps(SPEC))
    spec["issues"][0]["discussion"] = ["bob: hi"]
    path = write_spec(tmp_path, spec=spec, base=False)
    Path(path + ".base").write_text(apply_mod.dump(spec))
    r = runner.invoke(app, ["sync", path, "--yes"])
    assert r.exit_code == 0, r.output
    assert seen == [True, True]


def test_sync_missing_file_and_gitlab_problem_exit_1(gl, tmp_path, monkeypatch):
    r = runner.invoke(app, ["sync", "boards/none.yaml", "--yes"])
    assert r.exit_code == 1 and "no such board file" in r.output
    path = write_spec(tmp_path)

    def boom(*a):
        raise client.GitlabProblem("board vanished")

    monkeypatch.setattr(board_mod, "fetch", boom)
    r = runner.invoke(app, ["sync", path, "--yes"])
    assert r.exit_code == 1 and "board vanished" in r.output


# --- migrate: the handlers other than the prompt -------------------------------


def test_migrate_missing_file_and_gitlab_problem_exit_1(migrate, monkeypatch):
    def missing(path):
        raise apply_mod.SpecError(f"no such migration: {path}")

    monkeypatch.setattr(cli.migrate_mod, "load", missing)
    r = runner.invoke(app, ["migrate", "boards/none.migration.yaml", "--yes"])
    assert r.exit_code == 1 and "no such migration" in r.output

    def boom(_gl, mig):
        raise client.GitlabProblem("rate limited")

    monkeypatch.setattr(cli.migrate_mod, "load", lambda p: dict(MIG))
    monkeypatch.setattr(cli.migrate_mod, "plan", boom)
    r = runner.invoke(app, ["migrate", "x.yaml", "--yes"])
    assert r.exit_code == 1 and "rate limited" in r.output
    assert migrate["apply"] == []


def test_migrate_confirmed_prompt_runs(migrate, tmp_path):
    migrate["pending"]["plan"] = ONEWAY
    r = runner.invoke(app, ["migrate", "x.yaml"], input="y\n")
    assert r.exit_code == 0, r.output
    assert len(migrate["apply"]) == 1
    assert "2 op(s) run" in r.output


# --- migrate-comments ----------------------------------------------------------


@pytest.fixture
def mc(gl, monkeypatch):
    calls = {"copy": [], "close": []}
    copied = {"n": 2, "closed": True}

    def copy(_gl, path, src, dst, dst_path=None):
        calls["copy"].append((path, src, dst, dst_path))
        return copied["n"]

    def close(_gl, path, iid, superseded_by=()):
        calls["close"].append((path, iid, list(superseded_by)))
        return copied["closed"]

    monkeypatch.setattr(apply_mod, "migrate_comments", copy)
    monkeypatch.setattr(apply_mod, "close_issue", close)
    return calls, copied


def test_migrate_comments_copies_to_iids_and_other_projects(mc):
    calls, _ = mc
    r = runner.invoke(
        app, ["migrate-comments", "1", "2", "grp/other#7", "-p", "grp/proj"]
    )
    assert r.exit_code == 0, r.output
    assert calls["copy"] == [
        ("grp/proj", 1, 2, "grp/proj"),
        ("grp/proj", 1, 7, "grp/other"),
    ]
    assert "2 comment(s) copied #1 -> #2" in r.output
    assert "#1 -> grp/other#7" in r.output
    assert calls["close"] == []


def test_migrate_comments_nothing_to_copy_and_close_source(mc):
    calls, copied = mc
    copied["n"] = 0
    r = runner.invoke(
        app, ["migrate-comments", "1", "2", "-p", "grp/proj", "--close-source"]
    )
    assert r.exit_code == 0, r.output
    assert "nothing to copy to #2" in r.output
    assert calls["close"] == [("grp/proj", 1, ["#2"])]
    assert "closed #1" in r.output
    copied["closed"] = False
    r = runner.invoke(
        app, ["migrate-comments", "1", "2", "-p", "grp/proj", "--close-source"]
    )
    assert "#1 was already closed" in r.output


def test_migrate_comments_bad_destination_is_an_error_before_any_write(mc):
    calls, _ = mc
    r = runner.invoke(app, ["migrate-comments", "1", "grp/x#abc", "-p", "grp/proj"])
    assert r.exit_code == 1
    assert "bad destination 'grp/x#abc'" in r.output
    assert calls["copy"] == []


# --- snapshot ------------------------------------------------------------------


def test_snapshot_single_board_appends_to_the_chosen_log(gl, tmp_path):
    r = runner.invoke(app, ["snapshot", "grp/proj", "-o", "log.jsonl"])
    assert r.exit_code == 0, r.output
    assert "appended to log.jsonl" in r.output
    rows = snapshot_lines(tmp_path / "log.jsonl")
    assert {row["iid"] for row in rows} == {1, 2}
    runner.invoke(app, ["snapshot", "grp/proj", "-o", "log.jsonl"])
    assert len(snapshot_lines(tmp_path / "log.jsonl")) == 4  # appends, never rewrites


def test_snapshot_all_reports_a_failing_board_and_still_does_the_rest(
    gl, tmp_path, monkeypatch
):
    write_spec(tmp_path, "boards/a.yaml", {**SPEC, "project": "grp/a"}, base=False)
    write_spec(tmp_path, "boards/b.yaml", {**SPEC, "project": "grp/b"}, base=False)
    real = board_mod.fetch

    def flaky(path, name=None):
        if path == "grp/a":
            raise client.GitlabProblem("grp/a is gone")
        return real(path, name)

    monkeypatch.setattr(board_mod, "fetch", flaky)
    r = runner.invoke(app, ["snapshot", "--all"])
    assert r.exit_code == 1
    assert "grp/a is gone" in r.output
    assert "of grp/b appended" in r.output


# --- pull: live-fetch branches --------------------------------------------------


def test_pull_defaults_to_boards_named_for_the_project(gl, tmp_path):
    r = runner.invoke(app, ["pull", "grp/proj", "--no-snapshot"])
    assert r.exit_code == 0, r.output
    assert apply_mod.load(str(tmp_path / "boards/proj.yaml")) == SPEC
    assert "wrote boards/proj.yaml" in r.output
    assert "--against" not in r.output
    assert not (tmp_path / "snapshots.jsonl").exists()


def test_pull_refuses_to_clobber_without_force(gl, tmp_path):
    path = write_spec(tmp_path, base=False)
    r = runner.invoke(app, ["pull", "grp/proj", "-o", path])
    assert r.exit_code == 1
    assert "already exists" in r.output and "--force" in r.output


def test_pull_force_over_an_unreadable_file_is_allowed(gl, tmp_path):
    bad = tmp_path / "boards/x.yaml"
    bad.parent.mkdir()
    bad.write_text("project: [unclosed")
    r = runner.invoke(app, ["pull", "grp/proj", "-o", "boards/x.yaml", "--force"])
    assert r.exit_code == 0, r.output
    assert apply_mod.load("boards/x.yaml") == SPEC


def test_pull_base_notes_and_force_refresh_a_clean_file(gl, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        apply_mod,
        "spec_from_board",
        lambda p, b, c, notes=False: seen.append(notes) or dict(SPEC),
    )
    path = write_spec(tmp_path)  # file == base: nothing staged, force is fine
    r = runner.invoke(
        app, ["pull", "grp/proj", "-o", path, "--force", "--base", "--notes"]
    )
    assert r.exit_code == 0, r.output
    assert seen == [True, True]
    assert (tmp_path / "boards/x.yaml.base.old").exists()
    flat = "".join(r.output.split())  # the line wraps
    assert f"gitboardplan{path}`--against{path}.base" in flat


def test_pull_unknown_board_name_is_a_gitlab_problem(monkeypatch):
    def boom(path, name=None):
        raise client.GitlabProblem(f"no board named {name!r}; have: ['Dev Board']")

    monkeypatch.setattr(board_mod, "fetch", boom)
    r = runner.invoke(app, ["pull", "grp/proj", "Nope"])
    assert r.exit_code == 1
    assert "no board named 'Nope'" in r.output


# --- report: the non-history paths -----------------------------------------------


def log_snapshots(tmp_path, rows):
    (tmp_path / "snapshots.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows)
    )


def snap(iid, columns, ts, assignee="alice", title=None):
    return {
        "project": "grp/proj",
        "board": "Dev Board",
        "iid": iid,
        "title": title or f"t{iid}",
        "ts": ts.isoformat(timespec="seconds"),
        "columns": columns,
        "assignee": assignee,
    }


def test_report_with_no_log_says_to_run_snapshot(gl):
    r = runner.invoke(app, ["report", "grp/proj"])
    assert r.exit_code == 0, r.output
    assert "no snapshot of grp/proj within 7 day(s)" in r.output


def test_report_with_one_snapshot_asks_for_a_second(gl, tmp_path):
    now = datetime.now(UTC)
    log_snapshots(tmp_path, [snap(1, ["Doing"], now)])
    r = runner.invoke(app, ["report", "grp/proj"])
    assert r.exit_code == 0, r.output
    assert "need two snapshots of grp/proj" in r.output


def test_report_lists_moved_new_closed_and_the_tally(gl, tmp_path):
    now = datetime.now(UTC)
    before = now - timedelta(days=1)
    log_snapshots(
        tmp_path,
        [
            snap(1, ["Doing"], before),
            snap(2, ["Doing"], before, "bob"),
            snap(4, ["Doing"], before, "bob"),
            snap(1, ["Verify"], now),
            snap(2, ["Doing"], now, "bob"),
            snap(3, ["Backlog"], now, "carol"),
        ],
    )
    r = runner.invoke(app, ["report", "grp/proj"])
    assert r.exit_code == 0, r.output
    assert "2 snapshots within 7 day(s)" in r.output
    assert "Doing -> Verify" in r.output
    assert "#3" in r.output and "new in Backlog" in r.output
    assert "#4" in r.output and "closed" in r.output
    assert "1 issue(s) did not move" in r.output
    assert re.search(r"alice\s+1", r.output)


def test_report_with_no_movement_says_so(gl, tmp_path):
    now = datetime.now(UTC)
    log_snapshots(
        tmp_path, [snap(1, ["Doing"], now - timedelta(days=1)), snap(1, ["Doing"], now)]
    )
    r = runner.invoke(app, ["report", "grp/proj"])
    assert "no movement in the window" in r.output


def test_report_flags_a_card_stuck_past_its_columns_threshold(gl, tmp_path):
    now = datetime.now(UTC)
    days = [now - timedelta(days=d) for d in (60, 30, 0)]
    log_snapshots(tmp_path, [snap(1, ["Verify"], ts) for ts in days])
    r = runner.invoke(app, ["report", "grp/proj", "-d", "90"])
    assert r.exit_code == 0, r.output
    assert "stuck" in r.output
    assert "#1" in r.output and "Verify for" in r.output


def test_report_since_without_a_base_is_an_error(gl, tmp_path):
    path = write_spec(tmp_path, base=False)
    r = runner.invoke(app, ["report", "--since", path])
    assert r.exit_code == 1
    assert f"no {path}.base" in r.output and "pull --base" in r.output


def test_report_since_the_pull_reads_the_window_from_the_base(gl, tmp_path):
    path = write_spec(tmp_path)  # project comes from the spec
    now = datetime.now(UTC)
    log_snapshots(
        tmp_path,
        [
            snap(1, ["Doing"], now + timedelta(hours=1)),
            snap(1, ["Verify"], now + timedelta(hours=2)),
        ],
    )
    r = runner.invoke(app, ["report", "--since", path])
    assert r.exit_code == 0, r.output
    assert "since the pull at" in r.output
    assert "Doing -> Verify" in r.output


def test_report_repo_matches_authors_and_lists_the_rest(gl, tmp_path, monkeypatch):
    now = datetime.now(UTC)
    log_snapshots(
        tmp_path,
        [snap(1, ["Doing"], now - timedelta(days=1)), snap(1, ["Verify"], now)],
    )
    monkeypatch.setattr(
        cli.report_mod,
        "commit_counts",
        lambda repo, days: {("Alice A", "a@x"): 3, ("Zed", "z@x"): 2},
    )
    monkeypatch.setattr(
        cli.report_mod,
        "match_author",
        lambda name, authors: next((k for k in authors if name in k[0].lower()), None),
    )
    r = runner.invoke(app, ["report", "grp/proj", "--repo", "."])
    assert r.exit_code == 0, r.output
    assert "commits" in r.output
    assert re.search(r"alice\s+1\s+3", r.output)
    assert "2 commit(s) by Zed <z@x> matched no assignee" in r.output


# --- config --------------------------------------------------------------------


def test_config_lists_every_source_and_exits_0_with_tokens(tmp_path):
    (tmp_path / "gitboard.toml").write_text(
        'url = "https://gl.example"\nproject = "g/p"\nboard = "B"\nspec = "s.yaml"\n'
    )
    r = runner.invoke(app, ["config"])
    assert r.exit_code == 0, r.output
    flat = "".join(r.output.split())  # a long tmp path wraps
    assert "configfile/" in flat  # a path, not "none"
    for text in ("https://gl.example", "g/p"):
        assert text in flat
    assert "unset" not in flat  # board and spec are set too
    assert "found" in r.output
    assert "not found" not in r.output
    assert "unset — push reuses" not in r.output  # a write token is set


def test_config_without_a_write_token_says_push_reuses_the_read_one(monkeypatch):
    monkeypatch.delenv("GITLAB_WRITE_TOKEN", raising=False)
    monkeypatch.setattr(
        config.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout=""),
    )
    r = runner.invoke(app, ["config"])
    assert r.exit_code == 0, r.output
    assert "unset — push reuses the read token" in r.output
    assert r.output.count("unset") == 4  # project, board, spec, write token


# --- estimate / stats: the live and empty branches --------------------------------


def test_estimate_with_no_history_says_nothing_to_estimate(tmp_path, monkeypatch):
    spec, dump = _estimate_setup(tmp_path, monkeypatch)
    data = json.loads(dump.read_text())
    data["history"] = []
    dump.write_text(json.dumps(data))
    before = spec.read_text()
    r = runner.invoke(app, ["estimate", str(spec), "--history", str(dump)])
    assert r.exit_code == 0, r.output
    assert "nothing to estimate" in r.output
    assert spec.read_text() == before


def test_estimate_live_fetches_history(tmp_path, monkeypatch):
    spec, dump = _estimate_setup(tmp_path, monkeypatch)
    data = json.loads(dump.read_text())
    monkeypatch.setattr(board_mod, "fetch", lambda *a: fake_board("g/p", "dev"))
    monkeypatch.setattr(
        board_mod, "fetch_history", lambda *a, **k: (data["history"], ["Doing"])
    )
    monkeypatch.setattr(board_mod, "active_milestones", lambda p: [])
    r = runner.invoke(app, ["estimate", str(spec)])
    assert r.exit_code == 0, r.output
    assert "due date(s) staged" in r.output


def live_history(monkeypatch, tmp_path):
    path, data = history_file(tmp_path)
    monkeypatch.setattr(board_mod, "fetch", lambda *a: fake_board())
    monkeypatch.setattr(
        board_mod, "fetch_history", lambda *a, **k: (data["history"], data["columns"])
    )
    monkeypatch.setattr(board_mod, "active_milestones", lambda p: [])
    return path


def test_stats_live_dump_and_json(gl, tmp_path, monkeypatch):
    live_history(monkeypatch, tmp_path)
    r = runner.invoke(app, ["stats", "grp/proj", "--dump", "d.json", "--json"])
    assert r.exit_code == 0, r.output
    assert "wrote d.json" in r.output
    assert json.loads((tmp_path / "d.json").read_text())["project"] == "grp/proj"
    assert (tmp_path / "reports/stats.jsonl").exists()


def test_stats_weeks_without_history_still_renders_a_heading(gl, tmp_path):
    r = runner.invoke(app, ["stats", "grp/proj", "--weeks", "4"])
    assert r.exit_code == 0, r.output
    assert "grp/proj — last 4 weeks" in r.stdout


# --- tui: the interactive loop, driven by scripted keys -----------------------
#
# `_key` is the only input and `rich.live.Live` the only output, so both are
# faked: keys come from a list, and every redraw is rendered to text and kept.
# Each script ends with "q"; running out of keys fails the test loudly.


class FakeLive:
    frames: list = []

    def __init__(self, console=None, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def update(self, renderable, refresh=False):
        c = Console(width=120, file=io.StringIO(), theme=THEME)
        c.print(renderable)
        FakeLive.frames.append(c.file.getvalue())

    def stop(self):
        pass

    def start(self, refresh=False):
        pass


class Tui:
    """`run(keys, *argv)` -> the CliRunner result; `.frames` are the redraws."""

    def __init__(self, monkeypatch):
        import signal

        import rich.live

        self.mp = monkeypatch
        FakeLive.frames = []
        self.keys = []
        monkeypatch.setattr(rich.live, "Live", FakeLive)
        monkeypatch.setattr(signal, "signal", lambda *a: None)
        monkeypatch.setattr(
            cli,
            "sys",
            types.SimpleNamespace(stdin=types.SimpleNamespace(isatty=lambda: True)),
        )
        self.errbuf = io.StringIO()
        monkeypatch.setattr(
            cli,
            "err",
            lambda: Console(file=self.errbuf, width=120, height=50, theme=THEME),
        )
        monkeypatch.setattr(cli, "_key", self._key)
        monkeypatch.setattr(subprocess, "call", lambda argv: self.edits.append(argv))
        self.edits = []
        self.on_edit = None

    def _key(self):
        assert self.keys, "script ran out of keys (no q?)"
        return self.keys.pop(0)

    def run(self, keys, *argv):
        self.keys = list(keys)
        r = runner.invoke(app, ["tui", *argv])
        assert r.exit_code == 0, (r.output, r.exception)
        assert not self.keys, f"unused keys {self.keys}"
        return r

    @property
    def last(self):
        return FakeLive.frames[-1]

    @property
    def text(self):
        return "\n".join(FakeLive.frames)


@pytest.fixture
def tui(monkeypatch):
    return Tui(monkeypatch)


def typed(s):
    return list(s) + ["\r"]


def test_tui_refuses_without_a_terminal(gl):
    r = runner.invoke(app, ["tui", "grp/proj"])
    assert r.exit_code == 1
    assert "tui needs a terminal" in r.output


def test_tui_offline_arrows_select_wrap_and_esc_drops(tui, tmp_path):
    path = write_spec(tmp_path)
    tui.run(["down", "q"], "--from", path, "--no-guide")
    assert "▶ " in tui.last and "one" in tui.last.split("▶ ")[1].splitlines()[0]
    tui.run(["down", "down", "q"], "--from", path, "--no-guide")  # one -> two
    assert "two" in tui.last.split("▶ ")[1].splitlines()[0]
    tui.run(["down", "down", "down", "q"], "--from", path, "--no-guide")  # wraps
    assert "one" in tui.last.split("▶ ")[1].splitlines()[0]
    tui.run(["j", "l", "h", "k", "q"], "--from", path, "--no-guide")
    assert "▶ " in tui.last
    tui.run(["down", "\x1b", "q"], "--from", path, "--no-guide")
    assert "▶ " not in tui.last


def test_tui_offline_move_stages_into_the_yaml_and_refetches(tui, tmp_path):
    path = write_spec(tmp_path)
    # card one (Doing) -> pick 1 = Backlog
    tui.run(["down", "v", "1", "q"], "--from", path, "--no-guide")
    spec = apply_mod.load(path)
    assert "labels" not in spec["issues"][0]
    assert "staged: #1 Doing → Backlog · p shows, the host pushes" in tui.last
    assert "staged this session (1)" in tui.last
    assert "Backlog (1)" in tui.last  # the reload moved the card


def test_tui_move_out_of_verify_is_refused_with_the_reason(tui, tmp_path):
    path = write_spec(tmp_path)
    tui.run(["down", "down", "v", "1", "q"], "--from", path, "--no-guide")  # card two
    assert "it leaves on a `verified:` or `failed:` comment" in tui.last
    assert apply_mod.load(path)["issues"][1]["labels"] == ["Verify"]


def test_tui_move_cancelled_by_a_key_that_is_not_a_choice(tui, tmp_path):
    path = write_spec(tmp_path)
    before = (tmp_path / path).read_text()
    tui.run(["down", "v", "x", "q"], "--from", path, "--no-guide")
    assert "cancelled" in tui.last
    assert (tmp_path / path).read_text() == before


def test_tui_card_key_without_selection_asks_for_a_number(tui, tmp_path):
    path = write_spec(tmp_path)
    tui.run(["v", *typed("9"), "q"], "--from", path, "--no-guide")
    assert "#9 is not on this board" in tui.last
    # a typed number that exists works like a selection; backspace edits it
    tui.run(
        ["c", "5", "\x7f", "1", "\r", *typed("ship it"), "q"],
        "--from",
        path,
        "--no-guide",
    )
    assert apply_mod.load(path)["issues"][0]["notes"] == ["ship it"]
    # esc at the number prompt cancels, and so does an empty enter
    tui.run(["v", "\x1b", "q"], "--from", path, "--no-guide")
    assert "cancelled" in tui.last
    tui.run(["u", "\r", "q"], "--from", path, "--no-guide")
    assert "cancelled" in tui.last


def test_tui_assign_types_a_username_or_picks_from_people(tui, tmp_path):
    spec = {**SPEC, "people": {"Al": "alice"}}
    path = write_spec(tmp_path, spec=spec)
    tui.run(["down", "u", "t", *typed("@bob"), "q"], "--from", path, "--no-guide")
    assert apply_mod.load(path)["issues"][0]["assignee"] == "bob"
    assert "assignee none → bob" in tui.last
    # now bob is on the board, so he is the numbered choice with alice
    tui.run(["down", "u", "2", "q"], "--from", path, "--no-guide")
    assert apply_mod.load(path)["issues"][0]["assignee"] == "bob"
    tui.run(["down", "u", "1", "q"], "--from", path, "--no-guide")
    assert apply_mod.load(path)["issues"][0]["assignee"] == "alice"
    tui.run(["down", "u", "t", "\x1b", "q"], "--from", path, "--no-guide")
    assert "cancelled" in tui.last


def test_tui_due_date_plus_n_bad_text_and_offline_estimate(tui, tmp_path):
    path = write_spec(tmp_path)
    tui.run(["down", "d", *typed("+3"), "q"], "--from", path, "--no-guide")
    want = (datetime.now(UTC).date() + timedelta(days=3)).isoformat()
    assert apply_mod.load(path)["issues"][0]["due_date"] == want
    tui.run(["down", "d", *typed("soon"), "q"], "--from", path, "--no-guide")
    assert "a date is YYYY-MM-DD" in tui.last
    tui.run(["down", "d", "e", "q"], "--from", path, "--no-guide")
    assert "offline — no history here; type a date" in tui.last
    tui.run(["down", "d", "\x1b", "q"], "--from", path, "--no-guide")
    assert "cancelled" in tui.last


def test_tui_comment_stages_once_and_refuses_the_duplicate(tui, tmp_path):
    path = write_spec(tmp_path)
    tui.run(["down", "c", *typed("lgtm"), "q"], "--from", path, "--no-guide")
    assert apply_mod.load(path)["issues"][0]["notes"] == ["lgtm"]
    tui.run(["down", "c", *typed("lgtm"), "q"], "--from", path, "--no-guide")
    assert "already stages that comment" in tui.last
    tui.run(["down", "c", "\r", "q"], "--from", path, "--no-guide")
    assert "cancelled" in tui.last


def test_tui_new_card_lands_in_the_picked_column_and_blank_title_cancels(tui, tmp_path):
    path = write_spec(tmp_path)
    tui.run(["n", *typed("fresh"), "2", "q"], "--from", path, "--no-guide")
    issues = apply_mod.load(path)["issues"]
    assert issues[-1] == {"title": "fresh", "labels": ["Doing"]}
    assert "(new) fresh → Doing" in tui.last
    tui.run(["n", "\x1b", "q"], "--from", path, "--no-guide")
    assert "cancelled" in tui.last
    tui.run(["n", *typed("fresh"), "2", "q"], "--from", path, "--no-guide")
    assert "already here" in tui.last
    # a card with no number is named by its title
    tui.run(["down", "down", "c", *typed("hi"), "q"], "--from", path, "--no-guide")
    assert apply_mod.load(path)["issues"][-1]["notes"] == ["hi"]
    assert "“fresh” note: hi" in tui.last


def test_tui_guide_toggle_help_and_unknown_keys(tui, tmp_path):
    path = write_spec(tmp_path)
    tui.run(["v", "\x1b", "g", "g", "?", "z", "q"], "--from", path)
    assert "guide — " in tui.text  # v showed its guide
    assert "guide off — g brings it back" in tui.text
    assert "guide on — press a key" in tui.text
    assert "refetch the board" in tui.text  # the ? panel
    assert "refetch the board" not in tui.last  # z cleared it


def test_tui_offline_hides_the_online_keys(tui, tmp_path):
    path = write_spec(tmp_path)
    for key in "sbma":
        tui.run([key, "q"], "--from", path, "--no-guide")
        assert "offline — not here; the host does that" in tui.last, key
    assert "offline —" in tui.last  # and the subtitle says so


def test_tui_reload_picks_up_an_outside_edit(tui, tmp_path):
    path = write_spec(tmp_path)
    orig_key = tui._key

    def key():
        if tui.keys[0] == "r":  # someone edits the file just before the reload
            spec = apply_mod.load(path)
            spec["issues"].append({"title": "from outside"})
            (tmp_path / path).write_text(apply_mod.dump(spec))
        return orig_key()

    tui.mp.setattr(cli, "_key", key)
    tui.run(["r", "q"], "--from", path, "--no-guide")
    assert "from outside" in tui.last


def test_tui_edit_shows_the_diff_against_the_base_or_says_there_is_none(tui, tmp_path):
    path = write_spec(tmp_path)
    tui.mp.setenv("VISUAL", "myedit --wait")

    def edit(argv):
        spec = apply_mod.load(path)
        spec["issues"][0]["labels"] = ["Verify"]
        (tmp_path / path).write_text(apply_mod.dump(spec))

    tui.mp.setattr(
        subprocess, "call", lambda argv: (tui.edits.append(argv), edit(argv))
    )
    tui.run(["e", "q"], "--from", path, "--no-guide")
    assert tui.edits == [["myedit", "--wait", path]]
    assert "the host pushes" in tui.last and "one: labels" in tui.last
    # no .base: nothing to diff against
    (tmp_path / (path + ".base")).unlink()
    tui.run(["e", "q"], "--from", path, "--no-guide")
    assert f"edited; no {path}.base to diff against" in tui.last


def test_tui_edit_with_no_change_says_the_board_matches(tui, tmp_path):
    path = write_spec(tmp_path)
    tui.mp.delenv("VISUAL", raising=False)
    tui.mp.delenv("EDITOR", raising=False)
    tui.run(["e", "q"], "--from", path, "--no-guide")
    assert tui.edits == [["vim", path]]
    assert "no changes — board already matches" in tui.last


def test_tui_offline_p_diffs_and_never_writes(tui, tmp_path):
    path = write_spec(tmp_path, spec=edited())
    tui.run(["p", "q"], "--from", path, "--no-guide")
    assert "the host pushes" in tui.last and "labels" in tui.last
    clean = write_spec(tmp_path, "boards/c.yaml")
    tui.run(["p", "q"], "--from", clean, "--no-guide")
    assert "no changes — board already matches" in tui.last
    (tmp_path / "boards/c.yaml.base").unlink()
    tui.run(["p", "q"], "--from", clean, "--no-guide")
    assert "no boards/c.yaml.base to diff against" in tui.last


# --- tui: online (GitLab faked) --------------------------------------------------


@pytest.fixture
def live_tui(tui, gl, monkeypatch, tmp_path):
    boards = [
        types.SimpleNamespace(name="Dev Board"),
        types.SimpleNamespace(name="Other"),
    ]

    def fetch(path, name=None):
        gl["fetch"].append((path, name))
        proj = types.SimpleNamespace(
            path_with_namespace=path,
            boards=types.SimpleNamespace(list=lambda **k: boards),
        )
        return proj, types.SimpleNamespace(name=name or "Dev Board")

    monkeypatch.setattr(board_mod, "fetch", fetch)
    write_spec(tmp_path)  # boards/x.yaml defines grp/proj, so a/p exist
    return tui


def test_tui_online_reload_snapshot_and_board_switch(live_tui, gl, tmp_path):
    live_tui.run(["r", "s", "b", "2", "q"], "grp/proj", "--no-guide")
    assert gl["fetch"] == [
        ("grp/proj", None),  # first read
        ("grp/proj", None),  # r
        ("grp/proj", "Other"),  # b, 2
    ]
    assert len(snapshot_lines(tmp_path / "snapshots.jsonl")) == 2
    assert "defined by boards/x.yaml" in live_tui.text


def test_tui_board_switch_cancel_and_nothing_to_switch_to(
    live_tui, gl, tmp_path, monkeypatch
):
    live_tui.run(["b", "x", "q"], "grp/proj", "--no-guide")
    assert "cancelled" in live_tui.last
    one = [types.SimpleNamespace(name="Dev Board")]

    def fetch(path, name=None):
        proj = types.SimpleNamespace(
            path_with_namespace=path, boards=types.SimpleNamespace(list=lambda **k: one)
        )
        return proj, types.SimpleNamespace(name="Dev Board")

    monkeypatch.setattr(board_mod, "fetch", fetch)
    live_tui.run(["b", "q"], "grp/proj", "--no-guide")
    assert "nothing else to switch to" in live_tui.last


def test_tui_online_plan_push_branches(live_tui, gl, tmp_path):
    # empty plan
    live_tui.run(["p", "a", "q"], "grp/proj", "--no-guide")
    assert "no changes — board already matches" in live_tui.last
    # p shows the table, a refuses on drift
    gl["pending"]["plan"] = [("drift", "issue", "one: labels")]
    live_tui.run(["p", "q"], "grp/proj", "--no-guide")
    assert "a pushes" in live_tui.last
    live_tui.run(["a", "q"], "grp/proj", "--no-guide")
    assert "1 field(s) changed on GitLab since the pull" in live_tui.last
    assert "refused" in live_tui.last
    assert gl["apply"] == []
    # a clean write: n declines, y writes and clears the staged panel
    gl["pending"]["plan"] = [("changed", "issue", "one: labels")]
    live_tui.run(["a", "n", "q"], "grp/proj", "--no-guide")
    assert "not pushed" in live_tui.last and gl["apply"] == []
    live_tui.run(["a", "y", "q"], "grp/proj", "--no-guide")
    assert "push 1 change(s)?  y / n" in live_tui.text
    assert "1 change(s) written" in live_tui.last
    assert gl["apply"] == [{"base": SPEC, "force": False}]


def test_tui_online_push_refused_by_the_token_stays_up_and_says_why(
    live_tui, gl, monkeypatch
):
    import gitlab as gitlab_pkg

    gl["pending"]["plan"] = [("changed", "issue", "one: labels")]

    def forbidden(*a, **k):
        raise gitlab_pkg.exceptions.GitlabAuthenticationError("403", response_code=403)

    monkeypatch.setattr(apply_mod, "apply", forbidden)
    live_tui.run(["a", "y", "q"], "grp/proj", "--no-guide")
    assert "needs `api` scope" in live_tui.last


def test_tui_without_a_yaml_says_so_and_e_pulls_one(live_tui, gl, tmp_path):
    (tmp_path / "boards/x.yaml").unlink()
    (tmp_path / "boards/x.yaml.base").unlink()
    live_tui.run(["p", "e", "q"], "grp/proj", "--no-guide")  # p: no spec, no-op
    assert "no YAML yet — e pulls the board into one" in live_tui.text
    assert apply_mod.load(str(tmp_path / "boards/proj.yaml")) == SPEC
    assert live_tui.edits == [["vim", "boards/proj.yaml"]]


def test_tui_online_edit_diffs_against_the_live_board(live_tui, gl, tmp_path):
    gl["pending"]["plan"] = [("changed", "issue", "one: labels")]
    live_tui.run(["e", "q"], "grp/proj", "--no-guide")
    assert "a pushes" in live_tui.last and "one: labels" in live_tui.last


def test_tui_online_card_edit_adopts_a_card_the_yaml_lacks(live_tui, gl, tmp_path):
    spec = {**SPEC, "issues": [SPEC["issues"][0]]}  # card two is not in the YAML
    write_spec(tmp_path, spec=spec, base=False)
    live_tui.run(["down", "down", "c", *typed("hello"), "q"], "grp/proj", "--no-guide")
    issues = apply_mod.load("boards/x.yaml")["issues"]
    assert [i["iid"] for i in issues] == [1, 2]
    assert issues[1]["notes"] == ["hello"]
    assert "a pushes" in live_tui.last


def test_tui_online_card_edit_takes_a_card_the_yaml_holds_by_title_only(
    live_tui, gl, tmp_path
):
    """A seeded YAML has no iids; staging an edit on card two must not append
    a second `two`, or load() refuses the file as a duplicate title."""
    spec = {
        **SPEC,
        "issues": [{k: v for k, v in i.items() if k != "iid"} for i in SPEC["issues"]],
    }
    write_spec(tmp_path, spec=spec, base=False)
    live_tui.run(["down", "down", "c", *typed("hello"), "q"], "grp/proj", "--no-guide")
    issues = apply_mod.load("boards/x.yaml")["issues"]
    assert [(i["title"], i.get("iid")) for i in issues] == [("one", None), ("two", 2)]
    assert issues[1]["notes"] == ["hello"]
    assert "a pushes" in live_tui.last


def test_tui_online_due_estimate_uses_the_assignees_history(
    live_tui, gl, tmp_path, monkeypatch
):
    shutil.rmtree(tmp_path / "boards")  # live_tui's; the helper makes its own
    _, dump = _estimate_setup(tmp_path, monkeypatch)
    data = json.loads(dump.read_text())
    spec = {**SPEC, "issues": [{**SPEC["issues"][0], "assignee": "alice"}]}
    write_spec(tmp_path, spec=spec, base=False)
    monkeypatch.setattr(
        board_mod, "fetch_history", lambda *a, **k: (data["history"], ["Doing"])
    )
    monkeypatch.setattr(board_mod, "active_milestones", lambda p: [])
    live_tui.run(["down", "d", "e", "q"], "grp/proj", "--no-guide")
    due = apply_mod.load("boards/x.yaml")["issues"][0]["due_date"]
    assert due == (datetime.now(UTC).date() + timedelta(days=2)).isoformat()
    assert "p85 of 5 cards: alice" in live_tui.last
    # too few finished cards: no estimate, and the date is left alone
    monkeypatch.setattr(
        board_mod, "fetch_history", lambda *a, **k: (data["history"][:2], ["Doing"])
    )
    live_tui.run(["down", "down", "d", "e", "q"], "grp/proj", "--no-guide")
    assert "no estimate: too little finished history" in live_tui.last


def test_tui_migrate_comments_to_several_cards_and_close_the_source(
    live_tui, gl, tmp_path, monkeypatch
):
    copies, closed = [], []

    def copy(_gl, path, src, dst, dst_path=None):
        copies.append((path, src, dst, dst_path))
        return 2

    monkeypatch.setattr(apply_mod, "migrate_comments", copy)
    monkeypatch.setattr(
        apply_mod,
        "close_issue",
        lambda _gl, p, i, superseded_by=(): closed.append((i, list(superseded_by))),
    )
    write_spec(tmp_path, "boards/b.yaml", {**SPEC, "project": "grp/b"}, base=False)
    keys = ["m", *typed("1"), *typed("2"), "b", "3", *typed("7"), "\r", "y", "q"]
    live_tui.run(keys, "grp/proj", "--no-guide")
    assert copies == [("grp/proj", 1, 2, "grp/proj"), ("grp/proj", 1, 7, "grp/b")]
    assert closed == [(1, ["#2", "grp/b#7"])]
    assert "4 comment(s) copied; #1 closed" in live_tui.last
    assert "copy comments from" in live_tui.text
    assert "#1 “one”  →  onto" in live_tui.text


def test_tui_migrate_comments_n_keeps_the_source_open_and_empty_cancels(
    live_tui, gl, monkeypatch
):
    monkeypatch.setattr(apply_mod, "migrate_comments", lambda *a, **k: 0)
    monkeypatch.setattr(
        apply_mod, "close_issue", lambda *a, **k: pytest.fail("source stays open")
    )
    live_tui.run(
        ["m", *typed("1"), *typed("2"), "\r", "n", "q"], "grp/proj", "--no-guide"
    )
    assert "0 comment(s) copied #1 -> #2" in live_tui.last
    live_tui.run(["m", "\r", "q"], "grp/proj", "--no-guide")
    assert "cancelled" in live_tui.last
    live_tui.run(["m", *typed("1"), "\x1b", "q"], "grp/proj", "--no-guide")
    assert "cancelled" in live_tui.last


# --- the old names are gone; sync and pull from the TUI (gb-35b) -----------------


@pytest.mark.parametrize("old", ["apply", "land"])
def test_the_old_command_names_no_longer_exist(old, gl):
    r = runner.invoke(app, [old, "boards/x.yaml", "--yes"])
    assert r.exit_code != 0
    assert "No such command" in r.output


def test_tui_f_with_staged_edits_warns_and_n_writes_nothing(live_tui, gl, tmp_path):
    write_spec(tmp_path, spec=edited())
    before = (tmp_path / "boards/x.yaml").read_text()
    live_tui.run(["f", "n", "q"], "grp/proj", "--no-guide")
    assert "These 2 staged edits will be lost" in live_tui.text
    assert "not pulled" in live_tui.last
    assert (tmp_path / "boards/x.yaml").read_text() == before


def test_tui_f_y_overwrites_the_file_with_the_live_board(live_tui, gl, tmp_path):
    write_spec(tmp_path, spec=edited())
    live_tui.run(["f", "y", "q"], "grp/proj", "--no-guide")
    assert apply_mod.load("boards/x.yaml") == SPEC
    assert apply_mod.load("boards/x.yaml.base") == SPEC  # a .base existed
    assert "pulled boards/x.yaml" in live_tui.last


def test_tui_f_with_nothing_staged_says_so_and_any_other_key_cancels(
    live_tui, gl, tmp_path
):
    live_tui.run(["f", "x", "q"], "grp/proj", "--no-guide")
    assert "nothing staged is lost" in live_tui.text
    assert "not pulled" in live_tui.last


def test_tui_f_without_a_base_does_not_claim_nothing_is_lost(live_tui, gl, tmp_path):
    write_spec(tmp_path, spec=edited())
    (tmp_path / "boards/x.yaml.base").unlink()
    live_tui.run(["f", "n", "q"], "grp/proj", "--no-guide")
    assert "with no .base to compare" in live_tui.text
    assert "nothing staged is lost" not in live_tui.text


def test_tui_y_syncs_push_snapshot_and_refresh(live_tui, gl, tmp_path):
    write_spec(tmp_path, spec=edited())
    gl["pending"]["plan"] = [("changed", "issue", "one: labels")]
    live_tui.run(["y", "y", "q"], "grp/proj", "--no-guide")
    assert gl["apply"] == [{"base": SPEC, "force": False}]
    assert len(snapshot_lines(tmp_path / "snapshots.jsonl")) == 2  # one batch
    assert apply_mod.load("boards/x.yaml") == SPEC  # nothing left staged
    assert "refreshed from GitLab" in live_tui.last


def test_tui_y_declined_leaves_the_files_alone(live_tui, gl, tmp_path):
    write_spec(tmp_path, spec=edited())
    gl["pending"]["plan"] = [("changed", "issue", "one: labels")]
    live_tui.run(["y", "n", "q"], "grp/proj", "--no-guide")
    assert gl["apply"] == [] and apply_mod.load("boards/x.yaml") == edited()


def test_tui_offline_has_no_sync_or_pull(tui, tmp_path):
    path = write_spec(tmp_path)
    tui.run(["y", "f", "q"], "--from", path, "--no-guide")
    assert "offline — not here" in tui.last


# --- tui: P and B hand the terminal to perch or Budgie (PI_SUITE) -------------

SUITE = {
    "perch": {"cwd": "/ws", "argv": ["perch", "tui"]},
    "budgie": {"cwd": "/b", "argv": ["budgie", "tui"]},
}


@pytest.fixture
def execs(monkeypatch):
    calls = []
    monkeypatch.setattr(os, "chdir", lambda d: calls.append(("chdir", d)))
    monkeypatch.setattr(os, "execvp", lambda f, a: calls.append(("exec", f, a)))
    return calls


def test_tui_shift_p_and_b_exec_the_target(tui, tmp_path, monkeypatch, execs):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("PI_SUITE", json.dumps(SUITE))
    path = write_spec(tmp_path)
    tui.run(["P"], "--from", path, "--no-guide")
    tui.run(["B"], "--from", path, "--no-guide")
    assert execs == [
        ("chdir", "/ws"),
        ("exec", "perch", ["perch", "tui"]),
        ("chdir", "/b"),
        ("exec", "budgie", ["budgie", "tui"]),
    ]


def test_tui_switch_without_pi_suite_stays_and_says_why(
    tui, tmp_path, monkeypatch, execs
):
    monkeypatch.delenv("PI_SUITE", raising=False)
    path = write_spec(tmp_path)
    tui.run(["P", "q"], "--from", path, "--no-guide")
    assert "start from perch tui to switch apps" in tui.last
    assert execs == []


def test_tui_shift_g_still_toggles_the_guide(tui, tmp_path, monkeypatch, execs):
    monkeypatch.setenv("PI_SUITE", json.dumps(SUITE))
    path = write_spec(tmp_path)
    tui.run(["G", "q"], "--from", path, "--no-guide")
    assert "guide on" in tui.text
    assert execs == []


def test_tui_switch_that_cannot_exec_exits_1(tui, tmp_path, monkeypatch):
    gone = {"perch": {"cwd": str(tmp_path / "gone"), "argv": ["perch", "tui"]}}
    monkeypatch.setenv("PI_SUITE", json.dumps(gone))
    path = write_spec(tmp_path)
    tui.keys = ["P"]
    r = runner.invoke(app, ["tui", "--from", path, "--no-guide"])
    assert r.exit_code == 1
    assert "switch failed" in tui.errbuf.getvalue()


SUITE_TMUX = "/private/tmp/tmux-501/pi,123,0"
TMUX = ["tmux", "-L", "pi"]
P_KEY = json.dumps(SUITE["perch"], sort_keys=True)


@pytest.fixture
def hops(monkeypatch):
    """In perch suite; records tmux argv; list-windows answers .listing."""
    calls = []
    real = subprocess.run  # other subprocess users on the tui path stay real

    class Fake:
        listing = ""
        stderr = ""

    def run(argv, **kw):
        if argv[:1] != ["tmux"]:
            return real(argv, **kw)
        calls.append(list(argv))
        if "list-windows" in argv:
            return subprocess.CompletedProcess(argv, 0, Fake.listing, "")
        return subprocess.CompletedProcess(
            argv, 1 if Fake.stderr else 0, "", Fake.stderr
        )

    Fake.calls = calls
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("TMUX", SUITE_TMUX)
    return Fake


def test_in_suite_only_on_the_pi_socket(monkeypatch):
    monkeypatch.setenv("TMUX", SUITE_TMUX)
    assert cli._in_suite()
    monkeypatch.setenv("TMUX", "/private/tmp/tmux-501/default,1,0")
    assert not cli._in_suite()
    monkeypatch.delenv("TMUX")
    assert not cli._in_suite()


def test_hop_rule(hops, monkeypatch):
    monkeypatch.setenv("PI_SUITE", json.dumps(SUITE))
    env = f"PI_SUITE={json.dumps(SUITE)}"
    hops.listing = f"perch\t{P_KEY}\n"
    assert cli._hop("perch", SUITE["perch"]) is None
    assert hops.calls[-1] == [*TMUX, "select-window", "-t", "pi:=perch"]
    hops.listing = "gitboard\t{}\n"
    cli._hop("perch", SUITE["perch"])
    assert hops.calls[-1] == [
        *TMUX, "new-window", "-t", "pi:", "-n", "perch",
        "-c", "/ws", "-e", env, "perch", "tui",
        ";", "set-option", "-w", "-t", "pi:=perch", "@entry", P_KEY,
    ]  # fmt: skip
    hops.listing = "perch\t\n"
    cli._hop("perch", SUITE["perch"])
    assert hops.calls[-1] == [
        *TMUX, "respawn-window", "-k", "-t", "pi:=perch",
        "-c", "/ws", "-e", env, "perch", "tui",
        ";", "set-option", "-w", "-t", "pi:=perch", "@entry", P_KEY,
        ";", "select-window", "-t", "pi:=perch",
    ]  # fmt: skip


def test_tui_in_suite_shift_p_hops_and_stays(tui, tmp_path, monkeypatch, execs, hops):
    monkeypatch.setenv("PI_SUITE", json.dumps(SUITE))
    hops.listing = f"perch\t{P_KEY}\n"
    path = write_spec(tmp_path)
    tui.run(["P", "B", "q"], "--from", path, "--no-guide")
    assert execs == []
    assert [c[3] for c in hops.calls] == [
        "list-windows", "select-window", "list-windows", "new-window",
    ]  # fmt: skip


def test_tui_in_suite_a_failed_hop_shows_on_the_status_line(
    tui, tmp_path, monkeypatch, execs, hops
):
    monkeypatch.setenv("PI_SUITE", json.dumps(SUITE))
    hops.stderr = "no server running"
    path = write_spec(tmp_path)
    tui.run(["P", "q"], "--from", path, "--no-guide")
    assert "switch failed: no server running" in tui.text
    assert execs == []


def test_tui_in_suite_without_pi_suite_runs_no_tmux(
    tui, tmp_path, monkeypatch, execs, hops
):
    monkeypatch.delenv("PI_SUITE", raising=False)
    path = write_spec(tmp_path)
    tui.run(["P", "q"], "--from", path, "--no-guide")
    assert "start from perch tui to switch apps" in tui.last
    assert hops.calls == []


def test_switch_with_an_empty_program_exits_1(tmp_path):
    with pytest.raises(typer.Exit):
        cli._switch({"cwd": str(tmp_path), "argv": [""]})


# --- tui: GitLab down stays up (perch-8xc.3) ----------------------------------

DOWN = "cannot reach http://localhost:8929: ConnectionError"


@pytest.fixture
def down(live_tui, monkeypatch):
    """GitLab unreachable until `state["up"]` is set; then the live_tui fetch."""
    real = board_mod.fetch
    state = {"up": False}

    def fetch(path, name=None):
        if not state["up"]:
            raise client.GitlabProblem(DOWN)
        return real(path, name)

    monkeypatch.setattr(board_mod, "fetch", fetch)
    live_tui.state = state
    return live_tui


def test_tui_unreachable_at_start_shows_the_error_and_q_quits(down):
    down.run(["q"], "grp/proj", "--no-guide")
    assert DOWN in down.last
    assert "r retry" in down.last and "q quit" in down.last


def test_tui_unreachable_other_keys_just_reshow_the_error(down):
    down.run(["v", "a", "?", "j", "q"], "grp/proj", "--no-guide")
    assert DOWN in down.last


def test_tui_unreachable_then_r_retries_into_the_board(down):
    down.run(["r", "q"], "grp/proj", "--no-guide")  # still down
    assert DOWN in down.last
    down.state["up"] = True
    down.run(["r", "q"], "grp/proj", "--no-guide")
    assert "defined by boards/x.yaml" in down.last
    assert DOWN not in down.last


def test_tui_unreachable_shift_p_still_switches(down, monkeypatch, execs):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("PI_SUITE", json.dumps(SUITE))
    down.run(["P"], "grp/proj", "--no-guide")
    assert execs == [("chdir", "/ws"), ("exec", "perch", ["perch", "tui"])]


def test_tui_gitlab_lost_mid_session_stays_up(down):
    down.state["up"] = True

    def key():
        k = down.keys.pop(0)
        down.state["up"] = k != "r"  # r finds GitLab gone
        return k

    down.mp.setattr(cli, "_key", key)
    down.run(["r", "q"], "grp/proj", "--no-guide")
    assert DOWN in down.last
    assert "defined by boards/x.yaml" in down.last  # the old board stays


def test_tui_failed_board_switch_keeps_the_old_board_for_r(live_tui, gl, monkeypatch):
    real = board_mod.fetch

    def fetch(path, name=None):
        asked.append(name)
        if name == "Other":
            raise client.GitlabProblem("boom")
        return real(path, name)

    asked = []

    monkeypatch.setattr(board_mod, "fetch", fetch)
    live_tui.run(["b", "2", "r", "q"], "grp/proj", "--no-guide")
    assert asked == [None, "Other", None]  # r retried the board on screen
