"""Sphinx config. Build with `make docs`; PYTHONPATH=src makes gitboard importable."""

import os
from pathlib import Path

import gitboard

project = "gitboard"
author = "Ryan Adams"
version = release = gitboard.__version__

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
]

myst_enable_extensions = ["colon_fence", "deflist", "tasklist"]
myst_heading_anchors = 2
# superpowers holds specs and plans, not user docs
exclude_patterns = ["_build", "_cli.md", "superpowers"]

html_theme = "furo"
html_title = "gitboard"
# The shared pi apps terminal skin: see the header of _static/hacker.css.
html_static_path = ["_static"]
html_css_files = ["hacker.css"]
html_favicon = "_static/favicon.png"
pygments_style = pygments_dark_style = "native"
html_theme_options = {
    # the perch bird in the skin's green; the skin is dark-only, so one file
    "light_logo": "logo.png",
    "dark_logo": "logo.png",
    "source_repository": "https://github.com/adamsrnmsu/remote-gitboard",
    "source_branch": "main",
    "source_directory": "docs/",
}


def _write_cli_help():
    """docs/_cli.md: every command's --help, verbatim.

    sphinx-click cannot read a Typer 0.27 app (typer vendors its own click, so
    TyperGroup is no longer a click.Command). The help text is the reference
    anyway, so dump it and {include} it from reference.md.
    """
    from typer.testing import CliRunner

    from gitboard.cli import app

    os.environ["COLUMNS"] = "88"
    os.environ["NO_COLOR"] = "1"
    runner = CliRunner()
    names = sorted(c.name or c.callback.__name__ for c in app.registered_commands)
    commands = [None, *names]
    parts = []
    for name in commands:
        args = ([name] if name else []) + ["--help"]
        text = runner.invoke(app, args, prog_name="gitboard").output.rstrip()
        parts.append(f"### gitboard {name or ''}\n\n```text\n{text}\n```\n")
    Path(__file__).with_name("_cli.md").write_text("\n".join(parts))


def _literal_module_docstring(app, what, name, obj, options, lines):
    # gitboard.log's docstring is an indented prose table, not valid RST.
    if what == "module" and name == "gitboard.log":
        lines[:] = ["::", ""] + ["    " + line for line in lines]


def setup(app):
    _write_cli_help()
    app.connect("autodoc-process-docstring", _literal_module_docstring)
