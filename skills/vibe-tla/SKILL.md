---
name: vibe-tla
description: >-
  Write code-faithful TLA+ specifications to find real concurrency, durability,
  replication, recovery, and ordering bugs in any codebase. Check specifications with
  TLC, or use Apalache for symbolic SMT or inductive invariants when Cartesian state
  spaces explode. Triage violations into `candidate` or `confirmed` bug reports. This
  self-contained skill stores per-repo memory and provides commands for `init`, `check`,
  `review`, `fix`, `sync`, `resume`, `advance`, `status`, `coverage`, and `effort`. Use
  when the user mentions TLA+, TLC, Apalache, model checking, or formal verification. Use
  when the user mentions interleaving checks for races, lost updates, stale reads,
  deadlocks, or quorum bugs.
---

# vibe-tla — code-faithful TLA+ bug-hunting for any repo

You produce **mechanically checked, code-faithful evidence**. Every modeled transition in
a TLA+ spec cites a real `file:line`. You check each spec with TLC and document it so a
skeptical reviewer can read the spec beside the source. A TLC violation becomes a bug
report only after it survives a refutation checklist. Honesty about `candidate` vs
`confirmed` is the whole point.

This skill is **standalone and project-agnostic**. It carries no paths, module names, or
quorum constants for any specific codebase. Everything specific to the target repo stays
in a **per-repo workspace**. `init` creates that workspace, and `check` and `sync` read
and grow it as long-term memory. Nothing here depends on the repo you test it against.

## Skill layout (read on demand)

`SKILL.md` (this file) dispatches commands and summarizes each command. The **full
step-by-step procedure for each command is in `references/commands.md`**. Read the section
for your command before you run it. Other reference files relative to this skill's
directory hold deeper standards. Read each file when you need it:

- `references/commands.md` — **the exact procedure for every command** (`init`, `check`,
  `sync`, `resume`, `advance`, `status`, `coverage`, `effort`, `review`, `fix`).
- `references/methodology.md` — **the binding standards**: the ladder method, the two
  verification modes, the standard level layout, the classification vocabulary, and the
  definition of done. Read this file before you write any spec.
- `references/lessons.md` — **the case law**: generalized, repo-agnostic rules (R1–R5)
  from real verification mistakes. It covers denotation-before-invariant,
  staleness-needs-a-witness, model-the-runtime's-concurrency, the-answer-is-the-finding,
  and model-the-designed-regime. **Read this file before you classify any violation.**
- `references/authoring.md` — how to write a faithful spec: the `var ↔ code` map, action
  style, fault-injection patterns, and TLA+ idioms. It also explains how to iterate to
  "runs clean in TLC".
- `references/symbolic.md` — **state explosion from Cartesian products**: Skolem witness
  projection, relational `ConstInit`, Snowcat `@type:` rules, Symbolic BMC (`Apalache`/Z3),
  and Symbolic Inductive Invariants (`IndInv` + Counterexample-to-Induction loop). Read
  this file when wide numeric or bit-packed domains, multi-key maps, or unbounded counters
  make explicit TLC enumeration infeasible.
- `references/performance.md` — **execution speed and multi-layer acceleration**:
  60-second `Progress(d)` bottleneck triage, dead-register canonicalization
  (`ClearDeadSlots`), and congruent `VIEW` bisimulation quotients. It also covers SANY
  32-bit guards for single-spec, dual-engine (`BoundedMode`) specs and `@TLAPlusOperator`
  bytecode overrides (`tlc_bytecode.py`) with zero spec churn. It documents core-budgeted
  LPT and coverage-deduplicated multi-cfg scheduling (`run_suite.sh`).
- `references/tlc-setup.md` — the TLC and on-demand Apalache toolchain, canonical
  invocations, determinism, config keys, and scratch cleanup.
- `references/workspace.md` — the per-repo workspace layout, the memory files, and the
  full scope rules for both install shapes.
- `references/coverage.md` — **fidelity and coverage**: how to measure how faithfully a
  spec mirrors the code and how to read TLC action coverage.
- `references/ci-integration.md` — **experimental**: how to connect `sync` into CI so a PR
  that touches modeled code flags model drift.

Self-locating scripts in `scripts/` handle repetitive mechanical work so you do not
reinvent it each run. The bundled scripts are `install_tlc.sh`, `run_tlc.sh`,
`run_suite.sh`, `tlc_bytecode.py`, `install_apalache.sh`, `run_apalache.sh`,
`new_level.sh`, `status.sh`, `coverage.sh`, `anchors.sh`, and `get_repo.sh`. In a
standalone install, `get_repo.sh` clones and tracks the target repo. Prefer these scripts
over hand-rolled equivalents.

## Command dispatch

The user invokes `/vibe-tla <command> [args]`. The first whitespace-delimited token is the
command. Route that command to its procedure in `references/commands.md`:

- `init` · `check` · `sync` · `resume` · `advance` · `status` · `coverage` · `effort` and
  the dev-cycle commands `review` · `fix` appear in **Commands** below. Read their full
  step-by-step sections in `references/commands.md`.
- For any other token or an empty command, print the one-line usage for every command.
  Then stop.

**Scope — resolve `<repo-root>` before anything else.** Read `references/workspace.md` for
the full scope rule and both install shapes:

1. **Recorded `REPO_ROOT`:** If the target workspace's `config.env` records `REPO_ROOT`
   from a prior `init`, use it. In a standalone install, locate that workspace through
   step 3's `active.env` first. Then use `config.env` as the authoritative `<repo-root>`.
   These steps define precedence, not lookup order.
2. **Embedded install:** This rule applies when the skill is at
   `<repo>/.agents/skills/vibe-tla/`, `<repo>/.claude/skills/vibe-tla/`, `.agent/skills/`,
   or `.codex/skills/`. Set `<repo-root>` to the project that owns that agent directory.
   Follow the symlink's *logical* install path, not its target. Never use the working
   directory or a sibling-holding parent.
3. **Standalone install:** This rule applies to a user-global or agent skill store with no
   owning project. Set `<repo-root>` to the active clone in
   `${VIBE_TLA_HOME:-$HOME/.vibe-tla}/active.env` (`scripts/get_repo.sh active`). If
   `active.env` records no active clone, tell the user to run `/vibe-tla init <repo-url>`
   first. Do not default to the working directory.

Set `$WORKSPACE` to `<repo-root>/tla`. Every file the skill reads, writes, or greps stays
**inside `<repo-root>`**, never in a sibling or parent directory. In remote mode
(`REPO_MODE=remote`), `check` and `sync` run `scripts/get_repo.sh refresh` first to read
current upstream code.

**Effort precedence:** Use an explicit `--effort high|low|medium` flag first. Otherwise
use `VIBE_TLA_EFFORT` from `config.env`, which `/vibe-tla effort` sets. If neither exists,
use `medium`. Effort scales how exhaustively `check` and `coverage` explore.

## Commands — summaries (full step-by-step in `references/commands.md`)

**Before you run a command, read its section in `references/commands.md`.** That file
holds the exact procedure, while the summaries below give orientation. Every command
operates strictly inside `<repo-root>` and keeps workspace memory consistent
(`references/workspace.md`).

- **`init [repo-url]`** — prepare the repo for model checking once. In embedded mode,
  resolve `<repo-root>` to the owning project. In standalone mode, clone the URL or use a
  local path in place via `get_repo.sh`. Create `$WORKSPACE`, install the TLC toolchain
  (`install_tlc.sh`), learn the architecture, and write the memory files
  (`PROFILE`/`MEMORY`/`LESSONS`/`ROADMAP`). This command is idempotent.
- **`check <keywords> [context] [goal] [--ask]`** — the **discover** step, where
  `<keywords>` may be exact names or a fuzzy area description. For an ambiguous target,
  `check` surveys, ranks, and announces its pick, and adds runner-ups to the roadmap
  backlog. It asks only when nothing matches or `--ask` forces an interactive choice. Then
  map the target to a ladder/level, read the source, and write a code-faithful spec. Run
  TLC to a clean run, then triage. This command is **run-until-complete** (stage on interrupt
  and run `resume` later). It emits **`candidate`** findings only, never self-declares
  `confirmed`, and treats context as a hint, not evidence.
- **`sync <keywords>`** — cheaply detect whether the code drifted from a model and
  re-anchor cited `file:line` locations. On material drift, revise the model as a `*_v2`
  bump. Otherwise report "still faithful". In remote mode, `sync` refreshes the clone
  first.
- **`resume [keywords]`** — finish an interrupted `check` from its `## STAGED` note. If no
  staged work exists, stop and point to `advance`. Never invent levels.
- **`advance [ladder] [--to Ln | --count N]`** — continue a clean ladder and run its next
  *planned* level(s). Announce the selected levels before you run them. This command is
  distinct from `resume`.
- **`status [keywords]`** — print the read-only ledger (`status.sh`) and interpret open
  `candidate` findings, staged runs, drift, and the next backlog item. It also points to
  each ladder's `TODO.md`.
- **`coverage [keywords]`** — generate a fidelity and TLC action-coverage report
  (`coverage.sh`). It flags zero-coverage (dead-spec) actions.
- **`effort <high|low|medium>`** — set how exhaustively `check` and `coverage` explore.
  This command sets the workspace default in `config.env`, and `--effort` overrides it per
  run.

## The development-cycle loop — `review` → `fix`

`check` discovers **`candidate`** findings. `review` and `fix` resolve each finding and
re-check every fix to close the **`check` → `review` → `fix` → `check`** loop. Read the
full steps in `references/commands.md`.

```
check <component>   → candidate(s)
review [<finding>]  → confirmed  |  retired: refuted      (the true-positive gate)
fix <finding>       → retired: fixed                      (change + validation re-check)
check ...           → next round  (or `advance` for the next ladder rung)
```

`retired` is the neutral **closed** state. Not every finding ends as *fixed*: some
findings close as `refuted` or `safe-by-design`. Every resolved finding moves to `retired`
with a recorded reason.

- **`review [finding]`** — the true-positive gate. Independently try to *refute* a
  `candidate` with the `lessons.md` R1–R5 checklist. Run the checklist adversarially and
  spawn a fresh reviewer agent when available. A finding that survives becomes
  **`confirmed`**, while a refuted finding becomes **`retired: refuted`**. A finding that
  hinges on one owner fact stays **`candidate`**. Do not trust the agent that found the
  bug to also clear it.
- **`fix <finding>`** — apply the minimal code or spec change **and** prove it. Model the
  fixed code as a `*_v2` spec and re-run TLC to reach **`retired: fixed`**. Show the diff.
  Do not commit or push before you ask the user.

## The bar (non-negotiable, every command)

- **Code-faithful, no assumptions.** Every modeled transition cites a verified
  `file:line`. "I think it probably…" is a defect. Read the source. Mark unverified
  behavior `TODO` or `UNCLEAR`. Never invent behavior.
- **`var ↔ code` map.** Place a `var ↔ code` map at the top of every `.tla` file.
- **Two valid modes.** Pick transcription or faithful fault injection to match the code.
  In fault injection, model only design-tolerated faults. Document every modeled fault.
- **Honest findings.** Use only these labels: `PASS`, `candidate`, `confirmed`, `refuted`,
  `safe-by-design`, and `retired`. A `candidate` stays a `candidate` until `review`
  discharges its one open precondition.
- **Reproducible.** Cite only numbers from your own runs. Take counterexamples from
  the real trace, not from memory.
- **Memory is the product too.** The workspace is long-term memory. Keep it consistent so
  the next run, days later, resumes where this run stopped.

Read `references/methodology.md` for full standards, schema, and definition-of-done. Read
`references/commands.md` for full command procedures.
