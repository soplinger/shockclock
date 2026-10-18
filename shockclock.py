#!/usr/bin/env python3
"""Shockclock: offline pool sanitiser planner driven by a plain-text log."""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Available chlorine (percent by weight) and density in g/mL for liquids.
PRODUCTS = {
    "calhypo": (65.0, None),
    "dichlor": (56.0, None),
    "trichlor": (90.0, None),
    "bleach": (12.5, 1.2),
}

# Rules of thumb, all in one place so they are easy to review or change.
MIN_FC_PER_CYA = 0.075  # minimum free chlorine as a fraction of CYA
SHOCK_PER_CYA = 0.40  # shock level as a fraction of CYA
MAX_FC = 10.0  # swimming ceiling for free chlorine, ppm
MAX_CYA = 100.0  # stabiliser ceiling, ppm
CC_LIMIT = 0.5  # combined chlorine that calls for shocking, ppm
PH_RANGE = (7.2, 7.8)
TA_RANGE = (80.0, 120.0)
CH_RANGE = (200.0, 400.0)
LSI_RANGE = (-0.3, 0.3)
DEFAULT_TDS = 1000.0

# Field name -> allowed range for a test reading.
TEST_RANGES = {
    "FC": (0.0, 50.0),
    "CC": (0.0, 20.0),
    "pH": (6.0, 9.0),
    "TA": (1.0, 400.0),
    "CH": (1.0, 1000.0),
    "CYA": (0.0, 300.0),
    "T": (-5.0, 50.0),
}
TEST_CANON = {name.lower(): name for name in TEST_RANGES}
SETTING_KEYS = {"name", "volume", "product", "tds"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}(\s|$)")
AMOUNT_RE = re.compile(r"^(\d+(?:\.\d+)?)(g|ml)$")


@dataclass
class Test:
    line: int
    when: datetime
    values: dict[str, float]


@dataclass
class Dose:
    line: int
    when: datetime
    product: str
    amount: float
    unit: str


@dataclass
class Pool:
    settings: dict = field(default_factory=dict)
    tests: list[Test] = field(default_factory=list)
    doses: list[Dose] = field(default_factory=list)
    errors: list[tuple[int, str]] = field(default_factory=list)


# ---------------------------------------------------------------- parsing


def parse_pool(text: str) -> Pool:
    pool = Pool()
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if DATE_RE.match(line):
            _parse_entry(pool, number, line)
        else:
            _parse_setting(pool, number, line)
    return pool


def _parse_setting(pool: Pool, number: int, line: str) -> None:
    key, sep, value = line.partition("=")
    key, value = key.strip(), value.strip()
    if not sep:
        pool.errors.append((number, "expected a setting (key=value) or a dated entry"))
        return
    if key not in SETTING_KEYS:
        pool.errors.append((number, f"unknown setting '{key}'"))
        return
    if key == "product":
        if value not in PRODUCTS:
            pool.errors.append((number, f"unknown product '{value}'"))
            return
        pool.settings[key] = value
    elif key == "name":
        if not value:
            pool.errors.append((number, "name cannot be empty"))
            return
        pool.settings[key] = value
    else:
        number_value = _number(value)
        if number_value is None or number_value <= 0:
            pool.errors.append((number, f"{key} must be a positive number, got '{value}'"))
            return
        pool.settings[key] = number_value


def _parse_entry(pool: Pool, number: int, line: str) -> None:
    parts = line.split()
    try:
        when = datetime.strptime(f"{parts[0]} {parts[1]}", "%Y-%m-%d %H:%M")
    except ValueError:
        pool.errors.append((number, f"invalid timestamp '{parts[0]} {parts[1]}'"))
        return
    if len(parts) < 3:
        pool.errors.append((number, "missing entry kind (test or dose)"))
        return
    previous = _latest_time(pool)
    if previous is not None and when < previous:
        pool.errors.append((number, f"entry is earlier than the previous one ({previous:%Y-%m-%d %H:%M})"))
        return
    kind, fields = parts[2], parts[3:]
    if kind == "test":
        _parse_test(pool, number, when, fields)
    elif kind == "dose":
        _parse_dose(pool, number, when, fields)
    else:
        pool.errors.append((number, f"unknown entry kind '{kind}' (use test or dose)"))


def _parse_test(pool: Pool, number: int, when: datetime, fields: list[str]) -> None:
    raw, problem = _pairs(fields)
    if problem:
        pool.errors.append((number, problem))
        return
    values: dict[str, float] = {}
    for key, text in raw.items():
        name = TEST_CANON.get(key.lower())
        if name is None:
            pool.errors.append((number, f"unknown test field '{key}'"))
            return
        if name in values:
            pool.errors.append((number, f"duplicate field '{name}'"))
            return
        value = _number(text)
        if value is None:
            pool.errors.append((number, f"'{key}' is not a number: '{text}'"))
            return
        low, high = TEST_RANGES[name]
        if not low <= value <= high:
            pool.errors.append((number, f"{name}={text} is outside the range {low:g}..{high:g}"))
            return
        values[name] = value
    missing = [name for name in TEST_RANGES if name not in values]
    if missing:
        pool.errors.append((number, "test is missing " + ", ".join(missing)))
        return
    pool.tests.append(Test(number, when, values))


def _parse_dose(pool: Pool, number: int, when: datetime, fields: list[str]) -> None:
    raw, problem = _pairs(fields)
    if problem:
        pool.errors.append((number, problem))
        return
    unknown = sorted(set(raw) - {"product", "amount"})
    if unknown:
        pool.errors.append((number, f"unknown dose field '{unknown[0]}'"))
        return
    product = raw.get("product")
    if product not in PRODUCTS:
        pool.errors.append((number, f"dose needs product=<{'|'.join(PRODUCTS)}>, got '{product}'"))
        return
    match = AMOUNT_RE.match(raw.get("amount", ""))
    if not match or float(match.group(1)) <= 0:
        pool.errors.append((number, "dose amount must look like 150g or 400ml"))
        return
    pool.doses.append(Dose(number, when, product, float(match.group(1)), match.group(2)))


def _pairs(tokens: list[str]) -> tuple[dict[str, str], str | None]:
    out: dict[str, str] = {}
    for token in tokens:
        key, sep, value = token.partition("=")
        if not sep or not key:
            return {}, f"expected key=value, got '{token}'"
        if key in out:
            return {}, f"duplicate field '{key}'"
        out[key] = value
    return out, None


def _number(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _latest_time(pool: Pool) -> datetime | None:
    times = [t.when for t in pool.tests] + [d.when for d in pool.doses]
    return max(times) if times else None


# ---------------------------------------------------------- chemistry


def min_free_chlorine(cya: float) -> float:
    return round(MIN_FC_PER_CYA * cya, 2)


def shock_level(cya: float) -> float:
    return round(SHOCK_PER_CYA * cya, 1)


def langelier_index(ph: float, ta: float, ch: float, temp_c: float, tds: float = DEFAULT_TDS) -> float:
    """Langelier saturation index: positive means scale-forming, negative corrosive."""
    a = (math.log10(tds) - 1) / 10
    b = -13.12 * math.log10(temp_c + 273) + 34.55
    c = math.log10(ch) - 0.4
    d = math.log10(ta)
    ph_s = (9.3 + a + b) - (c + d)
    return ph - ph_s


def pure_chlorine_grams(raise_ppm: float, volume_l: float) -> float:
    # 1 ppm (mg/L) across 1000 L is exactly 1 g of chlorine.
    return raise_ppm * volume_l / 1000


def product_amount(pure_g: float, product: str) -> tuple[float, str]:
    percent, density = PRODUCTS[product]
    grams = pure_g / (percent / 100)
    if density is None:
        return grams, "g"
    return grams / density, "ml"


def plan_dose(fc: float, cc: float, cya: float, volume_l: float, product: str,
              target: float | None = None, shock: bool = False) -> dict:
    minimum = min_free_chlorine(cya)
    if target is not None:
        reason = "custom target"
    elif shock or cc >= CC_LIMIT:
        target = shock_level(cya)
        reason = f"shock: combined chlorine at or above {CC_LIMIT:g} ppm"
    elif fc < minimum:
        target = minimum
        reason = "bring free chlorine up to the minimum for this CYA"
    else:
        return {"needed": False, "reason": f"free chlorine {fc:g} already meets the minimum {minimum:g}"}
    raise_ppm = round(target - fc, 2)
    if raise_ppm <= 0:
        return {"needed": False, "reason": f"free chlorine {fc:g} is already at or above target {target:g}"}
    pure = pure_chlorine_grams(raise_ppm, volume_l)
    amount, unit = product_amount(pure, product)
    return {
        "needed": True,
        "reason": reason,
        "target": target,
        "raise_ppm": raise_ppm,
        "product": product,
        "pure_chlorine_g": round(pure, 1),
        "amount": round(amount, 1),
        "unit": unit,
    }


def drain_plan(cya: float, target_cya: float, volume_l: float) -> dict:
    if target_cya < 0:
        raise ValueError("target CYA cannot be negative")
    if cya <= target_cya:
        return {"needed": False, "reason": f"CYA {cya:g} is already at or below {target_cya:g}"}
    fraction = (cya - target_cya) / cya
    return {
        "needed": True,
        "fraction": round(fraction * 100, 1),
        "litres": round(fraction * volume_l),
        "from": cya,
        "to": target_cya,
    }


def decay_rates(pool: Pool) -> list[float]:
    """Free-chlorine loss in ppm/hour between consecutive tests with no dose between them."""
    rates = []
    for before, after in zip(pool.tests, pool.tests[1:]):
        if any(before.when < d.when <= after.when for d in pool.doses):
            continue
        hours = (after.when - before.when).total_seconds() / 3600
        if hours <= 0:
            continue
        rates.append((before.values["FC"] - after.values["FC"]) / hours)
    return rates


def forecast(pool: Pool) -> dict:
    rates = decay_rates(pool)
    if not rates:
        return {"rate": None, "pairs": 0, "hours_to_minimum": None}
    rate = statistics.median(rates)
    result = {"rate": round(rate, 3), "pairs": len(rates), "hours_to_minimum": None}
    if rate <= 0:
        return result
    latest = pool.tests[-1].values
    margin = latest["FC"] - min_free_chlorine(latest["CYA"])
    result["hours_to_minimum"] = round(max(0.0, margin / rate), 1)
    return result


def assess(values: dict[str, float], tds: float = DEFAULT_TDS) -> list[str]:
    """Reasons a pool is not ready to swim in. An empty list means ready."""
    issues = []
    fc, cc, cya = values["FC"], values["CC"], values["CYA"]
    minimum = min_free_chlorine(cya)
    if fc < minimum:
        issues.append(f"free chlorine {fc:g} is below the minimum {minimum:g} for CYA {cya:g}")
    if fc > MAX_FC:
        issues.append(f"free chlorine {fc:g} is above the {MAX_FC:g} ppm swimming limit")
    if cc >= CC_LIMIT:
        issues.append(f"combined chlorine {cc:g} is at or above {CC_LIMIT:g} ppm; shock the pool")
    if not PH_RANGE[0] <= values["pH"] <= PH_RANGE[1]:
        issues.append(f"pH {values['pH']:g} is outside {PH_RANGE[0]:g}..{PH_RANGE[1]:g}")
    if not TA_RANGE[0] <= values["TA"] <= TA_RANGE[1]:
        issues.append(f"total alkalinity {values['TA']:g} is outside {TA_RANGE[0]:g}..{TA_RANGE[1]:g} ppm")
    if not CH_RANGE[0] <= values["CH"] <= CH_RANGE[1]:
        issues.append(f"calcium hardness {values['CH']:g} is outside {CH_RANGE[0]:g}..{CH_RANGE[1]:g} ppm")
    if cya > MAX_CYA:
        issues.append(f"CYA {cya:g} is above {MAX_CYA:g}; drain and refill part of the pool")
    lsi = langelier_index(values["pH"], values["TA"], values["CH"], values["T"], tds)
    if not LSI_RANGE[0] <= lsi <= LSI_RANGE[1]:
        kind = "scale-forming" if lsi > LSI_RANGE[1] else "corrosive"
        issues.append(f"Langelier index {lsi:+.2f} is {kind} (balanced is {LSI_RANGE[0]:g}..+{LSI_RANGE[1]:g})")
    return issues


def latest_test(pool: Pool) -> Test | None:
    return pool.tests[-1] if pool.tests else None


def build_report(pool: Pool) -> dict:
    latest = latest_test(pool)
    values = latest.values
    tds = pool.settings.get("tds", DEFAULT_TDS)
    report = {
        "pool": pool.settings.get("name", "pool"),
        "tested_at": latest.when.isoformat(sep=" ", timespec="minutes"),
        "readings": values,
        "targets": {
            "minimum_fc": min_free_chlorine(values["CYA"]),
            "shock_level": shock_level(values["CYA"]),
            "swim_max_fc": MAX_FC,
        },
        "lsi": round(langelier_index(values["pH"], values["TA"], values["CH"], values["T"], tds), 2),
        "issues": assess(values, tds),
        "forecast": forecast(pool),
        "dose": None,
    }
    volume = pool.settings.get("volume")
    if volume is not None:
        report["dose"] = plan_dose(values["FC"], values["CC"], values["CYA"], volume,
                                   pool.settings.get("product", "calhypo"))
    return report


# ---------------------------------------------------------- commands


def cmd_lint(pool: Pool, path: str) -> int:
    for number, message in pool.errors:
        print(f"{path}:{number}: error: {message}")
    if pool.errors:
        print(f"{len(pool.errors)} error(s) found")
        return 1
    print(f"OK: {len(pool.tests)} test(s), {len(pool.doses)} dose(s)")
    return 0


def cmd_status(pool: Pool, args: argparse.Namespace) -> int:
    report = build_report(pool)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    v = report["readings"]
    targets = report["targets"]
    print(f"{report['pool']}, tested {report['tested_at']}")
    print(f"  FC {v['FC']:g}  CC {v['CC']:g}  pH {v['pH']:g}  TA {v['TA']:g}  "
          f"CH {v['CH']:g}  CYA {v['CYA']:g}  T {v['T']:g} C")
    print(f"  targets: minimum FC {targets['minimum_fc']:g} ppm, shock {targets['shock_level']:g} ppm, "
          f"swim ceiling {targets['swim_max_fc']:g} ppm")
    print(f"  Langelier index {report['lsi']:+.2f}")
    fc = report["forecast"]
    if fc["rate"] is None:
        print("  FC decay: not enough tests to estimate")
    elif fc["rate"] <= 0:
        print(f"  FC decay: none observed (median {fc['rate']:g} ppm/h, {fc['pairs']} pair(s))")
    else:
        hours = fc["hours_to_minimum"]
        when = "already at or below the minimum" if hours == 0 else f"about {hours:g} h to the minimum"
        print(f"  FC decay: {fc['rate']:g} ppm/h (median of {fc['pairs']} pair(s)), {when}")
    dose = report["dose"]
    if dose is None:
        print("  dose: set volume= in the pool file to plan a dose")
    elif not dose["needed"]:
        print(f"  dose: none needed ({dose['reason']})")
    else:
        print(f"  dose: raise {dose['raise_ppm']:g} ppm to {dose['target']:g} ({dose['reason']}): "
              f"{dose['amount']:g} {dose['unit']} {dose['product']}")
    if report["issues"]:
        print("  issues:")
        for issue in report["issues"]:
            print(f"    - {issue}")
    else:
        print("  ready to swim")
    return 0


def cmd_check(pool: Pool) -> int:
    issues = assess(latest_test(pool).values, pool.settings.get("tds", DEFAULT_TDS))
    if not issues:
        print("READY")
        return 0
    print("NOT READY")
    for issue in issues:
        print(f"  - {issue}")
    return 1


def cmd_dose(pool: Pool, args: argparse.Namespace) -> int:
    volume = pool.settings.get("volume")
    if volume is None:
        print("shockclock: dose needs volume= in the pool file", file=sys.stderr)
        return 2
    values = latest_test(pool).values
    product = args.product or pool.settings.get("product", "calhypo")
    plan = plan_dose(values["FC"], values["CC"], values["CYA"], volume, product,
                     target=args.target, shock=args.shock)
    if not plan["needed"]:
        print(f"no dose needed: {plan['reason']}")
        return 0
    print(f"Add {plan['amount']:g} {plan['unit']} of {plan['product']} "
          f"({PRODUCTS[plan['product']][0]:g}% available chlorine) for {volume:g} L")
    print(f"  raise free chlorine by {plan['raise_ppm']:g} ppm to {plan['target']:g} ppm")
    print(f"  pure chlorine needed: {plan['pure_chlorine_g']:g} g")
    print(f"  reason: {plan['reason']}")
    return 0


def cmd_drain(pool: Pool, args: argparse.Namespace) -> int:
    volume = pool.settings.get("volume")
    if volume is None:
        print("shockclock: drain needs volume= in the pool file", file=sys.stderr)
        return 2
    cya = latest_test(pool).values["CYA"]
    try:
        plan = drain_plan(cya, args.target_cya, volume)
    except ValueError as exc:
        print(f"shockclock: {exc}", file=sys.stderr)
        return 2
    if not plan["needed"]:
        print(f"no drain needed: {plan['reason']}")
        return 0
    print(f"Drain {plan['litres']:,} L ({plan['fraction']:g}% of {volume:,g} L) and refill with fresh water")
    print(f"  CYA falls from {plan['from']:g} to {plan['to']:g} ppm (assumes the pool mixes fully)")
    return 0


def cmd_history(pool: Pool) -> int:
    events = [(t.when, 0, t) for t in pool.tests] + [(d.when, 1, d) for d in pool.doses]
    events.sort(key=lambda e: (e[0], e[1]))
    print(f"{'when':<16} {'FC':>5} {'CC':>5} {'pH':>5} {'TA':>5} {'CH':>5} {'CYA':>5} {'T':>5} {'LSI':>6}")
    for when, _, event in events:
        stamp = when.strftime("%Y-%m-%d %H:%M")
        if isinstance(event, Dose):
            unit = event.unit
            print(f"{stamp:<16} dose {event.product} {event.amount:g}{unit}")
            continue
        v = event.values
        tds = pool.settings.get("tds", DEFAULT_TDS)
        lsi = langelier_index(v["pH"], v["TA"], v["CH"], v["T"], tds)
        print(f"{stamp:<16} {v['FC']:>5g} {v['CC']:>5g} {v['pH']:>5g} {v['TA']:>5g} "
              f"{v['CH']:>5g} {v['CYA']:>5g} {v['T']:>5g} {lsi:>+6.2f}")
    return 0


def cmd_forecast(pool: Pool) -> int:
    fc = forecast(pool)
    if fc["rate"] is None:
        print("not enough tests without a dose between them to estimate decay")
        return 0
    print(f"median FC decay: {fc['rate']:g} ppm/h from {fc['pairs']} pair(s)")
    if fc["hours_to_minimum"] is None:
        print("FC is not falling, so no run-out time")
    else:
        print(f"hours until FC reaches the minimum: {fc['hours_to_minimum']:g}")
    return 0


def cmd_targets(cya: float) -> int:
    if cya < 0:
        print("shockclock: CYA cannot be negative", file=sys.stderr)
        return 2
    print(f"CYA {cya:g} ppm: minimum FC {min_free_chlorine(cya):g} ppm, "
          f"shock {shock_level(cya):g} ppm, swim ceiling {MAX_FC:g} ppm")
    return 0


# ---------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shockclock",
        description="Offline pool sanitiser planner driven by a plain-text water-test log.",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    sub.add_parser("lint", help="check a pool file for line-numbered errors").add_argument("pool")

    status = sub.add_parser("status", help="report the latest test, targets, decay and dose")
    status.add_argument("pool")
    status.add_argument("--json", action="store_true", help="print the report as JSON")

    sub.add_parser("check", help="swim-ready gate; exit 1 when not ready").add_argument("pool")

    dose = sub.add_parser("dose", help="chlorine dose to reach the target")
    dose.add_argument("pool")
    dose.add_argument("--target", type=float, help="free chlorine target in ppm")
    dose.add_argument("--product", choices=sorted(PRODUCTS), help="override the pool's product")
    dose.add_argument("--shock", action="store_true", help="dose to the shock level")

    drain = sub.add_parser("drain", help="litres to drain and refill to reach a CYA target")
    drain.add_argument("pool")
    drain.add_argument("--target-cya", type=float, required=True)

    sub.add_parser("history", help="chronological table of tests and doses").add_argument("pool")
    sub.add_parser("forecast", help="chlorine decay rate and run-out time").add_argument("pool")
    sub.add_parser("targets", help="minimum and shock levels for a CYA value").add_argument("cya", type=float)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "targets":
        return cmd_targets(args.cya)
    try:
        text = Path(args.pool).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"shockclock: cannot read {args.pool}: {exc.strerror}", file=sys.stderr)
        return 2
    pool = parse_pool(text)
    if args.command == "lint":
        return cmd_lint(pool, args.pool)
    if pool.errors:
        for number, message in pool.errors:
            print(f"{args.pool}:{number}: error: {message}", file=sys.stderr)
        print("shockclock: fix the errors above (run lint for the full list)", file=sys.stderr)
        return 2
    if not pool.tests:
        print("shockclock: the pool file has no test readings", file=sys.stderr)
        return 2
    if args.command == "status":
        return cmd_status(pool, args)
    if args.command == "check":
        return cmd_check(pool)
    if args.command == "dose":
        return cmd_dose(pool, args)
    if args.command == "drain":
        return cmd_drain(pool, args)
    if args.command == "history":
        return cmd_history(pool)
    return cmd_forecast(pool)


if __name__ == "__main__":
    sys.exit(main())
