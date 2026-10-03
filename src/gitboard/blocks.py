"""Blocks: the output format `perch tui` asks for with PI_BLOCKS=1.

Contract: perch/docs/superpowers/specs/2026-10-02-tui-blocks-design.md. One
JSON object per stdout line; plain dicts. Each report is built once as blocks
and its markdown is rendered from them (`to_md`), so the two cannot drift.
"""

import json
import os
import sys


def wanted():
    """True when the caller (the TUI) asked for blocks."""
    return os.environ.get("PI_BLOCKS") == "1"


def _block(kind, md=None, **fields):
    keep = {k: v for k, v in fields.items() if v is not None}
    return {"pi": 1, "block": kind, **keep, **({"md": md} if md is not None else {})}


def heading(text, level=2, md=None):
    return _block("heading", md, level=level, text=text)


def text(text, tone=None, md=None):
    return _block("text", md, text=text, tone=tone)


def figure(label, value, note=None, tone=None):
    """One tile of a `figures` block."""
    d = {"label": label, "value": value, "note": note, "tone": tone}
    return {k: v for k, v in d.items() if v is not None}


def figures(items, md=None):
    return _block("figures", md, items=items)


def table(columns, rows, title=None, align=None, md=None):
    return _block("table", md, title=title, columns=columns, rows=rows, align=align)


def bars(items, title=None, unit=None, md=None):
    return _block("bars", md, title=title, items=[[k, n] for k, n in items], unit=unit)


def bullets(items, md=None):
    """A `list` block."""
    return _block("list", md, items=items)


def emit(blocks, out=None):
    """One JSON line per block, to stdout unless told otherwise. NaN/Infinity
    are not JSON: ValueError, raised before anything is written."""
    out = out or sys.stdout
    lines = [json.dumps(b, ensure_ascii=False, allow_nan=False) for b in blocks]
    out.write("".join(line + "\n" for line in lines))
    out.flush()


def _grid(head, rows):
    if not rows:
        return "_none_"
    body = ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(["| " + " | ".join(head) + " |", "|" + "---|" * len(head), *body])


def _md(b):
    if "md" in b:
        return b["md"]
    kind = b["block"]
    if kind == "heading":
        return "#" * b["level"] + " " + b["text"]
    if kind == "text":
        return b["text"]
    if kind == "figures":
        return " · ".join(f"{f['label']} {f['value']}" for f in b["items"])
    if kind == "table":
        return _grid(b["columns"], b["rows"])
    if kind == "bars":
        return _grid(["name", "n"], [[k, str(n)] for k, n in b["items"]])
    return "\n".join(f"- {i}" for i in b["items"])


def to_md(blocks):
    """The markdown gitboard prints for these blocks: blank line between them,
    except that a heading sits directly on the table, bars or list under it."""
    out = ""
    prev = None
    for b in blocks:
        if prev is not None:
            tight = prev["block"] == "heading" and b["block"] in (
                "table",
                "bars",
                "list",
            )
            out += "\n" if tight else "\n\n"
        out += _md(b)
        prev = b
    return out
