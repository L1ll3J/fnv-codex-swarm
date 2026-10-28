"""Copied into an operator-created run; ordinary native scheduler owns model work."""

from pathlib import Path
import os
import subprocess
import sys
import shutil

from random_executor import RandomExecutor
from rolling_executor import read_json
import rolling_executor as base


class ManagedExecutor(RandomExecutor):
    def usage_decision(self):
        from native_swarm.policy import decision

        try:
            value = decision(self.root, self.clock())
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if value["reached"]:
            marker = self.root / "operator-usage-paused.json"
            if not marker.exists():
                base.atomic_json(marker, value)
            if value["mode"] == "stop_immediately":
                for worker, runtime in list(self.active.items()):
                    job = self.journal["jobs"][worker]
                    if not job.get("usage_stop_requested"):
                        job["usage_stop_requested"] = True
                        runtime["failure"] = (
                            "Operator usage policy stopped worker; artifacts retained"
                        )
                        self.journal_save()
                        try:
                            runtime["proc"].terminate()
                        except ProcessLookupError:
                            pass  # Native polling will collect an already exited child.
        return value

    def resume_prepared(self):
        # Run this even with a full pool: immediate-stop policy must not depend
        # on a free slot or a new acquisition.
        self.usage_decision()
        return super().resume_prepared()

    def try_launch_prepared(self, worker):
        from native_swarm.drain import release_unlaunched

        if (self.root / "operator-drain.json").exists():
            release_unlaunched(self, worker)
            return False
        state_dir = read_json(self.root / "native-app-managed.json", {}).get(
            "state_dir"
        )
        if state_dir:
            from native_swarm.allocation import selected_profile
            from native_swarm.group_check import locked

            state = Path(state_dir)
            # Order closure against both profile edits/claim dispatch and launch.
            # Only this controller writes its journal or closes its own claim.
            with locked(state / "allocation.lock"), locked(state / "capacity.lock"):
                wanted = selected_profile(
                    self.root, read_json(state / "config.json", {})
                )
                withdrawn = wanted is None or wanted.get("instances", 0) == 0
                pause = read_json(self.root / "operator-admissions-paused.json", {})
                if withdrawn and pause.get("source") == "profile-allocation":
                    if not (self.root / "STOP_NEW_CLAIMS").exists():
                        release_unlaunched(
                            self,
                            worker,
                            reason="Profile allocation changed before worker launch; local assignment preserved.",
                        )
                    return False
        return super().try_launch_prepared(worker)

    def prepare_assignment(self, worker, band, token, log_path):
        from native_swarm.operator_store import OperatorStore
        from native_swarm.selection import ordered_functions
        from service import call

        from native_swarm.official_tools import current as current_tools

        current_tools(self.root)
        profile = read_json(self.root / "native-app-profile.json", {})
        profile.update(
            read_json(self.root / "operator-control.json", {}).get(
                "native_app_selection", {}
            )
        )
        store = OperatorStore(
            read_json(self.root / "native-app-managed.json")["state_dir"]
        )
        item = store.reserve(
            profile["id"],
            worker,
            queue_enabled=profile["queue_first"],
            eligible=lambda target: base.band(target["bytes"]) in self.configured_mix(),
        )
        extra = []
        if item and item["target"].get("source_path"):
            directory = self.root / "selected-admissions"
            directory.mkdir(exist_ok=True)
            targets = read_json(self.root / "selected-targets.json", {"targets": []})
            if item["target"] not in targets["targets"]:
                targets["targets"].append(item["target"])
            base.atomic_json(self.root / "selected-targets.json", targets)
            reservation = directory / (worker + ".json")
            base.atomic_json(
                reservation,
                {"worker": worker, "state": "reserved", "target": item["target"]},
            )
            extra = ["--selected-row", str(reservation)]
        elif item:
            extra = ["--address", item["target"]["address"]]
        elif profile["priority"] != "random" or any(
            r.get("mode") == "exclusive"
            for r in read_json(store.state / "routes.json", {}).values()
        ):
            rows = ordered_functions(
                call, list(self.configured_mix()), profile["priority"], base.band
            )
            own = {
                str(read_json(Path(c["path"]) / "claim.json")["address"])
                for c in base.live_claims(self.root).values()
            }
            from native_swarm.routing import allowed

            if profile["priority"] == "random":
                import random

                random.SystemRandom().shuffle(rows)
            row = next(
                (
                    r
                    for r in rows
                    if str(r["address"]) not in own
                    and allowed(store.state, str(r["address"]), profile["id"])
                ),
                None,
            )
            if row is None:
                log_path.write_text(
                    "BOUNDED_CONTINUATION_UNAVAILABLE_NO_FALLBACK: catalog has no eligible target\n"
                )
                raise RuntimeError("No eligible target")
            call("function_info", {"address": row["address"]})
            extra = ["--address", str(row["address"])]
        command = [
            sys.executable,
            str(self.root / "prepare_assignment.py"),
            worker,
            band,
            "--fresh",
            "--prefer",
            "any",
            "--model",
            base.MODEL,
            "--recorder",
            "--scheduler-token",
            token,
            *extra,
        ]
        with log_path.open("ab") as log:
            result = subprocess.run(
                command, cwd=self.root, stdout=log, stderr=subprocess.STDOUT
            )
        if result.returncode:
            text = log_path.read_text(errors="replace")
            definite = "CONTINUATION_ADDRESS_UNAVAILABLE" in text
            withdrawn = "PROFILE_ALLOCATION_WITHDRAWN_BEFORE_CLAIM" in text
            if item:
                store.update(
                    item["id"],
                    (
                        "queued"
                        if withdrawn
                        else "unavailable"
                        if definite
                        else "review_required"
                    ),
                    log=str(log_path),
                )
            if definite and extra:
                with log_path.open("a") as f:
                    f.write("\nBOUNDED_CONTINUATION_UNAVAILABLE_NO_FALLBACK\n")
            raise RuntimeError("Preparation failed; original evidence retained")
        if item:
            assignment = read_json(self.root / "assignments" / (worker + ".json"))
            store.update(
                item["id"],
                "assigned",
                claim_id=assignment["claim_id"],
                run=str(self.root),
            )

    def launch(self, worker, claim_dir, assignment):
        from native_swarm.group_check import locked
        from native_swarm.assessment import occupied

        state = Path(read_json(self.root / "native-app-managed.json")["state_dir"])
        with locked(state / "capacity.lock"):
            cfg = read_json(state / "config.json")
            value = self.usage_decision()
            if (
                value is None
                or len(self.active) >= value["target"]
                or (self.root / "operator-admissions-paused.json").exists()
            ):
                self.journal["jobs"][worker]["state"] = (
                    "prepared_waiting_transient_gate"
                )
                self.journal_save()
                return False
            if occupied(cfg, state) + read_json(
                self.root / "native-app-profile.json"
            ).get("group_size", 1) > cfg.get("worker_capacity", 10):
                job = self.journal["jobs"][worker]
                job["state"] = "prepared_waiting_transient_gate"
                self.journal_save()
                return False
            return self.launch_supervised(worker, claim_dir, assignment)

    def launch_supervised(self, worker, claim_dir, assignment):
        profile = read_json(self.root / "native-app-profile.json", {})
        if profile.get("group_size", 1) == 1:
            session = self.root / "claims" / claim_dir / "session"
            if session.exists():
                job = self.journal["jobs"][worker]
                state = read_json(session / "state.json", {})
                history = (
                    session.parent
                    / "capacity-start-history"
                    / str(job.get("previous_worker"))
                )
                if not (
                    1 <= job.get("capacity_retry_number", 0) <= 10
                    and state.get("capacity_retryable")
                    and state.get("streams_closed")
                    and history.is_dir()
                    and not list(
                        (session.parent / "work/recorded-checks").glob("check-*.json")
                    )
                ):
                    raise RuntimeError(
                        "Existing session cannot be replayed; preserve for review"
                    )
                shutil.move(str(session), str(history / "session"))
            original = self.popen
            self.popen = lambda argv, **kwargs: original(
                [
                    sys.executable,
                    "-m",
                    "native_swarm.session",
                    str(self.root),
                    claim_dir,
                ],
                **kwargs,
            )
            try:
                return super().launch(worker, claim_dir, assignment)
            finally:
                self.popen = original
        directory = self.root / "claims" / claim_dir / "collaboration"
        if directory.exists():
            from native_swarm.group import group_terminal

            job = self.journal["jobs"][worker]
            state = read_json(directory / "group.json", {})
            history = (
                self.root
                / "claims"
                / claim_dir
                / "capacity-start-history"
                / str(job.get("previous_worker"))
            )
            if (
                not 1 <= job.get("capacity_retry_number", 0) <= 10
                or not state.get("capacity_retryable")
                or not group_terminal(directory.parent)
                or list(
                    (directory.parent / "work/recorded-checks").glob("check-*.json")
                )
                or not history.is_dir()
            ):
                raise RuntimeError(
                    "Existing group launch cannot be replayed; preserve for review"
                )
            shutil.move(str(directory), str(history / "collaboration"))
        original = self.popen

        def launch_group(argv, **kwargs):
            return original(
                [sys.executable, "-m", "native_swarm.group", str(self.root), claim_dir],
                **kwargs,
            )

        self.popen = launch_group
        try:
            return super().launch(worker, claim_dir, assignment)
        finally:
            self.popen = original

    def prepared_gate(self):
        kind, reason, evidence = super().prepared_gate()
        if (self.root / "operator-drain.json").exists():
            return "stop", "Operator requested a safe drain", evidence
        if kind == "ready" and (self.root / "operator-admissions-paused.json").exists():
            return "wait", "Operator paused admissions", evidence
        if kind == "ready":
            from native_swarm.policy import gate

            blocked = gate(self.root, self.clock())
            value = self.usage_decision()
            if blocked or value is None or len(self.active) >= value["target"]:
                return (
                    "wait",
                    blocked or "Usage taper retains prepared assignment",
                    evidence,
                )
        return kind, reason, evidence

    def configured_mix(self):
        cfg = read_json(self.root / "operator-control.json", {})
        bands = cfg.get(
            "native_app_bands", ["<=255", "256-511", "512-1K", "1K-4K", "4K+"]
        )
        bands = cfg.get("native_app_selection", {}).get("bands", bands)
        allowed = {"<=255", "256-511", "512-1K", "1K-4K", "4K+"}
        if not bands or any(b not in allowed for b in bands):
            raise RuntimeError("Operator size selection is invalid")
        return {b: 1 for b in bands}

    def maybe_acquire(self, target):
        if (self.root / "operator-drain.json").exists():
            self.stop("Operator requested a safe drain; no new or replacement workers")
            if not self.active and not base.live_claims(self.root):
                raise SystemExit(0)
            return False
        if (self.root / "operator-admissions-paused.json").exists():
            return False
        value = self.usage_decision()
        if (
            value is None
            or value["target"] <= 0
            or (self.root / "operator-usage-paused.json").exists()
        ):
            return False
        target = value["target"]
        if len(self.active) >= target:
            return False
        return super().maybe_acquire(target)


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    base.MODEL = read_json(root / "run-config.json")["model"]
    base.RESERVE = Path(read_json(root / "native-app-quota.json")["path"])
    from native_swarm.policy import gate

    base.quota_state = lambda now=None: gate(root, now)
    base.quota_blocked = lambda: bool(base.quota_state())
    with (root / "allowance-client.log").open("ab") as log:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "native_swarm.quota",
                str(base.RESERVE),
                str(os.getpid()),
                str(read_json(root / "native-app-codex.json")["executable"]),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    ManagedExecutor(root).loop()
