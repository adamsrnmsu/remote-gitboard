"""Board order: GitLab keeps one manual order per project (relative_position)
and every list shows it. The YAML's issue order stands for it. Pure.

Only iids present on every side are compared: a card new in the YAML or new
on GitLab is not an order change, and lands wherever GitLab puts it.
"""

from bisect import bisect_left


def _common(*seqs):
    keep = set(seqs[0]).intersection(*seqs[1:])
    return [[x for x in s if x in keep] for s in seqs]


def merge(edited, live, old):
    """The order as one field of the three-way merge: "write", "skipped",
    "drift", or None when there is nothing to do. No base (`old`) means the
    YAML was hand-written, and a hand-written YAML never reshuffles the board.
    """
    if old is None:
        return None
    edited, live, old = _common(edited, live, old)
    if edited == old:
        return "skipped" if live != old else None
    if live == edited:
        return None
    return "write" if live == old else "drift"


def _kept(wanted, live):
    """The longest run of `wanted` already in `live` order (patience LIS)."""
    pos = {x: i for i, x in enumerate(live)}
    seq = [pos[x] for x in wanted]
    tails, tails_at, prev = [], [], [None] * len(seq)
    for i, p in enumerate(seq):
        k = bisect_left(tails, p)
        if k == len(tails):
            tails.append(p)
            tails_at.append(i)
        else:
            tails[k], tails_at[k] = p, i
        prev[i] = tails_at[k - 1] if k else None
    kept, i = set(), tails_at[-1] if tails_at else None
    while i is not None:
        kept.add(wanted[i])
        i = prev[i]
    return kept


def moves(wanted, live):
    """(iid, after_iid, before_iid) per card outside the longest kept run.

    GitLab reorders one card per call, so the fewest calls is every card but
    the ones already in relative order. Emitted in `wanted` order, each
    card's predecessor is already placed when it moves; `before_iid` is set
    only for a card going to the front.
    """
    wanted, live = _common(wanted, live)
    kept = _kept(wanted, live)
    first_kept = next((x for x in wanted if x in kept), None)
    out = []
    for at, iid in enumerate(wanted):
        if iid in kept:
            continue
        after = wanted[at - 1] if at else None
        out.append((iid, after, first_kept if after is None else None))
    return out


def apply_moves(order, moves_):
    """`moves` played on a list, as GitLab would: for tests and plan text."""
    order = list(order)
    for iid, after, before in moves_:
        if iid not in order:
            continue
        order.remove(iid)
        at = order.index(after) + 1 if after is not None else order.index(before)
        order.insert(at, iid)
    return order
