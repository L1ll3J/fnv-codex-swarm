"""Fresh read-only blocker assessments with independent operator capacity."""

import hashlib
import html
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .executables import resolve_codex
from .group_check import locked
from .policy import evaluate
from .state import Projection, atomic, process_alive, read

MODEL = "gpt-6.1-sol"


def assessor_policy(cfg):
    return cfg.get("assessor", {}).get("usage_policy") or cfg.get("usage_policy")


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "evidence_refs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "artifact": {"type": "string"},
                    "observation": {"type": "string"},
                },
                "required": ["artifact", "observation"],
            },
        },
        "explanation": {"type": "string"},
        "decision": {
            "type": "string",
            "enum": ["established_blocker", "observation", "inconclusive"],
        },
        "missing_evidence": {"type": "array", "items": {"type": "string"}},
        "suggested_report": {"type": "string"},
    },
    "required": [
        "evidence_refs",
        "explanation",
        "decision",
        "missing_evidence",
        "suggested_report",
    ],
}


def paths(cfg):
    return [
        Path(p)
        for p in dict.fromkeys(
            cfg.get("observed_runs", []) + cfg.get("managed_runs", [])
        )
    ]


def evidence(report, rows):
    primary = Path(report["evidence_path"]).resolve()
    supplemental = sorted(
        {
            r["id"]
            for r in rows
            if r["address"] == report["address"] and Path(r["id"]).resolve() != primary
        }
    )
    roots = [primary] + [Path(p).resolve() for p in supplemental]
    manifest = []
    # Hash reports, assignments and checked receipts, not caches or assessment outputs.
    for root in roots:
        files = [
            root / "assignment.json",
            root / "pack.tar.gz",
            root / "defect-proposal.json",
            root / "work/defect-proposal.json",
            root / "submission-response.json",
        ]
        checks = sorted((root / "work/recorded-checks").glob("check-*.json"))
        files += checks
        for check in checks:
            record = read(check)
            for key in ("source_path", "log_path"):
                if record.get(key):
                    artifact = Path(record[key]).resolve()
                    if artifact.is_relative_to(root):
                        files.append(artifact)
        files += sorted((root / "collaboration").glob("peer-*/defect-proposal.json"))
        for file in files:
            if file.is_file():
                manifest.append(
                    {
                        "artifact": str(file),
                        "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                    }
                )
    payload = {
        "primary_report": report["id"],
        "primary_evidence_directory": str(primary),
        "supplemental_same_address_directories": list(map(str, roots[1:])),
        "report": report["proposal"],
        "artifacts": manifest,
    }
    # Ordinary extra failed checks do not trigger repeated model reviews. Report,
    # environment or verified-MATCH changes do; an operator can explicitly retry.
    identity = {
        "report": report["proposal"],
        "report_id": report["id"],
        "environments": [],
        "matches": [],
    }
    for root in roots:
        identity["environments"].append(
            {
                "directory": str(root),
                "pack": next(
                    (
                        a["sha256"]
                        for a in manifest
                        if a["artifact"] == str(root / "pack.tar.gz")
                    ),
                    None,
                ),
                "assignment": read(root / "assignment.json").get(
                    "checker_workflow", {}
                ),
            }
        )
        for check in sorted((root / "work/recorded-checks").glob("check-*.json")):
            row = read(check)
            if (
                row.get("checker_exit_code") == 0
                and row.get("checker_result", {}).get("status") == "match"
            ):
                identity["matches"].append(
                    [
                        a
                        for a in manifest
                        if a["artifact"]
                        in (str(check), row.get("source_path"), row.get("log_path"))
                    ]
                )
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode()
    ).hexdigest()
    return fingerprint, payload, roots


def results(state):
    return [read(p) for p in (Path(state) / "assessments").glob("*/decision.json")]


def annotated(state, reports, rows):
    known = {r.get("fingerprint"): r for r in results(state)}
    return [dict(r, assessment=known.get(evidence(r, rows)[0], {})) for r in reports]


def assessor_running(state):
    active = []
    for path in (Path(state) / "assessments").glob("*/launch.json"):
        launch = read(path)
        if process_alive(launch.get("pid"), path.parent) or (
            launch.get("intent")
            and not launch.get("pid")
            and not (path.parent / "decision.json").exists()
        ):
            active.append(str(path.parent))
    return active


def claim_reserved(claim, root):
    """Retain physical capacity for survivors and ambiguous supervisor exits."""
    session = claim / "session"
    if session.exists():
        state = read(session / "state.json")
        if not state.get("streams_closed") or any(
            process_alive(t.get("pid"), root) for t in state.get("turns", [])
        ):
            return True
        if any(not t.get("pid") for t in state.get("turns", [])):
            return True
    directory = claim / "collaboration"
    if directory.exists():
        state = read(directory / "group.json")
        if (
            state.get("state") != "sealed"
            or not state.get("streams_closed")
            or any(process_alive(m.get("pid"), root) for m in state.get("members", []))
        ):
            return True
    return False


def occupied(cfg, state=None):
    total = 0
    for root in paths(cfg):
        journal = read(root / "rolling-executor/journal.json")
        group_size = read(root / "native-app-profile.json").get("group_size", 1)
        seen = set()
        for job in journal.get("jobs", {}).values():
            claim_id = job.get("claim_id")
            if claim_id in seen:
                continue
            seen.add(claim_id)
            claim = root / "claims" / str(claim_id)
            if (
                process_alive(job.get("pid"), root)
                or claim_reserved(claim, root)
                or job.get("state") == "launch_intent"
            ):
                total += group_size
    return total


def requested(state):
    value = read(Path(state) / "assessment-service.json")
    return bool(
        value.get("pending")
        and time.time() - value.get("heartbeat", 0) < 15
        and process_alive(value.get("pid"), Path(state))
    )


def start(state):
    state = Path(state)
    with locked(state / "assessment-start.lock"):
        from .backend import Backend

        with Backend(state).locked():
            cfg = read(state / "config.json")
            cfg.setdefault("assessor", {})["enabled"] = True
            atomic(state / "config.json", cfg)
        (state / "assessment-admissions-paused.json").unlink(missing_ok=True)
        value = read(state / "assessment-service.json")
        if process_alive(value.get("pid"), state):
            if value.get("version") == 2:
                return "Automatic blocker assessment is already enabled."
            atomic(
                state / "assessment-admissions-paused.json",
                {"source": "service-upgrade"},
            )
            with (state / "assessment-service.log").open("ab") as log:
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "native_swarm.assessment",
                        "--upgrade",
                        str(state),
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            return "Assessor service upgrade queued; active assessment finishes before migration."
        with (state / "assessment-service.log").open("ab") as log:
            proc = subprocess.Popen(
                [sys.executable, "-m", "native_swarm.assessment", str(state)],
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        atomic(
            state / "assessment-service.json",
            {
                "pid": proc.pid,
                "version": 2,
                "heartbeat": time.time(),
                "pending": True,
                "status": "starting",
            },
        )
    return "Automatic blocker assessment enabled, with independent assessor capacity."


def observed_session(thread, cwd):
    sessions = (
        Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
    )
    for file in sessions.glob(f"*/*/*/*{thread}*.jsonl"):
        meta = None
        model = None
        usage = None
        for line in file.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            p = row.get("payload", {})
            if row.get("type") == "session_meta":
                meta = p
            if row.get("type") == "turn_context":
                model = p.get("model")
            if row.get("type") == "event_msg" and p.get("type") == "token_count":
                usage = (
                    p.get("info", {}).get("total_token_usage")
                    if p.get("info")
                    else usage
                )
        if meta and meta.get("id") == thread and meta.get("cwd") == str(cwd):
            return {"model": model, "usage": usage, "rollout": str(file)}
    return {}


def validate_result(value, roots):
    if (
        set(value) != set(SCHEMA["required"])
        or value.get("decision") not in SCHEMA["properties"]["decision"]["enum"]
    ):
        raise ValueError("Invalid assessor decision schema")
    if not isinstance(value["evidence_refs"], list) or not isinstance(
        value["missing_evidence"], list
    ):
        raise ValueError("Invalid evidence arrays")
    if not all(
        isinstance(value[k], str) for k in ("explanation", "suggested_report")
    ) or not all(isinstance(v, str) for v in value["missing_evidence"]):
        raise ValueError("Invalid assessor text")
    for ref in value["evidence_refs"]:
        file = Path(ref["artifact"])
        if (
            not file.is_absolute()
            or not file.is_file()
            or not any(file.resolve().is_relative_to(root) for root in roots)
            or not isinstance(ref.get("observation"), str)
            or not ref["observation"].strip()
        ):
            raise ValueError(
                "Evidence reference escaped supplied directories or lacked an observation"
            )
    if value["decision"] != "inconclusive" and (
        not value["evidence_refs"] or not value["explanation"].strip()
    ):
        raise ValueError("Classification lacks checked support")
    return value


def assess(
    state, report, rows, fingerprint, payload, roots, proc_start=subprocess.Popen
):
    cfg = read(state / "config.json")
    settings = cfg.get("assessor", {})
    model = settings.get("model", MODEL)
    effort = settings.get("effort", "medium")
    if len(assessor_running(state)) >= settings.get("capacity", 1):
        return False
    directory = state / "assessments" / (fingerprint + "-" + str(time.time_ns()))
    directory.mkdir(parents=True)
    atomic(directory / "input.json", payload)
    atomic(directory / "schema.json", SCHEMA)
    prompt = (
        (Path(__file__).parent / "prompts/blocker_assessor.xml").read_text()
        + "\n<assessment_input>\n"
        + html.escape(json.dumps(payload))
        + "\n</assessment_input>"
    )
    (directory / "prompt.txt").write_text(prompt)
    argv = [
        str(resolve_codex()),
        "exec",
        "--ignore-user-config",
        "--skip-git-repo-check",
        "--model",
        model,
        "--sandbox",
        "read-only",
        "-c",
        'approval_policy="never"',
        "-c",
        'web_search="disabled"',
        "-c",
        "agents.enabled=false",
        "-c",
        "model_reasoning_effort=" + json.dumps(effort),
        "-C",
        str(directory),
        "--json",
        "--output-schema",
        str(directory / "schema.json"),
        "-o",
        str(directory / "answer.json"),
        "-",
    ]
    proc = None
    try:
        with (
            locked(state / "assessment-launch.lock"),
            (directory / "stdout.jsonl").open("wb") as out,
            (directory / "stderr.log").open("wb") as err,
        ):
            cfg = read(state / "config.json")
            if (
                len(assessor_running(state))
                >= cfg.get("assessor", {}).get("capacity", 1)
                or (state / "assessment-admissions-paused.json").exists()
            ):
                return False
            if evaluate(read(Path(cfg["quota_path"])), assessor_policy(cfg))["reached"]:
                return False
            atomic(
                directory / "launch.json",
                {"intent": True, "at": time.time(), "model": model},
            )
            proc = proc_start(
                argv,
                cwd=directory,
                stdin=subprocess.PIPE,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
            atomic(
                directory / "launch.json",
                {"pid": proc.pid, "at": time.time(), "model": model},
            )
            proc.stdin.write(prompt.encode())
            proc.stdin.close()
        until = time.time() + 180
        last_refresh = time.time()
        while proc.poll() is None:
            if time.time() - last_refresh >= 30:
                refresh_allowance(state, cfg)
                last_refresh = time.time()
            policy = evaluate(
                read(Path(cfg["quota_path"])),
                assessor_policy(read(state / "config.json")),
            )
            if time.time() >= until or (
                policy["reached"] and policy["mode"] == "stop_immediately"
            ):
                from .group import stop_peers

                stop_peers([proc])
                raise ValueError("Assessment stopped by timeout or usage policy")
            time.sleep(0.5)
        if proc.returncode != 0:
            raise ValueError("Assessor did not finish successfully")
        events = [
            json.loads(line)
            for line in (directory / "stdout.jsonl").read_text().splitlines()
        ]
        threads = {
            r["thread_id"]
            for r in events
            if r.get("type") == "thread.started" and r.get("thread_id")
        }
        if len(threads) != 1 or not any(
            r.get("type") == "turn.completed" for r in events
        ):
            raise ValueError("Missing genuine assessor thread/completion")
        thread = next(iter(threads))
        observed = observed_session(thread, directory)
        if observed.get("model") != model:
            raise ValueError("Actual assessor model unverified")
        value = validate_result(read(directory / "answer.json"), roots)
        atomic(
            directory / "decision.json",
            {
                **value,
                "fingerprint": fingerprint,
                "report_id": report["id"],
                "thread_id": thread,
                **observed,
                "at": time.time(),
            },
        )
    except Exception as exc:
        atomic(
            directory / "decision.json",
            {
                "fingerprint": fingerprint,
                "report_id": report["id"],
                "decision": "inconclusive",
                "error": str(exc),
                "at": time.time(),
            },
        )
    finally:
        if proc is not None and proc.poll() is None:
            from .group import stop_peers

            stop_peers([proc])
    return True


def refresh_allowance(state, cfg):
    from .quota import refresh

    try:
        refresh(Path(cfg["quota_path"]), resolve_codex())
    except Exception as exc:
        atomic(
            state / "assessment-allowance-error.json",
            {"at": time.time(), "error": str(exc)},
        )


def serve(state):
    import concurrent.futures

    state = Path(state).resolve()
    last_refresh = 0
    active = {}
    with locked(state / "assessment-owner.lock"):
        while True:
            cfg = read(state / "config.json")
            settings = cfg.get("assessor", {})
            for key, (future, executor) in list(active.items()):
                if future.done():
                    try:
                        future.result()
                    except Exception as exc:
                        atomic(
                            state / "assessment-error.json",
                            {"fingerprint": key, "error": str(exc), "at": time.time()},
                        )
                    executor.shutdown(wait=False)
                    del active[key]
            snapshot = Projection().snapshot(paths(cfg), Path(cfg["quota_path"]))
            reports = annotated(state, snapshot["blockers"], snapshot["rows"])
            reviews = read(state / "reviews.json")
            pending = [
                r
                for r in reports
                if not r["assessment"]
                and reviews.get(r["id"], {}).get("status")
                not in ("observation", "resolved")
            ]
            paused = (
                state / "assessment-admissions-paused.json"
            ).exists() or not settings.get("enabled", True)
            atomic(
                state / "assessment-service.json",
                {
                    "pid": os.getpid(),
                    "version": 2,
                    "pending": bool(pending),
                    "active": len(active),
                    "capacity": settings.get("capacity", 1),
                    "heartbeat": time.time(),
                    "status": "paused"
                    if paused
                    else "assessing"
                    if active
                    else "waiting for allowance"
                    if pending
                    else "idle",
                },
            )
            if pending and not paused and time.time() - last_refresh >= 30:
                refresh_allowance(state, cfg)
                last_refresh = time.time()
            try:
                policy = evaluate(read(Path(cfg["quota_path"])), assessor_policy(cfg))
                allowed = not policy["reached"]
                capacity = int(settings.get("capacity", 1) * policy["fraction"])
            except (ValueError, KeyError, TypeError):
                allowed = False
            if allowed and not paused:
                for report in pending:
                    if len(active) >= capacity:
                        break
                    fingerprint, payload, roots = evidence(report, snapshot["rows"])
                    if fingerprint not in active:
                        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                        future = executor.submit(
                            assess,
                            state,
                            report,
                            snapshot["rows"],
                            fingerprint,
                            payload,
                            roots,
                        )
                        active[fingerprint] = (future, executor)
            time.sleep(1)


def upgrade(state):
    """Replace only the old idle CPU service, never an active assessor model."""
    import select

    state = Path(state)
    with locked(state / "assessment-upgrade.lock"):
        while True:
            with (
                locked(state / "assessment-launch.lock"),
                locked(state / "capacity.lock"),
            ):
                value = read(state / "assessment-service.json")
                if value.get("version") == 2:
                    return
                pid = value.get("pid")
                if not assessor_running(state):
                    if process_alive(pid, state):
                        fd = os.pidfd_open(pid)
                        try:
                            argv = (
                                Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                            )
                            if (
                                b"native_swarm.assessment" not in argv
                                or str(state).encode() not in argv
                                or read(state / "assessment-service.json").get("pid")
                                != pid
                            ):
                                raise RuntimeError(
                                    "Assessor service identity changed; upgrade withheld."
                                )
                            signal.pidfd_send_signal(fd, signal.SIGTERM)
                            if not select.select([fd], [], [], 5)[0]:
                                raise RuntimeError("Old assessor service did not stop.")
                        finally:
                            os.close(fd)
                    break
            time.sleep(1)
        start(state)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if sys.argv[1] == "--upgrade":
        upgrade(sys.argv[2])
    else:
        serve(sys.argv[1])
