-- ============ STAGING: load clean CSVs ============
CREATE OR REPLACE TABLE stg_cases     AS SELECT * FROM read_csv_auto('output/clean/cases.csv');
CREATE OR REPLACE TABLE stg_donations AS SELECT * FROM read_csv_auto('output/clean/donations.csv');
CREATE OR REPLACE TABLE stg_expenses  AS SELECT * FROM read_csv_auto('output/clean/expenses.csv');
CREATE OR REPLACE TABLE stg_marketing AS SELECT * FROM read_csv_auto('output/clean/marketing.csv');

-- ============ DIMENSIONS ============
CREATE OR REPLACE TABLE dim_date AS
SELECT CAST(strftime(d, '%Y%m%d') AS INTEGER) AS date_key,
       CAST(d AS DATE)            AS date,
       year(d)                    AS year,
       quarter(d)                 AS quarter,
       month(d)                   AS month,
       strftime(d, '%b')          AS month_name,
       strftime(d, '%Y-%m')       AS year_month
FROM range(DATE '2025-01-01', DATE '2027-01-01', INTERVAL 1 DAY) AS t(d);

CREATE OR REPLACE TABLE dim_program AS
SELECT program,
       program IN ('Advocacy', 'Volunteer Training') AS is_program_service
FROM (SELECT DISTINCT program FROM stg_expenses);

CREATE OR REPLACE TABLE dim_campaign AS
SELECT campaign
FROM (SELECT campaign FROM stg_donations UNION SELECT campaign FROM stg_marketing);

CREATE OR REPLACE TABLE dim_channel AS
SELECT channel,
       channel IN ('Google Ads', 'Meta') AS is_paid
FROM (SELECT channel FROM stg_donations UNION SELECT channel FROM stg_marketing);

CREATE OR REPLACE TABLE dim_county AS
SELECT DISTINCT county FROM stg_cases;

CREATE OR REPLACE TABLE dim_donor AS
SELECT donor_key,
       any_value(donor_name)  AS donor_name,
       min(donation_date)     AS first_gift_date
FROM stg_donations
GROUP BY donor_key;

-- ============ FACTS ============
CREATE OR REPLACE TABLE fact_donation AS
SELECT donation_id,
       CAST(strftime(donation_date, '%Y%m%d') AS INTEGER) AS date_key,
       donor_key, campaign, channel, amount, is_refund
FROM stg_donations;

CREATE OR REPLACE TABLE fact_expense AS
SELECT expense_id,
       CAST(strftime(txn_date, '%Y%m%d') AS INTEGER) AS date_key,
       source, account, program, amount_usd
FROM stg_expenses;

CREATE OR REPLACE TABLE fact_case AS
SELECT case_id, child_id, county, volunteer_id,
       CAST(strftime(date_assigned, '%Y%m%d') AS INTEGER) AS assigned_date_key,
       CAST(strftime(date_closed,   '%Y%m%d') AS INTEGER) AS closed_date_key,
       status, outcome, days_to_close, valid_dates
FROM stg_cases;

CREATE OR REPLACE TABLE fact_marketing_daily AS
SELECT CAST(strftime(date, '%Y%m%d') AS INTEGER) AS date_key,
       channel, campaign, spend, impressions, clicks, sessions, conversions
FROM stg_marketing;