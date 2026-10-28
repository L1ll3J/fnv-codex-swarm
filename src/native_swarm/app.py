from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
from pathlib import Path
import time

from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    SelectionList,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from .backend import Backend, BANDS, ControlError
from .state import Projection, read
from .operator_store import OperatorStore
from .dialogs import (
    ProfileEditor,
    EvidenceViewer,
    FlagEditor,
    CommandEditor,
    TargetEditor,
    AssessorEditor,
)
from .flags import submit as submit_flag
from .board import Board
from .policy import DEFAULT as DEFAULT_POLICY, MODES


def display(value):
    return "—" if value is None else str(value)


def report_visible(report, review, history=False):
    if history:
        return True
    if review.get("status"):
        return review["status"] not in ("observation", "resolved")
    return report.get("assessment", {}).get("decision") != "observation"


def duration(seconds):
    if seconds is None:
        return "—"
    return f"{int(seconds // 60)}m" if seconds < 3600 else f"{seconds / 3600:.1f}h"


class SwarmApp(App):
    TITLE = "Native Swarm"
    SUB_TITLE = "Operator control · independent checker · persistent sessions"
    BINDINGS = [
        ("ctrl+q", "quit", "Detach"),
        ("r", "refresh", "Refresh"),
        ("space", "mark_worker", "Mark worker"),
    ]
    CSS = """
    Screen { background: $surface; }
    #summary { height: 3; padding: 0 1; background: $boost; }
    #controls { height: 3; padding-left: 1; }
    #controls Button { margin-right: 1; min-width: 15; }
    #message { height: 2; padding: 0 1; color: $accent; }
    TabbedContent { height: 1fr; }
    DataTable { height: 1fr; }
    #details, #report-details { height: 11; border-top: solid $primary; }
    .choices { height: 3; }
    .choices Checkbox { margin-right: 2; }
    #settings-scroll { padding: 1 2; }
    #settings-scroll Input { width: 20; margin-bottom: 1; }
    #settings-scroll SelectionList { height: 7; width: 45; margin-bottom: 1; }
    #help { padding: 1 2; }
    .toolbar { height: 3; }
    .toolbar Button { margin-right: 1; }
    #prompt-editor { height: 1fr; }
    #queue-target { width: 30; }
    #profiles-note { height: 3; }
    #prompt-kind { width: 40; }

    """

    def __init__(self, backend: Backend, *, application_updates=False):
        super().__init__()
        self.application_updates = application_updates
        self.backend = backend
        self.projection = Projection()
        self.data = {
            "rows": [],
            "blockers": [],
            "improvements": [],
            "running": 0,
            "open": 0,
            "verified": 0,
            "pending": 0,
        }
        self.refreshing = False
        self.busy = False
        self.selected_report = None
        self.worker_columns = None
        self.marked_workers = set()
        self.selected_worker = None
        self.selected_history = None
        self.selected_profile = None
        self.selected_queue = None
        self.profile_drafts = self.backend.profiles()

    def compose(self) -> ComposeResult:
        cfg = self.backend.config
        yield Header()
        yield Static("Reading native receipts…", id="summary", markup=False)
        with Horizontal(id="controls"):
            yield Button("Start / Resume", id="start", variant="success")
            yield Button("Pause admissions", id="pause")
            yield Button("Drain safely", id="drain", variant="warning")
            yield Button("Process saved results", id="reconcile")
        yield Static(
            "Attached read-only. Opening or closing this app starts no work.",
            id="message",
            markup=False,
        )
        with TabbedContent(initial="workers-tab"):
            with TabPane("Workers", id="workers-tab"):
                with Horizontal(classes="choices"):
                    yield Checkbox("Completed", value=False, id="completed")
                    yield Checkbox("Timing", value=True, id="timing")
                    yield Checkbox("Checks since best", value=True, id="since-best")
                yield DataTable(id="workers", cursor_type="row", zebra_stripes=True)
                with Horizontal(classes="toolbar"):
                    yield Button(
                        "Worker / profile controls",
                        id="worker-controls",
                        variant="warning",
                    )
                    yield Button("Mark selected", id="mark-worker")
                    yield Button("Rendered prompt", id="rendered-prompt")
                    yield Button("Collaboration board", id="board")
                yield TextArea(read_only=True, id="details")
            with TabPane("Best history", id="history-tab"):
                yield DataTable(id="history", cursor_type="row", zebra_stripes=True)
                with Horizontal(classes="toolbar"):
                    yield Select(
                        [("Any queue-enabled profile", "*")]
                        + [(p["name"], p["id"]) for p in self.profile_drafts],
                        value="*",
                        allow_blank=False,
                        id="queue-target",
                    )
                    yield Button("Queue best source", id="enqueue", variant="primary")
            with TabPane("Blocker reports", id="reports-tab"):
                yield Static(
                    "Unverified agent reports. A flag requires evidence that permitted source changes cannot solve the assignment. Diagnostic quirks and suspicions belong in observations. Review classifications are local and send nothing to FNV.",
                    markup=False,
                )
                with Horizontal(classes="toolbar"):
                    yield Button("Assessor settings", id="assess")
                    yield Checkbox("Show assessment history", id="assessment-history")
                yield Static(
                    "Fresh assessor per report; independent assessment capacity.",
                    id="assessor-status",
                    markup=False,
                )
                yield DataTable(id="reports", cursor_type="row", zebra_stripes=True)
                with Horizontal(classes="toolbar"):
                    yield Button("Save operator review", id="review")
                    yield Select(
                        [
                            ("Observation / quirk", "observation"),
                            ("Suspected blocker", "suspected"),
                            ("Established blocker", "established"),
                            ("Resolved", "resolved"),
                        ],
                        value="observation",
                        allow_blank=False,
                        id="report-assessment",
                    )
                    yield Button("Review / submit flag", id="flag", variant="warning")
                yield TextArea(read_only=True, id="report-details")
            with TabPane("Worker profiles", id="profiles-tab"):
                yield Static(
                    "Total worker capacity is configurable in Settings. Concurrent functions × agents per function = slots.\nSelection edits apply to future admissions; counts apply to future admissions; model, reasoning, team size and prompts apply to fresh runs.",
                    id="profiles-note",
                    markup=False,
                )
                yield DataTable(id="profiles", cursor_type="row", zebra_stripes=True)
                with Horizontal(classes="toolbar"):
                    yield Button("Add profile", id="profile-add")
                    yield Button("Edit profile", id="profile-edit")
                    yield Button("Remove profile", id="profile-remove")
            with TabPane("Queue", id="queue-tab"):
                yield Static(
                    "Checked historical sources are starting evidence, not fresh checks. Active reservations cannot be deleted.",
                    markup=False,
                )
                yield DataTable(id="queue", cursor_type="row", zebra_stripes=True)
                with Horizontal(classes="toolbar"):
                    yield Button(
                        "Run / route function", id="target-add", variant="primary"
                    )
                    yield Button("Remove selected", id="queue-remove")
            with TabPane("Prompts", id="prompts-tab"):
                with Horizontal(classes="toolbar"):
                    yield Select(
                        [
                            ("Independent workers", False),
                            ("Generalist collaboration teams", True),
                        ],
                        value=False,
                        allow_blank=False,
                        id="prompt-kind",
                    )
                    yield Button("Save template", id="prompt-save", variant="primary")
                yield Static(
                    "Edits are versioned locally and apply to future assignments. Rendered prompt shows what each model actually received.",
                    markup=False,
                )
                yield TextArea(self.backend.prompt(), id="prompt-editor")
            with TabPane("Settings", id="settings-tab"):
                with VerticalScroll(id="settings-scroll"):
                    yield Label(
                        "Default concurrent functions (1–75), used when no custom profiles are saved."
                    )
                    yield Input(
                        str(min(75, cfg.get("workers", 10))),
                        type="integer",
                        id="worker-count",
                        disabled=bool(cfg.get("profiles")),
                    )
                    yield Label(
                        "Total worker capacity · allocation ceiling across all profiles"
                    )
                    yield Input(
                        str(cfg.get("worker_capacity", 10)),
                        type="integer",
                        id="worker-capacity",
                    )
                    yield Label(
                        "Default size brackets for new profiles (multiple allowed)"
                    )
                    yield SelectionList(
                        *[(b, b, b in cfg.get("bands", list(BANDS))) for b in BANDS],
                        id="bands",
                    )
                    yield Static(
                        "Model, reasoning, priority and per-profile brackets are under Worker profiles.\nProfile-defined rounds and optional rolling improvement window · 1–8 candidates/round\nUsage policy applies to controllers created by this release. Older/imported controllers retain their policy until drained.\nPreviously attempted functions are eligible. Original checker and submission gates stay in place.",
                        markup=False,
                    )
                    policy = {**DEFAULT_POLICY, **cfg.get("usage_policy", {})}
                    yield Label("Usage cutoff (% used, not remaining)")
                    yield Input(
                        str(policy["cutoff_used_percent"]),
                        type="number",
                        id="usage-cutoff",
                    )
                    yield Label("At the cutoff")
                    yield Select(
                        [(label, mode) for mode, label in MODES.items()],
                        value=policy["mode"],
                        allow_blank=False,
                        id="usage-mode",
                    )
                    yield Label("Taper starts at (% used; used only in Taper mode)")
                    yield Input(
                        str(policy["taper_start_used_percent"]),
                        type="number",
                        id="usage-taper",
                    )
                    yield Static(
                        "Stop immediately signals workers, preserves artifacts and retains ambiguous claims for review. Usage stops remain paused until Start / Resume; other controller stops remain protected.",
                        markup=False,
                    )
                    yield Button("Apply settings", id="apply", variant="primary")
                    from .app_updates import enabled

                    yield Checkbox(
                        "Automatic application updates (activate on next launch)",
                        value=enabled(self.backend.state),
                        id="app-updates",
                    )
                    yield Static("", id="app-update-status", markup=False)
            with TabPane("Help", id="help-tab"):
                yield Static(
                    "Native Swarm\n\nStart / Resume explicitly enables new work. Fresh agents receive checked history without the previous conversation.\n\nPause admissions prevents new claims, worker replacements and capacity retries. Existing workers continue. Resume is available for controllers created by this app.\n\nDrain safely lets existing work finish. Start / Resume can admit replacements under your saved allocation while the old generation drains. Workers finish under their budgets and lease deadlines, then the controller resolves their claims.\n\nCtrl+Q detaches the UI. It does not kill workers or release claims.\n\nImported runs remain under their original controllers. Wait for them to drain before starting a managed run. Process saved results reconciles ended workers through the existing audited resolver.\n\nA claim is not a worker. An open claim with no live process is shown explicitly. MATCH, submitted/pending and server-verified are separate states.\n\nBest history uses recorded checker evidence. Costs shown are API-equivalent worker lower bounds, not subscription consumption.\n\nBlocker reports preserve evidence. Existing deterministic defect validation remains in place. Marking a report reviewed sends nothing to FNV.\n\nMulti-agent profiles use a configurable number of agents per function under one lease, with shared or per-member budgets. Choose collaborative or isolated execution. Use Worker / profile controls for individual, marked, profile or global commands; immediate Stop preserves evidence and releases the claim. Rendered prompts and board entries are available from Workers.",
                    id="help",
                    markup=False,
                )
        yield Footer()

    async def on_mount(self):
        self.query_one("#history", DataTable).add_columns(
            "Function", "At UTC", "Check", "Previous", "New best"
        )
        self.query_one("#reports", DataTable).add_columns(
            "Function",
            "Category",
            "Assessor verdict",
            "Operator review",
            "Validator",
            "FNV flag",
        )
        self.query_one("#profiles", DataTable).add_columns(
            "Name",
            "Functions",
            "Agents/function",
            "Slots",
            "Model",
            "Reasoning",
            "Priority",
            "Brackets",
            "Queue first",
            "Rounds",
            "Budget",
            "Rolling window",
        )
        self.query_one("#queue", DataTable).add_columns(
            "Function", "Bytes", "Historical diff", "Profile", "State"
        )
        if self.application_updates:
            from .app_updates import start

            start(self.backend.state)
            self.set_interval(3600, lambda: start(self.backend.state))
        if self.backend.config.get("assessor", {}).get("enabled"):
            from .assessment import start as start_assessor

            try:
                await asyncio.to_thread(start_assessor, self.backend.state)
            except Exception as exc:
                self.query_one("#message", Static).update(
                    "Assessor service: " + str(exc)
                )
        self.set_interval(2, self.refresh_data)
        await self.refresh_data()

    async def action_refresh(self):
        await self.refresh_data()

    async def refresh_data(self):
        if self.refreshing:
            return
        self.refreshing = True
        try:
            cfg = self.backend.config
            self.data = await asyncio.to_thread(
                self.projection.snapshot, self.backend.runs(), Path(cfg["quota_path"])
            )
            from .assessment import annotated

            self.data["blockers"] = await asyncio.to_thread(
                annotated, self.backend.state, self.data["blockers"], self.data["rows"]
            )
            self.render_data()
        except Exception as exc:
            self.query_one("#message", Static).update(
                f"Refresh failed: {type(exc).__name__}. Existing controllers are unchanged."
            )
        finally:
            self.refreshing = False

    def render_data(self):
        data = self.data
        quota = data.get("quota", {})
        used = max(
            (w.get("used_percent", 0) for w in quota.get("active_windows", [])),
            default=None,
        )
        observed = quota.get("latest_counter", {}).get("observed_at")
        try:
            age = duration(
                time.time()
                - dt.datetime.fromisoformat(observed.replace("Z", "+00:00")).timestamp()
            )
        except (AttributeError, ValueError):
            age = "unknown"
        cost = sum(
            (read(run / "progress.json").get("known_cost_usd", 0) or 0)
            for run in self.backend.runs()
        )
        policy = {**DEFAULT_POLICY, **self.backend.config.get("usage_policy", {})}
        tools = read(self.backend.state / "official-tools-health.json")
        tool_status = (
            (
                "\nTools: "
                + tools.get("state", "unknown")
                + " · "
                + str(tools.get("id", tools.get("last_good_id", "")))[:12]
                + (" · " + tools["error"] if tools.get("error") else "")
            )
            if tools
            else ""
        )
        handover_block = next(
            (
                controller["handover"].get("reason", "Profile handover requires review")
                for controller in reversed(data.get("controllers", []))
                if controller.get("alive")
                and controller.get("handover", {}).get("state")
                in ("blocked", "review_required")
            ),
            None,
        )
        self.query_one("#summary", Static).update(
            f"{data['running']} workers running  ·  {data['open']} unresolved claims  ·  {data['verified']} verified  ·  {data['pending']} pending\nUsage: {display(used)}% used (observed {age} ago) / {policy['cutoff_used_percent']}% cutoff · {MODES.get(policy['mode'], 'invalid policy')} · API-equivalent cost ≥ ${cost:.2f}{tool_status}"
            + (
                "\nController: "
                + (
                    handover_block
                    or data["controllers"][-1]["stop"]
                    or (
                        "running / preparing assignments"
                        if data["controllers"][-1]["alive"]
                        else "stopped"
                    )
                )
                if data.get("controllers")
                else ""
            )
        )
        app_update = read(self.backend.state / "application-update-status.json")
        self.query_one("#app-update-status", Static).update(
            "Application update: "
            + app_update.get("state", "not checked")
            + (" · " + app_update["version"] if app_update.get("version") else "")
        )
        table = self.query_one("#workers", DataTable)
        selected = (
            str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
            if table.row_count and table.cursor_row < table.row_count
            else None
        )
        columns = [
            "Marked",
            "Function",
            "State",
            "Model",
            "Reasoning",
            "Agents",
            "Bytes",
            "Best",
            "Checks",
            "Rounds",
        ]
        if self.query_one("#since-best", Checkbox).value:
            columns.append("Checks since best")
        if self.query_one("#timing", Checkbox).value:
            columns.append("Time since best")
        columns.append("Error / action")
        if self.worker_columns != columns:
            table.clear(columns=True)
            table.add_columns(*columns)
            self.worker_columns = columns
        shown = [
            r
            for r in data["rows"]
            if self.query_one("#completed", Checkbox).value or not r["closed"]
        ]
        worker_rows = []
        for r in shown:
            values = [
                "✓" if r["id"] in self.marked_workers else "",
                r["address"],
                r["state"],
                r["model"],
                r["effort"],
                str(r["agents"]),
                display(r["bytes"]),
                display(r["best"]),
                str(r["checks"]),
                f"{r['rounds']}/{r['limit']}",
            ]
            if "Checks since best" in columns:
                values.append(display(r["checks_since_best"]))
            if "Time since best" in columns:
                values.append(duration(r["seconds_since_best"]))
            values.append(
                r.get("command", {}).get("reason")
                or r.get("command", {}).get("refill_status")
                or r["error"]
                or ("admissions paused" if r["stop"] else "")
            )
            worker_rows.append((r["id"], values))
        sync_rows(table, worker_rows)
        if selected in table.rows:
            table.move_cursor(row=table.get_row_index(selected))
        history = self.query_one("#history", DataTable)
        sync_rows(
            history,
            [
                (
                    history_key(e),
                    [
                        e["address"],
                        str(e["at"] or ""),
                        str(e["check"]),
                        display(e["from"]),
                        str(e["to"]),
                    ],
                )
                for e in data["improvements"][:500]
            ],
        )
        reports = self.query_one("#reports", DataTable)
        reviews = read(self.backend.state / "reviews.json")
        assessment_state = read(self.backend.state / "assessment-service.json")
        self.query_one("#assessor-status", Static).update(
            "Assessor: "
            + assessment_state.get("status", "disabled")
            + f" · active {assessment_state.get('active', 0)}/{self.backend.config.get('assessor', {}).get('capacity', 1)} · output: Assessor verdict column and report details"
        )
        flag_states = {
            r.get("report_id"): r.get("state", "")
            for p in (self.backend.state / "flag-receipts").glob("*.json")
            if (r := read(p))
        }
        sync_rows(
            reports,
            [
                (
                    r["id"],
                    [
                        r["address"],
                        str(r["proposal"].get("category", "unspecified")),
                        r.get("assessment", {}).get("decision", "unassessed"),
                        {
                            "suspected": "Needs evidence / do not flag yet",
                            "established": "Operator considers established",
                            "observation": "Observation / do not flag",
                            "resolved": "Resolved",
                        }.get(reviews.get(r["id"], {}).get("status"), "Not reviewed"),
                        str(r["validation"].get("status", "unvalidated")),
                        flag_states.get(r["id"], ""),
                    ],
                )
                for r in data["blockers"]
                if report_visible(
                    r,
                    reviews.get(r["id"], {}),
                    self.query_one("#assessment-history", Checkbox).value,
                )
            ],
        )
        allocated = sum(p["instances"] * p["group_size"] for p in self.profile_drafts)
        capacity = self.backend.config.get("worker_capacity", 10)
        self.query_one("#profiles-note", Static).update(
            f"Planned slots: {allocated}/{capacity} allocated · {capacity - allocated} free. Change Total worker capacity in Settings. Zero-slot profiles are skipped by Start.\nEdit a profile to assign slots: concurrent functions × agents/function. Count changes apply to future admissions; current workers keep their assignments."
        )
        sync_rows(
            self.query_one("#profiles", DataTable),
            [
                (
                    p["id"],
                    [
                        p["name"],
                        p["instances"],
                        p["group_size"],
                        p["instances"] * p["group_size"],
                        p["model"],
                        p["effort"],
                        p["priority"],
                        ", ".join(p["bands"]),
                        "yes" if p["queue_first"] else "no",
                        str(p.get("rounds", 20)),
                        p.get("budget_mode", "shared"),
                        str(p.get("improvement_window", 3)) + " rounds"
                        if p.get("extend_while_improving", True)
                        else "off",
                    ],
                )
                for p in self.profile_drafts
            ],
        )
        sync_rows(
            self.query_one("#queue", DataTable),
            [
                (
                    q["id"],
                    [
                        q["target"]["address"],
                        q["target"]["bytes"],
                        display(q["target"]["instruction_differences"]),
                        q.get("profile_id") or "any",
                        q["state"],
                    ],
                )
                for q in OperatorStore(self.backend.state).queue()
            ],
        )

    async def on_checkbox_changed(self, event: Checkbox.Changed):
        if event.checkbox.id == "app-updates":
            from .app_updates import set_enabled, start

            set_enabled(self.backend.state, event.value)
            if event.value and self.application_updates:
                start(self.backend.state)
        if self.is_mounted:
            self.render_data()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted):
        key = str(event.row_key.value)
        if event.data_table.id == "history":
            self.selected_history = key
        elif event.data_table.id == "profiles":
            self.selected_profile = key
        elif event.data_table.id == "queue":
            self.selected_queue = key
        elif event.data_table.id == "workers":
            self.selected_worker = key
            row = next((r for r in self.data["rows"] if r["id"] == key), None)
            if row:
                load_changed(
                    self.query_one("#details", TextArea),
                    f"{row['address']} · {row['claim_id']}\n{row['run']}\nLease: {row['lease']}\nWorker PID: {row['pid']} · live: {row['alive']} · peers: {row['group_size']}\nGroup API-equivalent known cost: {row['group_cost']} · unknown peer costs: {row['group_cost_unknown']}\n{row['residual']}\n\nController: {row['stop']}\nArtifacts: {row['id']}\nTool generation: {row.get('official_tools_id') or 'legacy fixed tools'}",
                )
        elif event.data_table.id == "reports":
            self.selected_report = key
            report = dict(
                next((r for r in self.data["blockers"] if r["id"] == key), {})
            )
            report["operator_review"] = read(self.backend.state / "reviews.json").get(
                key, {}
            )
            load_changed(
                self.query_one("#report-details", TextArea),
                json.dumps(report, indent=2),
            )

    async def on_button_pressed(self, event: Button.Pressed):
        if self.busy:
            return
        actions = {
            "start": self.backend.start,
            "pause": self.backend.pause,
            "drain": self.backend.drain,
            "reconcile": self.backend.reconcile,
        }
        self.busy = True
        try:
            action = event.button.id
            if action == "mark-worker":
                self.action_mark_worker()
                return
            if action == "worker-controls":
                self.push_screen(
                    CommandEditor(self.profile_drafts),
                    lambda result: (
                        self.run_worker(self.apply_command(result)) if result else None
                    ),
                )
                return
            if action == "target-add":
                self.push_screen(
                    TargetEditor(self.profile_drafts),
                    lambda result: (
                        self.run_worker(self.add_target(result)) if result else None
                    ),
                )
                return
            if action == "assess":
                self.push_screen(
                    AssessorEditor(
                        {
                            "usage_policy": self.backend.config.get(
                                "usage_policy", DEFAULT_POLICY
                            ),
                            **self.backend.config.get("assessor", {}),
                        }
                    ),
                    lambda result: (
                        self.run_worker(self.save_assessor(result)) if result else None
                    ),
                )
                return
            if action in ("profile-add", "profile-edit"):
                profile = next(
                    (
                        p
                        for p in self.profile_drafts
                        if p["id"] == self.selected_profile
                    ),
                    None,
                )
                if action == "profile-edit" and profile is None:
                    raise ValueError("Select a profile first.")
                self.push_screen(
                    ProfileEditor(
                        profile if action == "profile-edit" else None,
                        self.backend.config.get("bands", list(BANDS)),
                        used_elsewhere=sum(
                            p["instances"] * p["group_size"]
                            for p in self.profile_drafts
                            if action == "profile-add" or p["id"] != profile["id"]
                        ),
                        capacity=self.backend.config.get("worker_capacity", 10),
                    ),
                    self.save_profile,
                )
                return
            elif action == "profile-remove":
                profiles = [
                    p for p in self.profile_drafts if p["id"] != self.selected_profile
                ]
                message = self.backend.save_profiles(
                    profiles, expected=self.profile_drafts
                )
                self.profile_drafts = profiles
                target = self.query_one("#queue-target", Select)
                target.set_options(
                    [("Any queue-enabled profile", "*")]
                    + [(p["name"], p["id"]) for p in profiles]
                )
                target.value = "*"
            elif action == "prompt-save":
                version = OperatorStore(self.backend.state).save_prompt(
                    self.query_one("#prompt-editor", TextArea).text,
                    self.query_one("#prompt-kind", Select).value,
                )
                message = (
                    "Template saved: "
                    + version[:12]
                    + ". Current sessions are unchanged."
                )
            elif action == "enqueue":
                item = next(
                    (
                        e
                        for e in self.data["improvements"]
                        if history_key(e) == self.selected_history
                    ),
                    None,
                )
                if item is None:
                    raise ValueError("Select a best-history row first.")
                target = self.query_one("#queue-target", Select).value
                if target != "*" and target not in {
                    p["id"] for p in self.profile_drafts
                }:
                    raise ValueError("Choose an existing profile for this queue item.")
                OperatorStore(self.backend.state).enqueue(
                    item["claim_path"], None if target == "*" else target
                )
                message = "Best checked source queued. A queue-enabled profile will acquire it on a future admission."
            elif action == "queue-remove":
                OperatorStore(self.backend.state).remove(self.selected_queue)
                message = "Queue item removed."
            elif action in ("rendered-prompt", "board"):
                if not self.selected_worker:
                    raise ValueError("Select a worker first.")
                claim = Path(self.selected_worker)
                if action == "board":
                    board_path = claim / "collaboration/board.sqlite3"
                    text = (
                        json.dumps(Board(board_path).entries(latest=True), indent=2)
                        if board_path.exists()
                        else "This is an independent worker; it has no shared board."
                    )
                else:
                    paths = list(
                        (claim / "collaboration").glob("peer-*/cli-prompt.txt")
                    )
                    if not paths:
                        paths = [claim / "work/cli-prompt.txt"]
                    text = (
                        "\n\n".join(
                            str(p) + "\n" + p.read_text() for p in paths if p.is_file()
                        )
                        or "No model prompt has been launched yet."
                    )
                self.push_screen(EvidenceViewer(text))
                return
            elif action == "flag":
                report = next(
                    (
                        r
                        for r in self.data["blockers"]
                        if r["id"] == self.selected_report
                    ),
                    None,
                )
                if report is None:
                    raise ValueError("Select a blocker report first.")
                self.push_screen(
                    FlagEditor(report),
                    lambda payload: (
                        self.run_worker(self.send_flag(report, payload))
                        if payload
                        else None
                    ),
                )
                return
            elif action == "apply":
                count = int(self.query_one("#worker-count", Input).value)
                bands = self.query_one("#bands", SelectionList).selected
                policy = {
                    "cutoff_used_percent": float(
                        self.query_one("#usage-cutoff", Input).value
                    ),
                    "mode": self.query_one("#usage-mode", Select).value,
                    "taper_start_used_percent": float(
                        self.query_one("#usage-taper", Input).value
                    ),
                }
                capacity = int(self.query_one("#worker-capacity", Input).value)
                message = await asyncio.to_thread(
                    self.backend.settings, count, bands, policy, capacity
                )
                if not self.backend.config.get("profiles"):
                    self.profile_drafts = self.backend.profiles()
            elif event.button.id == "review":
                message = (
                    await asyncio.to_thread(
                        self.backend.review,
                        self.selected_report,
                        self.query_one("#report-assessment", Select).value,
                    )
                    if self.selected_report
                    else "Select a blocker report first."
                )
            else:
                self.query_one("#message", Static).update(
                    "Checking controller readiness…"
                )
                message = await asyncio.to_thread(actions[event.button.id])
            self.query_one("#message", Static).update(message)
        except Exception as exc:
            self.query_one("#message", Static).update(str(exc))
        finally:
            self.busy = False
            await self.refresh_data()

    def on_select_changed(self, event: Select.Changed):
        if event.select.id == "prompt-kind":
            self.query_one("#prompt-editor", TextArea).load_text(
                self.backend.prompt(event.value)
            )

    def save_profile(self, profile):
        if profile is None:
            return
        profiles = [p for p in self.profile_drafts if p["id"] != profile["id"]] + [
            profile
        ]
        try:
            message = self.backend.save_profiles(profiles, expected=self.profile_drafts)
            self.profile_drafts = profiles
            target = self.query_one("#queue-target", Select)
            target.set_options(
                [("Any queue-enabled profile", "*")]
                + [(p["name"], p["id"]) for p in profiles]
            )
            target.value = "*"
            self.render_data()
        except (ValueError, ControlError, OSError) as exc:
            message = str(exc)
        self.query_one("#message", Static).update(message)

    def action_mark_worker(self):
        if self.selected_worker:
            if self.selected_worker in self.marked_workers:
                self.marked_workers.remove(self.selected_worker)
            else:
                self.marked_workers.add(self.selected_worker)
            self.render_data()

    async def apply_command(self, value):
        try:
            scope, action = value.pop("scope"), value.pop("action")
            rows = [r for r in self.data["rows"] if action == "acknowledge" or not r["closed"]]
            if scope == "selected":
                rows = [r for r in rows if r["id"] == self.selected_worker]
            elif scope == "marked":
                rows = [r for r in rows if r["id"] in self.marked_workers]
            elif scope != "all":
                rows = [
                    r
                    for r in rows
                    if read(
                        Path(r["id"]).parent.parent / "native-app-profile.json"
                    ).get("id")
                    == scope
                ]
            if action in ("pause_profile", "resume_profile"):
                profiles = (
                    [p["id"] for p in self.profile_drafts]
                    if scope == "all"
                    else [scope]
                    if scope not in ("selected", "marked")
                    else list(
                        {
                            read(
                                Path(r["id"]).parent.parent / "native-app-profile.json"
                            ).get("id")
                            for r in rows
                        }
                    )
                )
                if not profiles:
                    raise ValueError("Select a worker or profile.")
                messages = [
                    await asyncio.to_thread(
                        self.backend.profile_pause, p, action == "pause_profile"
                    )
                    for p in profiles
                ]
                message = " · ".join(messages)
            elif action == "acknowledge":
                if scope in ("selected", "marked"):
                    runs = {str(Path(r["id"]).parent.parent) for r in rows}
                else:
                    runs = {
                        value for value in self.backend.config.get("managed_runs", [])
                        if scope == "all"
                        or read(Path(value) / "native-app-profile.json").get("id") == scope
                    }
                if not runs:
                    raise ValueError(
                        "Select a managed worker, profile or controller scope to acknowledge."
                    )
                message = " · ".join(
                    [
                        await asyncio.to_thread(self.backend.acknowledge_hold, r)
                        for r in runs
                    ]
                )
            elif action == "retry":
                from .commands import retry

                message = (
                    " · ".join(
                        [
                            await asyncio.to_thread(retry, self.backend.state, r["id"])
                            for r in rows
                        ]
                    )
                    or "Select an open claim."
                )
            else:
                if scope == "all":
                    await asyncio.to_thread(self.backend.drain)
                    if not value["replace"]:
                        await asyncio.to_thread(self.backend.pause)
                message = (
                    await asyncio.to_thread(
                        self.backend.worker_command,
                        [r["id"] for r in rows],
                        action,
                        **value,
                    )
                    if rows
                    else "No open workers selected. Existing preparations are draining."
                )
            self.query_one("#message", Static).update(message)
        except Exception as exc:
            self.query_one("#message", Static).update(str(exc))
        await self.refresh_data()

    async def add_target(self, value):
        try:
            from .routing import enqueue_target

            await asyncio.to_thread(enqueue_target, self.backend.state, **value)
            self.query_one("#message", Static).update(
                "Target queued with the selected route. Start / Resume enables admissions."
            )
        except Exception as exc:
            self.query_one("#message", Static).update(str(exc))
        await self.refresh_data()

    async def save_assessor(self, value):
        try:
            message = await asyncio.to_thread(self.backend.assessor_settings, value)
            self.query_one("#message", Static).update(message)
        except Exception as exc:
            self.query_one("#message", Static).update(str(exc))
        await self.refresh_data()

    async def send_flag(self, report, payload):
        try:
            with self.backend.locked():
                path = await asyncio.to_thread(
                    submit_flag, self.backend.state, report, payload
                )
            self.query_one("#message", Static).update(f"FNV receipt saved: {path}")
        except Exception as error:
            self.query_one("#message", Static).update(str(error))


def history_key(event):
    return event["claim_id"] + ":" + str(event["check"])


def load_changed(area, text):
    if area.text != text:
        area.load_text(text)


def sync_rows(table, rows):
    """Update stable keys in place so refresh never resets the operator's cursor."""
    wanted = {key for key, _ in rows}
    for row_key in list(table.rows):
        if row_key.value not in wanted:
            table.remove_row(row_key)
    for key, values in rows:
        if key not in table.rows:
            table.add_row(*values, key=key)
        else:
            for column, value in zip(table.columns, values):
                if table.get_cell(key, column) != value:
                    table.update_cell(key, column, value, update_width=True)


def main():
    parser = argparse.ArgumentParser(
        description="Native Swarm terminal application. Opening it starts no workers."
    )
    parser.add_argument(
        "--state-dir", type=Path, default=Path.home() / ".local/share/fnv-native-swarm"
    )
    args = parser.parse_args()
    from .app_updates import activate

    activate(args.state_dir)
    SwarmApp(Backend(args.state_dir), application_updates=True).run()


if __name__ == "__main__":
    main()
