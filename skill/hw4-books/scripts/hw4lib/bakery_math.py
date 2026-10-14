"""Deterministic bakery finance math. No LLM anywhere in this file.

Two jobs:
  1. statements(): the SERVICE we offer. Turn a list of transactions into a simple
     income statement + cash summary + a few ratio flags.
  2. projection(): the TASK we delegate. Given the synthetic dataset and the agreed
     formulas, compute ending cash per month per scenario, so a provider's answer can
     be recomputed and compared number by number.

Money is handled with Decimal and rounded half-up to cents only at the end.
"""
import csv
import io
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")


def money(x):
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------- delegated task
# The synthetic dataset for the fictional bakery "Pan y Mas". Nothing here is real.
SYNTHETIC = {
    "business": "Pan y Mas (fictional bakery, synthetic numbers)",
    "currency": "USD",
    "starting_cash": "8400.00",
    "sales_prev_month": "13500.00",          # month 0, only used for the collection lag
    "sales": ["14200.00", "15600.00", "13900.00"],   # months 1..3
    "ingredient_ratio": "0.34",             # ingredient cost as a share of sales
    "fixed_costs": {"rent": "2400.00", "payroll": "4100.00", "utilities": "620.00", "other": "380.00"},
    "collected_same_month": "0.90",         # share of a month's sales collected that month
    "collected_next_month": "0.10",         # the rest arrives the following month (card settlement)
}
SCENARIOS = {
    "base": {"sales_mult": "1.00", "ratio_add": "0.00"},
    "sales_up_10": {"sales_mult": "1.10", "ratio_add": "0.00"},
    "ingredients_up_5pp": {"sales_mult": "1.00", "ratio_add": "0.05"},
}
TOLERANCE = Decimal("1.00")   # a provider's number is accepted if within +/- 1.00 of ours


def projection(data=SYNTHETIC, scenarios=SCENARIOS):
    """Return {scenario: {"ending_cash": [m1, m2, m3], "net_income_total": x, "rows": [...]}}."""
    out = {}
    fixed = sum(Decimal(v) for v in data["fixed_costs"].values())
    for name, sc in scenarios.items():
        mult, ratio = Decimal(sc["sales_mult"]), Decimal(data["ingredient_ratio"]) + Decimal(sc["ratio_add"])
        prev_sales = Decimal(data["sales_prev_month"])  # scenario does NOT change the month-0 sales
        cash = Decimal(data["starting_cash"])
        rows, ending, ni_total = [], [], Decimal(0)
        for m, s in enumerate(data["sales"], start=1):
            sales = Decimal(s) * mult
            ingredients = sales * ratio
            net_income = sales - ingredients - fixed
            cash_in = sales * Decimal(data["collected_same_month"]) + prev_sales * Decimal(data["collected_next_month"])
            cash_out = ingredients + fixed
            cash = cash + cash_in - cash_out
            rows.append({"month": m, "sales": money(sales), "ingredients": money(ingredients), "fixed": money(fixed),
                         "net_income": money(net_income), "cash_in": money(cash_in), "cash_out": money(cash_out),
                         "ending_cash": money(cash)})
            ending.append(money(cash))
            ni_total += net_income
            prev_sales = sales
        out[name] = {"ending_cash": ending, "net_income_total": money(ni_total), "rows": rows}
    return out


def expected_lines(proj=None):
    """The exact machine-readable lines we ask the provider to include in its answer."""
    proj = proj or projection()
    lines = []
    for name, r in proj.items():
        for m, v in enumerate(r["ending_cash"], start=1):
            lines.append(f"{name},{m},{v}")
        lines.append(f"{name},net_income_total,{r['net_income_total']}")
    return lines


def parse_result_lines(text):
    """Pull `scenario,month,value` lines out of free-form forum text. Returns {(scenario, key): Decimal}.
    Tolerant of surrounding prose, $ signs, thousands separators and markdown table pipes."""
    found = {}
    for raw in text.splitlines():
        line = raw.strip().strip("|").replace("|", ",")
        parts = [p.strip().strip("`*").replace("$", "").replace(",", "") for p in _split_commas(line)]
        if len(parts) != 3 or parts[0] not in SCENARIOS:
            continue
        key = parts[1].lower()
        if key not in ("1", "2", "3", "net_income_total"):
            continue
        try:
            found[(parts[0], key)] = money(Decimal(parts[2]))
        except (InvalidOperation, ValueError):
            continue
    return found


def _split_commas(line):
    # "sales_up_10,2,12,345.67" -> keep thousands separators inside the number
    parts = [p.strip() for p in line.split(",")]
    if len(parts) > 3 and parts[0].strip("`*") in SCENARIOS:
        return [parts[0], parts[1], "".join(parts[2:])]
    return parts


def verify(text, values=None, proj=None, tolerance=TOLERANCE):
    """Compare a provider's numbers with ours. `values` (optional) are numbers extracted by the agent
    when the provider didn't use the requested line format; they go through the same recomputation.
    Returns a dict with ok, checks (one per expected number), missing, mismatches."""
    proj = proj or projection()
    got = parse_result_lines(text)
    if values:
        for k, v in values.items():
            sc, key = k.split(",", 1)
            try:
                got.setdefault((sc, key.strip()), money(Decimal(str(v).replace("$", "").replace(",", ""))))
            except (InvalidOperation, ValueError):
                pass
    checks, missing, mismatches = [], [], []
    for name, r in proj.items():
        expected = {str(m): v for m, v in enumerate(r["ending_cash"], start=1)}
        expected["net_income_total"] = r["net_income_total"]
        for key, exp in expected.items():
            theirs = got.get((name, key))
            if theirs is None:
                missing.append(f"{name},{key}")
                checks.append({"scenario": name, "key": key, "expected": str(exp), "got": None, "ok": False})
                continue
            ok = abs(theirs - exp) <= tolerance
            checks.append({"scenario": name, "key": key, "expected": str(exp), "got": str(theirs), "ok": ok,
                           "diff": str(money(theirs - exp))})
            if not ok:
                mismatches.append(f"{name},{key}: got {theirs}, recomputed {exp} (diff {money(theirs - exp)})")
    return {"ok": not missing and not mismatches, "checked": len(checks), "missing": missing,
            "mismatches": mismatches, "checks": checks, "tolerance": str(tolerance)}


# ---------------------------------------------------------------- offered service
def statements(csv_text, opening_cash="0"):
    """Income statement + cash summary from transactions.

    Input: CSV with header date,description,category,amount. Positive amount = money in,
    negative = money out. Category is free text; rows whose category contains 'sale' or
    'revenue' count as revenue, everything else is an expense (or other income if positive).
    """
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    need = {"date", "description", "category", "amount"}
    if not reader.fieldnames or not need.issubset({f.strip().lower() for f in reader.fieldnames}):
        raise ValueError("CSV header must be: date,description,category,amount")
    revenue, other_income, expenses, n = Decimal(0), Decimal(0), {}, 0
    for row in reader:
        row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        if not row.get("amount"):
            continue
        try:
            amt = Decimal(row["amount"].replace("$", "").replace(",", ""))
        except InvalidOperation:
            raise ValueError(f"bad amount on row {n + 1}: {row['amount']!r}")
        n += 1
        cat = row.get("category", "").lower() or "uncategorized"
        if amt >= 0:
            if "sale" in cat or "revenue" in cat:
                revenue += amt
            else:
                other_income += amt
        else:
            expenses[cat] = expenses.get(cat, Decimal(0)) + (-amt)
    total_exp = sum(expenses.values(), Decimal(0))
    net = revenue + other_income - total_exp
    ingredients = sum(v for k, v in expenses.items() if any(w in k for w in ("ingredient", "flour", "supplies", "cogs", "food")))
    flags = []
    if revenue > 0:
        pct = ingredients / revenue
        flags.append(f"ingredient_share_of_sales={money(pct * 100)}% (typical bakery range 25-35%)")
        if pct > Decimal("0.35"):
            flags.append("ingredient costs above the typical range: check portioning, waste and supplier prices")
        elif pct < Decimal("0.25"):
            flags.append("ingredient costs unusually low: check that every ingredient purchase was recorded")
    if net < 0:
        flags.append("net loss this period: cash is shrinking")
    return {
        "transactions": n,
        "revenue": str(money(revenue)),
        "other_income": str(money(other_income)),
        "expenses_by_category": {k: str(money(v)) for k, v in sorted(expenses.items())},
        "total_expenses": str(money(total_exp)),
        "net_income": str(money(net)),
        "opening_cash": str(money(opening_cash)),
        "closing_cash": str(money(Decimal(str(opening_cash)) + net)),
        "flags": flags,
    }


def statements_markdown(s):
    lines = ["| Line | Amount |", "|---|---|", f"| Revenue | {s['revenue']} |"]
    if Decimal(s["other_income"]) != 0:
        lines.append(f"| Other income | {s['other_income']} |")
    for k, v in s["expenses_by_category"].items():
        lines.append(f"| Expense: {k} | ({v}) |")
    lines += [f"| **Net income** | **{s['net_income']}** |", f"| Opening cash | {s['opening_cash']} |",
              f"| Closing cash | {s['closing_cash']} |"]
    return "\n".join(lines)
