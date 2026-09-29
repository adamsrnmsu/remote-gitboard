"""order.py: three-way order merge and the fewest reorder calls."""

import itertools

from gitboard import order


def test_merge_unmanaged_without_base():
    assert order.merge([2, 1], [1, 2], None) is None


def test_merge_cases():
    assert order.merge([1, 2, 3], [1, 2, 3], [1, 2, 3]) is None
    assert order.merge([1, 2, 3], [3, 2, 1], [1, 2, 3]) == "skipped"
    assert order.merge([3, 2, 1], [1, 2, 3], [1, 2, 3]) == "write"
    assert order.merge([3, 2, 1], [3, 2, 1], [1, 2, 3]) is None
    assert order.merge([3, 1, 2], [2, 1, 3], [1, 2, 3]) == "drift"


def test_merge_compares_only_common_iids():
    # 4 is new in the YAML, 5 appeared on GitLab: neither is an order change
    assert order.merge([1, 4, 2], [5, 1, 2], [1, 2]) is None


def test_moves_keep_the_longest_run():
    got = order.moves([1, 2, 3, 4, 5], [2, 3, 4, 5, 1])
    assert got == [(1, None, 2)]


def test_moves_after_predecessor():
    assert order.moves([1, 3, 2], [1, 2, 3]) == [(3, 1, None)]


def test_moves_reach_every_permutation():
    base = [1, 2, 3, 4, 5]
    for wanted in itertools.permutations(base):
        got = order.moves(list(wanted), base)
        assert order.apply_moves(base, got) == list(wanted)
        assert len(got) == len(base) - _lis(list(wanted), base)


def _lis(wanted, live):
    pos = [live.index(x) for x in wanted]
    best = [1] * len(pos)
    for i in range(len(pos)):
        for j in range(i):
            if pos[j] < pos[i]:
                best[i] = max(best[i], best[j] + 1)
    return max(best, default=0)


def test_moves_ignore_cards_not_in_both():
    got = order.moves([9, 2, 1], [1, 2, 8])
    assert got == [(2, None, 1)]
    assert order.apply_moves([1, 2], got) == [2, 1]


def test_moves_nothing_when_in_order():
    assert order.moves([], []) == []
    assert order.moves([1, 2, 3], [1, 2, 3]) == []
