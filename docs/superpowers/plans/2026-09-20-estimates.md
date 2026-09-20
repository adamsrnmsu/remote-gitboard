# Per-person estimates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Estimate a card's days-to-done from each person's history, stage due dates from it, and warn when a due date contradicts it.

**Architecture:** One pure module, `estimate.py`, owns samples, the bucket ladder, `suggest` (mutates a spec) and `tight` (rows for stats). The CLI adds an `estimate` command and attaches `tight` to the summary in `_summary`; `stats.py`/`mail.py` only render what they find under `flow.tight`. `estimate` imports `stats`; `stats` never imports `estimate`.

**Tech Stack:** Python stdlib, typer, rich, pytest. No new dependency.

**Spec:** `docs/superpowers/specs/2026-09-20-estimates-design.md`

## Global Constraints

- Run everything as `PYTHONPATH=src .venv/bin/...`; no uv, no editable install.
- Tests never touch the network.
- stdout is the board/markdown only; tables and logs go through `err()`.
- `mail.py`: every `td` has `bgcolor`, every text run a `color` — build rows only from `_rows`/`sub`/`td`/`span`.
- Spec defaults: `suggest_due: true`, `method: p85`, `min_samples: 5`; methods are exactly `median` (0.5) and `p85` (0.85), nearest-rank; `HISTORY_DAYS = 90`.
- A `due_date` that exists is never overwritten. `Verify`/`Done`/`Failed` cards are never estimated.
- Beads: claim the task's bead on start, close it with a reason on finish. Commit only when the user asks.

---

### Task 1 (gb-j38.1): `estimate.py` core

**Files:**
- Create: `src/gitboard/estimate.py`
- Test: `tests/test_estimate.py`

**Interfaces:**
- Consumes: `stats.parse_ts`, `stats.done_at`, `stats.scoped`, `stats.DAY`, `stats.VERIFY/DONE/FAILED`; `apply.SpecError`; test helpers `issue`, `ts` from `tests/test_stats.py`.
- Produces:
  - `METHODS: dict[str, float]`, `DEFAULTS: dict`, `HISTORY_DAYS = 90`, `SKIP: set[str]`
  - `config(spec: dict) -> dict` — `{suggest_due, method, min_samples}`; raises `SpecError`
  - `started(issue: dict) -> datetime`
  - `samples(history: list[dict]) -> list[tuple[str | None, str | None, float]]` — `(assignee, type, active_days)`
  - `estimate(assignee, labels, pool, method="p85", min_samples=5) -> dict | None` — `{"days": int, "n": int, "basis": str}`
  - `suggest(spec, history, today: date) -> {"rows": [...], "changed": bool}` — rows: `iid, title, assignee, due, days, n, basis`
  - `tight(history, columns, now: datetime, cfg) -> list[dict]` — rows: `iid, title, assignee, due, expected, basis, url`

- [ ] **Step 1: Write the failing tests** — `tests/test_estimate.py`:

```python
"""Tests for estimate.py — pure arithmetic over a fetch_history-shaped list."""

from datetime import date

import pytest
from test_stats import COLUMNS, NOW, issue, ts

from gitboard import estimate
from gitboard.apply import SpecError


def done(iid, who, days, kind=None, start=1):
    """A card `who` finished `days` after it entered Doing on day `start`."""
    return issue(
        iid,
        created=1,
        closed=start + days,
        assignee=who,
        labels=[f"type::{kind}"] if kind else [],
        transitions=[(ts(start), "add", "Doing")],
    )


def test_config_defaults_overrides_and_rejections():
    assert estimate.config({}) == estimate.DEFAULTS
    cfg = estimate.config({"estimates": {"suggest_due": False, "method": "median"}})
    assert (cfg["suggest_due"], cfg["method"], cfg["min_samples"]) == (False, "median", 5)
    for bad in ("yes", {"method": "p99"}, {"min_samples": 0}, {"min_samples": "3"}):
        with pytest.raises(SpecError):
            estimate.config({"estimates": bad})


def test_active_days_start_at_the_first_column_add_not_creation():
    i = issue(1, created=1, closed=9, assignee="ana",
              transitions=[(ts(6), "add", "Doing"), (ts(8), "add", "Verify")])  # fmt: skip
    assert estimate.samples([i]) == [("ana", None, 3.0)]
    assert estimate.samples([issue(2, created=2, closed=4)]) == [(None, None, 2.0)]
    assert estimate.samples([issue(3)]) == []  # not finished: not a sample


def test_nearest_rank_percentiles_round_up_to_whole_days():
    pool = [("ana", None, d) for d in (1, 2, 3, 4, 10.2)]
    assert estimate.estimate("ana", [], pool, "p85")["days"] == 11
    assert estimate.estimate("ana", [], pool, "median")["days"] == 3
    assert estimate.estimate("ana", [], [("ana", None, 0.0)] * 5)["days"] == 1


def test_ladder_takes_the_narrowest_bucket_with_enough_samples():
    pool = (
        [("ana", "bug", 2.0)] * 2
        + [("ana", "task", 4.0)] * 3
        + [("bob", "bug", 6.0)] * 3
        + [("bob", "task", 8.0)] * 9
    )
    bug = ["type::bug"]
    assert estimate.estimate("ana", bug, pool, min_samples=2)["basis"] == (
        "p85 of 2 cards: ana, type::bug"
    )
    assert estimate.estimate("ana", bug, pool, min_samples=5)["basis"] == (
        "p85 of 5 cards: ana"
    )
    assert estimate.estimate("cat", bug, pool, min_samples=5)["basis"] == (
        "p85 of 5 cards: team, type::bug"
    )
    assert estimate.estimate("cat", [], pool, min_samples=5)["basis"] == (
        "p85 of 17 cards: team"
    )
    assert estimate.estimate("cat", [], pool, min_samples=18) is None


def _spec(**estimates):
    spec = {
        "issues": [
            {"title": "open", "assignee": "ana", "labels": ["Doing"]},
            {"title": "dated", "assignee": "ana", "due_date": "2026-10-01"},
            {"title": "nobody"},
            {"title": "waiting", "assignee": "ana", "labels": ["Verify"]},
        ]
    }
    if estimates:
        spec["estimates"] = estimates
    return spec


HISTORY = [done(n, "ana", 3) for n in range(1, 6)]


def test_suggest_dates_only_assigned_undated_cards_outside_verify_done_failed():
    spec = _spec()
    out = estimate.suggest(spec, HISTORY, date(2026, 9, 20))
    assert out["changed"] and [r["title"] for r in out["rows"]] == ["open"]
    assert spec["issues"][0]["due_date"] == "2026-09-23"
    assert spec["issues"][1]["due_date"] == "2026-10-01"
    assert "due_date" not in spec["issues"][2] and "due_date" not in spec["issues"][3]


def test_suggest_due_false_reports_but_writes_nothing():
    spec = _spec(suggest_due=False)
    out = estimate.suggest(spec, HISTORY, date(2026, 9, 20))
    assert not out["changed"] and out["rows"][0]["due"] == "2026-09-23"
    assert "due_date" not in spec["issues"][0]


def test_suggest_without_enough_history_is_empty():
    assert estimate.suggest(_spec(), HISTORY[:4], date(2026, 9, 20)) == {
        "rows": [],
        "changed": False,
    }


def _open(iid, due, entered=None, **kw):
    return issue(
        iid,
        assignee="ana",
        due_date=due,
        labels=["Doing"] if entered else [],
        transitions=[(ts(entered), "add", "Doing")] if entered else [],
        **kw,
    )


def test_tight_flags_a_due_date_before_the_expected_finish():
    # NOW is 2026-09-16; ana's cards take 3 days
    history = HISTORY + [
        _open(10, "2026-09-17", entered=16),  # 16 + 3 = 19 > 17: tight
        _open(11, "2026-09-19", entered=16),  # 19 <= 19: fine
        _open(12, "2026-09-15", entered=10),  # already overdue: not ours
        _open(13, "2026-09-18"),  # Backlog: today + 3 = 19 > 18: tight
        _open(14, "2026-09-16", entered=1),  # long started: expected is today
    ]
    rows = estimate.tight(history, COLUMNS, NOW, estimate.DEFAULTS)
    assert [(r["iid"], r["due"], r["expected"]) for r in rows] == [
        (10, "2026-09-17", "2026-09-19"),
        (13, "2026-09-18", "2026-09-19"),
    ]
    assert rows[0]["basis"] == "p85 of 5 cards: ana" and rows[0]["assignee"] == "ana"
```

- [ ] **Step 2: Run to verify it fails** — `PYTHONPATH=src .venv/bin/pytest tests/test_estimate.py -q` → collection error, `cannot import name 'estimate'`.

- [ ] **Step 3: Implement** — `src/gitboard/estimate.py`:

```python
"""Per-person estimates from the board's own history. Pure, stdlib only.

A sample is a finished card's active days; an estimate is a percentile of
the narrowest bucket — person + type, person, team + type, team — that holds
enough samples. `suggest` stages due dates into a spec; `tight` lists the
due dates the history contradicts. Imports `stats`; `stats` must not import
this back.
"""

from datetime import timedelta
from math import ceil

from gitboard import stats
from gitboard.apply import SpecError

METHODS = {"median": 0.5, "p85": 0.85}
DEFAULTS = {"suggest_due": True, "method": "p85", "min_samples": 5}
HISTORY_DAYS = 90
SKIP = {stats.VERIFY, stats.DONE, stats.FAILED}


def config(spec):
    """The spec's `estimates:` over DEFAULTS; a bad value is a SpecError."""
    raw = spec.get("estimates")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SpecError("estimates: expected a mapping")
    cfg = {**DEFAULTS, **raw}
    if cfg["method"] not in METHODS:
        raise SpecError(
            f"estimates.method: {cfg['method']!r} is not one of {', '.join(METHODS)}"
        )
    if not isinstance(cfg["min_samples"], int) or cfg["min_samples"] < 1:
        raise SpecError("estimates.min_samples: expected a whole number, 1 or more")
    return cfg


def started(issue):
    """When the card first entered a column; Backlog wait is nobody's pace."""
    adds = [stats.parse_ts(t) for t, act, _ in issue["transitions"] if act == "add"]
    return min(adds) if adds else stats.parse_ts(issue["created_at"])


def samples(history):
    """[(assignee, type, active_days)] for every finished card."""
    out = []
    for i in history:
        done = stats.done_at(i)
        if done is not None:
            days = max(0.0, (done - started(i)) / stats.DAY)
            out.append((i["assignee"], stats.scoped(i["labels"], "type"), days))
    return out


def estimate(assignee, labels, pool, method="p85", min_samples=5):
    """{days, n, basis} from the narrowest bucket with enough samples, or None.

    Nearest-rank percentile, so the figure is a duration that happened;
    rounded up to whole days, at least 1.
    """
    kind = stats.scoped(labels, "type")
    ladder = [
        (assignee and kind, f"{assignee}, type::{kind}",
         lambda s: s[0] == assignee and s[1] == kind),
        (assignee, f"{assignee}", lambda s: s[0] == assignee),
        (kind, f"team, type::{kind}", lambda s: s[1] == kind),
        (True, "team", lambda s: True),
    ]  # fmt: skip
    for usable, name, keep in ladder:
        days = sorted(s[2] for s in pool if keep(s)) if usable else []
        if len(days) >= min_samples:
            pick = days[ceil(METHODS[method] * len(days)) - 1]
            return {
                "days": max(1, ceil(pick)),
                "n": len(days),
                "basis": f"{method} of {len(days)} cards: {name}",
            }
    return None


def suggest(spec, history, today):
    """Stage `due_date = today + estimate` on assigned, undated, open work.

    Mutates `spec` unless `estimates.suggest_due` is false; either way the
    rows say what it would set and why. A date that exists is never touched.
    """
    cfg = config(spec)
    pool = samples(history)
    rows = []
    for issue in spec["issues"]:
        labels = issue.get("labels") or []
        who = issue.get("assignee")
        if issue.get("due_date") or not who or SKIP & set(labels):
            continue
        est = estimate(who, labels, pool, cfg["method"], cfg["min_samples"])
        if est is None:
            continue
        # ponytail: today + days ignores how many cards the person already
        # holds; queue-aware dating if the dates prove optimistic
        due = (today + timedelta(days=est["days"])).isoformat()
        rows.append(
            {"iid": issue.get("iid"), "title": issue["title"], "assignee": who,
             "due": due, **est}
        )  # fmt: skip
        if cfg["suggest_due"]:
            issue["due_date"] = due
    return {"rows": rows, "changed": cfg["suggest_due"] and bool(rows)}


def tight(history, columns, now, cfg):
    """Open cards whose due date falls before the finish the history expects.

    Past due dates are `overdue`'s business. A card on the board is expected
    `started + days` (never earlier than today); a Backlog card `today + days`.
    """
    pool = samples(history)
    today = now.date()
    out = []
    for i in history:
        due, who = i.get("due_date"), i["assignee"]
        if i["state"] != "opened" or not who or not due or due < today.isoformat():
            continue
        if SKIP & set(i["labels"]):
            continue
        est = estimate(who, i["labels"], pool, cfg["method"], cfg["min_samples"])
        if est is None:
            continue
        span = timedelta(days=est["days"])
        on_board = any(c in i["labels"] for c in columns)
        expected = max((started(i) + span).date(), today) if on_board else today + span
        if expected.isoformat() > due:
            out.append(
                {"iid": i["iid"], "title": i["title"], "assignee": who, "due": due,
                 "expected": expected.isoformat(), "basis": est["basis"],
                 "url": i["web_url"]}
            )  # fmt: skip
    return sorted(out, key=lambda t: (t["due"], t["iid"]))
```

- [ ] **Step 4: Verify** — `PYTHONPATH=src .venv/bin/pytest tests/test_estimate.py -q` → all pass; `.venv/bin/ruff format src tests && .venv/bin/ruff check src tests` → clean.

---

### Task 2 (gb-j38.2): render tight dates — markdown, mail, trend row

**Files:**
- Modify: `src/gitboard/stats.py` (`for_person`, `stat_row`, `render_team_md`, `render_person_md`, new `_tight_table`)
- Modify: `src/gitboard/mail.py` (new `_tight`, `_stuck`, `_yours`)
- Test: `tests/test_stats.py`, `tests/test_mail.py`

**Interfaces:**
- Consumes: `summary["flow"]["tight"]` rows from Task 1's `tight` — read with `.get("tight", [])` everywhere, because `summarise` never sets it.
- Produces: `for_person(...)["tight"]`; `stat_row(...)["tight"]: int`; "### Tight dates" (team) / "## Tight dates" (person) markdown; mail rows.

- [ ] **Step 1: Failing tests.** Append to `tests/test_stats.py` (and add `"tight"` to `ROW_KEYS`):

```python
TIGHT = {
    "iid": 7, "title": "slow one", "assignee": "bob", "due": "2026-09-17",
    "expected": "2026-09-19", "basis": "p85 of 5 cards: bob", "url": "http://x/7",
}  # fmt: skip


def test_tight_dates_reach_person_row_and_markdown():
    s = stats.summarise(history(), COLUMNS, START, END, NOW)
    assert stats.for_person(s, history(), "bob", NOW)["tight"] == []  # key absent
    s["flow"]["tight"] = [TIGHT]
    bob = stats.for_person(s, history(), "bob", NOW)
    assert bob["tight"] == [TIGHT]
    assert stats.for_person(s, history(), "alice", NOW)["tight"] == []
    assert stats.stat_row(s, "g/p", "b", "t")["tight"] == 1
    assert "| 7 | slow one | bob | 2026-09-17 | 2026-09-19 |" in stats.render_team_md(s)
    mine = stats.render_person_md(bob, s, "bob").split("\n---\n")[0]
    assert "| 7 | slow one | 2026-09-17 | 2026-09-19 |" in mine
```

Append to `tests/test_mail.py` (import `TIGHT` from `test_stats`):

```python
def test_tight_dates_show_on_team_and_on_the_owner_only():
    s = summary()
    s["flow"]["tight"] = [TIGHT]
    team = mail.render_team_html(s, series())
    assert "bob · due 2026-09-17 · likely 2026-09-19" in team
    assert NO_BG.findall(team) == []
    bob = mail._yours(stats.for_person(s, history(), "bob", NOW))
    assert "due 2026-09-17 · likely 2026-09-19" in bob and NO_BG.findall(bob) == []
    assert "likely" not in mail._yours(stats.for_person(s, history(), "alice", NOW))
```

- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/pytest tests/test_stats.py tests/test_mail.py -q` → the new tests and `test_stat_row_keys_and_values` fail.

- [ ] **Step 3: Implement.** `stats.py`:

```python
# for_person, next to "weak":
        "tight": [
            t for t in summary["flow"].get("tight", []) if t["assignee"] == username
        ],
# stat_row, next to "weak":
        "tight": len(f.get("tight") or []),
# next to _weak_table:
def _tight_table(tight, who=False):
    """Due dates earlier than the finish the person's history expects."""
    return _table(
        [
            (t["iid"], t["title"], *([t["assignee"]] if who else []),
             t["due"], t["expected"], t["basis"])
            for t in tight
        ],
        "iid", "title", *(("assignee",) if who else ()), "due", "expected", "basis",
    )  # fmt: skip
# render_team_md, after the "### Stuck" table and its "":
        "### Tight dates",
        _tight_table(f.get("tight", []), who=True),
        "",
# render_person_md, after the "## Overdue" table and its "":
        "## Tight dates",
        _tight_table(person.get("tight", [])),
        "",
```

`mail.py`:

```python
# next to _weak:
def _tight(items, who=False):
    """Tight-date rows; nothing at all when there are none."""
    if not items:
        return ""

    def why(t):
        name = [t.get("assignee", "")] if who else []
        return " · ".join(name + [f"due {t.get('due')}", f"likely {t.get('expected')}"])

    return sub("Tight dates, by each person's own history") + _rows(items, "tight", why)
# _stuck: inside `if person is None:` after the Stuck rows:
        inner += _tight(f.get("tight", []), who=True)
# _yours: after the Overdue rows:
    inner += _tight(person.get("tight", []))
```

- [ ] **Step 4: Verify** — full suite `PYTHONPATH=src .venv/bin/pytest -q`, ruff clean.

---

### Task 3 (gb-j38.3): CLI — `estimate` command, `tight` in `_summary`, longer history

**Files:**
- Modify: `src/gitboard/cli.py` (`_history`, `_summary`, new `estimate` command, import)
- Modify: `Makefile` (`estimate` target)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `estimate_mod.config/suggest/tight/HISTORY_DAYS`; `_need`, `_history`, `find_spec`, `apply_mod.load/dump`, `_shortest`, `err()`.
- Produces: `gitboard estimate [SPEC] [--history FILE]`; `summary["flow"]["tight"]` on every `stats`/`digest` run.

- [ ] **Step 1: Failing tests** in `tests/test_cli.py` (use the file's existing `history_file`, `runner`, `app`; the dump's project is what `history_file` writes):

```python
def _estimate_setup(tmp_path, monkeypatch, estimates=""):
    """A spec with one undated card and a dump where alice finished 5 cards."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "boards").mkdir()
    spec = tmp_path / "boards" / "b.yaml"
    spec.write_text(
        "project: g/p\nboard: dev\n" + estimates
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
```

(Check `tests/test_cli.py`'s imports for `apply_mod`, `datetime`, `UTC`, `timedelta`; add what is missing.)

- [ ] **Step 2: Run** → `No such command 'estimate'`.

- [ ] **Step 3: Implement** in `cli.py`:

```python
from gitboard import estimate as estimate_mod   # with the other gitboard imports

# _history: fetch far enough back for estimate samples
    history, columns = board_mod.fetch_history(
        proj, board,
        since=now - timedelta(days=max(2 * days, estimate_mod.HISTORY_DAYS)),
    )

# _summary: after summarise(), before append_row
    spec_path = find_spec(meta["project"])
    cfg = estimate_mod.config(apply_mod.load(spec_path) if spec_path else {})
    summary["flow"]["tight"] = estimate_mod.tight(history, columns, now, cfg)


@app.command()
def estimate(
    spec: str | None = typer.Argument(
        None, help="Board YAML. Defaults to the config spec."
    ),
    history_file: str | None = typer.Option(
        None, "--history", help="A `stats --dump` file instead of GitLab. No network."
    ),
):
    """Stage due dates from each person's history into a board YAML. Local only."""

    def go():
        spec_path = _need(spec, "spec", "spec file")
        sp = apply_mod.load(spec_path)
        history, _, _ = _history(
            sp["project"], sp["board"], estimate_mod.HISTORY_DAYS // 2, history_file
        )
        out = estimate_mod.suggest(sp, history, datetime.now(UTC).date())
        table = Table(box=None)
        for head in ("card", "assignee", "due", "basis"):
            table.add_column(head)
        for r in out["rows"]:
            card = f"#{r['iid']} {r['title']}" if r["iid"] else f"(new) {r['title']}"
            table.add_row(card, r["assignee"], r["due"], r["basis"])
        if out["rows"]:
            err().print(table)
        if out["changed"]:
            Path(spec_path).write_text(apply_mod.dump(sp))
            err().print(
                f"[added]{len(out['rows'])} due date(s) staged[/] -> "
                f"{_shortest(spec_path)} — `gitboard plan` shows them"
            )
        elif out["rows"]:
            err().print("[muted]estimates.suggest_due is false: nothing written[/]")
        else:
            err().print(
                "[muted]nothing to estimate: no assigned, undated card has "
                "enough finished history behind it[/]"
            )

    _run(go)
```

`Makefile`, after the `stats` target:

```make
estimate: install  ## stage due dates from each person's history: make estimate SPEC=boards/x.yaml
	@$(GITBOARD) estimate $(SPEC)
```

- [ ] **Step 4: Verify** — full suite, ruff, and by hand: `gitboard stats --from <dump>` still renders; `gitboard estimate --help`.

---

### Task 4 (gb-j38.4): `/board` permission and rules, docs, CLAUDE.md

**Files:**
- Modify: `.claude/commands/board.md` (allowed-tools + a rule paragraph)
- Modify: `docs/board-yaml.md` (Keys: `estimates`), `docs/commands.md` (two lines), `CLAUDE.md` (Commands blurb, Architecture bullet, AI-pass list)

- [ ] **Step 1:** `board.md` allowed-tools gains `Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli estimate:*)`. Rule paragraph:

> **Estimate.** `gitboard estimate <spec>` (offline: `--history h.json`) stages a `due_date` on assigned, undated cards from that person's finished history and prints the basis per card. Run it after your own edits and before `plan`; quote the basis lines in your summary. Never hand-write a due date the tool declined to give — "no estimate" means not enough history. Never change a date a person set; if stats lists it under *Tight dates*, say so and let the lead decide.

- [ ] **Step 2:** `docs/board-yaml.md` Keys entry:

> `estimates`
> : Optional map. `suggest_due` (default `true`; `false` makes `gitboard estimate` print its table and write nothing), `method` (`p85` default, or `median`), `min_samples` (default 5: the fewest finished cards a bucket needs). Not sent to GitLab.

- [ ] **Step 3:** `docs/commands.md`: `gitboard estimate boards/x.yaml` and the `--history h.json` form. `CLAUDE.md`: `estimate` in the CLI-only blurb and the AI-pass allowed list; an `estimate.py` Architecture bullet (samples, ladder, `suggest`, `tight`, the no-import-back rule, `_history`'s 90-day floor).

- [ ] **Step 4: Verify** — `make docs` builds without new warnings (skip if Sphinx is not installed; say so).
