"""Explicit operator editing and confirmation dialogs."""

import json
import subprocess
import uuid
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Label,
    Select,
    SelectionList,
    Static,
    TextArea,
)

from .flags import CATEGORIES, draft
from .profiles import BANDS, MODELS, PRIORITIES, default_profile, validate_profiles


class ProfileEditor(ModalScreen):
    CSS = """
    ProfileEditor { align: center middle; }
    #profile-form { width: 72; height: 90%; border: thick $primary; background: $surface; padding: 1 2; }
    #profile-form Input, #profile-form Select { margin-bottom: 1; }
    #profile-error { color: $error; }
    """

    def __init__(self, profile=None, bands=None, used_elsewhere=0, capacity=10):
        super().__init__()
        self.used_elsewhere = used_elsewhere
        self.capacity = capacity
        self.profile = dict(
            profile
            or {
                **default_profile(0, bands),
                "id": uuid.uuid4().hex[:12],
                "name": "New profile",
            }
        )

    def compose(self) -> ComposeResult:
        p = self.profile
        with VerticalScroll(id="profile-form"):
            yield Label("Worker profile · applies to fresh controllers")
            yield Input(p["name"], id="profile-name", placeholder="Name")
            yield Label("Concurrent functions / groups · 0 = no slots assigned")
            yield Input(str(p["instances"]), type="integer", id="profile-instances")
            yield Label("Agents per function · 1 = independent, 2–4 = generalist team")
            yield Select(
                [(str(n), n) for n in range(1, 5)],
                value=p["group_size"],
                allow_blank=False,
                id="profile-group",
            )
            yield Static("", id="profile-allocation", markup=False)
            yield Label("Batch rounds per function · each round permits 1–8 candidates")
            yield Input(str(p.get("rounds", 20)), type="integer", id="profile-rounds")
            yield Checkbox(
                "Extend while improving",
                value=p.get("extend_while_improving", True),
                id="profile-extend",
            )
            yield Label(
                "Improvement window · rounds without a new best before stopping"
            )
            yield Input(
                str(p.get("improvement_window", 3)), type="integer", id="profile-window"
            )
            yield Select(
                [("Shared team budget", "shared"), ("Per-member budget", "per_member")],
                value=p.get("budget_mode", "shared"),
                allow_blank=False,
                id="profile-budget-mode",
            )
            yield Label("Model")
            yield Select(
                [(m, m) for m in MODELS],
                value=p["model"],
                allow_blank=False,
                id="profile-model",
            )
            yield Label("Reasoning")
            yield Select(
                [(x, x) for x in MODELS[p["model"]]["efforts"]],
                value=p["effort"],
                allow_blank=False,
                id="profile-effort",
            )
            yield Label("New function priority")
            yield Select(
                [(x.title(), x) for x in PRIORITIES],
                value=p["priority"],
                allow_blank=False,
                id="profile-priority",
            )
            yield Checkbox(
                "Use operator queue first", value=p["queue_first"], id="profile-queue"
            )
            yield Label(
                "Function size brackets · select one or more; also filters queued work"
            )
            yield SelectionList(
                *[(b, b, b in p.get("bands", BANDS)) for b in BANDS],
                id="profile-bands",
            )
            yield Static("", id="profile-error")
            with Horizontal():
                yield Button("Save profile", id="profile-save", variant="primary")
                yield Button("Cancel", id="profile-cancel")

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == "profile-cancel":
            self.dismiss(None)
        elif event.button.id == "profile-save":
            try:
                p = dict(self.profile)
                p.update(
                    name=self.query_one("#profile-name", Input).value,
                    instances=int(self.query_one("#profile-instances", Input).value),
                    group_size=self.query_one("#profile-group", Select).value,
                    rounds=int(self.query_one("#profile-rounds", Input).value),
                    extend_while_improving=self.query_one(
                        "#profile-extend", Checkbox
                    ).value,
                    improvement_window=int(
                        self.query_one("#profile-window", Input).value
                    ),
                    budget_mode=self.query_one("#profile-budget-mode", Select).value,
                    model=self.query_one("#profile-model", Select).value,
                    effort=self.query_one("#profile-effort", Select).value,
                    priority=self.query_one("#profile-priority", Select).value,
                    queue_first=self.query_one("#profile-queue", Checkbox).value,
                    bands=self.query_one("#profile-bands", SelectionList).selected,
                )
                allocated = validate_profiles([p], self.capacity) + self.used_elsewhere
                if allocated > self.capacity:
                    raise ValueError(
                        f"{allocated}/{self.capacity} slots allocated. Reduce another profile or increase Total worker capacity in Settings; {self.capacity - self.used_elsewhere} slots are available."
                    )
                self.dismiss(p)
            except ValueError as error:
                self.query_one("#profile-error", Static).update(str(error))

    def on_mount(self):
        self.update_allocation()

    def on_input_changed(self, event):
        if event.input.id == "profile-instances":
            self.update_allocation()

    def on_select_changed(self, event):
        if event.select.id == "profile-group":
            self.update_allocation()

    def update_allocation(self):
        if not self.is_mounted:
            return
        try:
            instances = int(self.query_one("#profile-instances", Input).value)
            group = self.query_one("#profile-group", Select).value
            if type(group) is not int or not 0 <= instances <= 75:
                raise ValueError
            slots = instances * group
            total = self.used_elsewhere + slots
            text = f"This profile: {slots} slots · Total: {total}/{self.capacity} allocated · {max(0, self.capacity - total)} free"
            if total > self.capacity:
                text += f" · Over capacity by {total - self.capacity}; reduce an allocation or increase capacity in Settings."
        except ValueError:
            text = "Enter 0–75 concurrent functions per profile to allocate slots."
        self.query_one("#profile-allocation", Static).update(text)


class EvidenceViewer(ModalScreen):
    CSS = """
    EvidenceViewer { align: center middle; }
    #evidence-form { width: 95%; height: 95%; background: $surface; border: thick $primary; }
    #evidence-text { height: 1fr; }
    """

    def __init__(self, text):
        super().__init__()
        self.text = text

    def compose(self):
        with VerticalScroll(id="evidence-form"):
            yield TextArea(self.text, read_only=True, id="evidence-text")
            yield Button("Close", id="evidence-close")

    def on_button_pressed(self, event):
        event.stop()
        self.dismiss()


class FlagEditor(ModalScreen):
    CSS = """
    FlagEditor { align: center middle; }
    #flag-form { width: 90%; height: 90%; background: $surface; border: thick $warning; padding: 1; }
    #flag-note { height: 10; }
    #flag-payload { height: 10; }
    """

    def __init__(self, report):
        super().__init__()
        self.report = report
        self.payload = None

    def compose(self):
        proposal = self.report["proposal"]
        category = proposal.get("category")
        with VerticalScroll(id="flag-form"):
            yield Static(
                f"Report {self.report['address']} · historical address report\nSubmission can quarantine the function. Flag only an established defect that prevents a valid solution under permitted source rules and the assigned environment. Diagnostic quirks, workable naming changes and untested hypotheses are observations, not established blockers.",
                markup=False,
            )
            yield Select(
                [(x, x) for x in CATEGORIES],
                value=category if category in CATEGORIES else "checker_problem",
                allow_blank=False,
                id="flag-category",
            )
            yield TextArea(
                str(
                    proposal.get("note")
                    or proposal.get("summary")
                    or json.dumps(proposal, indent=2)
                ),
                id="flag-note",
            )
            yield Static(
                "Optional compiler-flag report: provide a candidate from this claim and the complete alternate flag set. Proof requires a current owned claim with finished workers.",
                markup=False,
            )
            yield Input(placeholder="Candidate source path", id="flag-source")
            yield Input(
                placeholder="Complete alternate flags, comma-separated", id="flag-flags"
            )
            yield Checkbox(
                "Approve this diagnostic compilation and saving its matching source as an FNV checkpoint",
                id="flag-proof-approve",
            )
            yield Button("Check flags + save proof checkpoint", id="flag-proof")
            yield Button("Review exact payload", id="flag-preview")
            yield TextArea(read_only=True, id="flag-payload")
            yield Checkbox(
                "I reviewed the evidence and approve sending this exact flag to FNV.",
                id="flag-approve",
            )
            yield Checkbox(
                "Checked evidence establishes a blocker; permitted source changes cannot resolve it.",
                id="flag-blocker",
            )
            with Horizontal():
                yield Button("Submit flag", id="flag-submit", variant="warning")
                yield Button("Cancel", id="flag-cancel")
            yield Static("", id="flag-error", markup=False)

    async def on_button_pressed(self, event):
        event.stop()
        action = event.button.id
        if action == "flag-cancel":
            self.dismiss(None)
            return
        try:
            if action == "flag-proof":
                if not self.query_one("#flag-proof-approve", Checkbox).value:
                    raise ValueError(
                        "Approve the diagnostic check and checkpoint first"
                    )
                import asyncio
                from .flag_proof import prepare

                proof = await asyncio.to_thread(
                    prepare,
                    self.report,
                    self.query_one("#flag-source", Input).value,
                    [
                        f.strip()
                        for f in self.query_one("#flag-flags", Input).value.split(",")
                        if f.strip()
                    ],
                )
                self.report["compile_flag_proof"] = proof["path"]
                self.query_one("#flag-category", Select).value = "pack_problem"
                self.payload = None
                self.query_one("#flag-error", Static).update(
                    "Proof MATCH and checkpoint saved. Review the exact flag payload before submitting."
                )
                return
            if self.query_one(
                "#flag-flags", Input
            ).value.strip() and not self.report.get("compile_flag_proof"):
                raise ValueError(
                    "Compile-flag reports need a checked proof checkpoint first"
                )
            if self.report.get("compile_flag_proof"):
                from .flag_proof import verify

                proof = verify(self.report, self.report["compile_flag_proof"])
                flags = [
                    f.strip()
                    for f in self.query_one("#flag-flags", Input).value.split(",")
                    if f.strip()
                ]
                if flags != proof["compile_flags"]:
                    raise ValueError(
                        "Flags changed after proof; prepare a new proof first"
                    )
            current = draft(
                self.report,
                self.query_one("#flag-category", Select).value,
                self.query_one("#flag-note", TextArea).text,
            )
            if action == "flag-preview":
                self.payload = current
                self.query_one("#flag-payload", TextArea).load_text(
                    json.dumps(current, indent=2)
                )
                self.query_one("#flag-approve", Checkbox).value = False
                self.query_one("#flag-blocker", Checkbox).value = False
            elif action == "flag-submit":
                if (
                    current != self.payload
                    or not self.query_one("#flag-approve", Checkbox).value
                    or not self.query_one("#flag-blocker", Checkbox).value
                ):
                    raise ValueError(
                        "Review the current payload, establish a genuine blocker and check both confirmation boxes first."
                    )
                self.dismiss(current)
        except (ValueError, OSError, subprocess.TimeoutExpired) as error:
            self.query_one("#flag-error", Static).update(str(error))
