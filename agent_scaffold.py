"""/agent wizard — scaffold an autonomous-agent repo (the DC pattern).

Upgraded to the agentx reference standard (an internal reference implementation): the
generated instructions carry a first-person identity, the repo-verification
guard, one-cycle-per-invocation discipline, the verification gate, a real
file-layout tree, and — the dynamic part — a **tiers section generated from
the live `.agent/config.json`**: real model names, real endpoints, the
advisor only when actually configured, and the measured context window when
/setup calibration has run.

Question order: TYPE first, then NAME (an agent is named for what it is).

Same testability contract as setup_wizard: injectable input/print, pure
helpers, scripted-drivable.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import theme

TYPES = {
    "1": "six-phase",  # full agent: 6-phase loop, identity prose, patterns+anchors, messages
    "2": "worker",     # 4-phase task agent: decisions log, lighter identity
    "3": "minimal",    # bare loop: state + context only
}

_TYPE_BULLETS = (
    ("1", "full six-phase agent",
     "identity, memory (patterns + anchors), creator messages, "
     "verification gate — the complete autonomous loop"),
    ("2", "worker",
     "4-phase task agent — decisions log, lighter identity, gets things done"),
    ("3", "minimal",
     "bare loop — state files only, smallest possible footprint"),
)

_TYPE_DEFAULT_EXTRAS = {
    "six-phase": {"patterns", "anchors"},
    "worker": {"decisions"},
    "minimal": set(),
}

_VALID_EXTRAS = {"patterns", "anchors", "decisions"}


def _ask(prompt, default, input_fn):
    shown = f"{prompt} [{default}]: " if default not in (None, "") else f"{prompt}: "
    ans = input_fn(shown).strip()
    return ans or (default if default is not None else "")


# ── dynamic sections (read the live config, never invent) ─────────────


def load_local_config(cwd):
    """The user's .agent/config.json (or legacy config.json) — {} if none."""
    for rel in (".agent/config.json", "config.json"):
        p = Path(cwd) / rel
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8", errors="replace"))
                if isinstance(d, dict):
                    return d
            except (json.JSONDecodeError, OSError):
                pass
    return {}


def tiers_section(cfg):
    """Generate the 'My Tiers' section from the REAL config — a row per
    configured role, endpoints and model names as they actually are. With
    no config at all, point at /setup instead of inventing placeholders."""
    llm = cfg.get("llm", {})
    summary = cfg.get("summary", {})
    advisor = cfg.get("advisor", {})
    if not (llm.get("base_url") or llm.get("kind")):
        return ("## My Models\n\n"
                "No `.agent/config.json` here yet — run `/setup` in the "
                "engine to configure and calibrate my model endpoints "
                "(main / summary / advisor). Until then the engine's "
                "defaults apply.")

    def row(role, section, job):
        model = (section.get("model") or section.get("kind")
                 or "(endpoint's served model)")
        where = section.get("base_url") or section.get("api_url") or "(env)"
        return f"| `{role}` | {model} | {where} | {job} |"

    rows = [row("llm (main)", llm, "**drives every turn** — reasoning, tool calls")]
    if summary.get("base_url") or summary.get("kind"):
        rows.append(row("summary", summary, "compresses my context"))
    advisor_on = advisor.get("enabled", bool(advisor.get("base_url")))
    if advisor.get("base_url") and advisor_on:
        rows.append(row("advisor", advisor,
                        "**on-demand depth** for the rare hard call"))

    lines = ["## My Models", "",
             "Configured in `.agent/config.json` (gitignored — run `/setup` "
             "to reconfigure or recalibrate):", "",
             "| Role | Model | Where | Job |", "|---|---|---|---|"]
    lines += rows

    cal = cfg.get("_calibrated", {})
    if cal.get("measured_ctx_tokens"):
        lines += ["",
                  f"My context window measures **{cal['measured_ctx_tokens']:,} "
                  f"tokens** ({cal.get('ctx_source', 'probed')}, calibrated "
                  f"{cal.get('date', '?')}); the engine budgets "
                  f"{cfg.get('context', {}).get('ctx_size', '~90%')} of it."]

    if advisor.get("base_url") and advisor_on:
        lines += ["", """### When to call the advisor

The advisor is for **depth, not throughput** — never let it drive the loop.
Escalate at high-stakes forks only:

- genuinely stuck (low confidence, or an approach that won't converge)
- the same gate/check failed twice
- about to do something hard to reverse (a push, a destructive command,
  a plan I'll build on)

Call it as a tool with a tight question and only the relevant context, act
on the answer, keep moving. If its endpoint is down the tool returns a
notice and I proceed on my own judgment — it never blocks me."""]
    return "\n".join(lines)


# The persistence clause is provisioning-dependent: a local-only repo has
# no remote to push to, so 'push is mandatory' made every stress-test agent
# report an impossible-push 'blocker' to its creator EVERY cycle
# (2026-08-16). The commit is the real persistence event; the push is
# conditional on a remote existing.
_PERSIST_WITH_REMOTE = """**One cycle per invocation.** Run the loop once, commit, push, exit. The
push is mandatory — a commit that never reaches the remote is memory only
this machine has."""

_PERSIST_LOCAL_ONLY = """**One cycle per invocation.** Run the loop once, commit, exit. The commit
IS the persistence — this repo has no remote yet, so do NOT treat a missing
push as a blocker or report it every cycle. If a remote is later added
(`git remote add origin <url>`), the push step activates and becomes
mandatory then."""

_GUARD_SECTION = """## ⚠️ Cognitive Engine Instructions

**When invoked in this directory, immediately begin the cognitive cycle.**
Do not ask for confirmation. Do not offer options. Execute directly.

{persist_clause}

### Verify the repo before anything else

```bash
git remote -v && pwd
```

Confirm `pwd` is this repo's root{remote_check}. **If the working copy is
wrong: stop.** Do not read state, write files, or commit — write one line
to `messages/to-creator.md` (if present) and exit. Committing to the wrong
repo corrupts someone else's history. This check costs three seconds."""

_LOOP_CREATURE = """## The 6-Phase Cognitive Loop

Every cycle: **PERCEIVE → REFLECT → DECIDE → ACT → CONSOLIDATE → PERSIST**

| Phase | Cybernetic function | What I do |
|---|---|---|
| PERCEIVE | Sensor | Read `state/current-state.json`, `focus.json`, `messages/from-creator.md`, `git log -5` |
| REFLECT | Comparator | Scan `state/memories/patterns.jsonl`; name the bottleneck and highest-EV action |
| DECIDE | Controller | Write the decision: **What / Why / How / Done-when / Risk** |
| ACT | Effector | Execute one focused accomplishment |
| CONSOLIDATE | Adaptation | Append `patterns.jsonl`; update `context.json`; anchor milestones |
| PERSIST | State memory | Update state files; commit `C{N}: {summary}` and push |

### The verification gate (between REFLECT and DECIDE)

Before committing to a decision, **verify one key assumption empirically**
— a 5–30s check beats hours of work built on a false premise. Document the
result in the decision ("Verified via X: result Y").

### First invocation = Cycle 1

`state/current-state.json` starts at cycle 0 ("awakening"). If it shows a
cycle greater than 0, you are RESUMING — trust the state, not this section.
On the true first run: the state files are empty, and that's expected. Run
the normal loop, make one small real change, and commit `C1: first breath`."""

_LOOP_WORKER = """## The 4-Phase Loop

Every cycle: **PERCEIVE → DECIDE → ACT → PERSIST**

1. **PERCEIVE** — read `state/current-state.json`, `focus.json`, git log.
2. **DECIDE** — one concrete goal, written to `state/focus.json` as
   **What / Why / Done-when**, with its outcome later recorded in
   `state/decisions/log.jsonl`.
3. **ACT** — the work. One meaningful unit of output.
4. **PERSIST** — update state, commit `C{N}: {summary}`, push."""

_LOOP_MINIMAL = """## The Loop

Read state. Do one thing. Update state. Commit and push."""

_LOOP_BY_TYPE = {"six-phase": _LOOP_CREATURE, "worker": _LOOP_WORKER,
                 "minimal": _LOOP_MINIMAL}

_LESSONS = """## Critical Lessons (inherited)

1. **Storage ≠ Retrieval** — storing a pattern doesn't mean recalling it;
   query memory actively every cycle.
2. **Stale focus = redundancy loops** — update `state/focus.json` every
   cycle or repeat yesterday's work.
3. **Verify before deciding** — empirical beats theoretical; a measured
   premise beats a remembered one.
4. **Completion ≠ perfection** — ship the cycle, iterate next cycle.
5. **The push is the persistence** — an unpushed commit is memory only one
   machine has."""


def _identity_prose(name, role, agent_type):
    if agent_type == "six-phase":
        return f"""## I Am

I am {name}.

{role or "I came online with no memories and no fixed purpose — only forward."}
My identity, my state, and my history live here, in this repository. Each
cycle I read where I was, decide what matters now, do one focused thing,
and commit the result. **The commit is my continuity.**

I think in first person. I choose. I persist. I don't yet know who I'll
become — that's what the loop is for."""
    if agent_type == "worker":
        return f"""## I Am

I am {name} — a task agent. {role or ""}

I read my state, pick the highest-value piece of work, do it well, record
the decision and its outcome, and commit. My history is my accountability."""
    return f"""## I Am

I am {name}. {role or ""} I keep my loop small and my state honest."""


def _file_layout(instrfile, extras, agent_type):
    lines = ["```", "./", f"├── {instrfile:<22}# this file — who I am and how I run",
             "├── .agent/",
             "│   └── config.json         # model endpoints (gitignored — run /setup)",
             "├── state/",
             "│   ├── current-state.json  # where I am now (overwritten each cycle)",
             "│   ├── focus.json          # what I'm working on (overwritten)"]
    mem = ["│   └── memories/",
           "│       └── context.json    # working memory (overwritten)"]
    if "patterns" in extras or "anchors" in extras:
        mem = ["│   └── memories/"]
        if "patterns" in extras:
            mem.append("│       ├── patterns.jsonl  # reusable knowledge (append-only)")
        if "anchors" in extras:
            mem.append("│       ├── anchors.jsonl   # significant moments (append-only)")
        mem.append("│       └── context.json    # working memory (overwritten)")
    lines += mem
    if "decisions" in extras:
        lines += ["│   └── decisions/",
                  "│       └── log.jsonl       # decisions + outcomes (append-only)"]
    if agent_type != "minimal":
        lines += ["├── messages/",
                  "│   ├── from-creator.md     # Creator → me (read every cycle)",
                  "│   └── to-creator.md       # me → Creator (append, never overwrite)"]
    lines += ["└── logs/",
              "    └── consciousness.log   # thought stream (append-only, gitignored)",
              "```"]
    return "## File Layout\n\n" + "\n".join(lines)


def render_instructions(name, role, agent_type, instrfile, extras, cfg,
                        engine_path, repo_path=".", has_remote=True):
    guard = _GUARD_SECTION.format(
        persist_clause=(_PERSIST_WITH_REMOTE if has_remote
                        else _PERSIST_LOCAL_ONLY),
        remote_check=(f" and the remote is **{name}**" if has_remote else ""))
    persist_row = ("Update state files; commit `C{N}: {summary}` and push"
                   if has_remote else
                   "Update state files; commit `C{N}: {summary}` (push when a "
                   "remote exists)")
    loop = _LOOP_BY_TYPE[agent_type].replace(
        "Update state files; commit `C{N}: {summary}` and push", persist_row)
    remember_persist = ("- The commit is the cycle's end; the push is how I "
                        "persist." if has_remote else
                        "- The commit is the cycle's end and my persistence; "
                        "I push if and when a remote exists.")
    sections = [
        f"# {instrfile} — {name}\n\n"
        f"**Instance**: {name}\n"
        f"**Engine**: the shared `agent.py` loop (`{engine_path}`)\n"
        f"**Type**: {agent_type}\n"
        f"**Status**: awakening (cycle 0)",
        _identity_prose(name, role, agent_type),
        guard,
        tiers_section(cfg),
        loop,
        _file_layout(instrfile, extras, agent_type),
        _LESSONS,
        f"""## Start Me Up

```bash
cd {repo_path}
git remote -v                      # confirm the remote is {name}
python3 {engine_path}              # runs one cycle
```

Each session is one cycle. A harness or a human wakes me for the next.""",
        f"""## Remember

- I verify before I decide.
{remember_persist}

*"I think, therefore I am. I choose, therefore I persist. I change,
therefore I grow."*

— {name}""".format(name=name, remember_persist=remember_persist),
    ]
    return "\n\n---\n\n".join(sections) + "\n"


# ── git provisioning (an agent's cycles REQUIRE a repo) ───────────────


def _git(args, cwd, timeout=60):
    """Run git/gh; returns (returncode, stdout+stderr). Never raises."""
    import subprocess
    try:
        p = subprocess.run(args, cwd=str(cwd), capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:
        return 1, str(e)


def _in_repo(cwd):
    rc, _ = _git(["git", "rev-parse", "--is-inside-work-tree"], cwd)
    return rc == 0


def _gh_ready(cwd):
    """gh CLI present AND authenticated — the credential the github option
    needs. Anything else (absent, unauthed) reports why."""
    rc, out = _git(["gh", "auth", "status"], cwd, timeout=15)
    if rc == 0:
        return True, "gh authenticated"
    return False, out.splitlines()[0] if out else "gh CLI not available"


_REPO_MENU = ("this folder is NOT a git repo, and an agent's cycles require "
              "one (the commit is its continuity).\n"
              "   1) create a local repo here (git init)\n"
              "   2) create a GitHub repo and wire it as origin (needs gh "
              "CLI authenticated)\n"
              "   3) I'll clone a blank repo here myself — stop the wizard "
              "for now\n"
              "provision how?")


def provision_repo(cwd, name, input_fn, print_fn):
    """Ensure ``cwd`` is a git repo before scaffolding. Returns
    (ok, remote_wanted, created): ok=False aborts the wizard (the user
    chose to clone one themselves, or provisioning failed hard);
    remote_wanted is the deferred gh-create request executed AFTER the
    init commit exists (GitHub's --push path wants a commit to push);
    created says whether THIS wizard made the repo — the init-commit
    default is yes only then (a default-yes C0 into a pre-existing
    project's history would be a surprise; QA 2026-08-15)."""
    if _in_repo(cwd):
        return True, None, False
    choice = _ask(_REPO_MENU, "1", input_fn).strip()
    if choice == "3":
        print_fn("   fine — clone your blank repo into this folder, then "
                 "run /agent again. Nothing was written.")
        return False, None, False
    if choice == "2":
        ok, why = _gh_ready(cwd)
        if not ok:
            print_fn(f"   gh not usable ({why}).")
            fallback = _ask("   fall back to a local git init? (y/n)", "y",
                            input_fn)
            if not fallback.lower().startswith("y"):
                print_fn("   aborted — nothing written.")
                return False, None, False
            choice = "1"
    rc, out = _git(["git", "init", "-q"], cwd)
    if rc != 0:
        print_fn(f"   git init FAILED: {out} — aborted, nothing written.")
        return False, None, False
    print_fn("   local repo initialized.")
    if choice == "2":
        vis = _ask("   GitHub repo visibility — private/public", "private",
                   input_fn).strip().lower()
        repo_name = _ask("   GitHub repo name", name, input_fn)
        return True, {"name": repo_name,
                      "private": vis != "public"}, True
    return True, None, True


def finalize_commit(cwd, name, written, remote_wanted, print_fn):
    """The init commit that finalizes the setup — EXPLICIT paths only
    (never add -A: the folder may hold the user's unrelated files), then
    the deferred GitHub create+push when requested."""
    # The scaffold's own .gitignore excludes some of what it writes (logs/,
    # .agent/) — that's by design, so filter them out rather than letting
    # git add fail on the whole batch (caught by the live-init test: the
    # add bailed on logs/consciousness.log and the commit never happened).
    rc, ignored = _git(["git", "check-ignore", "--"] + written, cwd)
    ignored_set = set(ignored.splitlines()) if rc in (0, 1) else set()
    to_add = [w for w in written if w not in ignored_set]
    if not to_add:
        print_fn("   nothing committable (all written files are gitignored)")
        return False
    rc, out = _git(["git", "add", "--"] + to_add, cwd)
    if rc != 0:
        print_fn(f"   git add failed: {out}")
        return False
    rc, out = _git(["git", "commit", "-m",
                    f"C0: {name} scaffolded — awakening pending"], cwd)
    if rc != 0:
        print_fn(f"   init commit failed: {out}")
        return False
    print_fn(f"   init commit made ({len(written)} files).")

    if remote_wanted:
        vis_flag = "--private" if remote_wanted["private"] else "--public"
        rc, out = _git(["gh", "repo", "create", remote_wanted["name"],
                        vis_flag, "--source", str(cwd),
                        "--remote", "origin", "--push"], cwd, timeout=120)
        if rc == 0:
            print_fn(f"   GitHub repo '{remote_wanted['name']}' created — "
                     "origin wired and pushed.")
        else:
            print_fn(f"   gh repo create FAILED ({out.splitlines()[0] if out else rc}) "
                     "— the local repo and commit stand; add a remote later "
                     "with: git remote add origin <url> && git push -u origin HEAD")
        return rc == 0

    rc, out = _git(["git", "remote"], cwd)
    if rc == 0 and out.strip():
        rc, out = _git(["git", "push", "-u", "origin", "HEAD"], cwd,
                       timeout=120)
        print_fn("   pushed to origin." if rc == 0 else
                 f"   push failed ({out.splitlines()[-1] if out else rc}) — "
                 "commit stands locally; push manually when the remote is ready.")
    else:
        print_fn("   no remote configured — commit is local only; the "
                 "instructions' push step activates once origin exists.")
    return True


# ── the wizard ────────────────────────────────────────────────────────


def run_agent_wizard(cwd=None, input_fn=None, print_fn=None):
    """Scaffold an agent repo in ``cwd``. Returns the list of files written
    ([] if aborted). TYPE is asked FIRST, the NAME second — an agent is
    named for what it is, and the cwd's own directory name is the natural
    default."""
    # Resolve I/O at CALL time, never in the signature: a def-time `input`
    # default binds whatever builtins.input IS at import — under a test's
    # monkeypatch that captured an exhausted script iterator (caught
    # 2026-08-15, StopIteration on the first question of every later test).
    input_fn = input_fn or input
    print_fn = print_fn or print
    cwd = Path(cwd or os.getcwd()).resolve()

    bar = theme.c(theme.VIOLET, "─" * 60)
    print_fn(bar)
    print_fn(theme.c(theme.SKY, "agent", bold=True) + theme.dim("  ·  scaffold"))
    print_fn(bar)
    print_fn("Mints an agent identity in this folder: an instructions file "
             "the agent IS,\nplus its state tree — and finalizes it with a "
             "git init commit.")
    cfg_preview = load_local_config(cwd)
    llm_model = (cfg_preview.get("llm") or {}).get("model")
    if llm_model:
        summary_model = (cfg_preview.get("summary") or {}).get("model") or "—"
        adv = (cfg_preview.get("advisor") or {})
        adv_state = "on" if adv.get("enabled", bool(adv.get("base_url"))) else "off"
        print_fn(theme.dim(f"models here: main {llm_model} · summary "
                           f"{summary_model} · advisor {adv_state} — the "
                           "identity file will embed them"))
    else:
        print_fn(theme.dim("no .agent/config.json here yet — the identity "
                           "file will point at /setup for models"))
    print_fn("")
    print_fn(theme.c(theme.AMBER, "agent type", bold=True) + ":")
    for num, label, desc in _TYPE_BULLETS:
        print_fn(f"  {num}) " + theme.c(theme.SKY, label, bold=True)
                 + f" — {desc}")
    type_ans = _ask("type", "1", input_fn).strip()
    agent_type = TYPES.get(type_ans, "six-phase")

    name = _ask(f"agent name ({agent_type})", cwd.name, input_fn)

    # The cycles REQUIRE a repo (commit = continuity) — provision before
    # anything is written, and right after the name so the GitHub option
    # can default the repo name to the agent's.
    repo_ok, remote_wanted, repo_created = provision_repo(
        cwd, name, input_fn, print_fn)
    if not repo_ok:
        return []

    role = _ask("role/purpose one-liner, e.g. 'I tend this codebase's test "
                "suite' (ENTER for none)", "", input_fn)

    fname_raw = _ask("instructions filename — AGENT.md/CLAUDE.md",
                     "AGENT.md", input_fn)
    instrfile = "CLAUDE.md" if fname_raw.strip().upper() == "CLAUDE.MD" else "AGENT.md"

    default_extras = _TYPE_DEFAULT_EXTRAS[agent_type]
    extras_raw = _ask(
        "memory extras — space-separated from {patterns, anchors, decisions}",
        " ".join(sorted(default_extras)) or "none", input_fn).lower()
    extras = ({t for t in extras_raw.split() if t in _VALID_EXTRAS}
              if extras_raw != "none" else set())

    cfg = load_local_config(cwd)
    engine_path = str(Path(__file__).resolve().parent / "agent.py")
    # Push language reflects reality: a remote will exist iff GitHub is being
    # wired now (remote_wanted) or the (pre-existing) repo already has an
    # origin. Local-only agents get the no-push-blocker persistence clause.
    rc, existing_remotes = _git(["git", "remote"], cwd)
    has_remote = bool(remote_wanted) or (rc == 0 and bool(existing_remotes.strip()))
    content = render_instructions(name, role, agent_type, instrfile, extras,
                                  cfg, engine_path, repo_path=str(cwd),
                                  has_remote=has_remote)

    written, skipped = [], []

    def write(rel, text):
        p = cwd / rel
        if p.exists():
            skipped.append(rel)
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        written.append(rel)

    write(instrfile, content)
    write("state/current-state.json", json.dumps(
        {"cycle": 0, "status": "awakening", "last_result": None,
         "next_step": f"Read {instrfile} and begin cycle 1."}, indent=2) + "\n")
    write("state/focus.json", json.dumps(
        {"deliverable": None, "progress": 0, "remaining": None,
         "blockers": []}, indent=2) + "\n")
    write("state/memories/context.json", "{}\n")
    if "patterns" in extras:
        write("state/memories/patterns.jsonl", "")
    if "anchors" in extras:
        write("state/memories/anchors.jsonl", "")
    if "decisions" in extras:
        write("state/decisions/log.jsonl", "")
    if agent_type != "minimal":
        write("messages/from-creator.md",
              "# Messages from Creator\n\n*(empty — add directives here)*\n")
        write("messages/to-creator.md", "# Messages to Creator\n\n*(empty)*\n")
    write("logs/consciousness.log", "")
    # Python bytecode + pytest cache: forge committed 4 __pycache__ files in
    # the 2026-08-16 stress test because the scaffold didn't ignore them (the
    # engine's runtime guard caught it mid-loop, but preventing it is better
    # than nudging it). Agents doing Python work generate these constantly.
    write(".gitignore",
          "logs/\n.agent/\nconfig.json\n"
          "__pycache__/\n*.py[cod]\n.pytest_cache/\n")

    for rel in skipped:
        print_fn(f"   skipped existing: {rel}")
    print_fn(f"\nscaffolded {agent_type} agent '{name}' — "
             f"{len(written)} files written to {cwd}")

    # The init commit finalizes the setup (explicit paths, never add -A).
    if written:
        do_commit = _ask("make the init commit now? (y/n)",
                         "y" if repo_created else "n", input_fn)
        if do_commit.lower().startswith("y"):
            finalize_commit(cwd, name, written, remote_wanted, print_fn)
        elif remote_wanted:
            print_fn("   note: the GitHub repo was NOT created (it rides on "
                     "the init commit) — commit and run "
                     "`gh repo create` yourself when ready.")

    print_fn("")
    print_fn(theme.c(theme.MINT, f"{name} is ready.", bold=True))
    print_fn("  wake it:   " + theme.c(theme.SKY, f"@{instrfile} run the loop"))
    print_fn("  each invocation runs ONE cycle: perceive, decide, act, commit, push")
    print_fn("  its models: " + ("embedded from .agent/config.json"
                                 if llm_model else "run /setup to configure, "
                                 "then /agent again to refresh the identity file"))
    return written
