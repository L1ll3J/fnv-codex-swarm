"""Profile attempt allowances, copied into immutable new assignments."""


def validate(rounds, mode):
    if type(rounds) is not int or not 1 <= rounds <= 1000:
        raise ValueError("Batch rounds must be a whole number from 1 to 1000.")
    if mode not in ("shared", "per_member"):
        raise ValueError("Choose a shared team or per-member allowance.")


def allocation(profile):
    rounds, mode = profile.get("rounds", 20), profile.get("budget_mode", "shared")
    validate(rounds, mode)
    size = profile.get("group_size", 1)
    total = rounds * size if mode == "per_member" else rounds
    return {
        "max_batch_rounds": total,
        "max_plain_checks": total * 8,
        "max_compiler_invocations": total * 8 * 4,
        "budget_mode": mode,
        "rounds_per_member": rounds if mode == "per_member" else None,
    }


def for_run(root):
    from .state import read

    profile = read(root / "native-app-profile.json")
    profile.update(read(root / "operator-control.json").get("native_app_selection", {}))
    return allocation(profile)
