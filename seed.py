#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["python-gitlab"]
# ///
"""Mint a root PAT on the local GitLab container and seed a demo board.

    ./seed.py            # create everything, print the token
    ./seed.py --token    # just mint and print a token, seed nothing

Only for the throwaway instance in docker-compose.yml. It shells into the
container as root; never point it at anything you care about.
"""
import subprocess
import sys

URL = "http://localhost:8929"
TOKEN_NAME = "gitboard-seed"
# Fixed so re-seeding doesn't invalidate your keychain entry. Safe only
# because this instance is disposable and bound to localhost.
TOKEN = "glpat-" + "seed" * 5
COLUMNS = [("Doing", "#428bca"), ("Blocked", "#d9534f"), ("Review", "#5cb85c")]
ISSUES = [
    ("Wire up board reader", ["Doing"]),
    ("Decide on MCP server", ["Doing", "Blocked"]),
    ("Token rotation policy", []),
    ("Draft README", ["Review"]),
    ("Figure out SSO constraints at work", ["Blocked"]),
]

# PATs can only be read at creation, so mint one with a value we choose.
RUBY = f"""
u = User.find_by_username('root')
u.personal_access_tokens.where(name: '{TOKEN_NAME}').delete_all
t = u.personal_access_tokens.create!(
  scopes: [:api], name: '{TOKEN_NAME}', expires_at: 30.days.from_now)
t.set_token(ARGV[0]); t.save!
puts 'minted'
"""


def mint(token):
    """Create a root PAT inside the container via the Rails console.

    The API can't bootstrap its own first token, so this is the one step
    that has to reach inside the container.
    """
    r = subprocess.run(
        ["docker", "compose", "exec", "-T", "gitlab",
         "gitlab-rails", "runner", RUBY, token],
        capture_output=True, text=True,
    )
    if "minted" not in r.stdout:
        sys.exit(f"could not mint token — is the container healthy?\n"
                 f"{r.stdout}\n{r.stderr}")
    return token


def seed(gl):
    project = next((p for p in gl.projects.list(owned=True, all=True)
                    if p.path == "demo"), None)
    if project:
        print("project 'demo' already exists — leaving it alone")
        return project
    project = gl.projects.create(
        {"name": "demo", "path": "demo", "initialize_with_readme": True})

    labels = {}
    for name, color in COLUMNS:
        labels[name] = project.labels.create({"name": name, "color": color})

    board = project.boards.create({"name": "Dev Board"})
    for name, _ in COLUMNS:
        board.lists.create({"label_id": labels[name].id})

    for title, names in ISSUES:
        project.issues.create({"title": title, "labels": names})

    return project


def main():
    import gitlab

    mint(TOKEN)
    if "--token" in sys.argv:
        print(TOKEN)
        return

    gl = gitlab.Gitlab(URL, private_token=TOKEN)
    project = seed(gl)

    print(f"""
seeded {project.path_with_namespace} — {URL}/{project.path_with_namespace}/-/boards

  security add-generic-password -U -a "$USER" -s gitlab-token -w '{TOKEN}'
  export GITLAB_URL={URL}
  ./board.py {project.path_with_namespace}
""")


if __name__ == "__main__":
    main()
