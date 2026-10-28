from textual.widgets import Button, Checkbox
import asyncio
from unittest.mock import Mock
from native_swarm.app import SwarmApp
from native_swarm.backend import Backend
from native_swarm.state import atomic
from textual.widgets import Input, TabbedContent


def test_open_refresh_edit_and_detach_never_start_workers(tmp_path, monkeypatch):
    async def scenario():
        atomic(
            tmp_path / "config.json",
            {
                "observed_runs": [],
                "workers": 10,
                "bands": ["<=255"],
                "quota_path": str(tmp_path / "quota"),
            },
        )
        backend = Backend(tmp_path)
        start = Mock(side_effect=AssertionError("Opening UI must not start work"))
        monkeypatch.setattr(backend, "start", start)
        app = SwarmApp(backend)
        async with app.run_test(size=(150, 45)) as pilot:
            await pilot.pause()
            app.query_one(TabbedContent).active = "settings-tab"
            await pilot.pause()
            field = app.query_one("#worker-count", Input)
            field.value = "7"
            await app.refresh_data()
            assert field.value == "7"
            app.query_one("#apply").focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert backend.config["workers"] == 7
            await pilot.press("ctrl+q")
        start.assert_not_called()

    asyncio.run(asyncio.wait_for(scenario(), timeout=20))


def test_reports_keep_selection_and_templates_keep_unsaved_edits(tmp_path):
    from textual.widgets import DataTable, TextArea

    async def scenario():
        atomic(
            tmp_path / "config.json",
            {
                "observed_runs": [],
                "workers": 10,
                "bands": ["<=255"],
                "quota_path": str(tmp_path / "quota"),
            },
        )
        app = SwarmApp(Backend(tmp_path))
        async with app.run_test(size=(170, 55)) as pilot:
            await pilot.pause()
            app.data["blockers"] = [
                {
                    "id": str(i),
                    "address": hex(i),
                    "proposal": {"category": "checker_problem"},
                    "validation": {},
                    "evidence_path": str(tmp_path),
                }
                for i in range(3)
            ]
            app.query_one(TabbedContent).active = "reports-tab"
            app.render_data()
            await pilot.pause()
            reports = app.query_one("#reports", DataTable)
            reports.move_cursor(row=2)
            await pilot.pause()
            assert app.selected_report == "2"
            app.render_data()
            await pilot.pause()
            assert reports.cursor_row == 2 and app.selected_report == "2"
            app.query_one(TabbedContent).active = "prompts-tab"
            await pilot.pause()
            editor = app.query_one("#prompt-editor", TextArea)
            editor.load_text("operator draft")
            app.render_data()
            await pilot.pause()
            assert editor.text == "operator draft"
            await pilot.press("ctrl+q")

    asyncio.run(asyncio.wait_for(scenario(), timeout=20))


def test_profile_form_changes_model_and_group_size_without_launch(tmp_path):
    from textual.widgets import Select, DataTable

    async def scenario():
        atomic(
            tmp_path / "config.json",
            {
                "observed_runs": [],
                "workers": 10,
                "bands": ["<=255"],
                "quota_path": str(tmp_path / "quota"),
            },
        )
        app = SwarmApp(Backend(tmp_path))
        async with app.run_test(size=(170, 60)) as pilot:
            await pilot.pause()
            app.query_one(TabbedContent).active = "profiles-tab"
            await pilot.pause()
            app.query_one("#profiles", DataTable).move_cursor(row=0)
            await pilot.pause()
            await pilot.click("#profile-edit")
            await pilot.pause()
            app.screen.query_one("#profile-instances", Input).value = "2"
            app.screen.query_one("#profile-group", Select).value = 3
            app.screen.query_one("#profile-model", Select).value = "gpt-6.1-terra"
            app.screen.query_one("#profile-effort", Select).value = "high"
            app.screen.query_one("#profile-save", Button).focus()
            await pilot.press("enter")
            await pilot.pause()
            saved = app.backend.config["profiles"][0]
            assert (
                saved["model"] == "gpt-6.1-terra"
                and saved["instances"] == 2
                and saved["group_size"] == 3
            )
            await pilot.press("ctrl+q")

    asyncio.run(asyncio.wait_for(scenario(), timeout=20))


def test_worker_selection_survives_projection_reordering(tmp_path, monkeypatch):
    from textual.widgets import DataTable

    async def scenario():
        atomic(
            tmp_path / "config.json",
            {
                "observed_runs": [],
                "workers": 10,
                "bands": ["<=255"],
                "quota_path": str(tmp_path / "quota"),
            },
        )
        app = SwarmApp(Backend(tmp_path))
        async with app.run_test(size=(170, 55)) as pilot:
            await pilot.pause()
            rows = []
            for name in ("A", "B"):
                rows.append(
                    {
                        "id": name,
                        "claim_id": name,
                        "address": name,
                        "closed": False,
                        "state": "working",
                        "model": "gpt-6.1-sol",
                        "effort": "medium",
                        "agents": 1,
                        "bytes": 100,
                        "best": 5,
                        "checks": 1,
                        "rounds": 1,
                        "limit": 20,
                        "checks_since_best": 0,
                        "seconds_since_best": None,
                        "error": "",
                        "stop": "",
                        "run": "test",
                        "lease": None,
                        "pid": None,
                        "alive": True,
                        "residual": "",
                        "group_size": 1,
                        "group_cost": None,
                        "group_cost_unknown": 0,
                    }
                )
            app.data["rows"] = rows
            monkeypatch.setattr(app.projection, "snapshot", lambda *args: app.data)
            app.render_data()
            await pilot.pause()
            table = app.query_one("#workers", DataTable)
            table.move_cursor(row=0)
            await pilot.pause()
            assert app.selected_worker == "A"
            app.data["rows"] = [rows[1], rows[0]]
            app.render_data()
            await pilot.pause()
            assert app.selected_worker == "A"
            assert (
                table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
                == "A"
            )
            await pilot.press("ctrl+q")

    asyncio.run(asyncio.wait_for(scenario(), timeout=20))


def test_profile_lock_conflict_is_shown_without_crashing(tmp_path):
    from textual.widgets import Static

    async def scenario():
        atomic(
            tmp_path / "config.json",
            {
                "observed_runs": [],
                "workers": 10,
                "bands": ["<=255"],
                "quota_path": str(tmp_path / "quota"),
            },
        )
        backend = Backend(tmp_path)
        app = SwarmApp(backend)
        async with app.run_test(size=(170, 55)) as pilot:
            await pilot.pause()
            with backend.locked():
                app.save_profile({**app.profile_drafts[0], "name": "Edited profile"})
            assert "Another app" in str(app.query_one("#message", Static).render())
            assert app.profile_drafts[0]["name"] == "Default"
            await pilot.press("ctrl+q")

    asyncio.run(asyncio.wait_for(scenario(), timeout=20))


def test_profile_brackets_and_usage_drafts_survive_refresh(tmp_path):
    from textual.widgets import Select, SelectionList
    from native_swarm.dialogs import ProfileEditor

    async def scenario():
        atomic(
            tmp_path / "config.json",
            {
                "observed_runs": [],
                "workers": 2,
                "bands": ["<=255"],
                "quota_path": str(tmp_path / "quota"),
            },
        )
        backend = Backend(tmp_path)
        app = SwarmApp(backend)
        async with app.run_test(size=(170, 55)) as pilot:
            await pilot.pause()
            app.push_screen(ProfileEditor(backend.profiles()[0]), app.save_profile)
            await pilot.pause()
            assert app.screen.query_one("#profile-extend", Checkbox).value is True
            app.screen.query_one("#profile-extend", Checkbox).value = False
            app.screen.query_one("#profile-window", Input).value = "5"
            await app.refresh_data()
            assert app.screen.query_one("#profile-window", Input).value == "5"
            bands = app.screen.query_one("#profile-bands", SelectionList)
            bands.deselect_all()
            bands.select("256-511")
            bands.select("1K-4K")
            app.screen.query_one("#profile-save").scroll_visible(animate=False)
            await pilot.pause()
            app.screen.query_one("#profile-save", Button).focus()
            await pilot.press("enter")
            await pilot.pause()
            assert backend.profiles()[0]["bands"] == ["256-511", "1K-4K"]
            assert backend.profiles()[0]["extend_while_improving"] is False
            assert backend.profiles()[0]["improvement_window"] == 5
            app.query_one(TabbedContent).active = "settings-tab"
            app.query_one("#usage-cutoff", Input).value = "75"
            app.query_one("#usage-taper", Input).value = "50"
            app.query_one("#usage-mode", Select).value = "taper"
            await app.refresh_data()
            assert app.query_one("#usage-cutoff", Input).value == "75"
            app.query_one("#apply").scroll_visible(animate=False)
            await pilot.pause()
            await pilot.click("#apply")
            await pilot.pause()
            assert backend.config["usage_policy"]["mode"] == "taper"
            assert backend.config["usage_policy"]["cutoff_used_percent"] == 75
            await pilot.press("ctrl+q")

    asyncio.run(asyncio.wait_for(scenario(), timeout=20))


def test_create_zero_slot_profile_at_full_capacity_and_edit_allocation(tmp_path):
    from textual.widgets import Static, DataTable

    async def scenario():
        atomic(
            tmp_path / "config.json",
            {
                "observed_runs": [],
                "workers": 10,
                "bands": ["<=255"],
                "quota_path": str(tmp_path / "quota"),
            },
        )
        backend = Backend(tmp_path)
        app = SwarmApp(backend)
        async with app.run_test(size=(170, 60)) as pilot:
            await pilot.pause()
            await pilot.click("#--content-tab-profiles-tab")
            await pilot.pause()
            await pilot.click("#profile-add")
            await pilot.pause()
            assert app.screen.query_one("#profile-instances", Input).value == "0"
            assert "0 free" in str(
                app.screen.query_one("#profile-allocation", Static).render()
            )
            app.screen.query_one("#profile-save").scroll_visible(animate=False)
            await pilot.pause()
            app.screen.query_one("#profile-save", Button).focus()
            await pilot.press("enter")
            await pilot.pause()
            assert len(backend.profiles()) == 2
            assert backend.profiles()[1]["instances"] == 0
            assert "10/10" in str(app.query_one("#profiles-note", Static).render())
            await pilot.click("#--content-tab-settings-tab")
            await pilot.pause()
            app.query_one("#worker-capacity", Input).value = "30"
            app.query_one("#apply").scroll_visible(animate=False)
            await pilot.pause()
            await pilot.click("#apply")
            await pilot.pause()
            assert backend.config["worker_capacity"] == 30
            await pilot.click("#--content-tab-profiles-tab")
            table = app.query_one("#profiles", DataTable)
            table.move_cursor(row=table.get_row_index(backend.profiles()[1]["id"]))
            await pilot.pause()
            await pilot.click("#profile-edit")
            await pilot.pause()
            app.screen.query_one("#profile-instances", Input).value = "20"
            await pilot.pause()
            assert "30/30" in str(
                app.screen.query_one("#profile-allocation", Static).render()
            )
            app.screen.query_one("#profile-save").scroll_visible(animate=False)
            await pilot.pause()
            app.screen.query_one("#profile-save", Button).focus()
            await pilot.press("enter")
            await pilot.pause()
            assert sum(p["instances"] for p in backend.profiles()) == 30
            await pilot.press("ctrl+q")

    asyncio.run(asyncio.wait_for(scenario(), timeout=25))


def test_default_settings_refresh_fallback_allocation_before_adding_profile(tmp_path):
    from textual.widgets import Static

    async def scenario():
        atomic(
            tmp_path / "config.json",
            {
                "workers": 10,
                "observed_runs": [],
                "bands": ["<=255"],
                "quota_path": str(tmp_path / "quota"),
            },
        )
        backend = Backend(tmp_path)
        app = SwarmApp(backend)
        async with app.run_test(size=(170, 60)) as pilot:
            await pilot.pause()
            await pilot.click("#--content-tab-settings-tab")
            await pilot.pause()
            app.query_one("#worker-count", Input).value = "5"
            app.query_one("#worker-capacity", Input).value = "5"
            app.query_one("#apply").focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert app.profile_drafts == backend.profiles()
            assert "5/5 allocated" in str(
                app.query_one("#profiles-note", Static).render()
            )
            await pilot.click("#--content-tab-profiles-tab")
            await pilot.pause()
            await pilot.click("#profile-add")
            await pilot.pause()
            app.screen.query_one("#profile-save").focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert len(backend.profiles()) == 2
            assert backend.config["workers"] == 5
            await pilot.press("ctrl+q")

    asyncio.run(asyncio.wait_for(scenario(), timeout=20))


def test_command_center_forms_expose_scopes_routing_and_separate_assessor(tmp_path, monkeypatch):
    from native_swarm.dialogs import CommandEditor, TargetEditor, AssessorEditor
    from textual.widgets import Select
    async def scenario():
        atomic(tmp_path / 'config.json', {'workers': 1, 'bands': ['4K+'], 'quota_path': str(tmp_path / 'quota'), 'observed_runs': []})
        backend = Backend(tmp_path)
        app = SwarmApp(backend)
        async with app.run_test(size=(170, 60)) as pilot:
            await pilot.pause()
            await pilot.click('#worker-controls')
            await pilot.pause()
            assert isinstance(app.screen, CommandEditor)
            app.screen.query_one('#command-scope', Select).value = 'all'
            app.screen.query_one('#command-action', Select).value = 'stop_now'
            app.screen.query_one('#command-cancel', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            app.query_one(TabbedContent).active = 'queue-tab'
            await pilot.pause()
            app.query_one('#target-add', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            assert isinstance(app.screen, TargetEditor)
            app.screen.query_one('#target-address', Input).value = '0xea3d60'
            app.screen.query_one('#target-size', Input).value = '14124'
            app.screen.query_one('#target-profile', Select).value = 'default'
            app.screen.query_one('#target-route', Select).value = 'exclusive'
            app.screen.query_one('#target-save', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            from native_swarm.state import read
            assert read(tmp_path / 'routes.json')['0x00ea3d60']['mode'] == 'exclusive'
            assert read(tmp_path / 'queue.json', [])[0]['immediate'] is True
            app.query_one(TabbedContent).active = 'reports-tab'
            await pilot.pause()
            app.query_one('#assess', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            assert isinstance(app.screen, AssessorEditor)
            app.screen.query_one('#assessor-capacity', Input).value = '3'
            app.screen.query_one('#assessor-cutoff', Input).value = '90'
            # Disabled settings must not launch a model or reconstruction worker.
            app.screen.query_one('#assessor-save', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            assert backend.config['assessor']['capacity'] == 3
            assert backend.config['assessor']['enabled'] is False
            await pilot.press('ctrl+q')
    asyncio.run(asyncio.wait_for(scenario(), timeout=25))


def test_acknowledge_profile_and_global_scope_without_open_claim(tmp_path, monkeypatch):
    async def scenario():
        run = tmp_path / "run"
        atomic(run / "native-app-profile.json", {"id": "default"})
        state = tmp_path / "state"
        atomic(state / "config.json", {"workers": 1, "bands": ["4K+"], "managed_runs": [str(run)], "observed_runs": [], "quota_path": str(state / "quota")})
        backend = Backend(state)
        acknowledge = Mock(return_value="Controller acknowledged")
        monkeypatch.setattr(backend, "acknowledge_hold", acknowledge)
        app = SwarmApp(backend)
        async with app.run_test(size=(150, 45)) as pilot:
            await pilot.pause()
            app.data["rows"] = []
            await app.apply_command({"scope": "default", "action": "acknowledge"})
            acknowledge.assert_called_once_with(str(run))
            acknowledge.reset_mock()
            await app.apply_command({"scope": "all", "action": "acknowledge"})
            acknowledge.assert_called_once_with(str(run))
            acknowledge.reset_mock()
            claim = run / "claims/closed"
            app.data["rows"] = [{"id": str(claim), "closed": True}]
            app.selected_worker = str(claim)
            await app.apply_command({"scope": "selected", "action": "acknowledge"})
            acknowledge.assert_called_once_with(str(run))
            await pilot.press("ctrl+q")
    asyncio.run(asyncio.wait_for(scenario(), timeout=20))
