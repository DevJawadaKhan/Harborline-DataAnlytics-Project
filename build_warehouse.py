import duckdb
from pathlib import Path

Path("warehouse/parquet").mkdir(parents=True, exist_ok=True)
con = duckdb.connect("warehouse/harborline.duckdb")
con.execute(Path("warehouse/warehouse.sql").read_text())

tables = ["dim_date", "dim_program", "dim_campaign", "dim_channel", "dim_county", "dim_donor",
          "fact_donation", "fact_expense", "fact_case", "fact_marketing_daily"]
for t in tables:
    con.execute(f"COPY {t} TO 'warehouse/parquet/{t}.parquet' (FORMAT PARQUET)")
    print(f"{t:22} {con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:>6} rows")

print("\nKPI CHECKS (Power BI must show the same numbers)")
checks = {
    "Net revenue":            "SELECT SUM(amount) FROM fact_donation",
    "Total expense":          "SELECT SUM(amount_usd) FROM fact_expense",
    "Programme expense ratio":"""SELECT SUM(CASE WHEN p.is_program_service THEN amount_usd END) / SUM(amount_usd)
                                 FROM fact_expense e JOIN dim_program p USING (program)""",
    "Children served":        "SELECT COUNT(DISTINCT child_id) FROM fact_case",
    "Unique donors":          "SELECT COUNT(*) FROM dim_donor",
    "Avg days to close":      "SELECT AVG(days_to_close) FROM fact_case WHERE valid_dates",
}
for name, sql in checks.items():
    print(f"  {name:25} {con.execute(sql).fetchone()[0]:,.2f}")
# Export all tables into one Excel file so Power BI can load them in one step
import pandas as pd
with pd.ExcelWriter("warehouse/harborline_warehouse.xlsx") as xl:
    for t in tables:
        con.execute(f"SELECT * FROM {t}").df().to_excel(xl, sheet_name=t, index=False)
print("Excel export done: warehouse/harborline_warehouse.xlsx")

con.close()
