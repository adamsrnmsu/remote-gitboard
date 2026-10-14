"""Who is on a GitLab project, for the board YAML's `people:` map.

`fetch` is the one API call (read token); `merge` is pure. The map is
display name -> username, as `ingest` already reads it. A merge only adds:
the lead may have renamed someone to match another tool's roster, and a
later pull must not undo that.
"""

import re

KEEP = ("people", "emails", "estimates")  # local-only keys a re-pull carries over
_BOT = re.compile(r"(project|group)_\d+_bot")


def fetch(project):
    """{username: display name} for active, human members (inherited included)."""
    return {
        m.username: m.name
        for m in project.members_all.list(all=True)
        if m.state == "active" and not _BOT.match(m.username)
    }


def merge(people, found):
    """(people with every unknown username added, the usernames added)."""
    merged = dict(people or {})
    known = set(merged.values())
    added = []
    for username, name in sorted(found.items()):
        if username in known:
            continue
        key = name if name not in merged else f"{name} ({username})"
        merged[key] = username
        added.append(username)
    return merged, added
