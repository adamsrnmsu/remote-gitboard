"""The GitLab connection, and the only place API errors become English.

Every failure surfaces as GitlabProblem, which the CLI prints as one line.
A traceback is a bad answer to a typo'd project path or an expired token.
"""

from config import get_config


class GitlabProblem(Exception):
    """Something the user can act on. Rendered without a traceback."""


def gitlab():
    """An authenticated python-gitlab client from the config singleton."""
    import gitlab as gitlab_pkg

    cfg = get_config()
    return gitlab_pkg.Gitlab(cfg.url, private_token=cfg.token())


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
