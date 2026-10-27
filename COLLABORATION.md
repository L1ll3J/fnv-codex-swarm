# Generalist collaboration groups

Each group works on one function under one controller-owned lease. There are no fixed lead, integrator or specialist roles. Contributors brainstorm, review complete candidates and build on checked findings through an append-only shared board.

Each peer receives a separate workspace, an immutable prompt and assignment, the shared board command, and the shared checker command. Agent-facing source rules and the collaboration block are the exact individually approved versions in `src/native_swarm/prompts/generalists.xml`. Independent workers retain their original template.

The group checker serializes native batches under a group lock and the original recorded checker's round lock. All peers share twenty rounds, up to eight candidates per round and the separate compiler ceiling. Source reservations prevent duplicate work; completed duplicate sources return their existing receipts. Findings carry source hashes and compact references to real checker manifests; large diagnostics stay in the native artifacts and checker replies. Partial instruction improvements are never submitted as matches.

Only the supervisor finalizes the canonical result. It seals new checks, waits for an in-flight batch, reaps all peers and preserves every workspace and handoff. The ordinary resolver checks group provenance and actual peer model/usage before the original evidence audit and independent submission validator. Unsealed, live or approval-rejected groups cannot be released by the expiry fallback. Rejection evidence also sets the ordinary native admission stop and approval blocker. Zero-work capacity retries archive the complete old group; ordinary failures and uncertain launches are retained for review.

Profiles count physical agents: two concurrent functions with three peers each consumes six slots from the configurable total worker capacity (default ten). Closing the terminal app detaches; it does not restart or stop model work. Selection changes affect future admissions, while model/reasoning/allocation changes wait for a fresh run.
