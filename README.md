# Shockclock

**Offline pool sanitiser planner: one plain-text water-test log in, dose decisions, a swim-ready gate and a run-out forecast out.**

Shockclock reads the readings you already take (free chlorine, combined chlorine, pH, alkalinity, calcium hardness, stabiliser, temperature) and tells you what to do next: how much chlorine to add, whether the pool is safe to swim in, how fast your chlorine is falling, and how much water to drain if the stabiliser is too high. It runs entirely on your machine with the Python standard library. There are no accounts, no network calls and no API keys.

## Features

- **Plain-text pool file.** Settings (`volume=`, `product=`, `tds=`, `name=`) plus dated `test` and `dose` lines. Easy to type into a notebook or a text editor.
- **Line-numbered lint.** Every bad line is reported as `file:line: error: message`, covering bad timestamps, out-of-range readings, missing or duplicated fields, unknown products and out-of-order entries.
- **CI-friendly lint exit codes.** `lint` exits `0` when the file is clean and `1` when it has errors. Other commands exit `2` on bad input.
- **Latest-test status.** Shows the newest readings, the targets for your stabiliser level, the Langelier saturation index and the list of issues.
- **Swim-ready gate.** `check` prints `READY` or `NOT READY` with reasons, and exits `1` when the pool is not safe to swim in, so it can gate a script.
- **Targets from stabiliser (CYA).** `targets 40` gives the minimum free chlorine and the shock level for any CYA. The rules of thumb are 7.5% and 40% of CYA.
- **Langelier saturation index.** Calculated from pH, alkalinity, calcium hardness, temperature and TDS. Flags scale-forming and corrosive water.
- **Chlorine dose calculator.** Gives the amount of product to add to reach the minimum or the shock level. It uses calcium hypochlorite (65%), dichlor (56%), trichlor (90%) or liquid bleach (12.5%, measured in mL). `--target`, `--product` and `--shock` override the defaults.
- **Shock trigger.** A combined chlorine reading of 0.5 ppm or more calls for shocking, and the dose is then planned to the shock level.
- **Decay forecast from your own log.** The median chlorine loss per hour is taken from consecutive tests, skipping any pair with a dose between them. It estimates the hours until free chlorine reaches the minimum.
- **Partial drain planner.** `drain --target-cya 30` gives the litres to drain and refill to bring stabiliser down to a target, from the fraction `(CYA - target) / CYA`.
- **Chronological history.** `history` shows a table of tests with the LSI for each one, with doses marked in sequence.
- **Machine-readable status.** `status --json` prints the full report as JSON for dashboards or scripts.
- **Deterministic.** It never reads the clock, so the same file always gives the same answers.

## Quick start

Save this as `backyard.pool`, or use the copy in `examples/`:

```text
# Backyard pool: 40,000 L, calcium hypochlorite, tested twice a day
name=Backyard
volume=40000
product=calhypo
tds=1200

2026-10-01 08:00 test FC=3.2 CC=0.1 pH=7.5 TA=90 CH=260 CYA=40 T=24
2026-10-01 20:00 test FC=2.6 CC=0.1 pH=7.5 TA=90 CH=260 CYA=40 T=23
2026-10-02 08:00 test FC=2.0 CC=0.2 pH=7.6 TA=90 CH=260 CYA=40 T=23
2026-10-02 09:00 dose product=calhypo amount=150g
2026-10-02 10:00 test FC=4.5 CC=0.1 pH=7.6 TA=90 CH=260 CYA=40 T=24
2026-10-02 20:00 test FC=3.9 CC=0.1 pH=7.6 TA=90 CH=260 CYA=40 T=23
2026-10-03 08:00 test FC=2.9 CC=0.1 pH=7.7 TA=90 CH=260 CYA=40 T=22
```

Then run:

```bash
python3 shockclock.py status examples/backyard.pool
```

Expected output:

```text
Backyard, tested 2026-10-03 08:00
  FC 2.9  CC 0.1  pH 7.7  TA 90  CH 260  CYA 40  T 22 C
  targets: minimum FC 3 ppm, shock 16 ppm, swim ceiling 10 ppm
  Langelier index +0.02
  FC decay: 0.055 ppm/h (median of 4 pair(s)), already at or below the minimum
  dose: raise 0.1 ppm to 3 (bring free chlorine up to the minimum for this CYA): 6.2 g calhypo
  issues:
    - free chlorine 2.9 is below the minimum 3 for CYA 40
```

Other useful commands:

```bash
python3 shockclock.py check examples/backyard.pool     # prints NOT READY, exits 1
python3 shockclock.py dose examples/backyard.pool      # add 6.2 g of calhypo for 40000 L
python3 shockclock.py history examples/backyard.pool
python3 shockclock.py targets 50                       # minimum FC 3.75 ppm, shock 20 ppm
python3 shockclock.py drain examples/backyard.pool --target-cya 30
python3 shockclock.py lint examples/backyard.pool      # OK: 6 test(s), 1 dose(s)
```

## How to run

Shockclock needs Python 3.10 or newer and nothing else.

```bash
python3 shockclock.py COMMAND [ARGS]
python3 shockclock.py --help
python3 shockclock.py dose --help
```

| Command | What it does | Exit codes |
| --- | --- | --- |
| `lint FILE` | Checks the whole file and lists every error | 0 clean, 1 errors |
| `status FILE [--json]` | Latest readings, targets, LSI, decay, dose and issues | 0 ok, 2 bad input |
| `check FILE` | Swim-ready gate | 0 ready, 1 not ready, 2 bad input |
| `dose FILE [--target PPM] [--product P] [--shock]` | Product amount to add | 0 ok, 2 bad input |
| `drain FILE --target-cya N` | Litres to drain and refill | 0 ok, 2 bad input |
| `history FILE` | Chronological table of tests and doses | 0 ok, 2 bad input |
| `forecast FILE` | Decay rate and hours to the minimum | 0 ok, 2 bad input |
| `targets CYA` | Minimum and shock levels for a stabiliser value | 0 ok, 2 bad input |

### File format

- Lines starting with `#` and blank lines are ignored.
- Settings are `key=value`. Allowed keys are `name`, `volume` (litres), `product` (`calhypo`, `dichlor`, `trichlor` or `bleach`) and `tds` (ppm, default 1000).
- A test line is `YYYY-MM-DD HH:MM test FC= CC= pH= TA= CH= CYA= T=`. All seven fields are required. Units are ppm, except `pH` (none) and `T` (°C).
- A dose line is `YYYY-MM-DD HH:MM dose product=NAME amount=150g` or `amount=400ml`.
- Entries must be in time order.

## How to test

```bash
python3 -m pip install pytest
python3 -m pytest -q
```

The suite covers the file parser and its line numbers, the CYA targets, the Langelier index against a hand calculation, dose arithmetic for each product, the decay forecast when a dose falls between tests, the drain fraction, and the CLI exit codes and output. It makes no network calls and writes only to pytest's temporary directory.

## Rules of thumb used

These are the defaults in `shockclock.py`. Each one is a single constant, so change them if your local guidance differs.

- Minimum free chlorine is 7.5% of CYA. Shock level is 40% of CYA.
- The swimming ceiling for free chlorine is 10 ppm. CYA above 100 ppm is flagged.
- Combined chlorine of 0.5 ppm or more calls for shocking.
- pH 7.2 to 7.8, total alkalinity 80 to 120 ppm, calcium hardness 200 to 400 ppm and an LSI of -0.3 to +0.3 are the acceptable ranges.
- One ppm across 1000 litres is one gram of chlorine. Liquid bleach is taken as 12.5% available chlorine at 1.2 g/mL.

## Layout

```text
shockclock.py            the whole tool (standard library only)
examples/backyard.pool   a worked example log
tests/test_shockclock.py pytest suite
conftest.py              empty, puts the project root on the import path
pyproject.toml           pytest configuration
```
