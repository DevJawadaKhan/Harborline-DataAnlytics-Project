"""Harborline Community Trust: clean the raw exports and log every data quality issue.

Reads the 5 raw CSVs in raw/, cleans them, and writes:
  output/clean/cases.csv, donations.csv, expenses.csv, marketing.csv
  output/data_quality_report.csv   one row per logged issue
  output/data_quality_summary.csv  issue counts per source / issue / severity / action
  output/before_after.csv          raw rows vs clean rows per source
  output/dq_summary.json           headline numbers for Power Automate

Nothing is silently deleted: every fix or exclusion is logged with
source, issue, severity, record_id and action.
  critical = excluded from KPIs or logically impossible
  warning  = auto-corrected
  info     = noted, no change

Run from the project root:
    python clean.py
"""

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

# =============================================================================
# 1. Settings
# =============================================================================

RAW_DIR = Path("raw")
OUTPUT_DIR = Path("output")
CLEAN_DIR = OUTPUT_DIR / "clean"

# Assumption: fixed exchange rate for GBP card transactions
FX_GBP_TO_USD = 1.27

VALID_CAMPAIGNS = [
    "Spring Gala 2025",
    "Light of Hope 2025",
    "Year End Appeal 2025",
    "Spring Gala 2026",
    "Superhero Run 2026",
    "General Fund",
]
# 'spring-gala-2025' -> 'Spring Gala 2025'
CAMPAIGN_BY_SLUG = {c.lower().replace(" ", "-"): c for c in VALID_CAMPAIGNS}

DATE_FORMAT = "%Y-%m-%d"  # how dates are written in the clean outputs


# =============================================================================
# 2. Issue log and helpers
# =============================================================================

issues = []  # every fix or exclusion, one dict per record


def log(source, issue, severity, record_ids, action):
    """Add one log entry per record id."""
    for record_id in record_ids:
        issues.append({
            "source": source,
            "issue": issue,
            "severity": severity,
            "record_id": record_id,
            "action": action,
        })


def read(filename):
    """Read a raw CSV as text only, so blanks stay '' and nothing is auto-converted."""
    return pd.read_csv(RAW_DIR / filename, dtype=str, keep_default_na=False)


def strip_all(df):
    """Trim leading/trailing spaces in every column."""
    return df.apply(lambda col: col.str.strip())


def remove_duplicates(df, source, id_col):
    """Drop exact duplicate rows and log them as critical."""
    dupes = df.duplicated()
    log(source, "exact duplicate row", "critical", df.loc[dupes, id_col], "removed")
    return df[~dupes].copy()


def to_date(df, col, source, id_col):
    """Parse mixed date formats; log non-blank values that can't be parsed."""
    parsed = pd.to_datetime(df[col], format="mixed", errors="coerce")
    bad = parsed.isna() & df[col].ne("")
    log(source, f"unparseable {col}", "critical", df.loc[bad, id_col], "set to blank")
    return parsed


def to_number(series):
    """'$1,250.00', 'USD 900', '-$45.10' -> 1250.0, 900.0, -45.1; blank -> NaN."""
    return pd.to_numeric(series.str.replace(r"[^0-9.\-]", "", regex=True), errors="coerce")


def to_program(series):
    """'Prog - Advocacy', 'ADVOCACY', 'advocacy' -> 'Advocacy'."""
    return series.str.replace(r"(?i)^prog\s*-\s*", "", regex=True).str.strip().str.title()


def to_slug(series):
    """'Spring Gala 2025', 'spring_gala_2025', 'SPRING GALA 2025' -> 'spring-gala-2025'."""
    return series.str.strip().str.lower().str.replace(r"[\s_]+", "-", regex=True)


# =============================================================================
# 3. Case management
# =============================================================================

def clean_cases(raw):
    src = "case_management"
    df = remove_duplicates(strip_all(raw), src, "case_id")

    # County: 'HARDIN', 'hardin', 'Hardin County' -> 'Hardin'
    county = df["county"].str.replace(r"(?i)\s*county$", "", regex=True).str.title()
    changed = county != df["county"]
    log(src, "inconsistent county spelling", "warning", df.loc[changed, "case_id"], "standardised")
    df["county"] = county

    # Status: 'OPEN', 'open' -> 'Open'
    status = df["status"].str.title()
    changed = status != df["status"]
    log(src, "inconsistent status casing", "warning", df.loc[changed, "case_id"], "title cased")
    df["status"] = status

    # Blank volunteer: case not yet assigned to a volunteer
    blank = df["volunteer_id"].eq("")
    log(src, "blank volunteer_id", "warning", df.loc[blank, "case_id"], "set to Unassigned")
    df.loc[blank, "volunteer_id"] = "Unassigned"

    df["date_assigned"] = to_date(df, "date_assigned", src, "case_id")
    df["date_closed"] = to_date(df, "date_closed", src, "case_id")

    # A case can't close before it was assigned: keep the row, but flag it
    impossible = df["date_closed"] < df["date_assigned"]
    log(src, "date_closed before date_assigned", "critical", df.loc[impossible, "case_id"],
        "kept, valid_dates = False, excluded from days_to_close")
    df["valid_dates"] = ~impossible

    # Days to close: blank for open cases and for impossible dates
    df["days_to_close"] = (df["date_closed"] - df["date_assigned"]).dt.days
    df.loc[~df["valid_dates"], "days_to_close"] = pd.NA
    df["days_to_close"] = df["days_to_close"].astype("Int64")
    return df


# =============================================================================
# 4. Donations (CRM)
# =============================================================================

def clean_donations(raw):
    src = "crm_donations"
    df = raw.copy()

    # Donor key: built from the raw email so whitespace/case fixes are logged too
    df["donor_key"] = df["donor_email"].str.strip().str.lower()
    changed = df["donor_key"] != df["donor_email"]
    log(src, "email case or spacing differs", "warning", df.loc[changed, "donation_id"],
        "donor_key = trimmed lower-case email")

    df = strip_all(df)
    df = remove_duplicates(df, src, "donation_id")

    # Amount: text -> number
    df["amount"] = to_number(df["amount"])
    blank = df["amount"].isna()
    log(src, "blank amount", "critical", df.loc[blank, "donation_id"], "excluded from revenue")
    df = df[~blank].copy()

    # Refunds stay negative so revenue totals are net
    df["is_refund"] = df["amount"] < 0
    log(src, "negative amount (refund)", "info", df.loc[df["is_refund"], "donation_id"],
        "kept as negative, is_refund = True")

    # Campaign: every spelling -> one valid name
    campaign = to_slug(df["campaign"]).map(CAMPAIGN_BY_SLUG)
    unknown = campaign.isna()
    log(src, "unknown campaign", "critical", df.loc[unknown, "donation_id"], "set to blank")
    changed = campaign.notna() & (campaign != df["campaign"])
    log(src, "inconsistent campaign spelling", "warning", df.loc[changed, "donation_id"],
        "mapped to valid campaign name")
    df["campaign"] = campaign

    df["donation_date"] = to_date(df, "donation_date", src, "donation_id")
    df = df.rename(columns={"source_channel": "channel"})
    return df


# =============================================================================
# 5. Ramp card spend
# =============================================================================

def clean_ramp(raw):
    src = "ramp_card_spend"
    df = remove_duplicates(strip_all(raw), src, "ramp_txn_id")

    # Department -> program
    df["program"] = to_program(df["department"])
    changed = df["program"] != df["department"]
    log(src, "non-standard department name", "warning", df.loc[changed, "ramp_txn_id"],
        "mapped to program")

    df["amount"] = to_number(df["amount"])
    bad = df["amount"].isna()
    log(src, "blank or invalid amount", "critical", df.loc[bad, "ramp_txn_id"], "excluded from expenses")
    df = df[~bad].copy()

    # Currency: convert GBP to USD (original amount and currency are kept)
    gbp = df["currency"].eq("GBP")
    log(src, "GBP transaction", "warning", df.loc[gbp, "ramp_txn_id"],
        f"converted to USD at {FX_GBP_TO_USD}")
    df["amount_usd"] = (df["amount"] * df["currency"].map({"USD": 1.0, "GBP": FX_GBP_TO_USD})).round(2)

    df["txn_date"] = to_date(df, "txn_date", src, "ramp_txn_id")
    return df


# =============================================================================
# 6. QuickBooks general ledger
# =============================================================================

def clean_quickbooks(raw):
    """Return (QuickBooks rows that are not Ramp copies, total amount of the Ramp copies)."""
    src = "quickbooks_gl"
    df = remove_duplicates(strip_all(raw), src, "qb_txn_id")

    df["amount"] = to_number(df["amount"])
    bad = df["amount"].isna()
    log(src, "blank or invalid amount", "critical", df.loc[bad, "qb_txn_id"], "excluded from expenses")
    df = df[~bad].copy()

    # Double counting: card transactions are synced from Ramp ("Ramp sync RMP-700123").
    # Ramp is the source of truth, so these copies come out of QuickBooks.
    synced = df["memo"].str.contains(r"Ramp sync RMP-\d+", case=False, regex=True)
    double_count = df.loc[synced, "amount"].sum()
    log(src, "Ramp transaction synced into QuickBooks (double count)", "critical",
        df.loc[synced, "qb_txn_id"], "excluded, Ramp is the source of truth for card spend")
    df = df[~synced].copy()

    # Class -> program; blank class -> 'Unallocated'
    df["program"] = to_program(df["class"])
    blank = df["program"].eq("")
    log(src, "blank class", "warning", df.loc[blank, "qb_txn_id"], "set to Unallocated")
    df.loc[blank, "program"] = "Unallocated"
    changed = ~blank & (df["program"] != df["class"])
    log(src, "non-standard class name", "warning", df.loc[changed, "qb_txn_id"], "mapped to program")

    df["txn_date"] = to_date(df, "txn_date", src, "qb_txn_id")
    return df, double_count


# =============================================================================
# 7. Combined expenses (QuickBooks non-card + Ramp card spend)
# =============================================================================

def build_expenses(qb, ramp):
    exp_qb = pd.DataFrame({
        "expense_id": qb["qb_txn_id"],
        "txn_date": qb["txn_date"],
        "source": "QuickBooks",
        "account": qb["account"],
        "program": qb["program"],
        "amount_usd": qb["amount"],
    })
    exp_ramp = pd.DataFrame({
        "expense_id": ramp["ramp_txn_id"],
        "txn_date": ramp["txn_date"],
        "source": "Ramp",
        "account": "Card Spend",
        "program": ramp["program"],
        "amount_usd": ramp["amount_usd"],
    })
    return pd.concat([exp_qb, exp_ramp], ignore_index=True)


# =============================================================================
# 8. Marketing
# =============================================================================

def clean_marketing(raw):
    src = "marketing_daily"
    df = strip_all(raw)
    # Marketing has no ID column, so date|platform|campaign identifies a row
    df["record_id"] = df["date"] + "|" + df["platform"] + "|" + df["utm_campaign"]
    df = remove_duplicates(df, src, "record_id")

    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")
    bad = df["date"].isna()
    log(src, "unparseable date", "critical", df.loc[bad, "record_id"], "set to blank")

    # 'spring-gala-2025' -> 'Spring Gala 2025'
    df["campaign"] = to_slug(df["utm_campaign"]).map(CAMPAIGN_BY_SLUG)
    unknown = df["campaign"].isna()
    log(src, "unknown utm_campaign", "critical", df.loc[unknown, "record_id"], "campaign set to blank")

    df = df.rename(columns={"platform": "channel", "donate_page_conversions": "conversions"})

    # Numbers: blank spend is left blank (not imputed) and logged
    for col in ["spend", "impressions", "clicks", "sessions", "conversions"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    blank = df["spend"].isna()
    log(src, "blank spend", "warning", df.loc[blank, "record_id"], "left blank, not imputed")

    # Gaps: every channel should report every day in the date range
    all_days = pd.date_range(df["date"].min(), df["date"].max(), freq="D")
    expected = pd.MultiIndex.from_product([all_days, df["channel"].unique()], names=["date", "channel"])
    present = pd.MultiIndex.from_frame(df[["date", "channel"]].drop_duplicates())
    missing = expected.difference(present)
    log(src, "no data for date + channel", "info",
        [f"{d:%Y-%m-%d}|{ch}" for d, ch in missing], "noted, no row created")

    columns = ["date", "channel", "campaign", "utm_campaign", "spend",
               "impressions", "clicks", "sessions", "conversions"]
    return df[columns]


# =============================================================================
# 9. Run everything and save outputs
# =============================================================================

def main():
    run_time = datetime.now().isoformat(timespec="seconds")
    CLEAN_DIR.mkdir(parents=True, exist_ok=True)

    raw = {
        "case_management": read("case_management_export.csv"),
        "crm_donations": read("crm_donations.csv"),
        "ramp_card_spend": read("ramp_card_spend.csv"),
        "quickbooks_gl": read("quickbooks_gl.csv"),
        "marketing_daily": read("marketing_daily.csv"),
    }

    cases = clean_cases(raw["case_management"])
    donations = clean_donations(raw["crm_donations"])
    ramp = clean_ramp(raw["ramp_card_spend"])
    qb, double_count = clean_quickbooks(raw["quickbooks_gl"])
    expenses = build_expenses(qb, ramp)
    marketing = clean_marketing(raw["marketing_daily"])

    # --- Clean tables -------------------------------------------------------
    for name, df in {"cases": cases, "donations": donations,
                     "expenses": expenses, "marketing": marketing}.items():
        df.to_csv(CLEAN_DIR / f"{name}.csv", index=False, date_format=DATE_FORMAT)

    # --- Data quality report and summary ------------------------------------
    report = pd.DataFrame(issues, columns=["source", "issue", "severity", "record_id", "action"])
    report.to_csv(OUTPUT_DIR / "data_quality_report.csv", index=False)

    summary = (
        report.groupby(["source", "issue", "severity", "action"]).size()
        .reset_index(name="rows")
        .sort_values(["source", "rows"], ascending=[True, False])
    )
    summary.to_csv(OUTPUT_DIR / "data_quality_summary.csv", index=False)

    # --- Before / after row counts ------------------------------------------
    clean_rows = {
        "case_management": len(cases),
        "crm_donations": len(donations),
        "ramp_card_spend": len(ramp),
        "quickbooks_gl": len(qb),
        "marketing_daily": len(marketing),
    }
    before_after = pd.DataFrame({
        "source": list(raw),
        "raw_rows": [len(df) for df in raw.values()],
        "clean_rows": [clean_rows[s] for s in raw],
    })
    before_after["rows_removed"] = before_after["raw_rows"] - before_after["clean_rows"]
    before_after.to_csv(OUTPUT_DIR / "before_after.csv", index=False)

    # --- JSON summary for Power Automate ------------------------------------
    true_total = expenses["amount_usd"].sum()
    dq_summary = {
        "run_time": run_time,
        "rows_flagged": int(report[["source", "record_id"]].drop_duplicates().shape[0]),
        "critical_issues": int((report["severity"] == "critical").sum()),
        "double_counted_amount": round(float(double_count), 2),
        "true_total_expense_usd": round(float(true_total), 2),
    }
    (OUTPUT_DIR / "dq_summary.json").write_text(json.dumps(dq_summary, indent=2))

    # --- Console summary ----------------------------------------------------
    print("Row counts (raw -> clean)")
    print(before_after.to_string(index=False))
    print(f"expenses (combined): {len(expenses):,} rows")

    print("\nData quality issues")
    print(summary.to_string(index=False))

    print("\nExpenses")
    print(f"  QuickBooks + Ramp simply added: ${true_total + double_count:,.0f}")
    print(f"  True total:                     ${true_total:,.0f}")
    print(f"  Overstated by:                  ${double_count:,.0f} ({double_count / true_total:.1%})")

    print("\nDonors")
    print(f"  Raw unique emails:  {raw['crm_donations']['donor_email'].nunique():,}")
    print(f"  Real unique donors: {donations['donor_key'].nunique():,}")


if __name__ == "__main__":
    main()
