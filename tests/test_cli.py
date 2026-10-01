"""Tests for cli.py: the drift guard, pull's guards, status, land, --all.

No network — `board_mod.fetch`, `board_mod.board_columns`, `apply_mod.*`
and `client.gitlab` are patched. Each test runs in an empty cwd with the
config singleton reset, like tests/test_config.py's fixture, so the repo's
own .env and boards/ never leak in.
"""

import json
import re
import types
from datetime import UTC, datetime, timedelta

import pytest
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
        self.assignee = self.due_date = None


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


# --- apply: the drift guard ------------------------------------------------


def test_apply_refuses_on_drift(gl, tmp_path):
    path = write_spec(tmp_path, spec=edited())
    gl["pending"]["plan"] = [("drift", "issue", "one: labels")]
    r = runner.invoke(app, ["apply", path, "--yes"])
    assert r.exit_code == 1
    assert "1 field(s) changed on GitLab since the pull" in r.output
    assert "--ignore-drift" in r.output
    assert gl["apply"] == []
    assert gl["plan"][0]["base"] == SPEC  # the .base was passed


def test_apply_ignore_drift_forces_and_snapshots(gl, tmp_path):
    path = write_spec(tmp_path, spec=edited())
    gl["pending"]["plan"] = [("drift", "issue", "one: labels")]
    r = runner.invoke(app, ["apply", path, "--yes", "--ignore-drift"])
    assert r.exit_code == 0, r.output
    assert gl["apply"] == [{"base": SPEC, "force": True}]
    assert (tmp_path / "snapshots.jsonl").exists()


def test_apply_without_a_base_passes_none(gl, tmp_path):
    path = write_spec(tmp_path, spec=edited(), base=False)
    gl["pending"]["plan"] = [("changed", "issue", "one: labels")]
    r = runner.invoke(app, ["apply", path, "--yes"])
    assert r.exit_code == 0, r.output
    assert gl["apply"] == [{"base": None, "force": False}]


def test_apply_with_only_skipped_rows_writes_nothing(gl, tmp_path):
    path = write_spec(tmp_path)
    gl["pending"]["plan"] = [("skipped", "issue", "one: labels")]
    r = runner.invoke(app, ["apply", path, "--yes"])
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


# --- land ------------------------------------------------------------------


def test_land_yes_happy_path(gl, tmp_path):
    path = write_spec(tmp_path, spec=edited())
    (tmp_path / "boards/issues.jsonl").write_text("")
    gl["pending"]["plan"] = [("changed", "issue", "one: labels")]
    r = runner.invoke(app, ["land", path, "--yes"])
    assert r.exit_code == 0, r.output
    assert gl["apply"] == [{"base": SPEC, "force": False}]
    # both files move forward to the live board: nothing is staged after a land
    assert apply_mod.load(path) == SPEC
    assert apply_mod.load(path + ".base") == SPEC
    assert (tmp_path / "boards/x.yaml.base.old").exists()
    assert snapshot_lines(tmp_path / "snapshots.jsonl")
    assert "bd import" in r.output


def test_land_refuses_drift_and_keeps_the_base(gl, tmp_path):
    path = write_spec(tmp_path, spec=edited())
    gl["pending"]["plan"] = [("drift", "issue", "one: labels")]
    r = runner.invoke(app, ["land", path, "--yes"])
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
    monkeypatch.setattr(
        board_mod,
        "fetch_history",
        lambda p, b, since: (data["history"], data["columns"]),
    )
    r = runner.invoke(app, ["stats", "grp/proj", "--dump", "h2.json"])
    assert r.exit_code == 0, r.output
    dumped = json.loads((tmp_path / "h2.json").read_text())
    assert set(dumped) == {
        "project",
        "board",
        "columns",
        "fetched_at",
        "milestones",
        "history",
    }
    assert dumped["history"] == data["history"]
    again = runner.invoke(app, ["stats", "--from", "h2.json"])
    assert again.exit_code == 0, again.output
    assert again.stdout == r.stdout


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


# --- tui: plan/apply carry the .base (gb-0yy) --------------------------------


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


def test_tui_apply_gets_the_same_base(monkeypatch, tmp_path):
    """The write half of _staged's wiring: apply_mod.plan sees the .base."""
    parsed, path = _tui_files(tmp_path, [1, 2], [2, 1])
    seen = {}
    monkeypatch.setattr(client, "gitlab", lambda write=False: object())
    monkeypatch.setattr(
        apply_mod, "plan", lambda _gl, spec, base=None: seen.update(base=base) or []
    )
    cli._staged(parsed, path, write=True)
    assert seen["base"]["issues"][0]["iid"] == 1
