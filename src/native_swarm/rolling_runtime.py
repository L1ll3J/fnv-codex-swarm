"""Adapt copied admission and final evidence guards to receipt-derived ceilings."""

from .runtime import replace_once


def configure_batch(path):
    replace_once(
        path,
        "        path=rounds/(batch_id+'.json')",
        "        from native_swarm.commands import block_new_batch\n        block_new_batch(claim)\n        path=rounds/(batch_id+'.json')",
    )
    replace_once(
        path,
        "assignment['max_batch_rounds'],'Assigned batch-round budget exhausted'",
        "__import__('native_swarm.budgets',fromlist=['effective_limits']).effective_limits(claim,assignment)['max_batch_rounds'],'Assigned batch-round budget exhausted'",
    )
    replace_once(
        path,
        "batch={'identity':identity,'batch_id':batch_id,",
        "batch={'round_number':len(list(rounds.glob('*.json')))+1,'identity':identity,'batch_id':batch_id,",
    )


def configure_recorder(path):
    replace_once(
        path,
        "  assert args.candidate is not None,",
        "  from native_swarm.commands import block_new_check\n  block_new_check(claim)\n  assert args.candidate is not None,",
    )
    replace_once(
        path,
        "  limit=assignment.get('max_compiler_invocations',assignment['max_plain_checks'])",
        "  from native_swarm.budgets import effective_limits\n  effective=effective_limits(claim,assignment)\n  limit=effective['max_compiler_invocations']",
    )
    replace_once(
        path,
        "len(prior)<assignment['max_plain_checks']",
        "len(prior)<effective['max_plain_checks']",
    )


def configure_evidence(path):
    replace_once(
        path,
        "count<=assignment['max_plain_checks']",
        "count<=__import__('native_swarm.budgets',fromlist=['effective_limits']).effective_limits(claim,assignment)['max_plain_checks']",
    )
    replace_once(
        path,
        "upper<=assignment['max_compiler_invocations']",
        "upper<=__import__('native_swarm.budgets',fromlist=['effective_limits']).effective_limits(claim,assignment)['max_compiler_invocations']",
    )
