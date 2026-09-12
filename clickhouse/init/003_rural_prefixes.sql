-- Load the rural billed-prefix list baked into the image by
-- Dockerfile.clickhouse. Table is defined in 001_schema.sql.
--
-- INSERT ... SELECT FROM file() rather than a dictionary: the list is queried
-- with `billed_prefix IN (SELECT ...)`, which ClickHouse turns into a set and
-- pushes into the raw_cdr scan, and it needs to be joinable/aggregatable.
--
-- Safe to re-run: ReplacingMergeTree collapses duplicate billed_prefix rows.
INSERT INTO cdr.rural_prefix_list (billed_prefix, effective_date, expiry_date, created_at)
SELECT billed_prefix, effective_date, expiry_date, created_at
FROM file('rural_prefixes.csv', 'CSVWithNames',
          'billed_prefix String, effective_date Date, expiry_date Date, created_at Date');
