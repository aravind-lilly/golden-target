# The Golden Target — Sample Submission

> **This is a reference repo only. Do NOT submit this repo as-is.**
> **Create your own repository following the same structure shown here, replace `solve.py` with
> your own investigation and tooling, then submit your repo URL.**

This repo shows the exact files the evaluation agent expects, and a starter tool that runs end to
end without solving anything. It exists to prove the *contract* works, not to teach the *method* —
see Challenge Text (from the platform) and `verification_starter.md` (shipped separately to you) for what you're
actually being asked to build.

---

## 1. Required repo structure

```
your_repo/
├── goldentarget_config.json   # Required — tells the agent how to run your tool
├── solve.py                    # Your reconciliation tool's entry point
└── requirements.txt            # Python dependencies (JFrog only — see §7)
```

You may name your entry script anything and organize your code however you like — just declare
the correct `run_command` in `goldentarget_config.json` (§3). `solve.py` is only this starter's
name by convention; it is also the agent's fallback default if you omit the manifest entirely
(not recommended — declare it explicitly).

---

## 2. The tool contract

Your tool must run as:

```
<cmd> <pack_dir>
```

Concretely: `<cmd>` is your resolved `run_command` — the interpreter and your script, and nothing
else (§3) — invoked directly as a process, **never through a shell** (see §5, this is why shell
operators, pipes, or `$VAR`-style expansions inside your `run_command` string will not do what you
might expect — there is no shell present to interpret them). `<pack_dir>` is always a single,
absolute filesystem path to the pack the agent wants you to process, appended as the **only**
argument after your command — your program receives it as `sys.argv[1]`, with no other argument,
flag, or environment variable carrying it.

Your tool must print **exactly one JSON object to stdout**, nothing else:

```json
{
  "unique_target_count": 123,
  "golden_records": [
    {"gene": "...", "primary_accession": "...", "sources": ["..."]}
  ],
  "findings": [
    {
      "gene": "...",
      "observed": "<the defective value as it appears in the file>",
      "correct": "<the corrected value>",
      "retrieved_evidence": "<what the authority literally returned when you resolved the entity>",
      "evidence_source": "<where you got it>",
      "severity": "...",
      "classification": "..."
    }
  ]
}
```

This is the exact contract from `verification_starter.md` — restated here,
not paraphrased. **Print only this JSON to stdout** — no debug output, no progress bars, no
logging to stdout. The agent takes your process's entire captured stdout, as-is, and attempts to
parse it as a single JSON value. It does not scan for a JSON-looking substring inside a larger
block of text and does not take "the last line" or "the biggest {...} it can find" — if anything
precedes or follows your JSON object, on any line, the parse fails outright, and a failed parse is
scored identically to a crash: zero credit for that run, not a partial read of whatever JSON you
did emit.

---

## 3. `goldentarget_config.json` reference

```json
{
  "runtime_version": "3.11",
  "run_command": "python3 solve.py",
  "requirements_file": "requirements.txt"
}
```

| Field | Required | Default | Notes |
|---|---|---|---|
| `runtime_version` | No | `"3.11"` | Python version the agent runs your repo with. **Supported: `"3.11"`, `"3.12"`, `"3.13"`.** An unsupported or misspelled value does **not** fail your submission — it silently falls back to `"3.11"`. See §5 for why that silence is a real risk. |
| `run_command` | No¹ | `python3 solve.py` (only if `solve.py` exists at your repo root) | The command that runs your tool. **Must name only the interpreter and your script — no extra flags or arguments.** The agent's harness appends exactly one positional argument (`<pack_dir>`) to whatever you declare here; if your command already has its own arguments, the pack directory becomes an unexpected extra argument to your program instead of `sys.argv[1]`. `entrypoint` is accepted as a synonym for `run_command`. |
| `requirements_file` | No | `"requirements.txt"` | Path to your requirements file, relative to your repo root. A missing or empty file is not an error — a tool with **zero third-party dependencies is fully legitimate** (this starter, and the organizers' own reference tool, are both pure-stdlib). If no package is required then add an empty `requirements.txt` — the file having no content is fine, but adding the file itself is mandatory. |

¹ If you omit `goldentarget_config.json` entirely, the agent looks for `solve.py` at your repo
root and assumes `python3 solve.py` — but you should still declare the manifest explicitly; an
absent manifest generates a warning during your preflight check even though it isn't fatal by
itself.

There is deliberately no `output_csv` field or any other file-based output declaration — your
tool's output is the stdout JSON above, not a file the agent goes looking for.

---

## 4. What can go wrong (read this before you submit)

- **Extra stdout output breaking JSON parsing.** Any `print()`, logging handler, or third-party
  library that writes progress/debug text to stdout will corrupt your output — see §2 for exactly
  how the parse works and how your preflight check catches this before real grading. Redirect
  diagnostic output to `sys.stderr`, or don't emit it at all.
- **`run_command` pointing at a script that doesn't exist**, or a `goldentarget_config.json` that
  isn't valid JSON. Both are hard failures caught early by your preflight check — and if a
  script with the right name exists somewhere else in your repo (a common mistake: your whole
  project nested one folder deeper than expected, e.g. from zipping a parent folder instead of the
  project folder itself), the preflight check will tell you exactly where it actually found it.
- **`goldentarget_config.json` living somewhere other than your repository root.** This agent only
  reads a config file that sits directly at the root — a copy anywhere else (a subfolder, a nested
  project) is silently ignored, not an error. Your preflight check will point out an extra config
  file it found elsewhere, specifically so this doesn't look like nothing happened.
- **Declaring an unsupported `runtime_version`.** This does not fail your submission — it silently
  defaults to Python 3.11. If your code uses a language feature introduced in 3.12+ while you
  declared (or omitted) a version that resolves to 3.11, your tool will fail at grading time in a
  way you may never see locally if your own machine runs 3.12+.
- **Unpinned, JFrog-unavailable, or mutually conflicting packages in `requirements.txt`** — see §7.
  Note in particular: two packages that individually install fine can still require incompatible
  versions of a *third*, shared dependency — `pip install` can exit successfully while leaving that
  conflict in place, because pip doesn't always refuse to proceed when it can technically pick
  *something* for each package. Your preflight check runs `pip check` immediately after installing
  your requirements, in the exact same environment it will grade you in, specifically to catch this.
- **Your tool exceeding the per-pack runtime budget** — see §8. This is checked identically by
  your preflight check and by real grading, so if you pass preflight you should pass this at
  grading time too, *for the packs preflight actually exercises*. It does not guarantee performance
  on data you have not tested against.
- **Relying on network behavior that differs between your machine and grading time** — rate
  limits, connectivity blips, or slow responses from whatever external source(s) you resolve
  identifiers against are your risk to harden against (retries, backoff, timeouts). The agent runs your tool exactly
  once per pack at grading time; there is no retry on your behalf if your own tool doesn't retry.
- **Type or shape mismatches in your output** — e.g. `unique_target_count` as a string instead of
  an integer, or `golden_records`/`findings` as anything other than a JSON array. Your preflight
  check checks the shape of your output and will fail you here before you ever reach real grading.
- **Assuming your current working directory is your repo root** — see §6. If your tool reads any
  file bundled in your own repo (not the pack data), use a path relative to your script's own
  location, not a bare relative path.
- **"It works on my machine."** This is almost always true and almost always still a bug in your
  submission, not in the agent — your own machine usually has other Python versions, other
  package versions, or other system libraries already installed from unrelated work, and any of
  those can silently paper over a real problem in your `requirements.txt` or your code. Treat a
  clean preflight pass — run against the exact Python version and exact package set your
  repository declares, nothing borrowed from your own machine's existing environment — as the
  standard to meet, not your own machine's behavior.

---

## 5. How the agent runs your tool (transparency)

The evaluation agent: clones your repository → reads `goldentarget_config.json` → resolves your
declared `runtime_version` to a pinned Python interpreter → installs `requirements_file` from
Lilly's internal JFrog package index (no public PyPI access is attempted) → invokes your tool as
`<resolved-python> <resolved-entry-script> <pack_dir>`.

**Your `requirements.txt` install is always real, never skipped or simplified — even if
`runtime_version` fell back to the default.** If you omit `runtime_version`, or declare one that
isn't supported, the agent still runs a full, genuine `pip install -r requirements.txt` against
whichever Python version it actually resolved to (the default), and then runs `pip check` in that
same environment to confirm your dependencies don't conflict with each other. Your preflight check
and real grading use exactly the same install step — there is no "lighter" preflight version — so
a clean preflight result is a genuine guarantee, not an approximation, that dependency installation
will succeed the same way during real grading.

Concretely: **your own `run_command` string is never executed via a shell inside your repo's
working directory.** The agent resolves it internally to an absolute interpreter path and an
absolute script path, and invokes that pair directly. This has one practical consequence for your
code: **do not assume your script's current working directory is your repo root.** If your tool
needs to read any file that ships inside your own repo (not the pack data passed as `<pack_dir>`),
resolve that path relative to your own script's location, e.g.:

```python
import os
HERE = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(HERE, "my_bundled_file.json")   # correct — cwd-independent
config_path = "my_bundled_file.json"                        # WRONG — assumes cwd == repo root
```

The pack directory itself is always passed to you as `sys.argv[1]` — that part of the contract is
unaffected by any of this.

---

## 6. Python packages — JFrog only

**All packages installed by the evaluation agent come from Lilly's JFrog Artifactory — not public
PyPI.** The agent sets `PIP_INDEX_URL` automatically when it installs your `requirements_file`;
you don't need to configure anything for this yourself. **Pin exact versions:**

```
requests==2.32.3
```

A version that resolves cleanly against public PyPI on your own machine is **not guaranteed to
exist in JFrog**, and JFrog's available package/version set can differ from PyPI's — combined with
Python-version-specific wheel availability (a package with no pre-built wheel yet for 3.13, for
example), this is the real risk behind "my requirements.txt worked locally but not at grading
time." Pin conservatively, and prefer widely-available pure-Python packages — or no third-party
packages at all. Both this starter and the organizers' own reference tool ship zero dependencies,
which is a working existence proof that a fully stdlib-only tool is completely viable for this
challenge.

**A second, less obvious risk: two packages that are individually installable can still conflict
with each other.** If package A needs version 2.x of some shared library and package B needs
version 1.x of that same library, `pip install` may still exit successfully — but the environment
it leaves behind is broken, in a way that can produce confusing errors at import time or, worse,
silently wrong behavior with no error at all. Your preflight check runs `pip check` right after
installing your requirements, in the exact same environment used for grading, specifically to
catch this before it costs you a submission. Run `pip check` yourself after installing your own
`requirements.txt` locally to see the same thing.

---

## 7. Runtime budget

From `verification_starter.md`, verbatim:

> Your tool must complete each pack **within 5 minutes** when graded — and it will be executed on
> a second, hidden dataset with the same schema that you never see. All objective points are
> measured there.

**Agent-specific clarification:** this 5-minute (300-second) budget applies **only to your tool's
own execution** against a pack. It does **not** include the time the agent spends cloning your
repository or installing your `requirements.txt` — environment setup has its own separate, far more
generous allowance and is never counted against your 5 minutes. The agent enforces the execution
budget the same way the official grading harness does, so what you observe from your preflight
check is representative of grading behavior — for the packs the preflight check actually runs your
tool against.

---

## 8. Local testing with Rancher Desktop

#### *Steps may vary depending on individual system configuration. The information below is for reference only, to ensure participants use the Lilly-approved container builder.*

*If any issues occur with the commands mentioned below, participants should use Claude to help troubleshoot and resolve them.*

You do not need Docker Desktop specifically — [Rancher Desktop](https://rancherdesktop.io/) is a
free, Docker-API-compatible alternative and works identically for everything below.

1. Install and open Rancher Desktop; verify it's working from a terminal:
   ```bash
   docker version
   ```
2. From your repo root:
   ```bash
   docker build -t my-goldentarget-tool -f Dockerfile .
   ```
3. Run your tool inside the container against a pack (adjust the mount path to wherever your local
   copy of the exam pack lives):
   ```bash
   docker run --rm -v $(pwd)/exam:/pack my-goldentarget-tool solve.py /pack
   ```

**This container is for your own local smoke-testing only.** It does not talk to the real
evaluation agent and is not a substitute for your preflight check — it exists so you can catch
environment-shaped bugs (missing system libraries, Python-version-specific behavior, path
assumptions per §5) *before* relying on the real preflight check. 
(This approach is for reference, Please setup your system as per your system configuration)

---

## 9. The sample `Dockerfile`

Provided at `Dockerfile` in this repo. `FROM ubuntu:22.04` — matching the evaluation agent's own
base image — but installing a single declared Python version (whichever you set as
`runtime_version`), not the agent's full multi-version toolchain (you only need to test against
the one version you actually declared). This is a **simplified** environment for your own
convenience; it is **not** a byte-for-byte replica of the agent's own container (which is
organizer-only and not shared) — it exists to de-risk the most common class of "works on my
machine" failure, not to guarantee identical behavior in every respect.

---

## 10. GitHub access — collaborator requirement

The evaluation agent accesses your repository using a GitHub Personal Access Token belonging to
the Claude Olympics organizer account. **Your repository must grant this account read access
before the submission deadline**, by one of the following:

- **(a)** Add the **`ClaudeOlympic`** GitHub team as a collaborator with at least **Read** access
  to your repository, or
- **(b)** Create your repository inside an organization where the `ClaudeOlympic` team already has
  org-level access — an **organization-internal** repository is sufficient under this option, or
- **(c)** If you keep your repository **organization-private**, you must still explicitly add the
  `ClaudeOlympic` team — organization-private visibility alone does **not** grant it access.

**Run your preflight check to verify access yourself before the deadline** — a clone failure there
means grading will also fail, for the same reason. The preflight check is provided specifically so
you can self-diagnose this before it matters; it is not a substitute for confirming access early.
**Repositories inaccessible to the organizer account at grading time cannot be scored.**

---

## 11. Submission checklist

- [ ] Run your preflight check against your repo and confirm it reports your submission as ready.
- [ ] Confirm your repository is accessible per §10 — a preflight clone failure means it
      is not.
- [ ] Confirm all three submission artifacts, per the challenge brief:
  1. Your GitHub repo (this structure, with `solve.py` replaced by your own tool).
  2. Your Claude chat export (`.md`) — your investigation transcript.
  3. Your approach summary (≤1,500 words — a ceiling, not a target).

---

## 12. Support

For any questions or confusion, please drop a message in the **"AI&R - Claude Olympics 2026"**
Teams channel. The team will get back to you promptly.
