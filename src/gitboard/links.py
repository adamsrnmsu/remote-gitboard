"""Blocker links: refs, the description footer, the cycle check, and the
GitLab read/sync. Pure except `read`/`sync`, which take API objects.

A ref is a string: "9" (this project), "grp/x#4" (another project), or
"new:<title>" (a spec card GitLab has not seen). Native `is_blocked_by`
links are what gets written; a `Blocked by: #9` last line is only read, so
a Free/CE dev instance (which downgrades `blocks` to `relates_to`) can still
show a graph.
"""

import graphlib
import re

from gitlab.exceptions import GitlabError

from gitboard import client
from gitboard.log import get_logger

log = get_logger()

BLOCKED_BY = "is_blocked_by"
PRIORITY = "priority::"
FOOTER = re.compile(r"^\s*blocked by:\s*(.+?)\s*$", re.IGNORECASE)
REF = re.compile(r"^(?P<path>[\w.\-/]+)?#(?P<iid>[0-9]+)$")
_warned = set()  # projects already warned about unreadable links


def _is_iid(text):
    """ASCII digits only: "²".isdigit() is True, and int("²") raises."""
    return text.isascii() and text.isdigit()


def ref_key(ref):
    """Same-project refs by number, then external refs, then new cards."""
    if _is_iid(ref):
        return (0, int(ref), "")
    if ref.startswith("new:"):
        return (2, 0, ref)
    return (1, 0, ref)


def norm_ref(value, project_path, titles):
    """One canonical ref, so `9`, `#9` and `grp/proj#9` compare equal.
    `titles` maps a spec card's stripped title to its iid (None if new)."""
    if isinstance(value, int):
        return str(value)
    text = str(value).strip()
    if _is_iid(text):
        return text
    if m := REF.match(text):
        path = m["path"]
        return m["iid"] if path in (None, project_path) else f"{path}#{m['iid']}"
    if text in titles:
        iid = titles[text]
        return str(iid) if iid is not None else f"new:{text}"
    raise ValueError(
        f"unknown blocker {text!r}: not an iid, a group/project#iid, or a card title"
    )


def norm_refs(values, project_path, titles):
    return sorted(
        {norm_ref(v, project_path, titles) for v in values or []}, key=ref_key
    )


def footer_refs(description, project_path):
    """Refs from a `Blocked by: #9, grp/x#4` last non-empty line. Anything
    that is not a ref is ignored: this is prose a person wrote."""
    lines = [
        x for x in (description or "").replace("\r\n", "\n").split("\n") if x.strip()
    ]
    if not lines or not (m := FOOTER.match(lines[-1])):
        return []
    refs = [x.strip() for x in m[1].split(",")]
    return norm_refs([r for r in refs if REF.match(r)], project_path, {})


def priority(labels):
    """The lowest `priority::N` digit (1 is most urgent), or None."""
    digits = [
        int(x[len(PRIORITY) :])
        for x in labels or []
        if x.startswith(PRIORITY) and _is_iid(x[len(PRIORITY) :])
    ]
    return min(digits) if digits else None


def check(spec):
    """Unknown title refs, self-blocks and cycles among the spec's cards."""
    project = spec["project"]
    titles = {i["title"].strip(): i.get("iid") for i in spec["issues"]}
    key_of = {
        t: (str(iid) if iid is not None else f"new:{t}") for t, iid in titles.items()
    }
    graph = {}
    for i in spec["issues"]:
        if "blocked_by" not in i:
            continue
        title = i["title"].strip()
        try:
            refs = norm_refs(i["blocked_by"], project, titles)
        except ValueError as e:
            raise ValueError(f"{title!r}: {e}") from e
        me = key_of[title]
        if me in refs:
            raise ValueError(f"{title!r} blocks itself")
        graph[me] = set(refs)
    try:
        graphlib.TopologicalSorter(graph).prepare()
    except graphlib.CycleError as e:
        raise ValueError(f"blocked_by cycle: {' -> '.join(e.args[1])}") from e


def _ref_of(link, project):
    if link.project_id == project.id:
        return str(link.iid)
    full = (getattr(link, "references", None) or {}).get("full")
    if full:
        return norm_ref(full, project.path_with_namespace, {})
    path = link.web_url.split("/-/issues/")[0].split("://", 1)[-1].split("/", 1)[1]
    return f"{path}#{link.iid}"


def _shown(ref):
    return f"#{ref}" if _is_iid(ref) else ref


def read(issue, project):
    """Native is_blocked_by links, then footer refs not already linked."""
    path = project.path_with_namespace
    try:
        native = [
            {
                "ref": _ref_of(x, project),
                "since": getattr(x, "link_created_at", None),
                "link_id": x.issue_link_id,
                "source": "native",
                "state": getattr(x, "state", None),  # the linked issue's
            }
            for x in issue.links.list(all=True)
            if getattr(x, "link_type", None) == BLOCKED_BY
        ]
    except GitlabError as e:
        if e.response_code not in (403, 404):
            raise
        if path not in _warned:
            _warned.add(path)
            log.warning(
                "%s: issue links unavailable (%s); reading footer refs only",
                path,
                e.response_code,
            )
        native = []
    seen = {x["ref"] for x in native}
    footer = [
        {"ref": r, "since": None, "link_id": None, "source": "footer", "state": None}
        for r in footer_refs(getattr(issue, "description", None), path)
        if r not in seen
    ]
    return sorted(native, key=lambda x: ref_key(x["ref"])) + footer


def sync(issue, project, want, have, project_id_of):
    """Make the native links match `want` (refs, no "new:"). Footer refs are
    never duplicated as links and never removed (that is a description edit).
    A target that does not exist or cannot be seen is skipped, the rest apply."""
    records = []
    have_refs = {x["ref"]: x for x in have}
    # read() hides a footer ref behind a native link to the same card, and
    # deleting that link alone would leave the blocker showing.
    footer = set(footer_refs(issue.description, project.path_with_namespace))
    for ref, x in have_refs.items():
        if ref in want:
            continue
        if x["source"] == "native":
            issue.links.delete(x["link_id"])
        if x["source"] == "footer" or ref in footer:
            why = f"blocker {_shown(ref)} is a footer ref — edit the description"
            records.append(("skipped", "link", f"{issue.title}: {why}"))
    added = []
    for ref in want:
        if ref in have_refs:
            continue
        try:
            if _is_iid(ref):
                target, iid = project.id, int(ref)
            else:
                path, iid = ref.rsplit("#", 1)
                target, iid = project_id_of(path), int(iid)
            issue.links.create(
                {
                    "target_project_id": target,
                    "target_issue_iid": iid,
                    "link_type": BLOCKED_BY,
                }
            )
        except (client.GitlabProblem, GitlabError) as e:
            code = getattr(e, "response_code", 404)
            if code not in (403, 404, 409):
                raise
            # 409: a relates_to link to that card already exists.
            why = "is already linked another way" if code == 409 else "not found"
            records.append(
                ("skipped", "link", f"{issue.title}: blocker {_shown(ref)} {why}")
            )
            continue
        added.append((target, iid))
    if added:
        # CE answers 201 but stores relates_to: the only tell is the re-read.
        wrong = [
            x
            for x in issue.links.list(all=True)
            if (x.project_id, x.iid) in added and x.link_type != BLOCKED_BY
        ]
        for x in wrong:
            issue.links.delete(x.issue_link_id)
        if wrong:
            raise client.GitlabProblem(
                "blocking links need GitLab Premium: this instance made a "
                f"{wrong[0].link_type} link instead (removed again)"
            )
    return records
