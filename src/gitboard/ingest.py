"""Turn a tasks.md into board issues — the tasks file becomes the board.

Other projects' agents leave verification work in a tasks.md: a heading per
person, checkbox tasks, how to verify each, and an optional **Feedback**
block where the person answers. `parse` reads that shape (the contract is
docs/tasks-md-contract.md); `merge` folds it into a spec.

Work is done when a PERSON said so, on the issue: the latest comment whose
first line is `verified` moves the task to Done, `failed` moves it to
Failed. A `[x]` in the file moves nothing — it is only a warning when no
verdict backs it. Every issue carries a one-line `Source:` footer
(`parse_footer`) so id, commit and MR survive the trip and a commit change
can flag the task for re-verification.
Pure: no network, no file writes — the CLI does those.
"""

import re
from copy import deepcopy
from difflib import SequenceMatcher

from gitboard.apply import norm_text

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
TASK = re.compile(r"^\s*[-*+]\s+\[([ xX])\]\s+(.+?)\s*$")
BULLET = re.compile(r"^[-*+]\s+")
FEEDBACK = re.compile(r"^\s*(\*\*|__)?feedback(\*\*|__)?\s*:?\s*$", re.I)
META_KEYS = ("commit", "branch", "mr", "session", "run")
META = re.compile(rf"^({'|'.join(META_KEYS)})\s*:\s*(.+?)\s*$", re.I)
_ID = r"([A-Za-z]+-[\w.-]+)"
ID_SUFFIX = re.compile(rf"\s*(?:·\s*id:\s*{_ID}|\(id:\s*{_ID}\)|\[{_ID}\])\s*$")
EVIDENCE = re.compile(r"^evidence\s*:\s*(.*)$", re.I)
FENCE = re.compile(r"^\s*```")
VERDICT = re.compile(r"^(verified|failed)\b", re.I)
FOOTER_PREFIX = "Source: "
SIMILAR = 0.85
REVERIFY, STALE = "re-verify", "stale"


def parse(text):
    """[{person, title, id, done, verify, evidence, feedback, meta}, ...].

    Header `key: value` lines before the first heading become `meta` on
    every task. `# Title` is the document, `## Name` a person. Lines under a
    task are its verify steps until the next task, heading or feedback
    marker; an `evidence:` line among them is lifted out. Fenced code is
    skipped. Text before any task is otherwise ignored.
    """
    tasks, meta, person, task, bucket = [], {}, None, None, None
    seen_heading = fenced = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        if m := HEADING.match(line):
            seen_heading, task, bucket = True, None, None
            person = m.group(2) if len(m.group(1)) > 1 else None
            continue
        if not seen_heading and (m := META.match(line)):
            meta[m.group(1).lower()] = m.group(2)
            continue
        if m := TASK.match(line):
            title, tid = m.group(2), None
            if s := ID_SUFFIX.search(title):
                tid, title = next(g for g in s.groups() if g), title[: s.start()]
            task = {
                "person": person,
                "title": title.strip(),
                "id": tid,
                "done": m.group(1).lower() == "x",
                "verify": [],
                "evidence": None,
                "feedback": [],
                "meta": meta,
            }
            tasks.append(task)
            bucket = task["verify"]
            continue
        if task is not None and FEEDBACK.match(line):
            bucket = task["feedback"]
            continue
        if task and bucket is task["verify"] and (m := EVIDENCE.match(line.strip())):
            task["evidence"] = m.group(1).strip()
            continue
        if bucket is not None:
            bucket.append(line.strip())
    for t in tasks:
        t["verify"] = norm_text("\n".join(t["verify"]))
        t["feedback"] = norm_text("\n".join(t["feedback"]))
    return tasks


def verdict(entry):
    """'verified' | 'failed' | None — the latest person's word on the issue."""
    note = _verdict_note(entry)
    return note and VERDICT.match(note["body"].lstrip()).group(1).lower()


def _verdict_note(entry):
    for d in reversed(entry.get("discussion") or []):
        if VERDICT.match((d.get("body") or "").lstrip()):
            return d
    return None


def _footer_raw(description):
    lines = [x for x in (description or "").splitlines() if x.startswith(FOOTER_PREFIX)]
    return lines[-1] if lines else ""


def parse_footer(description):
    """The last `Source:` line as {source, person, date, id?, commit?, mr?,
    session?}; {} when there is none."""
    raw = _footer_raw(description)
    if not raw:
        return {}
    parts = [p.strip() for p in raw[len(FOOTER_PREFIX) :].split(" · ")]
    out = dict(zip(("source", "person", "date"), parts, strict=False))
    for p in parts[3:]:
        k, _, v = p.partition(":")
        out[k.strip()] = v.strip()
    return out


def footer_line(source, person, date, tid=None, meta=None):
    """`Source: src · person · date[ · id: T-x][ · commit: sha][ · mr: !N]
    [ · session: id]` — one line, the inverse of `parse_footer`."""
    meta = meta or {}
    parts = [f"{FOOTER_PREFIX}{source}", person or "unattributed", date]
    if tid:
        parts.append(f"id: {tid}")
    if c := meta.get("commit"):
        parts.append(f"commit: {c}")
    if mr := meta.get("mr"):
        parts.append(f"mr: {mr if str(mr).startswith('!') else f'!{mr}'}")
    if s := meta.get("session"):
        parts.append(f"session: {s}")
    return " · ".join(parts)


def _with_footer(description, footer):
    """Replace the footer line, or append one; the rest is untouched."""
    lines = (description or "").splitlines()
    idx = [i for i, x in enumerate(lines) if x.startswith(FOOTER_PREFIX)]
    if idx:
        lines[idx[-1]] = footer
        return "\n".join(lines)
    return norm_text("\n".join(lines) + "\n\n" + footer)


def description(task, footer):
    """Verify steps as a GitLab task list, evidence, footer."""
    steps = [
        x if x.startswith("- [") else f"- [ ] {BULLET.sub('', x)}"
        for x in task["verify"].splitlines()
        if x.strip()
    ]
    blocks = ["\n".join(steps)] if steps else []
    if task["evidence"]:
        blocks.append(f"Evidence: {task['evidence']}")
    blocks.append(footer)
    return "\n\n".join(blocks)


def _key(title):
    return " ".join(title.strip().casefold().split())


def _ensure_column(spec, name, color):
    if not any(c["name"] == name for c in spec["columns"]):
        spec["columns"].append({"name": name, "color": color})


def _set_column(entry, target, columns):
    labels = entry.get("labels") or []
    if target in labels:
        return False
    entry["labels"] = [x for x in labels if x not in columns] + [target]
    return True


def _add_label(entry, label):
    if label not in (entry.get("labels") or []):
        entry.setdefault("labels", []).append(label)
        return True
    return False


def _drop_label(entry, label):
    if label in (entry.get("labels") or []):
        entry["labels"].remove(label)


def merge(spec, tasks, source, date, column="Verify", done="Done", failed="Failed"):
    """Fold parsed tasks into the spec, in place. Returns a summary dict:
    added, moved, notes, reverify, stale (ints); unmapped, unverified,
    similar, retitled (lists); changed (bool — False means the spec is as
    it was, so the caller need not rewrite the file).

    Identity: footer `id:`, then the exact title, then nothing — a title
    within SIMILAR of an existing one is reported, not added. Columns come
    only from verdicts (`verdict`); `[x]` without one is `unverified`.
    """
    before = deepcopy(spec)
    people = spec.get("people") or {}
    issues = spec["issues"]
    by_id, by_title = {}, {}
    for i in issues:
        if tid := parse_footer(i.get("description")).get("id"):
            by_id[tid] = i
        by_title[_key(i["title"])] = i
    out = {
        "added": 0,
        "moved": 0,
        "notes": 0,
        "reverify": 0,
        "stale": 0,
        "unmapped": set(),
        "unverified": [],
        "similar": [],
        "retitled": [],
    }
    matched = set()
    # a verdict moves the card out of *every* column, not just the three
    # this flow owns — a card someone parked in Review is still verified
    cols = {column, done, failed, *(c["name"] for c in spec["columns"])}

    for t in tasks:
        who = t["person"]
        if who and who not in people:
            out["unmapped"].add(who)
        entry = by_id.get(t["id"]) if t["id"] else None
        if entry is not None:
            if _key(entry["title"]) != _key(t["title"]):
                out["retitled"].append((entry["title"], t["title"]))
        else:
            entry = by_title.get(_key(t["title"]))
        if entry is None:
            want = _key(t["title"])
            scored = (
                (SequenceMatcher(None, k, want).ratio(), i) for k, i in by_title.items()
            )
            ratio, best = max(scored, default=(0, None), key=lambda x: x[0])
            if ratio >= SIMILAR:
                out["similar"].append((t["title"], best["title"], round(ratio, 2)))
                matched.add(id(best))
                continue
            _ensure_column(spec, column, "carrot orange")
            # type::verify is the scoped-label vocabulary (epic::/story::/type::)
            # the stats read; ingest is the one place new cards are born
            entry = {"title": t["title"], "labels": [column, "type::verify"]}
            if who and who in people:
                entry["assignee"] = people[who]
            entry["description"] = description(
                t, footer_line(source, who, date, t["id"], t["meta"])
            )
            issues.append(entry)
            by_title[want] = entry
            if t["id"]:
                by_id[t["id"]] = entry
            out["added"] += 1
        else:
            f = parse_footer(entry.get("description"))
            commit = t["meta"].get("commit")
            # a footer that never carried a commit is gaining lineage, not
            # losing a verification: only a *different* commit re-opens it
            changed = bool(commit) and bool(f.get("commit")) and f["commit"] != commit
            want = footer_line(
                f.get("source") or source,
                f.get("person") or who,
                date if changed else f.get("date") or date,
                f.get("id") or t["id"],
                {**f, **t["meta"]},
            )
            if want != _footer_raw(entry.get("description")):
                entry["description"] = _with_footer(entry.get("description"), want)
            if changed:
                _add_label(entry, REVERIFY)
                out["reverify"] += 1
        matched.add(id(entry))

        if note := _verdict_note(entry):
            target = done if verdict(entry) == "verified" else failed
            color = "medium sea green" if target == done else "crimson"
            _ensure_column(spec, target, color)
            out["moved"] += _set_column(entry, target, cols)
            # ponytail: footer date is bumped on a commit change, so a verdict
            # dated on/after it is "after"; same-day is ambiguous, counts as after
            stamp = parse_footer(entry.get("description")).get("date") or ""
            if str(note.get("at", "")) >= stamp:
                _drop_label(entry, REVERIFY)
        elif t["done"]:
            out["unverified"].append(t["title"])

        if t["feedback"]:
            name = f"@{people[who]}" if who in people else (who or "unattributed")
            body = f"*feedback from {name} via {source}:*\n\n{t['feedback']}"
            posted = {norm_text(d["body"]) for d in entry.get("discussion") or []}
            staged = [norm_text(n) for n in entry.get("notes") or []]
            if body not in posted and body not in staged:
                entry.setdefault("notes", []).append(body)
                out["notes"] += 1

    for i in issues:
        if parse_footer(i.get("description")).get("source") != source:
            continue
        if id(i) in matched:
            _drop_label(i, STALE)
        else:
            out["stale"] += _add_label(i, STALE)
    out["unmapped"] = sorted(out["unmapped"])
    out["changed"] = spec != before
    return out
