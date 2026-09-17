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
from typer.testing import CliRunner

from gitboard import apply as apply_mod
from gitboard import board as board_mod
from gitboard import cli, client, config
from gitboard.cli import SIGN, STYLE, _changes_table, app

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
    proj = types.SimpleNamespace(path_with_namespace=project)
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


def test_sign_and_style_cover_every_kind():
    assert set(SIGN) == set(STYLE) == {"added", "changed", "skipped", "drift", "oneway"}
    assert SIGN["oneway"] == SIGN["drift"] == "!"  # both mean: look before you leap


def test_unknown_kind_does_not_crash_the_table():
    table = _changes_table([("weird", "issue", "x")], "t")
    assert table.row_count == 1


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
        "1",
        "never",
    ]


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
    assert set(dumped) == {"project", "board", "columns", "fetched_at", "history"}
    assert dumped["history"] == data["history"]
    again = runner.invoke(app, ["stats", "--from", "h2.json"])
    assert again.exit_code == 0, again.output
    assert again.stdout == r.stdout


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
    preview = (folder / "alice.html").read_text()
    assert "<svg" in preview and "a@x" in preview  # browser copy: chart + headers
    index = (folder / "index.html").read_text()
    assert 'href="alice.eml"' in index and 'href="bob.html"' in index
    assert "index.html" in r.output


# --- config ----------------------------------------------------------------


def test_config_command_survives_a_missing_keychain(monkeypatch):
    monkeypatch.delenv("GITLAB_READ_TOKEN", raising=False)
    monkeypatch.delenv("GITLAB_WRITE_TOKEN", raising=False)

    def no_security(*a, **k):
        raise FileNotFoundError("security")

    monkeypatch.setattr(config.subprocess, "run", no_security)
    r = runner.invoke(app, ["config"])
    assert r.exit_code == 0, r.output
    assert "not found" in r.output
