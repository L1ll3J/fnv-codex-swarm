"""Build a fresh runtime without mutating an installed or running controller."""

from pathlib import Path

from .profiles import MODELS
from .state import atomic, read


def replace_once(path, old, new):
    text = path.read_text()
    if text.count(old) != 1:
        raise ValueError(
            f"Unsupported controller template in {path.name}; no launch permitted."
        )
    path.write_text(text.replace(old, new))


def configure(root: Path, profile: dict):
    acquire = root / "acquire.py"
    if acquire.exists():
        replace_once(
            acquire,
            "from service import ROOT,CONFIG,call,guard,save,download_pack",
            "from service import ROOT,CONFIG,call,guard,save,download_pack\nfrom native_swarm.allocation import claim_request",
        )
        replace_once(
            acquire,
            "assert len(local_active)<min(CONFIG['max_workers'],CONFIG['authorized_worker_ceiling']), 'Local active-worker reservation cap'",
            "# Live local reservation cap is checked atomically by claim_request below.",
        )
        replace_once(
            acquire,
            "try:c=call('claim',claim_args,scheduler_token=scheduler_token)",
            "try:c=claim_request(ROOT,lambda:call('claim',claim_args,scheduler_token=scheduler_token),address=claim_args.get('address'))",
        )
        replace_once(
            acquire,
            " c=call('claim',{'band':band,'prefer':prefer},scheduler_token=scheduler_token)",
            " c=claim_request(ROOT,lambda:call('claim',{'band':band,'prefer':prefer},scheduler_token=scheduler_token))",
        )
        replace_once(
            root / "rolling_executor.py",
            '                if "BOUNDED_CONTINUATION_UNAVAILABLE_NO_FALLBACK" in preparation_log:',
            '                if "PROFILE_ALLOCATION_WITHDRAWN_BEFORE_CLAIM" in preparation_log:\n                    job["state"] = "allocation_withdrawn_no_launch"\n                elif "BOUNDED_CONTINUATION_UNAVAILABLE_NO_FALLBACK" in preparation_log:',
        )
    release = root / "release.py"
    if release.exists():
        replace_once(
            release,
            "from expiry_cleanup import read_json",
            "from expiry_cleanup import read_json\nfrom native_swarm.reasons import one_line_reason",
        )
        replace_once(
            release,
            "args={'claim_id':cid,'reason':reason[:900]}",
            "args={'claim_id':cid,'reason':one_line_reason(reason)}",
        )
        replace_once(
            release,
            " args['reason']=reason[:900]",
            " args['reason']=one_line_reason(reason)",
        )
    batch = root / "recorded_batch.py"
    if batch.exists():
        replace_once(
            batch,
            "                assert record.exists(),'Checker did not produce a receipt; stop and preserve evidence'",
            "                if not record.exists():\n                    from native_swarm.duplicates import duplicate_result\n                    reused=duplicate_result(assignment,result.stdout,digest,result.returncode)\n                    if reused is not None:\n                        batch['results'].append(dict(reused,candidate_index=index+1));batch.pop('pending',None);save(path,batch);continue\n                assert record.exists(),'Checker did not produce a receipt; stop and preserve evidence'",
        )
    if batch.exists():
        from .rolling_runtime import configure_batch

        configure_batch(batch)
    replace_once(
        root / "resolve_ready.py",
        " for p in sorted((ROOT/'claims').glob('*')):",
        " for p in sorted((ROOT/'claims').glob('*')):\n  from native_swarm.session import resolution_ready\n  if not resolution_ready(p):continue",
    )
    if (root / "ingest.py").exists():
        replace_once(
            root / "ingest.py",
            "def _ingest_claim(p,threads):",
            "def _ingest_claim(p,threads):\n from native_swarm.session import resolution_ready\n if not resolution_ready(p):return False",
        )
    replace_once(
        root / "rolling_executor.py",
        'argv = ["codex", "exec",',
        'argv = [str(read_json(self.root / "native-app-codex.json")["executable"]), "exec",',
    )
    fresh = root / "native_fresh_restart.py"
    if fresh.exists():
        replace_once(
            fresh,
            "        usage=base.read_json(base.RESERVE,{}) or {}\n        used=max((w.get('used_percent',100) for w in usage.get('active_windows',[])),default=100)\n        if used>=50:\n            self.stop('Usage admission cutoff 50% used; let in-flight workers finish without taper')\n",
            "        # ManagedExecutor applies the configurable usage policy before admission.\n",
        )
    config = read(root / "run-config.json")
    config.update(
        model=profile["model"],
        reasoning_effort=profile["effort"],
        max_workers=profile["instances"],
    )
    config["pricing_by_model"] = {
        model: dict(
            zip(("input", "cached_input", "cache_write", "output"), data["rates"])
        )
        for model, data in MODELS.items()
    }
    atomic(root / "run-config.json", config)
    watcher = root / "campaign_watch.py"
    if watcher.exists():
        from .status_poll import configure as configure_status_poll

        configure_status_poll(root)
        replace_once(
            watcher,
            "reserve_file=ROOT.parent.parent/'research/usage-reserve/status.json'",
            "reserve_file=pathlib.Path(json.loads((ROOT/'native-app-quota.json').read_text())['path'])",
        )
        replace_once(
            watcher,
            "  if reserve_file.exists() and json.loads(reserve_file.read_text()).get('reserve_reached') and not (ROOT/'STOP_NEW_CLAIMS').exists():\n   stop_admission('Native allowance50%used admission cutoff; finish in-flight work')",
            "  # App-managed usage policy is enforced by app_controller; native account protection below stays active.",
        )
    helper = root / "prepare_assignment.py"
    replace_once(
        helper,
        "'max_plain_checks': 160",
        "'max_plain_checks': __import__('native_swarm.budgets',fromlist=['for_run']).for_run(ROOT)['max_plain_checks']",
    )
    replace_once(
        helper,
        "assignment['max_batch_rounds']=20",
        "from native_swarm.budgets import for_run\nassignment.update(for_run(ROOT))",
    )
    replace_once(
        helper,
        "assert model in ('gpt-6-luna','gpt-6.1-sol')",
        "assert model==json.loads((ROOT/'run-config.json').read_text())['model']",
    )
    replace_once(
        helper,
        "'reasoning_effort':'medium'",
        "'reasoning_effort':json.loads((ROOT/'run-config.json').read_text())['reasoning_effort']",
    )
    replace_once(
        helper,
        "acquire_args=['python3', str(ROOT / 'acquire.py'), band]",
        "acquire_args=[sys.executable, str(ROOT / 'acquire.py'), band]\nif '--address' in sys.argv:acquire_args+=['--address',sys.argv[sys.argv.index('--address')+1]]",
    )
    replace_once(
        helper,
        "subprocess.run(['python3', str(ROOT / 'assign.py'), worker, cid]",
        "subprocess.run([sys.executable, str(ROOT / 'assign.py'), worker, cid]",
    )
    # These commands execute controller adapters that import native_swarm, not
    # the official compiler. Keep them on the app interpreter even when the
    # worker shell or env.sh chooses a different Python.
    helper.write_text(
        helper.read_text()
        .replace("f'python3 ", "f'{sys.executable} ")
        .replace("'python3'", "sys.executable")
    )
    replace_once(
        root / "rolling_executor.py",
        '"model_reasoning_effort=medium"',
        '"model_reasoning_effort=" + assignment["reasoning_effort"]',
    )

    if profile["group_size"] > 1:
        accounting = root / "accounting.py"
        if accounting.exists():
            replace_once(
                accounting,
                " actual_model=t.get('model') if t else None",
                " actual_model=t.get('model') if t else None\n from native_swarm.group_accounting import totals\n collaboration=totals(p,threads)\n if collaboration is not None:usage=collaboration['known_tokens'] or None;cost=collaboration['total_cost_usd'];actual_model=collaboration['model']",
            )
            replace_once(
                accounting,
                ";attempts.append(e);save(p/'cost.json',e)",
                ";e['collaboration']=collaboration;attempts.append(e);save(p/'cost.json',e)",
            )
            replace_once(
                accounting,
                "'known_worker_cost_usd':cost or 0,'unknown_cost_attempts':int(cost is None)",
                "'known_worker_cost_usd':collaboration['known_cost_usd'] if collaboration is not None else cost or 0,'unknown_cost_attempts':int(cost is None)",
            )
            replace_once(
                accounting,
                "if z['unknown_cost_attempts']==z['attempts']:z['known_worker_cost_usd']=None",
                "if z['unknown_cost_attempts']==z['attempts'] and not z['known_worker_cost_usd']:z['known_worker_cost_usd']=None",
            )
        replace_once(
            root / "resolve_ready.py",
            "  a=json.loads((p/'assignment.json').read_text())",
            "  from native_swarm.group import audit_group\n  if not audit_group(p):continue\n  a=json.loads((p/'assignment.json').read_text())",
        )
        replace_once(
            root / "resolve_ready.py",
            "from expiry_cleanup import artifact_inventory,due_expired_claims",
            "from expiry_cleanup import artifact_inventory\nfrom native_swarm.group import due_expired_claims",
        )

    scheduler = root / "rolling_executor.py"
    lines = scheduler.read_text().splitlines(keepends=True)
    if sum(line.strip() == 'runtime["proc"].kill()' for line in lines) != 2:
        raise ValueError("Unsupported supervisor shutdown sites; no launch permitted.")
    guarded = []
    for line in lines:
        if line.strip() == 'runtime["proc"].kill()':
            indent = line[: len(line) - len(line.lstrip())]
            guarded.extend(
                [
                    indent
                    + "# A session/group supervisor must finish its shared checker and reap peers.\n",
                    indent
                    + "# Its real process remains active/reserved until wait confirms exit.\n",
                    indent + "pass  # Supervisor reaps its own children.\n",
                ]
            )
        else:
            guarded.append(line)
    scheduler.write_text("".join(guarded))

    if profile["group_size"] == 1:
        replace_once(
            root / "resolve_ready.py",
            "from expiry_cleanup import artifact_inventory,due_expired_claims",
            "from expiry_cleanup import artifact_inventory\nfrom native_swarm.session import due_expired_claims",
        )

    if (root / "recorded_fnv_check_v2.py").exists():
        from .tool_runtime import configure as configure_tools

        configure_tools(root)
