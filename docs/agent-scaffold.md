# Agent scaffold — `/agent`

`/agent` turns the current folder into an autonomous agent: it writes an instructions file the agent *is*, plus its state tree. A full [`/setup`](setup-wizard.md) run offers it as the last item on its checklist.

## Type, then name

| Type | What you get |
| --- | --- |
| `six-phase` | The full agent: identity, patterns, anchors, creator messages, verification gate. |
| `worker` | A 4-phase task agent with a decisions log. |
| `minimal` | A bare loop. |

The name defaults to the folder's.

## The identity file

The generated `AGENT.md`/`CLAUDE.md` carries:

- a first-person identity
- a wrong-repo guard (`git remote -v`, stop if it isn't yours)
- one-cycle-per-invocation discipline
- a verify-before-decide gate

Generated from your **live config**, it also has a "My Models" table with the real endpoints and model names, advisor escalation guidance only when an advisor is actually configured, and the measured context window. With no config it points at `/setup`; nothing is invented.

## Git

**An agent requires a git repo**: the commit is its continuity. In a bare folder the wizard offers three choices:

- a local `git init`
- create a GitHub repo through an authenticated `gh` CLI and wire it as `origin`
- stop cleanly, so you can clone a blank repo yourself first

Setup finishes with an init commit (`C0: <name> scaffolded — awakening pending`) of exactly the scaffolded files. Runtime state and logs stay untracked. It pushes when an origin exists.

## Running it

Both identity filenames load whole when referenced (`@AGENT.md` / `@CLAUDE.md`). Start the agent with:

```bash
agent "@AGENT.md run the loop"
```
