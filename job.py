"""The `--job` file: a declarative run with a runner-verified acceptance gate
(agent.py's `--job`; the contract is docs/job-contract.md).

Sugar over the existing flags plus the one thing flags cannot express: a check the RUNNER
executes after the agent exits, whose status is the verdict. The agent is the last thing
that should be asked whether the agent succeeded: a run can exit 0, report success
fluently, and have produced nothing. This module reads and validates the file, folds it
into the opening prompt, scrubs the environment to the job's allowlist and re-runs the
acceptance command; agent.py's main() turns the result into the process exit code.
"""
import json
import os
import subprocess

# Every top-level key a job file may carry. Anything else is refused at launch: a misspelled
# `acceptence:` is a gate that never arms, and a gate that never arms is indistinguishable
# from one that passed. Same class as the unparseable-command refusal in main().
KNOWN_KEYS = frozenset({"goal", "context", "constraints", "deliverable", "acceptance",
                             "timebox_sec", "env_allow", "acceptance_timeout_sec",
                             "result_contract", "refusal_max_turns"})


def load_spec(path):
    """Read a job file. JSON always; YAML only if PyYAML happens to be installed.

    PyYAML is NOT a declared dependency of this tool and is not made one for an optional
    feature — a public utility should not grow an install requirement for a flag most
    callers never use. A .json job works everywhere; a .yaml job says plainly what to
    install rather than failing on an opaque parse error.
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    looks_json = raw.lstrip().startswith(("{", "["))
    if looks_json or str(path).lower().endswith(".json"):
        return normalize_lists(json.loads(raw))
    try:
        import yaml  # noqa: PLC0415 — optional, imported only for YAML job files
    except ImportError:
        raise ValueError(
            f"{path} is not JSON and PyYAML is not installed. Either `pip install pyyaml` "
            f"or write the job file as JSON — the schema is identical."
        )
    return normalize_lists(yaml.safe_load(raw))


LIST_KEYS = ("context", "constraints", "deliverable")


def normalize_lists(spec):
    """A bare string in a list-valued field is ONE item, not a sequence of characters.

    `deliverable: hello.txt and done.txt in the working directory` reached the prompt as
    forty-eight single-character bullets and the deliverable guard as seventeen missing
    one-letter files — and the model, shown that, created a file per character. Python
    iterates a str; the contract does not.
    """
    if isinstance(spec, dict):
        for k in LIST_KEYS:
            v = spec.get(k)
            if isinstance(v, str):
                spec[k] = [v]
    return spec


def prompt(spec):
    """Fold goal/context/constraints/deliverable into the run's opening prompt.

    The acceptance command is shown to the run too. Not as a gate it can satisfy by talking
    — the runner re-runs it regardless — but because a run that knows how it will be judged
    can check itself before finishing, and one that does not is guessing at the target.
    """
    out = []
    if spec.get("goal"):
        out.append(f"GOAL: {str(spec['goal']).strip()}")
    ctx = spec.get("context") or []
    if ctx:
        out.append("CONTEXT (read these first; this run starts with no memory):")
        out.extend(f"  - {c}" for c in ctx)
    con = spec.get("constraints") or []
    if con:
        out.append("CONSTRAINTS:")
        out.extend(f"  - {c}" for c in con)
    deliv = spec.get("deliverable") or []
    if deliv:
        out.append("DELIVERABLE — this run must actually produce:")
        out.extend(f"  - {d}" for d in deliv)
    if spec.get("acceptance"):
        out.append(
            "ACCEPTANCE (the runner RE-RUNS this after you exit; its exit status is the "
            "verdict, and your own report does not override it):")
        out.append(f"  {spec['acceptance']}")
    out.append(
        "`blocked` is a legal and respected outcome: if the task cannot be done, say so with "
        "your reasoning rather than producing something in order to have produced something.")
    return "\n".join(out)


def apply_env_allow(allow):
    """Scrub the environment down to an explicit allowlist, plus what a process needs to run.

    Absent or unset -> NO-OP. An empty list is meaningful and different: it means this job
    declared that it needs nothing, and the scrub happens. Conflating "not specified" with
    "specified as empty" would silently strip a caller who never opted in.

    The interpreter's own knobs (PYTHON*) always stay. PYTHONUNBUFFERED is how a supervisor's
    log arrives while the run is still alive; a scrub that strips it re-buffers the very log
    the run is being read through, which breaks the thing doing the watching. Those variables
    configure the process, they are not the job's secrets.
    """
    if allow is None:
        return None
    keep = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TMPDIR", "TERM",
            "PWD", "PYTHONPATH", "PYTHONIOENCODING", "SystemRoot", "COMSPEC"}
    keep.update(str(k) for k in allow)
    removed = [k for k in list(os.environ) if k not in keep and not k.startswith("PYTHON")]
    for k in removed:
        os.environ.pop(k, None)
    return removed


def acceptance_parses(cmd):
    """(ok, message). `bash -n` on the command. A validator that cannot run is itself a
    refusal, not a traceback: a supervisor reads exit 14 and a reason, never a stack."""
    try:
        chk = subprocess.run(["bash", "-n"], input=str(cmd), capture_output=True, text=True)
    except OSError as e:
        return False, f"bash is not available to validate the acceptance command ({e})"
    if chk.returncode != 0:
        return False, (chk.stderr or "").strip()[:160]
    return True, ""


def save_acceptance_output(cmd, code, out, path=None):
    """Keep the gate's full output beside the run. Never raises; returns the path or None.

    The exit line carries one line of it, which is a pointer, not evidence. Whoever asks
    "why did the gate fail" after the process is gone needs the artifact, not the address.
    """
    path = path or os.path.join(".agent", "acceptance-out.txt")
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"command: {cmd}\nexit: {code}\n---\n{out}\n")
        return path
    except OSError:
        return None


def run_acceptance(cmd, cwd=None, timeout=300):
    """Re-run the acceptance command OUTSIDE the agent. Returns (ok, code, output)."""
    try:
        proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, None, f"acceptance command exceeded {timeout:g}s"
    except OSError as e:
        return False, None, f"acceptance command could not be run: {e}"
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode == 0, proc.returncode, out


def refresh_result_exit(result_file, info):
    """Rewrite the `exit` block of an already-written JSON result file so it matches the final
    verdict. Raw (non-JSON) result files are left alone; a file that cannot be parsed is not
    a result file this code owns."""
    if not result_file or not info or not os.path.exists(result_file):
        return
    try:
        with open(result_file, "r", encoding="utf-8") as f:
            obj = json.load(f)
        if not isinstance(obj, dict):
            return
        obj["exit"] = dict(info)
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)
    except (OSError, ValueError):
        return
