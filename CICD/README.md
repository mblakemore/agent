# CICD Improvement Loop

An autonomous, GitHub-integrated loop that continuously makes a target repo measurably better — one issue, one PR, one number moved per cycle.

Two agents run back-to-back each cycle:

- **Builder** (`agent.md`) — picks an issue, writes code in a git worktree, verifies tests, opens a draft PR
- **Reviewer** (`reviewer.md`) — checks out the PR branch, re-runs tests, re-measures the metric, and decides: merge, request changes, or close

Neither agent guesses. Every win has a number. Every null-result is logged.

---

## How Worktrees Are Used

The loop treats the cloned repo as a **read-only reference**. All code changes happen in isolated git worktrees, never on the main checkout.

```
temp/20260506_142301/
├── repo/               ← fresh clone — never commit here
└── worktrees/
    ├── 042-fix-timeout/        ← builder's working branch (cicd/042-fix-timeout)
    └── review-pr-137/          ← reviewer's read checkout (review/pr-137)
```

**Builder worktree lifecycle:**

1. `git worktree add <WORKTREE_ROOT>/NNN-slug -b cicd/NNN-slug` — new branch from main
2. Edit, compile-check, commit inside the worktree
3. `git push -u origin cicd/NNN-slug` — push the branch
4. `gh pr create --draft ...` — open the PR referencing the issue (`Closes #N`)
5. Worktree removed after a successful merge or null-result

**Reviewer worktree lifecycle:**

1. `git worktree add <WORKTREE_ROOT>/review-pr-N -B <branch> origin/<branch>` — checkout PR branch
2. Run tests, re-measure metric
3. `gh pr merge` / `gh pr comment` / `gh pr close` — act on verdict
4. Worktree removed

The two-worktree model means concurrent builder and reviewer cycles never touch each other's files, and the parent checkout on `main` stays clean throughout.

---

## Directory Layout (created at runtime)

```
<workspace>/
├── CICD/
│   ├── improvements/
│   │   ├── 042-fix-timeout.md          ← improvement plan
│   │   └── 042-fix-timeout.results.md  ← outcome + metric delta
│   ├── progress-1.md                   ← builder log (one row per cycle)
│   └── reviews-1.md                    ← reviewer log (one row per cycle)
└── temp/
    └── <stamp>/
        ├── repo/       ← git clone
        └── worktrees/  ← ephemeral worktrees
```

`CICD/` persists across runs and accumulates history. `temp/<stamp>/` is per-run and can be deleted once the cycle is done.

---

## `cicd.sh` — Local Agent Runner

Uses `agent.py` with local LLM backends (llamacpp or Bedrock).

### Basic usage

```bash
# Run builder + reviewer against any GitHub repo:
bash CICD/cicd.sh git@github.com:yourorg/yourrepo.git

# From the repo's own directory (self-improvement):
cd /path/to/yourrepo
bash /path/to/agent/CICD/cicd.sh git@github.com:yourorg/yourrepo.git
```

### Run a single phase

```bash
# Builder only:
CICD_PHASE=builder bash CICD/cicd.sh git@github.com:yourorg/yourrepo.git

# Reviewer only (e.g. to process a PR queue after a prior builder run):
CICD_PHASE=reviewer bash CICD/cicd.sh git@github.com:yourorg/yourrepo.git
```

### Multi-bot (parallel independent bots)

```bash
# Bot 1 in one terminal:
BOT_ID=1 bash CICD/cicd.sh git@github.com:yourorg/yourrepo.git

# Bot 2 in another terminal (different BOT_ID → different namespaced files + labels):
BOT_ID=2 bash CICD/cicd.sh git@github.com:yourorg/yourrepo.git
```

### Backend selection

```bash
# Summary model on Bedrock, main on local llamacpp:
CICD_BACKEND_SUMMARY=bedrock bash CICD/cicd.sh git@github.com:yourorg/yourrepo.git

# Both on Bedrock:
CICD_BACKEND_MAIN=bedrock CICD_BACKEND_SUMMARY=bedrock bash CICD/cicd.sh git@github.com:yourorg/yourrepo.git
```

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `BOT_ID` | `1` | Namespace for state files and GitHub labels (enables concurrent bots) |
| `CICD_PHASE` | `both` | `builder`, `reviewer`, or `both` |
| `CICD_BACKEND_MAIN` | *(unset — llamacpp)* | `bedrock` to route main LLM calls to Bedrock |
| `CICD_BACKEND_SUMMARY` | *(unset — llamacpp)* | `bedrock` to route summary calls to Bedrock |
| `CICD_MEM_MAX` | `12G` | systemd memory cap per agent process |
| `CICD_NO_SCOPE` | *(unset)* | Set to `1` to skip systemd-run wrapping |
| `AGENT_PY` | `/droid/repos/agent/agent.py` | Path to the agent binary |

---

## `cicd_claude.sh` — Claude Code Headless Runner

Uses `claude -p` (Claude Code's headless mode) instead of `agent.py`. Requires `ANTHROPIC_API_KEY`. No local LLM backend needed.

### Basic usage

```bash
# Run builder + reviewer:
bash CICD/cicd_claude.sh git@github.com:yourorg/yourrepo.git

# Opus is the default model. Specify a different one:
CICD_MODEL=sonnet bash CICD/cicd_claude.sh git@github.com:yourorg/yourrepo.git
```

### Run a single phase

```bash
# Builder only:
CICD_PHASE=builder bash CICD/cicd_claude.sh git@github.com:yourorg/yourrepo.git

# Reviewer only:
CICD_PHASE=reviewer bash CICD/cicd_claude.sh git@github.com:yourorg/yourrepo.git
```

### Cost control

```bash
# Cap spending per phase at $5 (default is $10):
CICD_BUDGET=5.00 bash CICD/cicd_claude.sh git@github.com:yourorg/yourrepo.git

# High-stakes run with a higher budget:
CICD_BUDGET=25.00 CICD_MODEL=opus bash CICD/cicd_claude.sh git@github.com:yourorg/yourrepo.git
```

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `BOT_ID` | `1` | Namespace for state files and GitHub labels |
| `CICD_PHASE` | `both` | `builder`, `reviewer`, or `both` |
| `CICD_MODEL` | `opus` | Claude model alias (`opus`, `sonnet`, `haiku`) or full model ID |
| `CICD_BUDGET` | `10.00` | Max USD per phase (`--max-budget-usd`) |
| `CICD_MEM_MAX` | `12G` | systemd memory cap per claude process |
| `CICD_NO_SCOPE` | *(unset)* | Set to `1` to skip systemd-run wrapping |

### Tool name mapping

`agent.md` was written using `agent.py` tool names. `cicd_claude.sh` injects a mapping note into the session prompt so Claude Code's built-in tools are used correctly:

| agent.py tool | Claude Code tool |
|---|---|
| `exec_command` | `Bash` |
| `file(action=read)` | `Read` |
| `file(action=write)` | `Write` |
| `file(action=insert/delete/append)` | `Edit` |
| `search_files` | `Grep` |
| `subagent` | `Agent` |
| `web_fetch` | `WebFetch` |

---

## Choosing Between the Two Scripts

| | `cicd.sh` | `cicd_claude.sh` |
|---|---|---|
| **LLM** | Local llamacpp or Bedrock | Anthropic API |
| **Cost model** | Compute/token cost via local inference | Per-token API billing |
| **Setup** | Local model server must be running | Only `ANTHROPIC_API_KEY` required |
| **Model selection** | Via `--backend-main` | Via `--model` / `CICD_MODEL` |
| **Telemetry** | Full OTLP via `AGENTPY_TELEMETRY=1` | Not wired (Claude Code session) |

Use `cicd.sh` when the local inference stack is running and you want cost control or telemetry. Use `cicd_claude.sh` for a quick run with no local setup, or when you want to compare Anthropic API model behaviour.

---

## Prerequisites

Both scripts require:

- `git` and `gh` (GitHub CLI, authenticated)
- `python3` with `requests` on the host interpreter
- A GitHub repo with issues enabled

`cicd.sh` additionally requires a running `agent.py`-compatible LLM backend.  
`cicd_claude.sh` additionally requires `ANTHROPIC_API_KEY` and `claude` on `PATH`.
