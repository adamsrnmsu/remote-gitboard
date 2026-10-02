#!/usr/bin/env python3
"""Mint a root PAT on the local GitLab container, then apply boards/demo.yaml.

    scripts/seed.py            # mint a token and seed the demo board
    scripts/seed.py --token    # just mint and print a token, seed nothing

Only for the throwaway instance in docker-compose.yml. It shells into the
container as root; never point it at anything you care about.

Re-seeding resets boards/demo.yaml only. Labels, boards and projects that
smoke tests leave behind (scoped labels, a second board, a second project)
stay until `make reset` wipes the volumes.

The board contents live in boards/demo.yaml and go through the CLI, so this
file owns nothing but the token — the one thing the REST API cannot bootstrap
for itself.

Plain python3 and no dependencies of its own. It calls .venv's python with
PYTHONPATH=src rather than a console script, for the reason in the Makefile
header: editable installs do not stay working on this machine. Run
`make install` first.
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = "http://localhost:8929"
SPEC = os.path.join(ROOT, "boards", "demo.yaml")
TOKEN_NAME = "gitboard-seed"
# Fixed so re-seeding doesn't invalidate your keychain entry. Safe only
# because this instance is disposable and bound to localhost.
TOKEN = "glpat-" + "seed" * 5

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
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "gitlab",
            "gitlab-rails",
            "runner",
            RUBY,
            token,
        ],
        cwd=ROOT,  # docker-compose.yml lives at the repo root, not in scripts/
        capture_output=True,
        text=True,
    )
    if "minted" not in r.stdout:
        sys.exit(
            f"could not mint token — is the container healthy?\n{r.stdout}\n{r.stderr}"
        )
    return token


def main():
    mint(TOKEN)
    if "--token" in sys.argv:
        print(TOKEN)
        return

    r = subprocess.run(
        [
            os.path.join(ROOT, ".venv", "bin", "python"),
            "-m",
            "gitboard.cli",
            "push",
            SPEC,
            "--yes",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": os.path.join(ROOT, "src"),  # see the Makefile header
            "GITLAB_URL": URL,
            "GITLAB_READ_TOKEN": TOKEN,
            "GITLAB_WRITE_TOKEN": TOKEN,
        },
    )
    if r.returncode:
        sys.exit(r.returncode)

    print(f"""
seeded root/demo — {URL}/root/demo/-/boards

  security add-generic-password -U -a "$USER" -s gitlab-read-token -w '{TOKEN}'
  export GITLAB_URL={URL}
  make show PROJECT=root/demo
""")


if __name__ == "__main__":
    main()
