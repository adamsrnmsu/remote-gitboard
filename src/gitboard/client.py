"""The GitLab connection, and the only place API errors become English.

Every failure surfaces as GitlabProblem, which the CLI prints as one line.
A traceback is a bad answer to a typo'd project path or an expired token.
"""

from contextlib import contextmanager

from gitboard.config import get_config


class GitlabProblem(Exception):
    """Something the user can act on. Rendered without a traceback."""


def gitlab(write=False):
    """An authenticated python-gitlab client from the config singleton.

    `write=True` picks the write token, which needs `api` scope where reading
    only needs `read_api`.
    """
    import gitlab as gitlab_pkg

    cfg = get_config()
    return gitlab_pkg.Gitlab(cfg.url, private_token=cfg.token(write=write))


@contextmanager
def write_errors():
    """Explain a refused write instead of dumping the API's 403.

    A `read_api` token reads boards perfectly and fails only here, so this is
    the most likely way to get the scopes wrong.
    """
    import gitlab as gitlab_pkg

    try:
        yield
    except gitlab_pkg.exceptions.GitlabError as e:
        if getattr(e, "response_code", None) in (401, 403):
            raise GitlabProblem(
                "the token cannot write — writing needs `api` scope, reading only "
                "needs `read_api`.\n"
                "  Put an api-scope token in GITLAB_WRITE_TOKEN (.env or the "
                "environment), pass --write-token,\n"
                "  or add a keychain item named 'gitlab-write-token'."
            ) from e
        raise GitlabProblem(str(e)) from e


def get_project(gl, path):
    """Fetch a project, turning the three common failures into plain English."""
    import gitlab as gitlab_pkg

    url = get_config().url
    try:
        return gl.projects.get(path)
    except gitlab_pkg.exceptions.GitlabAuthenticationError as e:
        raise GitlabProblem(
            f"{url} rejected the token — expired, or minted on another instance?"
        ) from e
    except gitlab_pkg.exceptions.GitlabGetError as e:
        # GitLab answers 404 for private projects too, rather than confirm
        # they exist. "Not found" and "not allowed" are indistinguishable.
        raise GitlabProblem(
            f"no project {path!r} on {url}, or the token cannot see it"
        ) from e
    except OSError as e:  # requests' ConnectionError/Timeout subclass this
        raise GitlabProblem(f"cannot reach {url}: {e.__class__.__name__}") from e
