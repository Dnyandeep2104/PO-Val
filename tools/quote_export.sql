-- Export quote data for local testing.
--
-- Mirrors the prototype's working query exactly: the four data columns
-- are selected WITHOUT a table prefix, because that is what worked.
-- (Adding "line." to them fails: opportunity and account are not on the
-- line table.)
--
-- Run in Databricks or the Snowflake UI, download as CSV, then:
--     python3 tools/quotes_from_csv.py <the-file>.csv
--     python3 run_local.py --quotes my_quotes.json --verbose
--
-- No quantity column yet, so quantity comparison skips; part numbers,
-- totals and the Opportunity are all checked. Once the real column names
-- are known, quantity / currency / status get added here.

SELECT
      quote.name            AS quote_number
    , cafsl_part_number_c   AS part_number
    , pi_total_price_c      AS total_price
    , cafsl_opportunity_c   AS opportunity_id
    , cafsl_account_c       AS account_id
FROM PRD_ENT_RAW.SALESFORCE.CAFSL_ORACLE_QUOTE_C quote
LEFT JOIN PRD_ENT_RAW.SALESFORCE.CAFSL_ORACLE_QUOTE_LINE_ITEM_C line
       ON quote.id = line.cafsl_oracle_quote_c
WHERE quote.name IN ('F5Q-01007707', 'F5Q-00986557', 'F5Q-00972677',
                     'F5Q-01062638', 'F5Q-00974839', 'F5Q-01083998')
  AND line.is_deleted = false
ORDER BY quote.name, cafsl_part_number_c;
