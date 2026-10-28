"""Read-only projection of native receipts; process state and ownership stay distinct."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import time
import uuid


def read(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def atomic(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    with tmp.open("w") as output:
        output.write(json.dumps(value, indent=2) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(tmp, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def process_alive(pid, root: Path):
    if type(pid) is not int:
        return False
    try:
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        return str(root).encode() in args or any(
            arg.startswith((str(root) + "/").encode()) for arg in args if arg
        )
    except OSError:
        return False


class Projection:
    def __init__(self):
        self.cache = {}

    def load(self, path):
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return {}
        old = self.cache.get(path)
        if old and old[0] == stamp:
            return old[1]
        value = read(path)
        self.cache[path] = (stamp, value)
        return value

    def snapshot(self, runs: list[Path], quota_path: Path):
        rows, blockers, improvements, controllers = [], [], [], []
        verdicts = {}
        for run in runs:
            verdicts.update(self.load(run / "observed-verdicts.json"))
        for run in runs:
            journal = self.load(run / "rolling-executor/journal.json")
            if self.load(run / "native-app-managed.json"):
                owner = self.load(run / ".rolling-executor-owner.json")
                stop_file = run / "STOP_NEW_CLAIMS"
                controllers.append(
                    {
                        "run": run.name,
                        "alive": process_alive(owner.get("pid"), run),
                        "stop": stop_file.read_text().strip()
                        if stop_file.exists()
                        else "",
                    }
                )
            jobs = {
                j.get("claim_id"): j
                for j in journal.get("jobs", {}).values()
                if j.get("claim_id")
            }
            for path in sorted((run / "claims").glob("*/claim.json")):
                claim = path.parent
                c = self.load(path)
                job = jobs.get(claim.name, {})
                result = self.load(claim / "result.json") or self.load(
                    claim / "work/result.json"
                )
                retry = self.load(claim / "capacity-retry.json")
                pid = (
                    retry.get("pid")
                    if retry.get("state") == "running"
                    else job.get("pid")
                )
                alive = process_alive(pid, run)
                submission = self.load(claim / "submission-response.json")
                released = (claim / "giveup-response.json").exists()
                verdict = verdicts.get(submission.get("submission_id"), {}).get(
                    "verdict"
                )
                if submission:
                    state = (
                        "verified"
                        if verdict == "verified"
                        else "verification failed"
                        if verdict in ("failed", "rejected", "needs_review")
                        else "verification pending"
                    )
                elif released:
                    state = "released"
                elif alive:
                    state = "working"
                elif result.get("match"):
                    state = "MATCH awaiting controller"
                elif result:
                    state = "result awaiting controller"
                else:
                    state = "open claim / no worker"
                expired = False
                try:
                    lease = dt.datetime.fromisoformat(
                        (c.get("expires") or c.get("expires_at") or "").replace(
                            "Z", "+00:00"
                        )
                    )
                    expired = not alive and lease.timestamp() <= time.time()
                except (ValueError, TypeError, OverflowError):
                    pass
                if expired and not submission and not released:
                    state = (
                        "expired lease / saved MATCH"
                        if result.get("match")
                        else "expired lease / saved result"
                        if result
                        else "expired lease"
                    )
                records = []
                for record in sorted(
                    (claim / "work/recorded-checks").glob("check-*.json")
                ):
                    value = self.load(record)
                    if value:
                        records.append(value)
                best, best_check, best_time = None, None, None
                history = []
                for index, value in enumerate(records, 1):
                    check = value.get("checker_result", {})
                    score = (
                        0
                        if check.get("status") == "match"
                        else check.get("ndiff_insn")
                        if check.get("status") == "wip"
                        else None
                    )
                    if type(score) is int and (best is None or score < best):
                        previous = best
                        best, best_check = score, index
                        best_time = value.get("finished_at") or value.get("started_at")
                        event = {
                            "claim_path": str(claim),
                            "claim_id": claim.name,
                            "address": c.get("address"),
                            "at": best_time,
                            "check": index,
                            "from": previous,
                            "to": score,
                        }
                        history.append(event)
                        improvements.append(event)
                elapsed = None
                if best_time:
                    try:
                        elapsed = max(
                            0,
                            time.time()
                            - dt.datetime.fromisoformat(
                                best_time.replace("Z", "+00:00")
                            ).timestamp(),
                        )
                    except ValueError:
                        pass
                proposal_paths = [
                    claim / "defect-proposal.json",
                    claim / "work/defect-proposal.json",
                ]
                proposal_paths.extend(
                    sorted(
                        (claim / "collaboration").glob("peer-*/defect-proposal.json")
                    )
                )
                for proposal_path in proposal_paths:
                    proposal = self.load(proposal_path)
                    if proposal:
                        blockers.append(
                            {
                                "id": str(proposal_path),
                                "claim_id": claim.name,
                                "address": c.get("address"),
                                "proposal": proposal,
                                "validation": self.load(
                                    claim / "defect-automation-result.json"
                                ),
                                "evidence_path": str(claim),
                            }
                        )
                assignment = self.load(claim / "assignment.json")
                group = self.load(claim / "collaboration/group.json")
                members = group.get("members", [])
                agents = (
                    sum(process_alive(member.get("pid"), run) for member in members)
                    if members
                    else int(alive)
                )
                usage = {
                    t.get("id"): t
                    for t in self.load(run / "usage-current.json").get("threads", [])
                }
                from .group_accounting import totals

                group_totals = totals(claim, usage) if group else None
                row = {
                    "official_tools_id": assignment.get("official_tools", {}).get("id"),
                    "model": assignment.get("model", "unknown"),
                    "effort": assignment.get("reasoning_effort", "unknown"),
                    "agents": agents,
                    "group_size": len(members) or 1,
                    "group_cost": group_totals["known_cost_usd"]
                    if group_totals
                    else None,
                    "group_cost_unknown": group_totals["unknown_cost_threads"]
                    if group_totals
                    else 0,
                    "id": str(claim),
                    "claim_id": claim.name,
                    "run": run.name,
                    "address": c.get("address", "?"),
                    "bytes": c.get("size"),
                    "state": state,
                    "alive": alive,
                    "closed": bool(submission or released or expired),
                    "best": best,
                    "checks": len(records),
                    "rounds": len(list((claim / "work/batch-rounds").glob("*.json"))),
                    "limit": assignment.get("max_batch_rounds", 20),
                    "checks_since_best": len(records) - best_check
                    if best_check
                    else None,
                    "seconds_since_best": elapsed,
                    "best_history": history,
                    "lease": c.get("expires") or c.get("expires_at"),
                    "error": "peer failed; checked work retained for review"
                    if group.get("ordinary_failure")
                    else job.get("state")
                    if "review" in str(job.get("state"))
                    else "",
                    "residual": result.get("residual_issue", ""),
                    "worker": job.get("worker"),
                    "pid": pid,
                    "stop": (run / "STOP_NEW_CLAIMS").read_text()
                    if (run / "STOP_NEW_CLAIMS").exists()
                    else "",
                }
                rows.append(row)
        rows.sort(key=lambda r: (not r["alive"], r["closed"], r["run"], r["address"]))
        return {
            "rows": rows,
            "blockers": blockers,
            "improvements": sorted(
                improvements, key=lambda x: x.get("at") or "", reverse=True
            ),
            "quota": self.load(quota_path),
            "controllers": controllers,
            "running": sum(r["agents"] for r in rows),
            "open": sum(not r["closed"] for r in rows),
            "verified": sum(r["state"] == "verified" for r in rows),
            "pending": sum(r["state"] == "verification pending" for r in rows),
        }
