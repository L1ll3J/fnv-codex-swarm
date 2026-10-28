"""Explicit operator controls over detached, deterministic native controllers."""

from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time

from .state import atomic, process_alive, read
from .profiles import BANDS, default_profile, validate_profiles, validate_capacity
from .operator_store import OperatorStore
from .runtime import configure
from .policy import validate as validate_policy, used_percent
from .executables import resolve_codex, executable as check_executable


class ControlError(RuntimeError):
    pass


class Backend:
    def __init__(self, state_dir: Path):
        self.state = state_dir

    @property
    def config(self):
        return read(self.state / "config.json")

    def runs(self):
        cfg = self.config
        return [
            Path(p)
            for p in dict.fromkeys(
                cfg.get("observed_runs", []) + cfg.get("managed_runs", [])
            )
        ]

    @property
    def current(self):
        value = self.config.get("current_run")
        return Path(value) if value else None

    @contextmanager
    def locked(self):
        self.state.mkdir(parents=True, exist_ok=True)
        with (self.state / "control.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ControlError(
                    "Another app is changing controller settings."
                ) from exc
            yield

    def settings(
        self, workers: int, bands: list[str], usage_policy=None, capacity=None
    ):
        if type(workers) is not int or not 0 <= workers <= 75:
            raise ControlError(
                "Choose between 1 and 75 default workers (native controller limit)."
            )
        if not bands or any(b not in BANDS for b in bands):
            raise ControlError("Select at least one size bracket.")
        with self.locked():
            cfg = self.config
            capacity = validate_capacity(
                cfg.get("worker_capacity", 10) if capacity is None else capacity
            )
            if workers == 0 and not cfg.get("profiles"):
                raise ControlError("Choose between 1 and 75 default workers.")
            if not cfg.get("profiles") and workers > capacity:
                raise ControlError(
                    "Default workers exceed Total worker capacity. Increase capacity or reduce the default target."
                )
            policy = validate_policy(usage_policy or cfg.get("usage_policy"))
            cfg.update(
                workers=validate_profiles(cfg["profiles"], capacity)
                if cfg.get("profiles")
                else workers,
                bands=bands,
                usage_policy=policy,
                worker_capacity=capacity,
            )
            atomic(self.state / "config.json", cfg)
            for current in self.runs():
                if (current / "native-app-managed.json").exists():
                    control = read(current / "operator-control.json")
                    if not cfg.get("profiles"):
                        control.update(native_app_bands=bands)
                    # Profile allocation is immutable for an active run. A
                    # legacy default count must never resize a collaboration.
                    profile = read(current / "native-app-profile.json")
                    if not cfg.get("profiles") and not profile:
                        control["target_pool"] = workers
                    atomic(current / "operator-control.json", control)
        return "Settings saved. Running workers keep their current assignments."

    def profiles(self):
        cfg = self.config
        bands = cfg.get("bands", list(BANDS))
        return [
            dict(p, bands=list(p.get("bands", bands)))
            for p in (
                cfg.get("profiles") or [default_profile(cfg.get("workers", 10), bands)]
            )
        ]

    def save_profiles(self, profiles, *, expected=None):
        known = {p["id"] for p in profiles}
        from .group_check import locked
        from .allocation import sync

        with (
            self.locked(),
            OperatorStore(self.state).locked(),
            locked(self.state / "allocation.lock"),
            locked(self.state / "capacity.lock"),
        ):
            total = validate_profiles(profiles, self.config.get("worker_capacity", 10))
            if expected is not None and self.profiles() != expected:
                raise ControlError(
                    "Profiles changed in another app. Reopen the app to load the latest profiles before editing."
                )
            if any(
                q.get("profile_id")
                and q["profile_id"] not in known
                and q["state"] in ("queued", "reserved", "assigned", "review_required")
                for q in OperatorStore(self.state).queue()
            ):
                raise ControlError(
                    "This profile still has queued or unresolved work. Clear queued entries or finish its active reservation first."
                )
            cfg = self.config
            cfg.update(profiles=profiles, workers=total)
            atomic(self.state / "config.json", cfg)
            by_id = {profile["id"]: profile for profile in profiles}
            for run in self.runs():
                active = read(run / "native-app-profile.json")
                selected = by_id.get(active.get("id"))
                if (run / "native-app-managed.json").exists():
                    sync(run, cfg)
                if selected:
                    control = read(run / "operator-control.json")
                    control["native_app_selection"] = {
                        key: selected.get(key, 20 if key == "rounds" else "shared")
                        for key in (
                            "priority",
                            "queue_first",
                            "bands",
                            "rounds",
                            "budget_mode",
                        )
                    }
                    control["native_app_bands"] = selected["bands"]
                    atomic(run / "operator-control.json", control)
        return "Allocation and selection saved for future admissions. Current tasks finish normally. Model, reasoning and team-size changes require fresh controllers."

    def prompt(self, group=False):
        default = (
            Path(__file__).parent
            / "prompts"
            / ("generalists.xml" if group else "independent.xml")
        )
        return OperatorStore(self.state).prompt(default, group)

    def pause(self):
        with self.locked():
            atomic(self.state / "assessment-admissions-paused.json", {"at": self.now()})
            for run in self.runs():
                if (run / "native-app-managed.json").exists():
                    atomic(
                        run / "operator-admissions-paused.json",
                        {"requested_at": self.now()},
                    )
                elif run.exists():
                    try:
                        with (run / "STOP_NEW_CLAIMS").open("x") as f:
                            f.write(
                                "OPERATOR_PAUSE: Finish in-flight work; no new or replacement workers.\n"
                            )
                    except FileExistsError:
                        pass
        return "Admissions paused. Current workers continue."

    def drain(self):
        with self.locked():
            atomic(self.state / "assessment-admissions-paused.json", {"at": self.now()})
            for run in self.runs():
                if (run / "native-app-managed.json").exists():
                    atomic(run / "operator-drain.json", {"requested_at": self.now()})
                elif run.exists():
                    try:
                        with (run / "STOP_NEW_CLAIMS").open("x") as f:
                            f.write(
                                "OPERATOR_DRAIN: Finish current work and release through the existing controller.\n"
                            )
                    except FileExistsError:
                        pass
        return "Draining: no new work; existing workers finish normally."

    def review(self, proposal_id: str, status: str, *, note=None, evidence_refs=None):
        if status not in (
            "reviewed",
            "open",
            "observation",
            "suspected",
            "established",
            "resolved",
        ):
            raise ControlError("Unknown review status.")
        with self.locked():
            reviews = read(self.state / "reviews.json")
            reviews[proposal_id] = {
                **reviews.get(proposal_id, {}),
                "status": status,
                "at": self.now(),
                "source": "operator",
            }
            if note is not None:
                reviews[proposal_id]["note"] = note
            if evidence_refs is not None:
                reviews[proposal_id]["evidence_refs"] = evidence_refs
            atomic(self.state / "reviews.json", reviews)
        return "Review status saved locally. No FNV flag or message was sent."

    def reconcile(self):
        processed = 0
        with self.locked():
            for run in self.runs():
                claims = [
                    p.parent
                    for p in (run / "claims").glob("*/claim.json")
                    if not any(
                        (p.parent / n).exists()
                        for n in ("giveup-response.json", "submission-response.json")
                    )
                ]
                if not claims:
                    continue
                journal = read(run / "rolling-executor/journal.json")
                if not self.groups_terminal(run):
                    continue
                if any(
                    process_alive(j.get("pid"), run)
                    for j in journal.get("jobs", {}).values()
                ):
                    continue
                with (run / "campaign-watch.lock").open("a") as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    for name in ("usage.py", "register_usage.py", "resolve_ready.py"):
                        proc = subprocess.run(
                            [sys.executable, str(run / name)],
                            cwd=run,
                            capture_output=True,
                            text=True,
                            timeout=300,
                        )
                        if proc.returncode:
                            raise ControlError(
                                f"Saved-result processing stopped in {name}. Original artifacts and stops remain; inspect {run}."
                            )
                    processed += 1
        return f"Processed {processed} idle run(s) through the audited resolver. Live workers and controllers were left alone."

    @staticmethod
    def groups_terminal(run):
        from .group import group_terminal

        return all(
            group_terminal(path.parent.parent)
            for path in (run / "claims").glob("*/collaboration/group.json")
        )

    @staticmethod
    def now():
        return dt.datetime.now(dt.timezone.utc).isoformat()

    def start(self):
        with self.locked():
            cfg = self.config
            if not cfg:
                raise ControlError("App setup is missing.")
            active = []
            for path in cfg.get("managed_runs", []):
                run = Path(path)
                owner = read(run / ".rolling-executor-owner.json")
                if process_alive(owner.get("pid"), run):
                    if (run / "STOP_NEW_CLAIMS").exists() or (
                        run / "operator-drain.json"
                    ).exists():
                        stop = run / "STOP_NEW_CLAIMS"
                        reason = (
                            stop.read_text(errors="replace").strip()[:1200]
                            if stop.exists()
                            else read(run / "operator-drain.json").get("reason")
                            or "Operator requested a safe drain"
                        )
                        raise ControlError(
                            f"Controller {run.name} is stopped: {reason}. "
                            "Its stop is preserved; resolve it before resuming."
                        )
                    active.append(run)
            if active:
                self.quota(
                    cfg, read(active[0] / "native-app-codex.json").get("executable")
                )
                from .allocation import sync, selected_profile

                profiles = self.profiles()
                validate_profiles(profiles, cfg.get("worker_capacity", 10))
                profiles = [p for p in profiles if p["instances"] > 0]
                by_id = {p["id"]: p for p in profiles}
                represented = set()
                for run in active:
                    running = read(run / "native-app-profile.json")
                    wanted = selected_profile(run, cfg)
                    if running.get("id") in by_id and wanted is None:
                        raise ControlError(
                            f"Profile {running['id']} has an active controller with different "
                            "model, reasoning or team size. Its current tasks must finish "
                            "and its controller must stop before starting the replacement."
                        )
                    if wanted and wanted["instances"] > 0:
                        if wanted["id"] in represented:
                            raise ControlError(
                                f"Multiple active controllers own profile {wanted['id']}; "
                                "resolve their ownership before resuming."
                            )
                        represented.add(wanted["id"])
                missing = [p for p in profiles if p["id"] not in represented]
                if missing:
                    # Never replace a controller that lost ownership while its
                    # model processes (including collaboration peers) remain live.
                    for run in self.runs():
                        if run in active:
                            continue
                        journal = read(run / "rolling-executor/journal.json")
                        if not self.groups_terminal(run) or any(
                            process_alive(job.get("pid"), run)
                            for job in journal.get("jobs", {}).values()
                        ):
                            raise ControlError(
                                "Workers without an active controller are still draining. "
                                "Resolve their ownership before starting another profile."
                            )
                    template = Path(cfg["template_run"])
                    self.verify_template(template, cfg)
                account = self.preflight(active[0], require_idle=False)
                created = []
                try:
                    for profile in missing:
                        self.create_run(cfg, template, account, profile)
                        created.append(self.current)
                except Exception:
                    for run in created:
                        atomic(
                            run / "operator-admissions-paused.json",
                            {"requested_at": self.now(), "reason": "profile start incomplete"},
                        )
                    raise
                for run in active:
                    sync(run, cfg)
                    wanted = selected_profile(run, cfg)
                    if not wanted or wanted["instances"] == 0:
                        continue
                    (run / "operator-admissions-paused.json").unlink(missing_ok=True)
                    (run / "operator-usage-paused.json").unlink(missing_ok=True)
                (self.state / "assessment-admissions-paused.json").unlink(
                    missing_ok=True
                )
                return (
                    f"Admissions resumed; started {len(created)} new profile controller(s). "
                    "Existing workers keep running under the saved allocations and total capacity."
                    if created
                    else "Admissions resumed for active profiles. Current saved allocations govern future admissions."
                )
            # A fresh controller never tries to reattach live model processes.
            for run in self.runs():
                owner = read(run / ".rolling-executor-owner.json")
                journal = read(run / "rolling-executor/journal.json")
                if (
                    not self.groups_terminal(run)
                    or process_alive(owner.get("pid"), run)
                    or any(
                        process_alive(j.get("pid"), run)
                        for j in journal.get("jobs", {}).values()
                    )
                ):
                    raise ControlError(
                        "Existing workers/controllers are still draining. Start becomes available after they finish; attaching does not restart them."
                    )
            profiles = self.profiles()
            validate_profiles(profiles, cfg.get("worker_capacity", 10))
            profiles = [p for p in profiles if p["instances"] > 0]
            if not profiles:
                raise ControlError(
                    "No slots allocated. Edit a worker profile and assign at least one concurrent function before Start."
                )
            self.quota(cfg)
            template = Path(cfg["template_run"])
            self.verify_template(template, cfg)
            account = self.preflight(template, require_idle=True)
            created = []
            try:
                for profile in profiles:
                    self.create_run(cfg, template, account, profile)
                    created.append(self.current)
            except Exception:
                for run in created:
                    atomic(
                        run / "operator-admissions-paused.json",
                        {
                            "requested_at": self.now(),
                            "reason": "profile start incomplete",
                        },
                    )
                raise
            (self.state / "assessment-admissions-paused.json").unlink(missing_ok=True)
            return "Native swarm started. Closing this app detaches; all profile controllers keep running."

    def quota(self, cfg, executable=None):
        from .quota import refresh

        try:
            self.codex_executable = (
                check_executable(executable) if executable else resolve_codex()
            )
            q = refresh(Path(cfg["quota_path"]), self.codex_executable)
        except Exception as exc:
            raise ControlError(
                f"Cannot read live Codex allowance: {exc}. No claim was made."
            ) from exc
        try:
            cutoff = validate_policy(cfg.get("usage_policy"))["cutoff_used_percent"]
            if used_percent(q) >= cutoff:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ControlError(
                f"A fresh allowance observation below {validate_policy(cfg.get('usage_policy'))['cutoff_used_percent']}% used is required. No claim was made."
            ) from None

    @staticmethod
    def verify_template(template, cfg):
        for name, expected in cfg["template_sha256"].items():
            try:
                actual = hashlib.sha256((template / name).read_bytes()).hexdigest()
            except OSError:
                actual = None
            if actual != expected:
                raise ControlError(
                    f"Controller template changed: {name}. No claim was made; inspect the template before updating its manifest."
                )
        ledger = template / "report-style-trial/ledger.json"
        if hashlib.sha256(ledger.read_bytes()).hexdigest() != cfg[
            "template_ledger_sha256"
        ] or not read(ledger).get("admission_closed"):
            raise ControlError(
                "The historical prompt experiment is not pinned closed. No claim was made."
            )

    @staticmethod
    def preflight(template, require_idle):
        code = "from service import call,guard; import json; s=call('status',{}); guard(s,admission=True); print(json.dumps(s))"
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=template,
            capture_output=True,
            text=True,
            timeout=90,
        )
        if proc.returncode:
            raise ControlError(
                "FNV readiness check failed. Inspect account access, health and controller logs; no new claim was made."
            )
        try:
            account = json.loads(proc.stdout)
        except ValueError as exc:
            raise ControlError("FNV readiness response could not be read.") from exc
        if require_idle and any(
            c.get("this_key") for c in account.get("open_claims", [])
        ):
            raise ControlError(
                "FNV still lists owned claims. Wait for completion/release before starting a fresh controller."
            )
        return account

    def create_run(self, cfg, template, account, profile=None):
        profile = profile or default_profile(cfg.get("workers", 10))
        if profile["instances"] == 0:
            raise ControlError(
                "This profile has no allocated slots; no controller was created."
            )
        codex = getattr(self, "codex_executable", None) or resolve_codex()
        root = Path(cfg["runs_root"]) / (
            "native-app-"
            + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
            + "-"
            + secrets.token_hex(3)
        )
        root.mkdir(parents=True)
        # Mutable accounting and ownership snapshots are copied as defaults, not
        # pinned like executable code. Normal status reads legitimately update them.
        names = dict.fromkeys(
            [
                *cfg["template_sha256"],
                "campaign-counts.json",
                "operator-control.json",
                "rolling-admission.json",
            ]
        )
        for name in names:
            source = template / name
            if source.is_file():
                shutil.copy2(source, root / name)
        for name in (
            "claims",
            "assignments",
            "rolling-executor",
            "dispatch",
            "report-style-trial",
        ):
            (root / name).mkdir(exist_ok=True)
        shutil.copy2(
            template / "report-style-trial/ledger.json",
            root / "report-style-trial/ledger.json",
        )
        (root / "submission-ledger.jsonl").touch()
        for name in ("account-initial.json", "account-current.json"):
            atomic(root / name, account)
        control = read(root / "operator-control.json")
        control.update(
            target_pool=profile["instances"],
            native_app_bands=profile.get("bands", cfg.get("bands", list(BANDS))),
            native_app_profile=profile,
            allow_already_attempted=True,
        )
        atomic(root / "operator-control.json", control)
        atomic(
            root / "native-app-managed.json",
            {
                "state_dir": str(self.state),
                "at": self.now(),
                "usage_policy_version": 1,
                "supervision_version": 2,
                "allocation_version": 1,
                "toolchain_version": 1,
            },
        )
        atomic(root / "native-app-quota.json", {"path": cfg["quota_path"]})
        atomic(root / "native-app-profile.json", profile)
        atomic(root / "native-app-codex.json", {"executable": str(codex)})
        if (root / "prepare_assignment.py").exists():
            configure(root, profile)
        prompt_default = (
            Path(__file__).parent
            / "prompts"
            / ("generalists.xml" if profile["group_size"] > 1 else "independent.xml")
        )
        # Packaged defaults include only individually approved prompt changes.
        (root / "worker-contract.xml").write_text(
            OperatorStore(self.state).prompt(
                prompt_default, group=profile["group_size"] > 1
            )
        )
        stop = "Explicit operator Start from native app; ownership handover before admissions.\n"
        (root / "STOP_NEW_CLAIMS").write_text(stop)
        auth = read(root / "rolling-admission.json")
        auth.update(
            nonce=secrets.token_hex(24),
            handover_stop_sha256=hashlib.sha256(stop.encode()).hexdigest(),
        )
        atomic(root / "rolling-admission.json", auth)
        atomic(
            root / "rolling-handover-ready.json",
            {
                "state": "ready",
                "dispatcher_terminal": True,
                "worker_processes_terminal": True,
                "unresolved_claim_count": 0,
                "new_acquisition_subprocesses": 0,
            },
        )
        shutil.copy2(
            Path(__file__).with_name("controller.py"), root / "app_controller.py"
        )
        # Pin accounting preflight before creating any external reservation.
        for name in (
            "usage.py",
            "register_usage.py",
            "usage.py",
            "accounting.py",
            "audit.py",
            "progress_update.py",
        ):
            result = subprocess.run(
                [sys.executable, str(root / name)],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=90,
            )
            if result.returncode:
                raise ControlError(
                    f"Controller preparation failed in {name}. STOP remains; no worker was launched."
                )
        with (root / "scheduler.log").open("ab") as log:
            scheduler = subprocess.Popen(
                [sys.executable, "-u", str(root / "app_controller.py")],
                cwd=root,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.wait(
            lambda: (
                read(root / ".rolling-executor-owner.json").get("pid") == scheduler.pid
            ),
            "Controller ownership was not established; STOP remains.",
        )
        with (root / "campaign-watch.log").open("ab") as log:
            watcher = subprocess.Popen(
                [sys.executable, "-u", str(root / "campaign_watch.py")],
                cwd=root,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.wait(
            lambda: (
                self.lock_held(root / "campaign-watch.lock") and watcher.poll() is None
            ),
            "Completion controller is not ready; STOP remains.",
        )
        if (
            scheduler.poll() is not None
            or hashlib.sha256((root / "STOP_NEW_CLAIMS").read_bytes()).hexdigest()
            != auth["handover_stop_sha256"]
        ):
            raise ControlError("Controller handover changed; stop retained for review.")
        cfg["managed_runs"] = cfg.get("managed_runs", []) + [str(root)]
        cfg["current_run"] = str(root)
        atomic(self.state / "config.json", cfg)
        atomic(
            root / "launch.json",
            {
                "run": str(root),
                "scheduler_pid": scheduler.pid,
                "completion_watch_pid": watcher.pid,
                "at": self.now(),
                "started_by": "native TUI operator",
            },
        )
        (root / "STOP_NEW_CLAIMS").unlink()
        return "Native swarm started. Closing this app detaches; the controller keeps running."

    @staticmethod
    def lock_held(path):
        with path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False

    @staticmethod
    def wait(predicate, message):
        until = time.time() + 15
        while time.time() < until:
            if predicate():
                return
            time.sleep(0.1)
        raise ControlError(message)
