"""Turn a tasks.md into board issues — the tasks file becomes the board.

Other projects' agents leave verification work in a tasks.md: a heading per
person, checkbox tasks, how to verify each, and an optional **Feedback**
block where the person answers. `parse` reads that shape tolerantly;
`merge` folds it into a spec: new tasks become issues in a Verify column,
checked ones move to Done, feedback is staged as a note with attribution,
and every issue carries a `Source:` footer so lineage survives the trip.
Pure: no network, no file writes — the CLI does those.
"""

import re

from gitboard.apply import norm_text

HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
TASK = re.compile(r"^\s*[-*+]\s+\[([ xX])\]\s+(.+?)\s*$")
FEEDBACK = re.compile(r"^\s*(\*\*|__)?feedback(\*\*|__)?\s*:?\s*$", re.I)


def parse(text):
    """[{person, title, done, verify, feedback}, ...] in file order.

    Lines under a task are its verify steps until the next task, heading or
    feedback marker; lines after **Feedback** belong to that task's feedback.
    Text before any task is ignored.
    """
    tasks, person, task, bucket = [], None, None, None
    for raw in text.splitlines():
        line = raw.rstrip()
        if m := HEADING.match(line):
            person, task, bucket = m.group(1), None, None
            continue
        if m := TASK.match(line):
            task = {
                "person": person,
                "title": m.group(2),
                "done": m.group(1).lower() == "x",
                "verify": [],
                "feedback": [],
            }
            tasks.append(task)
            bucket = task["verify"]
            continue
        if task is not None and FEEDBACK.match(line):
            bucket = task["feedback"]
            continue
        if bucket is not None:
            bucket.append(line.strip())
    for t in tasks:
        t["verify"] = norm_text("\n".join(t["verify"]))
        t["feedback"] = norm_text("\n".join(t["feedback"]))
    return tasks


def _ensure_column(spec, name, color):
    if not any(c["name"] == name for c in spec["columns"]):
        spec["columns"].append({"name": name, "color": color})


def merge(spec, tasks, source, date, column="Verify", done="Done"):
    """Fold parsed tasks into the spec, in place. Returns a summary dict.

    Identity is the title, like apply. A known title is updated (checked ->
    moves to `done`; new feedback -> one staged note); an unknown one is
    added. Feedback is attributed in the note body, so re-ingesting the same
    file stages nothing twice.
    """
    _ensure_column(spec, column, "carrot orange")
    _ensure_column(spec, done, "medium sea green")
    people = spec.get("people") or {}
    have = {i["title"]: i for i in spec["issues"]}
    out = {"added": 0, "moved": 0, "notes": 0, "unmapped": set()}

    for t in tasks:
        who = t["person"]
        if who and who not in people:
            out["unmapped"].add(who)
        target = done if t["done"] else column
        entry = have.get(t["title"])
        if entry is None:
            entry = {"title": t["title"], "labels": [target]}
            if who and who in people:
                entry["assignee"] = people[who]
            footer = f"Source: {source} · {who or 'unattributed'} · {date}"
            verify = t["verify"] + "\n\n" if t["verify"] else ""
            entry["description"] = verify + footer
            spec["issues"].append(entry)
            have[t["title"]] = entry
            out["added"] += 1
        elif t["done"] and done not in entry.get("labels", []):
            entry["labels"] = [x for x in entry.get("labels", []) if x != column] + [
                done
            ]
            out["moved"] += 1
        if t["feedback"]:
            body = f"*feedback from {who or 'the file'} via {source}:*\n\n"
            body += t["feedback"]
            posted = {norm_text(d["body"]) for d in entry.get("discussion") or []}
            staged = [norm_text(n) for n in entry.get("notes") or []]
            if body not in posted and body not in staged:
                entry.setdefault("notes", []).append(body)
                out["notes"] += 1
    out["unmapped"] = sorted(out["unmapped"])
    return out
