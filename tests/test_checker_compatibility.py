from native_swarm.checker_result import accepted_match


def test_accepts_only_exact_success_under_original_pack_flags():
    pack = {"flags": ["/O2"], "accept_also": [["/Od", "/Ob1"]]}
    assert accepted_match(0, "MATCH\n", pack) == (True, ["/O2"])
    line = "MATCH  (under /Od /Ob1, a flag set this function also accepts: submit it as it is)\n"
    assert accepted_match(0, line, pack) == (True, ["/Od", "/Ob1"])
    assert accepted_match(1, line, pack)[0] is False
    assert accepted_match(0, line, {"flags": ["/O2"]})[0] is False
    assert accepted_match(0, "LITERAL_MISMATCH\n", pack)[0] is False
    assert accepted_match(0, "diagnostic\nMATCH\n", pack)[0] is False
    assert (
        accepted_match(
            0,
            "MATCH  (under your --flags, not the pack's /O2: the site does not accept this yet.)\n",
            pack,
        )[0]
        is False
    )
