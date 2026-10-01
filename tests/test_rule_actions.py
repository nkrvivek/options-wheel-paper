from scripts.run_daily import rule_actions


def test_keeps_early_exit_and_hold_cap_lines():
    out = "earnings filter: excluded ['KMI']\nearly exit: UBER261016P00070000 kept_50 credit 1.38 limit 0.69\n  hold cap: selling 100 WMT, held since 2026-07-01\nother"
    assert rule_actions(out) == [
        "early exit: UBER261016P00070000 kept_50 credit 1.38 limit 0.69",
        "hold cap: selling 100 WMT, held since 2026-07-01",
    ]


def test_empty_or_missing_output():
    assert rule_actions("") == []
    assert rule_actions(None) == []
