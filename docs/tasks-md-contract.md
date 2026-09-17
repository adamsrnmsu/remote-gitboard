# tasks.md contract (v1)

What an agent writes so `gitboard ingest` can turn it into board issues.
Paste this block into the agent's briefing:

```text
Write tasks.md like this. Never tick a box: a person marks work verified
by commenting `verified` (or `failed`) on the issue, not you.

commit: <sha>            # header, optional, before the first heading;
mr: !<n>                 # also branch:, session:, run:
## <Person>              # one level-2 heading per person; `#` is the title
- [ ] <task title> · id: T-<slug>     # id optional but keeps identity on rename
  <how to verify, one step per line>
  evidence: <path or link>            # optional
  **Feedback**                        # optional; the person's reply
  <their words>
Fenced ``` blocks are ignored.
```

## Worked example

```markdown
commit: 9f3c2a1
mr: !41
session: run-7

# Round 3

## Alice
- [ ] Login as a viewer lands on the dashboard · id: T-login
  Open /login, sign in as `viewer`, expect /dashboard.
  Sign out, expect /login.
  evidence: screenshots/login.png
- [ ] Migration is idempotent · id: T-migrate
  `make migrate` twice; the second run prints nothing.
  **Feedback**
  Second run warned about a stale lock.

## Bob Lee
- [ ] API docs read cleanly [T-docs]
```

`ingest` turns each task into an issue: the verify lines become a GitLab
task list, `evidence:` a line of its own, and a one-line footer carries
the lineage:

```text
Source: proj-x/tasks.md · Alice · 2026-09-15 · id: T-login · commit: 9f3c2a1 · mr: !41 · session: run-7
```

## Rules the parser enforces

| element | rule |
|---|---|
| header `key: value` | only `commit`, `branch`, `mr`, `session`, `run`; only before the first heading; copied into every task |
| `# Title` | the document; tasks under it have no person |
| `## Name` | a person; `people:` in the YAML maps it to a username |
| `- [ ] title` | a task; `-`, `*`, `+` all work |
| `· id: T-x`, `(id: T-x)`, `[T-x]` at the end of the title | the id; letters, a dash, then anything |
| indented lines under a task | verify steps, until the next task, heading or feedback marker |
| `evidence:` among the steps | lifted out |
| `**Feedback**`, `__Feedback__`, `Feedback:` on its own line | the person's reply follows |
| ``` fences | skipped entirely |
| `[x]` | ignored for movement; reported as *unverified* when no verdict backs it |

Identity on the board is the `id:`, then the exact title (case and
whitespace do not matter). A new title within 85 % of an existing one is
reported and **not** added, so a rephrased task does not become a
duplicate: give it an id, or a clearly different title.
