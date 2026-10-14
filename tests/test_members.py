import types

from gitboard import members


def _member(username, name, state="active"):
    return types.SimpleNamespace(username=username, name=name, state=state)


def _project(*rows):
    return types.SimpleNamespace(
        members_all=types.SimpleNamespace(list=lambda **k: list(rows))
    )


def test_fetch_keeps_active_people_and_drops_bots():
    project = _project(
        _member("alice", "Alice Ng"),
        _member("gone", "Gone Person", state="blocked"),
        _member("project_12_bot_ab12", "Bot"),
        _member("group_7_bot", "Group bot"),
        _member("bruiz", "Bob Ruiz"),
    )
    assert members.fetch(project) == {"alice": "Alice Ng", "bruiz": "Bob Ruiz"}


def test_merge_adds_only_new_usernames_and_never_renames():
    people = {"Alice": "alice"}  # the lead renamed Alice Ng to match people.csv
    merged, added = members.merge(people, {"alice": "Alice Ng", "bruiz": "Bob Ruiz"})
    assert merged == {"Alice": "alice", "Bob Ruiz": "bruiz"}
    assert added == ["bruiz"]


def test_merge_names_a_display_name_clash_by_username():
    merged, added = members.merge({"Sam Lee": "slee"}, {"slee2": "Sam Lee"})
    assert merged == {"Sam Lee": "slee", "Sam Lee (slee2)": "slee2"}
    assert added == ["slee2"]


def test_merge_from_nothing():
    assert members.merge(None, {}) == ({}, [])
