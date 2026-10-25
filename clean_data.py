"""Clean every CSV in raw/ and write the results to warehouse/.

For each source file this script:
  - trims whitespace and fixes inconsistent text (case, spelling variants)
  - parses mixed date formats and "$"/"USD" text amounts
  - removes exact duplicate rows
  - removes invalid rows and outliers

Nothing is silently thrown away: every removed row is written to
warehouse/rejected/<file>.csv with `reject_reason` and `severity` columns.
Rows that are kept but worth a look are written to warehouse/flagged/<file>.csv.

Run from the project root:
    python clean_data.py
"""

from pathlib import Path

import numpy as np
import pandas as pd

RAW_DIR = Path("raw")
CLEAN_DIR = Path("warehouse")
REJECTED_DIR = CLEAN_DIR / "rejected"
FLAGGED_DIR = CLEAN_DIR / "flagged"

# Ramp has some GBP transactions. The source data has no exchange rate
# (QuickBooks books them at face value), so this is an assumed fixed rate.
# Update it if finance gives you the rate they use.
FX_GBP_TO_USD = 1.27

# Outlier rule: a value is an outlier if its log is more than
# OUTLIER_IQR_K interquartile ranges above the middle 50% of its group.
# Money is right-skewed, so the log scale stops large-but-normal values
# (e.g. a $1,000 donation) from being flagged.
OUTLIER_IQR_K = 1.5

# The three date formats found in the raw files
DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y"]

# The four programs that QuickBooks `class` and Ramp `department` should map to
PROGRAMS = {"Advocacy", "Fundraising", "Admin", "Volunteer Training"}

# QuickBooks lines with this memo are copies of Ramp card transactions
RAMP_SYNC_REASON = "Ramp sync: excluded, Ramp is the source of truth for card spend"

# Canonical campaign names used in crm_donations
CAMPAIGNS = [
    "Spring Gala 2025",
    "Spring Gala 2026",
    "Light of Hope 2025",
    "Year End Appeal 2025",
    "Superhero Run 2026",
    "General Fund",
]
# 'spring-gala-2026' -> 'Spring Gala 2026' (the slug also matches marketing utm_campaign)
CAMPAIGN_BY_SLUG = {c.lower().replace(" ", "-"): c for c in CAMPAIGNS}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def strip_text(df):
    """Trim leading/trailing spaces in every text column and turn '' into NaN."""
    df = df.copy()
    for col in df.select_dtypes("object"):
        df[col] = df[col].str.strip().replace("", np.nan)
    return df


def parse_dates(series):
    """Parse a column that mixes 2025-09-08, 04/30/2025 and 25-Oct-2025.

    Each format is tried explicitly (rather than letting pandas guess) so
    04/05/2025 is always read as April 5th, never May 4th.
    """
    result = pd.Series(pd.NaT, index=series.index)
    for fmt in DATE_FORMATS:
        parsed = pd.to_datetime(series, format=fmt, errors="coerce")
        result = result.fillna(parsed)
    return result


def parse_amount(series):
    """Turn text amounts like '$1,234.56', 'USD 24' or '-$12.50' into floats.

    Everything except digits, '.' and '-' is removed; blanks become NaN.
    """
    cleaned = series.astype(str).str.replace(r"[^0-9.\-]", "", regex=True)
    return pd.to_numeric(cleaned, errors="coerce")


def to_program(series):
    """Map 'ADVOCACY', 'Prog - Advocacy', 'advocacy' etc. to 'Advocacy'."""
    return (
        series.str.strip()
        .str.replace(r"(?i)^prog\s*-\s*", "", regex=True)
        .str.title()
    )


def outlier_mask(df, value_col, group_col=None):
    """Return True for rows whose value is an unusually HIGH outlier within its group.

    Only the high side is checked: small amounts (a $2 donation, a quiet ad day)
    are real activity, while extreme highs are the values that distort totals.
    """
    def is_outlier(values):
        logged = np.log(values.where(values > 0))
        q1, q3 = logged.quantile([0.25, 0.75])
        return logged > q3 + OUTLIER_IQR_K * (q3 - q1)

    if group_col is None:
        return is_outlier(df[value_col])
    return df.groupby(group_col)[value_col].transform(is_outlier).astype(bool)


class Rejects:
    """Collects removed rows and flagged rows (with the reason) so they can be reviewed."""

    def __init__(self):
        self.frames = []
        self.flagged_frames = []

    def take(self, df, mask, reason, severity="critical"):
        """Move rows where `mask` is True out of `df` and record why."""
        mask = mask.fillna(False)
        if mask.any():
            self.frames.append(df[mask].assign(reject_reason=reason, severity=severity))
        return df[~mask]

    def flag(self, df, mask, reason, severity="warning"):
        """Record rows where `mask` is True without removing them."""
        mask = mask.fillna(False)
        if mask.any():
            self.flagged_frames.append(df[mask].assign(flag_reason=reason, severity=severity))

    def to_frame(self):
        return pd.concat(self.frames) if self.frames else pd.DataFrame()

    def flagged_frame(self):
        return pd.concat(self.flagged_frames) if self.flagged_frames else pd.DataFrame()


# ---------------------------------------------------------------------------
# One cleaning function per source file
# ---------------------------------------------------------------------------

def clean_case_management(df, rejects):
    df = strip_text(df)
    df = rejects.take(df, df.duplicated(), "exact duplicate row")

    # County: 'HARDIN', 'hardin', 'Hardin County' -> 'Hardin'
    df["county"] = (
        df["county"].str.replace(r"\s*county$", "", case=False, regex=True).str.title()
    )

    # Status: 'OPEN' / 'open' / 'Open' -> 'Open'
    df["status"] = df["status"].str.title()

    df["date_assigned"] = parse_dates(df["date_assigned"])
    df["date_closed"] = parse_dates(df["date_closed"])

    # A case can't close before it was assigned
    df = rejects.take(df, df["date_closed"] < df["date_assigned"], "closed before assigned")

    # Closed cases must have a close date, open cases must not
    inconsistent = (df["status"].eq("Closed") & df["date_closed"].isna()) | (
        df["status"].eq("Open") & df["date_closed"].notna()
    )
    df = rejects.take(df, inconsistent, "status does not match date_closed")

    # Blank volunteer_id is a real state (no volunteer assigned yet), so keep it
    df["volunteer_id"] = df["volunteer_id"].fillna("Unassigned")

    df["days_open"] = (
        df["date_closed"].fillna(pd.Timestamp.today().normalize()) - df["date_assigned"]
    ).dt.days
    return df


def clean_crm_donations(df, rejects):
    # Donor key: the same donor appears as 'Donor1@Gmail.com ' and 'donor1@gmail.com',
    # so build it from the raw email before any trimming, to log every row it changes
    donor_key = df["donor_email"].str.strip().str.lower()
    email_changed = donor_key != df["donor_email"]

    df = strip_text(df)
    df["donor_key"] = donor_key

    # Duplicates: exact copies of a whole row (all share a donation_id with the original)
    df = rejects.take(df, df.duplicated(), "exact duplicate row", "critical")

    # Amount: '$1,234.56' / 'USD 24' -> 1234.56 / 24.0; blanks become NaN
    df["amount"] = parse_amount(df["amount"])

    # Blank amounts can't count towards revenue
    df = rejects.take(df, df["amount"].isna(), "missing amount: excluded from revenue", "critical")

    # Refunds: negative amounts are kept, so revenue totals come out net of refunds
    df["is_refund"] = df["amount"] < 0

    rejects.flag(df, email_changed.reindex(df.index), "email had different case or spaces")

    # Campaign: 'SPRING GALA 2026', 'spring_gala_2026', 'Spring Gala 2026' -> 'Spring Gala 2026'
    slug = df["campaign"].str.lower().str.replace(r"[\s_]+", "-", regex=True)
    df["campaign"] = slug.map(CAMPAIGN_BY_SLUG)
    # Key that matches marketing_daily.utm_campaign ('spring-gala-2026')
    df["campaign_key"] = slug
    unmapped = df["campaign"].isna().sum()
    assert unmapped == 0, f"{unmapped} donations have an unknown campaign"

    df["donation_date"] = parse_dates(df["donation_date"])
    df = rejects.take(df, df["donation_date"].isna(), "unparseable date", "critical")

    # Same column name as the marketing file
    df = df.rename(columns={"source_channel": "channel"})

    print("Donations:", len(df))
    print("Unique donors:", df["donor_key"].nunique())
    print(df["campaign"].value_counts())
    return df


def clean_marketing_daily(df, rejects):
    df = strip_text(df)
    df = rejects.take(df, df.duplicated(), "exact duplicate row")

    # Dates are stored as integers like 20250101
    df["date"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d")

    # Funnel must narrow: impressions >= clicks >= sessions >= conversions
    broken_funnel = (
        (df["clicks"] > df["impressions"])
        | (df["sessions"] > df["clicks"])
        | (df["donate_page_conversions"] > df["sessions"])
    )
    df = rejects.take(df, broken_funnel, "funnel counts out of order")

    # Check outliers before filling blanks, so only real recorded spend is judged
    df = rejects.take(df, outlier_mask(df, "spend", "platform"), "spend outlier")

    # Missing spend: estimate it from clicks x that platform's median cost per click,
    # rather than dropping the row (its impressions/clicks are still valid) or
    # assuming $0 (which would understate spend)
    df["spend_imputed"] = df["spend"].isna()
    median_cpc = (df["spend"] / df["clicks"]).groupby(df["platform"]).transform("median")
    df["spend"] = df["spend"].fillna((df["clicks"] * median_cpc).round(2))

    df = df.rename(columns={"utm_campaign": "campaign_key"})
    return df


def clean_quickbooks_gl(df, rejects):
    df = strip_text(df)
    df = rejects.take(df, df.duplicated(), "exact duplicate row")
    df = rejects.take(df, df["qb_txn_id"].duplicated(), "duplicate qb_txn_id")

    df["amount"] = pd.to_numeric(df["amount"])

    # Double counting: card transactions are synced from Ramp into QuickBooks
    # ("Ramp sync RMP-700123"). Adding both files would count them twice,
    # so they come out of QuickBooks and Ramp stays the source of truth.
    synced = df["memo"].str.contains("Ramp sync", case=False, na=False)
    df = rejects.take(df, synced, RAMP_SYNC_REASON, "critical")

    # Class -> program: 'ADVOCACY', 'Prog - Advocacy' -> 'Advocacy'; blanks -> 'Unallocated'
    df["program"] = to_program(df["class"])
    rejects.flag(df, df["program"].isna(), "blank class: set to Unallocated")
    df["program"] = df["program"].fillna("Unallocated")

    df["txn_date"] = parse_dates(df["txn_date"])
    df = rejects.take(df, df["txn_date"].isna(), "unparseable date", "critical")

    print("Synced rows removed:", synced.sum())
    print("QuickBooks rows left:", len(df))
    return df


def clean_ramp_card_spend(df, rejects):
    df = strip_text(df)
    df = rejects.take(df, df.duplicated(), "exact duplicate row")
    df = rejects.take(df, df["ramp_txn_id"].duplicated(), "duplicate ramp_txn_id")

    # Department -> program: 'ADVOCACY', 'Prog - Advocacy' -> 'Advocacy'
    df["program"] = to_program(df["department"])
    rejects.flag(df, df["department"] != df["program"], "department renamed to standard program")

    df["amount"] = pd.to_numeric(df["amount"])

    # Convert everything to USD so amounts can be summed; keep the original too
    is_gbp = df["currency"].eq("GBP")
    rejects.flag(df, is_gbp, f"GBP converted to USD at {FX_GBP_TO_USD}")
    rate = df["currency"].map({"USD": 1.0, "GBP": FX_GBP_TO_USD})
    df["amount_usd"] = (df["amount"] * rate).round(2)

    df["txn_date"] = parse_dates(df["txn_date"])
    df = rejects.take(df, df["txn_date"].isna(), "unparseable date", "critical")

    print("GBP rows:", is_gbp.sum())
    print("Programs:", sorted(df["program"].unique()))
    assert set(df["program"]) == PROGRAMS, "Ramp has an unexpected program name"
    return df


def build_expenses(qb, ramp):
    """Stack QuickBooks (non-card) expenses and Ramp card spend into one table."""
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


CLEANERS = {
    "case_management_export": clean_case_management,
    "crm_donations": clean_crm_donations,
    "marketing_daily": clean_marketing_daily,
    "quickbooks_gl": clean_quickbooks_gl,
    "ramp_card_spend": clean_ramp_card_spend,
}


def main():
    CLEAN_DIR.mkdir(exist_ok=True)
    REJECTED_DIR.mkdir(exist_ok=True)
    FLAGGED_DIR.mkdir(exist_ok=True)
    cleaned_files, rejected_files = {}, {}

    for name, clean in CLEANERS.items():
        print(f"\n=== {name} ===")
        raw = pd.read_csv(RAW_DIR / f"{name}.csv")
        rejects = Rejects()
        cleaned = clean(raw, rejects)
        rejected = rejects.to_frame()
        flagged = rejects.flagged_frame()
        cleaned_files[name], rejected_files[name] = cleaned, rejected

        cleaned.to_csv(CLEAN_DIR / f"{name}.csv", index=False)
        rejected.to_csv(REJECTED_DIR / f"{name}.csv", index=False)
        flagged.to_csv(FLAGGED_DIR / f"{name}.csv", index=False)

        # Summary: rows in, rows out, and why rows were removed or flagged
        print(f"{name}: {len(raw):,} raw -> {len(cleaned):,} clean, {len(rejected):,} rejected")
        if not rejected.empty:
            for reason, count in rejected["reject_reason"].value_counts().items():
                print(f"    {count:>4}  removed: {reason}")
        if not flagged.empty:
            for reason, count in flagged["flag_reason"].value_counts().items():
                print(f"    {count:>4}  flagged: {reason}")

    # One expenses table: QuickBooks without the Ramp-synced lines + all Ramp card spend
    print("\n=== expenses ===")
    expenses = build_expenses(cleaned_files["quickbooks_gl"], cleaned_files["ramp_card_spend"])
    expenses.to_csv(CLEAN_DIR / "expenses.csv", index=False)

    qb_rejected = rejected_files["quickbooks_gl"]
    double_count = qb_rejected.loc[qb_rejected["reject_reason"] == RAMP_SYNC_REASON, "amount"].sum()
    true_total = expenses["amount_usd"].sum()
    print(f"Expense rows: {len(expenses):,}")
    print(f"Wrong total (both added): ${true_total + double_count:,.0f}")
    print(f"True total:               ${true_total:,.0f}")
    print(f"Overstated by:            ${double_count:,.0f} ({double_count / true_total:.1%})")


if __name__ == "__main__":
    main()
