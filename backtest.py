import csv
import argparse
from collections import defaultdict

ZONES = [("0-15%", 0, 15), ("15-50%", 15, 50), ("50-85%", 50, 85), ("85-100%", 85, 100.000001)]

def run(path):
    counts = defaultdict(lambda: {"graduated": 0, "died": 0, "unresolved": 0})
    missing = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            passed = str(row.get("passed", row.get("filter_result", row.get("decision", "")))).strip().lower()
            if passed not in {"1", "true", "yes", "pass", "passed", "setup valid"}:
                continue
            raw = row.get("curve_pct_at_alert", row.get("curve_pct", row.get("bonding_curve_pct", "")))
            try:
                curve = float(str(raw).replace("%", ""))
                if not 0 <= curve <= 100: raise ValueError()
            except (ValueError, TypeError):
                missing += 1
                continue
            zone = next(name for name, low, high in ZONES if low <= curve < high)
            outcome = str(row.get("outcome", row.get("final_outcome", "unresolved"))).strip().lower()
            key = "graduated" if outcome in {"graduated", "graduate", "success"} else "died" if outcome in {"died", "dead", "rugged", "failed", "abandoned", "zero"} else "unresolved"
            counts[zone][key] += 1
    print("Curve zone | Graduated | Died | Unresolved")
    total = {k: 0 for k in ("graduated", "died", "unresolved")}
    for name, _, _ in ZONES:
        values = counts[name]
        print(f"{name:>8} | {values['graduated']:>9} | {values['died']:>4} | {values['unresolved']:>10}")
        for key in total: total[key] += values[key]
    print(f"TOTAL | {total['graduated']} graduated | {total['died']} died | {total['unresolved']} unresolved")
    resolved = total['graduated'] + total['died']
    if resolved: print(f"Graduation rate among resolved outcomes: {total['graduated'] / resolved * 100:.1f}%")
    else: print("Graduation rate unavailable: no resolved outcomes.")
    if missing: print(f"Passing rows missing valid curve percentage: {missing}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Offline tokens.csv backtest")
    parser.add_argument("csv_path", nargs="?", default="tokens.csv")
    run(parser.parse_args().csv_path)
