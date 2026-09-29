"""Card-level edits to a board spec — what the TUI's card keys stage. Pure.

Each action mutates the spec dict and returns the line the TUI shows
(`#12 Doing → Review`); a refusal raises `EditError` with the reason and
leaves the spec alone. Writing the YAML is the caller's job.

The verdict rule holds for people as it does for the agent: a card in
Verify is not moved by key, and Done / Failed are not targets — those are a
`verified:` / `failed:` comment on the card, so the board shows who checked.
"""

from datetime import date, timedelta

BACKLOG = "Backlog"
LOCKED = {"Verify"}
VERDICT_ONLY = {"Done": "verified", "Failed": "failed"}


class EditError(Exception):
    """The edit was refused; the message says why. The TUI shows it."""


def columns(spec):
    return [c["name"] for c in spec.get("columns") or []]


def column_of(spec, entry):
    """Column label(s) the entry carries, joined by "+", else Backlog."""
    have = entry.get("labels") or []
    return "+".join(c for c in columns(spec) if c in have) or BACKLOG


def find(spec, ref):
    """The entry for a card: by iid (an int), or by title for a card that
    has no number yet (a str, compared like `load` does: stripped, casefolded)."""
    if isinstance(ref, int):
        return next((i for i in spec["issues"] if i.get("iid") == ref), None)
    want = ref.strip().casefold()
    return next(
        (i for i in spec["issues"] if str(i["title"]).strip().casefold() == want), None
    )


def name(ref):
    """How a staged line names the card: `#12`, or `“title”` before it has one."""
    return f"#{ref}" if isinstance(ref, int) else f"“{ref.strip()}”"


def adopt(spec, entry):
    """Add a card the YAML has not seen yet; the existing entry wins. Its
    milestone gets a `milestones:` entry, or load() refuses the file."""
    if (have := find(spec, entry["iid"])) is not None:
        return have
    spec["issues"].append(entry)
    known = spec.get("milestones") or []
    if (m := entry.get("milestone")) and m not in {x["title"] for x in known}:
        spec["milestones"] = [*known, {"title": m}]
    return entry


def _need(spec, ref):
    entry = find(spec, ref)
    if entry is None:
        raise EditError(f"{name(ref)} is not on this board")
    return entry


def _target(spec, column):
    if column in VERDICT_ONLY:
        word = VERDICT_ONLY[column]
        raise EditError(f"{column} needs a `{word}:` comment on the card, not a move")
    if column != BACKLOG and column not in columns(spec):
        raise EditError(f"no column named {column!r}")


def move(spec, ref, column):
    entry = _need(spec, ref)
    was = column_of(spec, entry)
    if LOCKED & set(was.split("+")):
        raise EditError(
            f"{name(ref)} is in {was}: it leaves on a `verified:` or `failed:` comment"
        )
    _target(spec, column)
    if was == column:
        raise EditError(f"{name(ref)} is already in {column}")
    cols = set(columns(spec))
    labels = [x for x in entry.get("labels") or [] if x not in cols]
    if column != BACKLOG:
        labels.append(column)
    if labels:
        entry["labels"] = sorted(labels)
    else:
        entry.pop("labels", None)
    return f"{name(ref)} {was} → {column}"


def assign(spec, ref, username):
    entry = _need(spec, ref)
    username = username.strip().lstrip("@")
    if not username:
        raise EditError("type a username")
    was = entry.get("assignee")
    if was == username:
        raise EditError(f"{name(ref)} is already {username}'s")
    entry["assignee"] = username
    return f"{name(ref)} assignee {was or 'none'} → {username}"


def set_due(spec, ref, text, today):
    """`YYYY-MM-DD`, or `+N` days from `today`."""
    entry = _need(spec, ref)
    text = text.strip()
    try:
        if text.startswith("+"):
            due = today + timedelta(days=int(text[1:]))
        else:
            due = date.fromisoformat(text)
    except ValueError:
        raise EditError("a date is YYYY-MM-DD, or +N for days from today") from None
    was = entry.get("due_date")
    entry["due_date"] = due.isoformat()
    return f"{name(ref)} due {was or 'none'} → {due.isoformat()}"


def add_note(spec, ref, body):
    entry = _need(spec, ref)
    body = body.strip()
    if not body:
        raise EditError("an empty comment is not staged")
    notes = entry.setdefault("notes", [])
    if body in notes:
        raise EditError(f"{name(ref)} already stages that comment")
    notes.append(body)
    return f"{name(ref)} note: {body}"


def new_card(spec, title, column):
    title = title.strip()
    if not title:
        raise EditError("a card needs a title")
    if any(i["title"].casefold() == title.casefold() for i in spec["issues"]):
        raise EditError(f"a card titled {title!r} is already here")
    _target(spec, column)
    entry = {"title": title}
    if column != BACKLOG:
        entry["labels"] = [column]
    spec["issues"].append(entry)
    return f"(new) {title} → {column}"
