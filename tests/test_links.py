"""links.py: refs, the footer, the cycle check, and link read/sync on fakes."""

import types

import pytest
from gitlab.exceptions import GitlabCreateError, GitlabListError

from gitboard import client, links

P = "grp/proj"


def test_norm_ref_forms():
    titles = {"Token rotation": 9, "Brand new": None}
    assert links.norm_ref(9, P, titles) == "9"
    assert links.norm_ref("9", P, titles) == "9"
    assert links.norm_ref("#9", P, titles) == "9"
    assert links.norm_ref("grp/proj#9", P, titles) == "9"
    assert links.norm_ref("infra/platform#4", P, titles) == "infra/platform#4"
    assert links.norm_ref("Token rotation", P, titles) == "9"
    assert links.norm_ref(" Brand new ", P, titles) == "new:Brand new"
    with pytest.raises(ValueError, match="unknown blocker"):
        links.norm_ref("No such card", P, titles)


def test_norm_refs_sorts_numerically_and_dedupes():
    got = links.norm_refs(
        [10, "grp/proj#9", 9, "a/b#1", "Brand new"], P, {"Brand new": None}
    )
    assert got == ["9", "10", "a/b#1", "new:Brand new"]


def test_footer_refs_reads_only_the_last_nonempty_line():
    body = "Some text\nBlocked by: #9, infra/platform#4, grp/proj#11\n\n"
    assert links.footer_refs(body, P) == ["9", "11", "infra/platform#4"]
    assert links.footer_refs("Blocked by: #9\nmore text", P) == []
    assert links.footer_refs(None, P) == []
    assert links.footer_refs("blocked BY:  #3", P) == ["3"]


def test_priority_is_the_lowest_digit():
    assert links.priority(["Doing", "priority::3"]) == 3
    assert links.priority(["priority::2", "priority::1"]) == 1
    assert links.priority(["priority::high"]) is None
    assert links.priority([]) is None


def spec(*issues):
    return {"project": P, "board": "b", "issues": list(issues)}


def test_check_accepts_iids_titles_and_externals():
    links.check(
        spec(
            {"title": "a", "iid": 1, "blocked_by": ["b", 3, "x/y#2"]},
            {"title": "b"},
        )
    )


@pytest.mark.parametrize(
    "issues, match",
    [
        ([{"title": "a", "blocked_by": ["nope"]}], "'nope'"),
        ([{"title": "a", "iid": 1, "blocked_by": [1]}], "blocks itself"),
        (  # a cycle through an iid and a title
            [
                {"title": "a", "iid": 1, "blocked_by": ["b"]},
                {"title": "b", "iid": 2, "blocked_by": [1]},
            ],
            "cycle",
        ),
    ],
    ids=["unknown-title", "self-block", "cycle"],
)
def test_check_rejects(issues, match):
    with pytest.raises(ValueError, match=match):
        links.check(spec(*issues))


# --- read / sync on fakes ----------------------------------------------------


class FakeLinks:
    def __init__(self, items=(), downgrade=False, error=None, missing=()):
        self.items, self.downgrade, self.error = list(items), downgrade, error
        self.missing = set(missing)
        self.created, self.deleted = [], []

    def list(self, **_):
        if self.error:
            raise self.error
        return list(self.items)

    def create(self, data):
        if data["target_issue_iid"] in self.missing:
            raise GitlabCreateError("404 Not found", response_code=404)
        self.created.append(data)
        kind = "relates_to" if self.downgrade else data["link_type"]
        self.items.append(
            link(
                data["target_issue_iid"],
                data["target_project_id"],
                kind,
                900 + len(self.created),
            )
        )
        return (None, None)

    def delete(self, link_id):
        self.deleted.append(link_id)
        self.items = [x for x in self.items if x.issue_link_id != link_id]


def link(iid, project_id, kind="is_blocked_by", link_id=1, full=None):
    return types.SimpleNamespace(
        iid=iid,
        project_id=project_id,
        link_type=kind,
        issue_link_id=link_id,
        link_created_at="2026-09-20T10:00:00Z",
        references={"full": full or f"other/x#{iid}"},
    )


PROJECT = types.SimpleNamespace(id=7, path_with_namespace=P)


def issue(items=(), description="", **kw):
    return types.SimpleNamespace(
        iid=12, title="t", description=description, links=FakeLinks(items, **kw)
    )


def test_read_keeps_only_is_blocked_by_and_maps_projects():
    i = issue(
        [
            link(9, 7),
            link(4, 8, full="infra/platform#4", link_id=2),
            link(5, 7, "relates_to", 3),
        ]
    )
    got = links.read(i, PROJECT)
    assert [(g["ref"], g["source"], g["link_id"]) for g in got] == [
        ("9", "native", 1),
        ("infra/platform#4", "native", 2),
    ]
    assert got[0]["since"] == "2026-09-20T10:00:00Z"


def test_read_unions_the_footer_native_wins():
    i = issue([link(9, 7)], description="x\nBlocked by: #9, #11")
    got = links.read(i, PROJECT)
    assert [(g["ref"], g["source"]) for g in got] == [("9", "native"), ("11", "footer")]


def test_read_falls_back_to_footer_on_403():
    err = GitlabListError("forbidden", response_code=403)
    i = issue(description="Blocked by: #3", error=err)
    assert [g["ref"] for g in links.read(i, PROJECT)] == ["3"]


def test_read_raises_other_errors():
    i = issue(error=GitlabListError("boom", response_code=500))
    with pytest.raises(GitlabListError):
        links.read(i, PROJECT)


def test_sync_adds_and_removes_native_links():
    i = issue([link(9, 7, link_id=1), link(4, 8, full="infra/platform#4", link_id=2)])
    have = links.read(i, PROJECT)
    records = links.sync(
        i, PROJECT, ["9", "10", "other/y#5"], have, lambda path: {"other/y": 55}[path]
    )
    assert i.links.deleted == [2]
    assert {
        (d["target_issue_iid"], d["target_project_id"]) for d in i.links.created
    } == {
        (10, 7),
        (5, 55),
    }
    assert all(d["link_type"] == "is_blocked_by" for d in i.links.created)
    assert records == []


def test_sync_never_duplicates_or_removes_a_footer_ref():
    i = issue(description="Blocked by: #9")
    have = links.read(i, PROJECT)
    assert links.sync(i, PROJECT, ["9"], have, None) == []
    assert i.links.created == []
    records = links.sync(i, PROJECT, [], have, None)
    assert records == [
        ("skipped", "link", "t: blocker #9 is a footer ref — edit the description")
    ]


def test_sync_downgrade_deletes_and_raises_once():
    i = issue(downgrade=True)
    with pytest.raises(client.GitlabProblem, match="Premium"):
        links.sync(i, PROJECT, ["9", "10"], [], None)
    assert i.links.items == []  # the relates_to links were deleted again


def test_sync_skips_a_missing_target_and_links_the_rest():
    def project_id_of(path):
        raise client.GitlabProblem(f"no project {path!r}")

    i = issue(missing={11})
    records = links.sync(i, PROJECT, ["9", "11", "gone/x#4"], [], project_id_of)
    assert [d["target_issue_iid"] for d in i.links.created] == [9]
    assert records == [
        ("skipped", "link", "t: blocker #11 not found"),
        ("skipped", "link", "t: blocker gone/x#4 not found"),
    ]


def test_unicode_digits_are_not_iids():
    # "²".isdigit() is True but int("²") raises: a title or label must not crash.
    assert links.norm_ref("²", P, {"²": None}) == "new:²"
    assert links.priority(["priority::²"]) is None
    assert links.norm_refs(["²", 3], P, {"²": 5}) == ["3", "5"]


def test_sync_skips_a_target_already_linked_another_way():
    class Conflict(FakeLinks):
        def create(self, data):
            if data["target_issue_iid"] == 9:
                raise GitlabCreateError("Issue(s) already assigned", response_code=409)
            return super().create(data)

    i = issue()
    i.links = Conflict()
    records = links.sync(i, PROJECT, ["9", "10"], [], None)
    assert [d["target_issue_iid"] for d in i.links.created] == [10]
    assert records == [
        ("skipped", "link", "t: blocker #9 is already linked another way")
    ]


def test_sync_removing_a_native_link_the_footer_also_holds_says_so():
    i = issue([link(9, 7, link_id=1)], description="Blocked by: #9")
    records = links.sync(i, PROJECT, [], links.read(i, PROJECT), None)
    assert i.links.deleted == [1]
    assert records == [
        ("skipped", "link", "t: blocker #9 is a footer ref — edit the description")
    ]
