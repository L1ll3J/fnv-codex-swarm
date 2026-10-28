"""Recognize only ordinary or explicitly pack-accepted official successes."""

RTM_SUFFIX = "  (compiled with Visual C++ 2008 RTM, cl 15.00.21022.08, the compiler the game was built with: submit it as it is)"
VC8_SUFFIX = "  (compiled with Visual C++ 2005 SP1, cl 14.00.50727.762, the compiler this file was built with: submit it as it is)"


def accepted_match(code, output, pack):
    if code != 0:
        return False, None
    first = output.splitlines()[0] if output.splitlines() else ""
    if first.endswith(VC8_SUFFIX):
        if pack.get("compiler") != "vc8":
            return False, None
        first = first[: -len(VC8_SUFFIX)]
    elif pack.get("compiler") == "vc8":
        return False, None
    elif first.endswith(RTM_SUFFIX):
        first = first[: -len(RTM_SUFFIX)]
    if first == "MATCH":
        return True, list(pack["flags"])
    for flags in pack.get("accept_also") or []:
        expected = (
            "MATCH  (under %s, a flag set this function also accepts: submit it as it is)"
            % " ".join(flags)
        )
        if first == expected:
            return True, list(flags)
    return False, None
