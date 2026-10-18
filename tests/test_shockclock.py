import json
from pathlib import Path

import pytest

from shockclock import (
    assess,
    decay_rates,
    drain_plan,
    forecast,
    langelier_index,
    main,
    min_free_chlorine,
    parse_pool,
    plan_dose,
    product_amount,
    shock_level,
)

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "backyard.pool"

GOOD_LOG = """\
name=Test
volume=40000
product=calhypo
2026-10-01 08:00 test FC=3 CC=0.1 pH=7.5 TA=100 CH=250 CYA=40 T=25
"""


def write(tmp_path, text, name="pool.txt"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_example_file_parses_cleanly():
    pool = parse_pool(EXAMPLE.read_text(encoding="utf-8"))
    assert pool.errors == []
    assert len(pool.tests) == 6
    assert len(pool.doses) == 1


def test_errors_carry_the_right_line_numbers():
    text = (
        "volume=abc\n"
        "2026-13-01 08:00 test FC=1 CC=0 pH=7.5 TA=90 CH=250 CYA=40 T=20\n"
        "2026-10-01 08:00 test FC=60 CC=0 pH=7.5 TA=90 CH=250 CYA=40 T=20\n"
        "2026-10-01 09:00 test FC=1 CC=0 pH=7.5 TA=90 CH=250 CYA=40\n"
    )
    pool = parse_pool(text)
    lines = [number for number, _ in pool.errors]
    assert lines == [1, 2, 3, 4]
    assert "missing" in pool.errors[3][1]


def test_out_of_order_entries_are_rejected():
    text = (
        "2026-10-02 08:00 test FC=3 CC=0 pH=7.5 TA=90 CH=250 CYA=40 T=20\n"
        "2026-10-01 08:00 test FC=3 CC=0 pH=7.5 TA=90 CH=250 CYA=40 T=20\n"
    )
    pool = parse_pool(text)
    assert [number for number, _ in pool.errors] == [2]
    assert "earlier" in pool.errors[0][1]


def test_unknown_product_and_bad_dose_amount_are_errors():
    text = (
        "product=mystery\n"
        "2026-10-01 08:00 dose product=calhypo amount=12kg\n"
    )
    pool = parse_pool(text)
    assert [number for number, _ in pool.errors] == [1, 2]


def test_minimum_and_shock_levels_follow_cya():
    assert min_free_chlorine(40) == 3.0
    assert min_free_chlorine(30) == 2.25
    assert shock_level(40) == 16.0
    assert shock_level(100) == 40.0


def test_langelier_index_matches_hand_calculation():
    # pH 7.5, TA 100, CH 250, 25 C, TDS 1000 gives pHs of about 7.59.
    assert langelier_index(7.5, 100, 250, 25, 1000) == pytest.approx(-0.09, abs=0.01)


def test_one_ppm_in_1000_litres_is_one_gram_of_chlorine():
    assert plan_dose(0, 0, 40, 1000, "dichlor", target=1.0)["pure_chlorine_g"] == 1.0


def test_calcium_hypochlorite_dose_for_forty_thousand_litres():
    # Raising 1 ppm across 40,000 L needs 40 g of chlorine, or 61.5 g of 65% calhypo.
    amount, unit = product_amount(40, "calhypo")
    assert unit == "g"
    assert amount == pytest.approx(61.54, abs=0.01)


def test_liquid_bleach_is_measured_in_millilitres():
    amount, unit = product_amount(40, "bleach")
    assert unit == "ml"
    assert amount == pytest.approx(266.67, abs=0.01)


def test_dose_is_skipped_when_chlorine_is_already_enough():
    plan = plan_dose(fc=4.0, cc=0.1, cya=40, volume_l=40000, product="calhypo")
    assert plan["needed"] is False


def test_combined_chlorine_triggers_a_shock_to_shock_level():
    plan = plan_dose(fc=2.0, cc=0.8, cya=40, volume_l=40000, product="calhypo")
    assert plan["needed"] is True
    assert plan["target"] == 16.0
    assert "shock" in plan["reason"]


def test_decay_rate_ignores_pairs_that_span_a_dose():
    pool = parse_pool(EXAMPLE.read_text(encoding="utf-8"))
    rates = decay_rates(pool)
    # Six tests give five pairs; the pair spanning the 09:00 dose is skipped.
    assert len(rates) == 4
    fc = forecast(pool)
    assert fc["rate"] == pytest.approx(0.055, abs=0.001)
    assert fc["hours_to_minimum"] == 0.0


def test_forecast_reports_hours_to_minimum_when_margin_remains():
    text = (
        "volume=40000\n"
        "2026-10-01 08:00 test FC=6 CC=0 pH=7.5 TA=100 CH=250 CYA=40 T=25\n"
        "2026-10-01 18:00 test FC=5 CC=0 pH=7.5 TA=100 CH=250 CYA=40 T=25\n"
    )
    pool = parse_pool(text)
    fc = forecast(pool)
    assert fc["rate"] == pytest.approx(0.1)
    assert fc["hours_to_minimum"] == pytest.approx(20.0)


def test_assess_lists_every_problem_and_empty_means_ready():
    good = {"FC": 3.5, "CC": 0.1, "pH": 7.5, "TA": 100, "CH": 250, "CYA": 40, "T": 25}
    assert assess(good) == []
    bad = dict(good, FC=1.0, pH=8.2, CYA=120)
    issues = assess(bad)
    assert any("below the minimum" in issue for issue in issues)
    assert any("pH 8.2" in issue for issue in issues)
    assert any("CYA 120" in issue for issue in issues)


def test_drain_fraction_matches_cya_ratio():
    plan = drain_plan(cya=100, target_cya=30, volume_l=40000)
    assert plan["fraction"] == 70.0
    assert plan["litres"] == 28000
    assert drain_plan(cya=30, target_cya=40, volume_l=40000)["needed"] is False


def test_cli_lint_exit_codes(tmp_path, capsys):
    assert main(["lint", str(EXAMPLE)]) == 0
    bad = write(tmp_path, "2026-10-01 08:00 test FC=99 CC=0 pH=7.5 TA=90 CH=250 CYA=40 T=20\n")
    assert main(["lint", bad]) == 1
    assert f"{bad}:1: error:" in capsys.readouterr().out


def test_cli_check_fails_on_the_example_and_passes_on_a_good_pool(tmp_path, capsys):
    assert main(["check", str(EXAMPLE)]) == 1
    assert "NOT READY" in capsys.readouterr().out
    good = write(tmp_path, GOOD_LOG)
    assert main(["check", good]) == 0
    assert "READY" in capsys.readouterr().out


def test_cli_dose_needs_volume_and_reports_amount(tmp_path, capsys):
    no_volume = write(tmp_path, "2026-10-01 08:00 test FC=1 CC=0 pH=7.5 TA=100 CH=250 CYA=40 T=25\n", "nv.txt")
    assert main(["dose", no_volume]) == 2
    capsys.readouterr()
    assert main(["dose", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "6.2 g of calhypo" in out


def test_cli_status_json_is_machine_readable(capsys):
    assert main(["status", str(EXAMPLE), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["targets"]["minimum_fc"] == 3.0
    assert report["dose"]["needed"] is True
    assert any("minimum" in issue for issue in report["issues"])


def test_cli_history_marks_doses_in_order(capsys):
    assert main(["history", str(EXAMPLE)]) == 0
    lines = capsys.readouterr().out.splitlines()
    dose_row = next(i for i, line in enumerate(lines) if "dose calhypo 150g" in line)
    assert "2026-10-02 09:00" in lines[dose_row]
    assert "2026-10-02 10:00" in lines[dose_row + 1]


def test_cli_targets_needs_no_pool_file(capsys):
    assert main(["targets", "50"]) == 0
    assert "minimum FC 3.75 ppm" in capsys.readouterr().out


def test_cli_refuses_to_run_on_a_file_with_errors(tmp_path, capsys):
    bad = write(tmp_path, "nonsense line\n")
    assert main(["status", bad]) == 2
    assert "fix the errors" in capsys.readouterr().err
