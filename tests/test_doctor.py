import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gitboard import cli, client, config, doctor
from gitboard.config import Config

NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)
SPEC = "project: g/p\nboard: B\ncolumns: []\n"


def http_for(scopes=("read_api", "api"), self_status=200, mcp=404, calls=None):
    def http(path, token):
        if calls is not None:
            calls.append(path)
        if path.endswith("/version"):
            return 200, {"version": "17.0"}
        if path.endswith("/self"):
            if self_status == 200:
                return 200, {"scopes": list(scopes)}
            return self_status, None
        return mcp, None

    return http


def world(tmp_path, base_age_days=0, snap_age_hours=1, spec=SPEC, now=NOW):
    (tmp_path / "boards").mkdir(exist_ok=True)
    y = tmp_path / "boards" / "t.yaml"
    y.write_text(spec)
    b = tmp_path / "boards" / "t.yaml.base"
    b.write_text(spec)
    old = (now - timedelta(days=base_age_days)).timestamp()
    os.utime(b, (old, old))
    ts = (now - timedelta(hours=snap_age_hours)).isoformat()
    (tmp_path / "snapshots.jsonl").write_text(f'{{"ts": "{ts}", "iid": 1}}\n')
    return [y]


def cfg(**kw):
    kw.setdefault("token_override", "tok")
    kw.setdefault("write_token_override", "wtok")
    kw.setdefault("write_token_source", "environment")
    return Config(url="https://gl.example", source=Path("gitboard.toml"), **kw)


def go(tmp_path, c=None, http=None, resolve=lambda p, b: None, specs=None, **kw):
    return doctor.run(
        c or cfg(),
        http=http or http_for(),
        resolve=resolve,
        root=tmp_path,
        specs=world(tmp_path) if specs is None else specs,
        now=NOW,
        **kw,
    )


def statuses(checks):
    return {c.status for c in checks}


def test_good_setup_all_ok(tmp_path):
    assert statuses(go(tmp_path)) == {doctor.OK}


def test_missing_token_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_keychain_lookup", lambda s: None)
    checks = go(tmp_path, c=cfg(token_override=None, token_source="keychain"))
    bad = [c for c in checks if c.status == doctor.FAIL]
    assert "read token" in bad[0].what and bad[0].fix


def test_write_falls_back_to_read_only_token_warns(tmp_path):
    c = cfg(write_token_override=None, write_token_source=None)
    checks = go(tmp_path, c=c, http=http_for(scopes=("read_api",)))
    warn = [c for c in checks if c.status == doctor.WARN]
    assert len(warn) == 1 and "no api scope" in warn[0].what
    assert "GITLAB_WRITE_TOKEN" in warn[0].fix
    assert doctor.FAIL not in statuses(checks)


def test_scope_endpoint_404_warns_not_fails(tmp_path):
    checks = go(tmp_path, http=http_for(self_status=404))
    assert doctor.FAIL not in statuses(checks)
    assert any("scope unknown" in c.what for c in checks if c.status == doctor.WARN)


def test_rejected_token_fails(tmp_path):
    assert doctor.FAIL in statuses(go(tmp_path, http=http_for(self_status=401)))


def test_unreachable_fails(tmp_path):
    def down(path, token):
        raise ConnectionError("x")

    assert doctor.FAIL in statuses(go(tmp_path, http=down))


def test_stale_base_warns(tmp_path):
    checks = go(tmp_path, specs=world(tmp_path, base_age_days=8))
    assert any(".base: 8d old" in c.what for c in checks if c.status == doctor.WARN)


def test_stale_snapshot_warns_with_cron_fix(tmp_path):
    checks = go(tmp_path, specs=world(tmp_path, snap_age_hours=30))
    w = [c for c in checks if c.status == doctor.WARN]
    assert w[0].fix == "cron not running? make cron"


def test_broken_yaml_listed_by_file(tmp_path):
    specs = world(tmp_path, spec="project: [oops\n")
    bad = [c for c in go(tmp_path, specs=specs) if c.status == doctor.FAIL]
    assert "t.yaml" in bad[0].what


def test_project_unresolved_fails(tmp_path):
    def nope(p, b):
        raise client.GitlabProblem("no project 'g/p'")

    checks = go(tmp_path, c=cfg(project="g/p"), resolve=nope)
    assert any(c.status == doctor.FAIL and "g/p" in c.what for c in checks)


def test_mcp_never_fails(tmp_path):
    for code in (200, 404):
        assert doctor.FAIL not in statuses(go(tmp_path, http=http_for(mcp=code)))


def test_offline_makes_no_network_calls(tmp_path):
    calls = []
    checks = go(tmp_path, http=http_for(calls=calls), offline=True)
    assert calls == [] and doctor.FAIL not in statuses(checks)


def test_token_value_never_in_output(tmp_path):
    c = cfg(token_override="SECRET-abc")
    assert "SECRET" not in repr(go(tmp_path, c=c, http=http_for(self_status=401)))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("GITLAB_READ_TOKEN", "r")
    monkeypatch.setenv("GITLAB_WRITE_TOKEN", "w")
    monkeypatch.chdir(tmp_path)
    config.reset()
    yield
    config.reset()


def test_cli_exit_codes(tmp_path, env, monkeypatch):
    world(tmp_path, now=datetime.now(UTC))
    monkeypatch.setattr(cli, "_doctor_http", http_for())
    r = CliRunner().invoke(cli.app, ["doctor"])
    assert r.exit_code == 0, r.output
    monkeypatch.setattr(cli, "_doctor_http", http_for(self_status=401))
    assert CliRunner().invoke(cli.app, ["doctor"]).exit_code == 1


def test_cli_offline_no_calls(tmp_path, env, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_doctor_http", http_for(calls=calls))
    CliRunner().invoke(cli.app, ["doctor", "--offline"])
    assert calls == []


def test_snapshot_check_skips_a_bad_line(tmp_path):
    db = tmp_path / "s.jsonl"
    ts = (NOW - timedelta(hours=1)).isoformat()
    db.write_text(f'{{"ts": "{ts}"}}\n\nnot json\n')
    assert doctor._snapshot_check(db, NOW).status == doctor.OK


def test_unreadable_board_file_is_a_fail_row(tmp_path):
    d = tmp_path / "d.yaml"
    d.mkdir()
    assert doctor._spec_checks([d], NOW)[0].status == doctor.FAIL


def test_cli_bad_config_is_one_line_not_a_traceback(tmp_path, env):
    (tmp_path / "gitboard.toml").write_text("url = [oops\n")
    r = CliRunner().invoke(cli.app, ["doctor", "--offline"])
    assert r.exit_code == 1 and isinstance(r.exception, SystemExit), r.exception
