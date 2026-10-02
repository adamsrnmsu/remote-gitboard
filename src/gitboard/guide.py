"""The TUI's keybar and its guide texts. Data and one renderer; no I/O.

The guide is on by default: pressing a mode key shows what it does, a worked
example and what it will and will not write, beside the prompt. It never
blocks, so it costs an experienced user nothing. Off with `guide = false` in
gitboard.toml, `GITBOARD_GUIDE=0`, `tui --no-guide`, or `g` for the session.
A test pins that every keybar key below has a text.
"""

from rich.panel import Panel
from rich.text import Text

BOARD_KEYS = [("r", "reload"), ("b", "board"), ("s", "snapshot"), ("e", "edit")]
SPEC_KEYS = [
    ("p", "plan"),
    ("a", "push"),
    ("y", "sync"),
    ("f", "pull"),
]  # only once a YAML exists
CARD_KEYS = [
    ("↑↓", "select"),
    ("v", "move"),
    ("u", "assign"),
    ("d", "due"),
    ("c", "comment"),
    ("n", "new"),
]
TAIL_KEYS = [("m", "migrate"), ("g", "guide"), ("?", "help"), ("q", "quit")]
OFFLINE_KEYS = [("r", "reload"), ("e", "edit"), ("p", "diff")]
NO_GUIDE = {"g", "?", "q", "↑↓"}  # they explain themselves
PROMPTS = set("bevudcnma")  # keys that ask for input: their text needs an example


def rows(offline, has_spec):
    """The keybar as two rows of (key, label): the board's keys, the card's."""
    if offline:
        top = OFFLINE_KEYS + [k for k in TAIL_KEYS if k[0] != "m"]
    else:
        top = BOARD_KEYS + (SPEC_KEYS if has_spec else []) + TAIL_KEYS
    return [top, CARD_KEYS]


GUIDE = {
    "r": (
        "reload",
        [
            "Reads the board again. Nothing is written.",
            "Use it after someone moved a card in GitLab, or after a push.",
        ],
    ),
    "b": (
        "switch board",
        [
            "Lists this project's boards plus every board a boards/*.yaml names.",
            "Example: press 2 to open the second one. Any other key cancels.",
        ],
    ),
    "s": (
        "snapshot",
        [
            "Appends every card's column, assignee and due date to snapshots.jsonl.",
            "That log is what `gitboard report` reads. Local file; GitLab untouched.",
        ],
    ),
    "e": (
        "edit the YAML",
        [
            "Opens the whole board file in $EDITOR — for bulk edits. For one card,",
            "the card keys (v u d c n) are quicker and write the same file.",
            "Example: a card's column is a label, so `labels: [Doing]` ->",
            "`labels: [Review]` moves it. Save and quit; the diff shows here.",
            "Only the file changes. `a` is what pushes to GitLab.",
        ],
    ),
    "p": (
        "plan",
        [
            "Compares the YAML with the board and lists what `a` would push:",
            "+ added, ~ changed, - kept as GitLab has it, ! refused (both changed).",
            "Read-only. An empty plan means the board already matches.",
        ],
    ),
    "a": (
        "push",
        [
            "Sends your staged YAML edits to GitLab after a y/n. It only adds and",
            "changes; it never deletes or closes a card. Labels: push only manages",
            "column labels and labels your YAML uses. Any other label a card has in",
            "GitLab stays on it.",
            "Example: a card tagged `security` in GitLab keeps that tag even when",
            "your YAML never mentions `security`.",
            "Read the table, press y. Any other key backs out with nothing written.",
        ],
    ),
    "y": (
        "sync",
        [
            "Pushes your staged edits (after a y/n), logs a snapshot, then refreshes",
            "the YAML from GitLab so it holds your edits plus everything teammates",
            "changed. Nothing is left staged.",
        ],
    ),
    "f": (
        "pull",
        [
            "Replaces the YAML with the live board — GitLab to file. Asks first when",
            "that would overwrite the file, and lists any staged edits that would be",
            "lost. To keep your edits, use y (sync) instead.",
        ],
    ),
    "m": (
        "migrate comments",
        [
            "Copies a finished card's comments onto the card(s) that replace it.",
            "Example: 12 ⏎ is the source; 15 ⏎ then 16 ⏎ are destinations; ⏎ on an",
            "empty prompt runs it. b first picks another project for the next one.",
            "Then y closes #12 with a `superseded by` note; n leaves it open.",
            "Writes to GitLab at once. Nothing is deleted; a rerun copies nothing new.",
        ],
    ),
    "v": (
        "move a card",
        [
            "Stages a column change in the YAML. `a` pushes it.",
            "Example: v, 12 ⏎, then 3 for the third column listed. With a card",
            "selected (arrows or h j k l) there is no number to type: v, then 3.",
            "A card in Verify does not move by key, and Done / Failed are not",
            "offered: comment `verified: …` or `failed: …` on the card instead, so",
            "the board shows who checked it.",
        ],
    ),
    "u": (
        "assign a card",
        [
            "Stages an assignee in the YAML. `a` pushes it.",
            "Example: u, 12 ⏎, then 2 for the second person — or t to type a",
            "GitLab username that is not listed yet. A selected card (arrows)",
            "skips the number.",
        ],
    ),
    "d": (
        "due date",
        [
            "Stages a due date in the YAML. `a` pushes it.",
            "Example: d, 12 ⏎, then 2026-10-01 ⏎, or +3 ⏎ for three days from today.",
            "A selected card (arrows) skips the number.",
            "e instead asks the assignee's finished history (`gitboard estimate`)",
            "and says what the figure rests on. No estimate means too little history.",
        ],
    ),
    "c": (
        "comment",
        [
            "Stages one comment under the card's `notes:`. `a` pushes it, marked",
            "*staged via gitboard*; a body already on the card is not posted twice.",
            "Example: c, 12 ⏎ (or select the card with the arrows), then",
            "`Q: is the token rotation still blocking this?` ⏎",
            "— a line starting `Q:` shows up as a waiting question in the digests.",
        ],
    ),
    "n": (
        "new card",
        [
            "Stages a new card in the YAML; it shows as (new) until `a` pushes it.",
            "Example: n, `Rotate the deploy token` ⏎, then 1 for the first column.",
            "The title is its identity until GitLab gives it a number, so a second",
            "card with the same title is refused.",
        ],
    ),
}


def panel(key):
    """The guide for `key`, or None when it has none."""
    if key not in GUIDE:
        return None
    title, lines = GUIDE[key]
    body = Text("\n".join(lines))
    body.append("\n g hides these · `guide = false` in gitboard.toml for good", "dim")
    return Panel(body, title=f"guide — {title}", border_style="dim", padding=(0, 1))
