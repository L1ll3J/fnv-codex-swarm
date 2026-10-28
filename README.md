# Native Swarm

A separate terminal control centre for the local native FNV controller. The scheduler is ordinary Python; no model orchestrates the swarm.

For an already configured installation, run its `./swarm` launcher or the installed `native-swarm` entrypoint. Opening or closing the UI starts, restarts and kills no workers. **Start / Resume** explicitly admits work. **Pause admissions** stops new claims and fresh-session capacity retries; current work continues. **Drain safely** lets current work finish, resolves its claims and closes that controller. Existing imported runs keep their original controllers.

## Controls

- **Workers:** actual model/reasoning, live agents, function size, recorded checks and rounds, best-score history, lease and errors. Open the exact rendered prompt or collaboration board for the selected function. Local MATCH, submitted/pending and independently verified remain separate states.
- **Worker profiles:** set model, reasoning, largest/smallest/random/recently returned priority, multiple size brackets, queue preference, concurrent functions, agents per function, batch rounds (default 20), and shared/per-member team budgets. Recently returned favors functions reopened most recently; it is a selection preference, not evidence that a function is easier. Round settings apply to newly assigned functions; existing assignments keep their original ceilings. Brackets filter both automatic picks and queued work; incompatible queue entries stay queued. Total worker capacity is configurable in Settings (default ten). New profiles start with zero slots; edit concurrent functions and agents per function to allocate them. The profile list and editor show allocated/free slots. Start skips zero-slot profiles and requires at least one allocated slot. Each native profile controller supports up to 75 concurrent functions; larger totals can be split across profiles. Each profile controller has one fixed model; multiple profiles may use the same model. Selection preferences and concurrent-function counts apply to future admissions. Removed or zero-slot profiles stop admitting work; existing tasks finish under their original assignments. Model, reasoning, agents-per-function and prompt changes require fresh controllers. Freed physical slots become available to the saved solo/team split without exceeding the global capacity.
- **Best history / Queue:** queue a checked unfinished source for any queue-enabled profile or a specific profile. Historical scores are starting evidence, not fresh checks. The native seed installer verifies source/log hashes and records old/new pack provenance. Reservations are atomic; uncertain acquisitions remain held for review. A durable closure receipt makes an assigned queue item complete and eligible to requeue.
- **Prompts:** edit independent-worker or generalist-team templates. Operator edits are versioned locally; existing model sessions keep their actual prompt. Defaults contain the individually approved blocker wording; team defaults also contain the approved collaboration wording. Existing operator-authored overrides are preserved.
- **Blocker reports:** inspect reports from independent workers and every collaboration peer. Local assessments distinguish observations, suspected blockers, established blockers and resolved reports. Agent reports are initially unassessed. Flag submission requires confirmation that checked evidence establishes a defect preventing a permitted solution, as well as reviewing an editable factual note, official category and exact payload, then explicit confirmation. Actual responses and uncertain writes are retained; ambiguous writes are never automatically resent.
- **Settings:** set total worker capacity, select default brackets for new profiles and a global usage policy. Choose the cutoff as a percentage used; stop queuing, taper admissions from a configurable starting percentage, or signal active workers to stop at the cutoff. Taper distributes a single reduced function/group target across active profiles. Usage stops latch until explicit Start / Resume, and do not remove other controller stops. Stale or unknown observations block admissions without triggering immediate termination. Imported controllers retain their original lifecycle until drained. Older app-managed controllers receive count/pause compatibility updates without restarting models; requests already in flight may finish. Fresh controllers serialize allocation publication with claim dispatch and recheck allocation before launching a prepared worker. The default worker count applies only before custom profiles are configured; it cannot resize an active group allocation.

## Collaboration

Choose **2–4 agents per function** in a profile for a team of generalists. They share one controller-owned lease, one append-only board and one recorded batch checker. Each peer has its own workspace and immutable model prompt. The shared adapter reserves experiments by source hash, returns existing receipts for already checked sources and enforces the assignment’s shared allowance or each member’s configured allowance. Per-member mode scales aggregate check/compiler ceilings by team size. Board reads include authoritative budget and best-receipt state, and default to the latest entries. The board stores compact receipt references; full diagnostics remain in native artifacts and checker replies. Complete candidates still pass the original source/checker audit and independent plain-MATCH submission gate.

At the round limit the supervisor seals checking, waits for the current batch, and allows up to 45 seconds within the original deadline for handoffs before reaping peers and finalizing a canonical result. Model/usage evidence and peer prompt hashes are checked before resolution. Timeout and lease cleanup keep an unsealed group's slot and claim reserved. Approval or permission rejection stops admissions and holds the affected work for operator review, including at expiry. Verified group-wide zero-work capacity failures use the existing bounded fresh-session retry path, archiving every peer's prompts and logs first.

## Session persistence and blocker assessment

Normal early completions resume the same actual Codex thread using the approved continuation message. The claim, deadline and remaining check/compiler budgets are retained; failed results are archived before continuation. Genuine CLI/approval failures and ambiguous compiler dispatches stay held for review. Historical duplicate refusals reuse checked evidence without a new receipt or compiler count. Zero-work capacity failures keep the existing bounded retry path.

In **Blocker reports**, **Enable Sol assessor** enables a local queue watcher. Each new report launches one fresh Sol-medium read-only session, then exits; there is no continuously running assessor model. It requires one free physical worker slot and fresh usage allowance. Active older controllers must drain before assessment can run because they cannot reserve a slot cooperatively. Pause/drain stops new assessment admissions too. Opening the UI starts no assessments.

An unchanged report is not repeatedly assessed for ordinary failed checks. Changed reports, environments or checked MATCH evidence can invalidate a classification. Observations leave the active queue but remain available under **Show assessment history**; inconclusive and established reports remain visible. Assessor prompts, actual thread/model evidence, token counters and decisions are saved locally. No assessor can submit flags; operator confirmation remains required.

## Existing safeguards

Defaults remain ten Sol medium workers, twenty batch rounds, one to eight candidates per round, random eligible functions and a 50%-used admission cutoff with stop-queuing policy. In-flight work finishes at that default cutoff. Usage policy changes affect compatible controllers on their next scheduler tick. Quota uses the documented local Codex `account/rateLimits/read` RPC without model calls or loading credentials into the UI. Unknown/stale allowance blocks admission. Original pack/compiler/checker, ownership, account health, source hashes and independent submission validation remain enforced.

State lives in `~/.local/share/fnv-native-swarm/config.json`. Authentication and toolchain setup remain owned by the existing native runtime; this app is for the already configured local installation. No credentials are stored in this repository.

This repository contains the terminal app and its controller adapters. The existing deterministic controller template, compiler, permitted game image and account setup are external prerequisites; cloning this repository alone is not a complete new-machine installation. Install the Python package with `python -m pip install .` once those prerequisites are configured. Assignment resource paths resolve the local game image and environment script without embedding a particular user’s home directory.

## Validation

`python -m pytest -q` runs offline unit tests and Textual headless UI tests. Model, compiler and service calls are mocked. Local integration tests compile generated controller files against the installed pinned template and verify original checker/submission files remain byte-for-byte unchanged. Live swarm operation is operator-tested separately.

## Codex executable

The app uses Codex from `PATH`, or discovers a compatible user VS Code Codex extension, including a matching Windows user profile on WSL. Set `NATIVE_SWARM_CODEX=/absolute/path/to/codex` for an explicit override. Invalid overrides fail clearly rather than silently selecting another installation. A fresh run records the resolved executable in `native-app-codex.json` and uses it for independent workers, collaboration peers and allowance polling. Existing runs keep their executable. Allowance errors distinguish executable discovery, account-client startup and RPC failures; no authentication files are copied.

### Automatic official tools and data

New assignments refresh the official checker archive, function index, symbols,
compiler tips, large-function guidance and upstream agent guidance into validated content-addressed
snapshots. Freshness checks are shared across profiles and cached for five
minutes; failed refreshes retain the last good snapshot and block new admissions
until retry, without changing active assignments. Every assignment binds its
checker commands, environment, isolated cache, history eligibility and independent
submission validation to its selected snapshot. Tool refresh status appears in the
summary and each worker's generation appears in its details.

Checker compilation control flow has an explicit accounting compatibility gate.
The 27 October kit permits class-definition repair and accepted alternate flag
sets, requiring a larger conservative compiler bound than older kits. Data and
compatible tool generations update automatically; unknown compilation adapters or
control flow stop new admissions for compatibility review instead of guessing a
safe bound. Existing work retains its original generation and budget.

Compiler-flag reports use the blocker dialog's optional source/flags fields.
Explicitly approve the diagnostic check and checkpoint action, then review and
confirm the resulting flag payload. This requires an owned current claim with
finished workers and sufficient check/compiler allowance. Proof source, flags,
pack/tool identities, log and checkpoint receipt are preserved separately from
ordinary candidate MATCH receipts; a proof-only MATCH is never submitted as a
normal solution. Ambiguous checkpoint/flag writes remain for reconciliation.
Automatic tool updates never authorize automatic flags or prompt rewrites.

### Automatic application releases

The operator UI checks the configured GitHub repository for stable releases at
startup and hourly. Settings can disable these checks. A release must include a
package/version-bound `release.json` and a wheel with the matching SHA-256.
Installation and dependency/import checks happen in a separate runtime before an
atomic next-launch pointer is published. The running UI, workers and their pinned
assignments keep their current code. A failed check or install retains the last
good runtime and exposes update status; it does not block work.

Only a later operator UI launch activates a prepared version. Worker and
maintenance commands do not silently switch versions mid-operation. The first
installation of this updater still requires the usual explicit package upgrade.
Published Git tags trigger the release workflow, which builds the wheel and its
manifest. This is application code from the configured repository, not arbitrary
URLs supplied by a worker or report.


### Supported compiler generations

The audited 28 October checker generation supports VC++ 2008 SP1 15.00.30729.01,
VC++ 2008 RTM 15.00.21022.08 and VC++ 2005 SP1 14.00.50727.762.
Install each separately; never overwrite SP1. By default the
app looks for `~/fnvdecomp/tools/msvc9` and the optional siblings `msvc9rtm` and `msvc8`.
Override these roots with an owner-controlled `compiler-toolchains.json` in
the app state directory, for example `{"sp1":"/tools/msvc9","rtm":"/tools/msvc9rtm","vc8":"/tools/msvc8"}`.
New assignment generations record compiler versions and core component hashes;
existing assignments retain their recorded environment. An unrecorded ambient
compiler override cannot silently change an older assignment. The official checker
selects VC8 only for packs marked `compiler: vc8`; missing VC8 blocks preparation
of those assignments. Ordinary packs use SP1 with RTM fallback on near misses,
including integer code. Exact compiler annotations are validated at the independent
submission gate and when preparing operator flag proofs.

For this generation, recorded checks retain whole-function comparison scores,
exact-region and stack-slot diagnostics, and a `full_diff_path` receipt field when
a complete aligned diff was produced. Large-function guidance is available as
`resource_paths.large_functions`. Older assignments keep their original diagnostic
options and compiler generation.

The recorder reserves up to twenty internal compiler calls per accepted flag
set for this generation, covering both compilers' repair passes. Profile round
and candidate-check settings remain unchanged. Official annotated RTM MATCH
results still require independent plain validation against the original pack.
The upstream fallback returns RTM diagnostics only for exact matches; partial
RTM improvements are not exposed by its ordinary output.
