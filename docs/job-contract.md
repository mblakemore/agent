<title>Job Contract</title>

# The job contract — running `agent.py` as a verifiable worker

**Status: implemented.** `--job` and the runner-verified gate ship today; see
[the CLI reference](cli.md#declarative-jobs-and-the-acceptance-gate---job) for the flag.

---

## What problem this solves

Driving an agent headlessly is easy. Knowing whether the run *worked* is not.

The failure that makes autonomous runs untrustworthy is not a crash — a crash is loud and you
handle it. It is a run that exits `0`, reports success in fluent prose, and produced nothing, or
produced something subtly wrong. The agent is the last thing that should be asked whether the
agent succeeded.

So the contract has one central idea, and the rest is packaging:

> **The acceptance check is a command the RUNNER executes. The agent's own report is advisory.**

Everything else here exists to make that gate meaningful: a goal the run can be measured
against, a deliverable it must actually produce, a bound on how long it may take, and a
machine-readable result the caller can branch on.

---

## Already works today

These are shipped flags, not proposals. A caller can get most of the contract now:

| need | flag | behaviour |
|---|---|---|
| state the objective | `--goal "..."` | injected as a standing anchor; the run is reminded of it |
| require artifacts | `--deliverable path` (repeatable) | the run is *pushed back* to produce it — a bounded nudge, not a gate (see below) |
| bound the run | `--deadline SECONDS` | wall-clock; the run stands down and reports rather than being killed mid-write |
| machine-readable result | `--result-contract` + `--result-schema file` | forces a final JSON block, validated; refusal to exit without it |
| determinism | `--temperature` / `--top-p` / `--seed` | pinned generation for reproducible runs |
| stable cwd | `--no-realpath-cwd` | opt out of path canonicalisation |

And a typed exit vocabulary, so a supervisor can branch without parsing prose:

```
0   OK            10  DEADLINE      13  MEMORY
1   ERROR         11  CONTRACT      14  CONFIG
                  12  CONTEXT       15  BACKEND
```

Bulk tool output is spilled to files with a marker and a preview rather than pasted into the
window — measured at 99.1–99.5% reduction per payload, on four ordinary tool results that
together exceed a 196k context by themselves.

### Why the deliverable guard is not the gate

`--deliverable` reads as enforcement, and it is worth being exact about what it does, because
the difference is the reason the acceptance gate exists.

The guard polices *claims*, not outcomes: when a run tries to finish with a named deliverable
missing, it is turned back and told to produce it — but only up to a bounded number of times,
after which the exit proceeds with the deliverable still missing. A run that reports `blocked`
or `failed` passes untouched by design, as it should.

This was measured rather than assumed. In a run where the declared artifact was never written,
the run exited `0` reporting `done`, and the guard did not stop it. The same job with an
`acceptance` line exited `16`. **A nudge inside the run and a check outside it are different
instruments**; only the second produces a verdict that does not depend on the run's own account
of itself.

---

## The file

```yaml
# job.yaml — passed as: agent.py --job job.yaml
goal: >
  One sentence. What exists after this run that does not exist now.

context:
  - paths and documents to read first; the run starts with no memory of anything
  # If a path lies outside the working directory, SAY whether it is readable, navigable, or
  # both. Sandboxes commonly allow reading a path while refusing to `cd` into it, and that
  # boundary is invisible from inside the job file. A run that meets an unexplained refusal
  # tends to spend its whole budget re-orienting instead of working.

constraints:
  - what must not change; what is out of scope
  - "blocked-with-reasoning is a legal exit"        # see `blocked`, below
  - "bulk output goes to files, never into the reply"

deliverable:
  - path/to/thing_the_run_must_produce

acceptance: <a single executable line>
  # Re-run by the RUNNER after the agent exits. Its exit status is the verdict.
  # Must be ONE bare command — no prose, no parenthetical, no explanation. Fold an
  # explanation into `constraints` instead; a comment merged into the command line by a
  # YAML fold is a syntax error that silently disables the gate for the whole run.

timebox_sec: 3600        # wall clock, enforced by --deadline
env_allow: []            # exact variable names this job needs; default is none
acceptance_timeout_sec: 300   # how long the runner gives `acceptance`; overrun is a FAILURE
result_contract: true    # true = built-in result schema, "schema.json" = that file, false = off
```

Every field maps to something that already exists except `acceptance`, `env_allow`, and the
`blocked` outcome. `--job` is sugar plus a gate, not a new execution model.

Explicit flags beat the file, on every surface the value reaches — the goal anchor *and* the
opening prompt. An override applied to one and not the other hands the run two live objectives
and no way to tell which one it will be judged against.

A failed gate exits `16` (`acceptance`), alongside the existing typed exit codes. Failure is
closed: a missing binary, a timeout, or an unparseable command is a failure, never a pass. An
`acceptance` line that will not parse is refused at launch, because a gate discovered to be dead
after the run is a run spent for nothing.

**Unknown keys are refused at launch, for the same reason.** A misspelled `acceptence:` is a
gate that never arms, and a gate that never arms looks exactly like one that passed. The keys
in the file above are the whole vocabulary; anything else stops the launch and names itself.

**When the run also stopped hard, the cause keeps the exit code.** The gate still runs after a
deadline (`10`), contract (`11`), context (`12`), memory (`13`) or backend (`15`) stop, but a
failure then rides in the exit *detail* and on the `AGENT-ACCEPTANCE:` line rather than
replacing the code. `16` means *the run finished and the artifact is wrong*; a supervisor
branching on it would retry a run that actually needs a larger timebox or a live backend.

The gate's full output is written to `.agent/acceptance-out.txt` beside the run. The exit line
carries only its last line, which is where most tools print their verdict — a pointer, not the
evidence.

---

## Rules that carry their reasons

Each of these is here because a run failed for exactly this reason. They are stated as rules so
that the next author does not have to rediscover them.

### Dry-run the acceptance against a KNOWN-GOOD artifact before launch

Not against something you wrote for this job. Against an artifact that already exists and is
already correct — a prior deliverable, an upstream file, the tool as it stands today.

A gate written alongside a reference implementation inherits that implementation's assumptions.
One such gate invoked its target with an interface *neither* real input supported: it could never
pass, and the job's instructions told the run to self-check with it. The gate was tested only
against the thing that taught its author the mistake, which proves nothing.

**Testing a check against the artifact that taught you the error is not a test.**

### Size the timebox against measured throughput

Count the network round-trips the job needs, multiply by the observed latency, and add
generation time at the rate the model *actually* produces tokens — not the rate you hope for.

One run was measured at **2.3 tokens/second**. A four-site research task with ~25-second fetch
waits cannot complete in an hour at that rate, and no amount of agent capability repairs
arithmetic. A timebox smaller than the work is a failure the job author chose.

### `blocked` is a success of a different kind

A run that determines the task cannot be done, and says why, has produced a genuine result. It
is not a failure and should not be scored as one — otherwise the incentive is to produce
*something*, and something is worse than a clear refusal.

```json
{"status": "blocked", "summary": "...", "reason": "...", "scope": {"examined": [], "not_covered": []}}
```

### A missing verification artifact must REFUSE, not pass

If the runner produces a report — a filesystem delta, a sandbox audit, a coverage summary — then
its **absence is not a pass**. A missing report usually means the supervising process died, which
is precisely when a run was least observed and most likely to have done something unexpected.

An unsupervised run once wrote outside its workspace, and the verdict was recorded as clean
because the check that would have caught it had never executed. **Absence of a report and a clean
report must not render the same.**

### Never run the launcher in the foreground under a timeout

Kill the launcher and the child usually survives, reparented — but with no parent there is no
deadline enforcement, no artifact collection, and no post-run verification, because all three
live in the parent's exit path. The run keeps going, unobserved, past its bound.

---

## Result contract

The last fenced JSON block of the final message:

```json
{
  "status": "done | failed | blocked | cannot-tell",
  "summary": "one paragraph a human can act on",
  "artifacts": ["paths this run actually wrote"],
  "verify_output": "what the acceptance command printed when the run tried it",
  "scope": {"examined": [], "skipped": [], "not_covered": []}
}
```

`scope.not_covered` is the field that earns its keep. A run that examined three of five inputs
and says so is far more useful than one that reports success and leaves you to discover the
other two later. **A report with no stated scope reads as total coverage, and almost never is.**

---

## Deliberately not in scope

This contract stops at a single run and its verdict. It says nothing about who reviews the
result, how findings are categorised, where work items live, or how several runs are coordinated
— those belong to whatever system is driving `agent.py`, and they differ for every such system.

The line is drawn here on purpose. A contract that encoded one organisation's review workflow
would make that workflow the tool's assumed use, and every other user would be reading around
somebody else's process to find the part that applies to them.
