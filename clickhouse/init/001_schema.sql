CREATE DATABASE IF NOT EXISTS cdr;

CREATE TABLE IF NOT EXISTS cdr.raw_cdr
(
    record_key UInt64,
    entity LowCardinality(String),
    file_hour DateTime('UTC'),
    file_path String CODEC(ZSTD(3)),
    source_size UInt64,
    source_mtime_ns UInt64,
    ingested_at DateTime('UTC') DEFAULT now('UTC'),
    event_time DateTime('UTC'),

    cdr_uuid String,
    callid String,
    call_start_time Nullable(DateTime('UTC')),
    call_answered_time Nullable(DateTime('UTC')),
    call_end_time Nullable(DateTime('UTC')),
    from_did String,
    to_did String,
    lrn_did String,

    sip_code UInt16,
    sip_reason LowCardinality(String),
    reason LowCardinality(String),
    duration_real UInt32,
    pdd UInt32,
    attempt_counter UInt16,
    terminator LowCardinality(String),
    leg LowCardinality(String),
    direction LowCardinality(String),

    orig_carrier_name LowCardinality(String),
    orig_trunk_group_name LowCardinality(String),
    orig_ip String,
    orig_media_ip String,
    orig_juris LowCardinality(String),
    orig_rate Float64,
    orig_billed_duration UInt32,
    orig_cost Float64,

    term_carrier_name LowCardinality(String),
    term_trunk_group_name LowCardinality(String),
    term_ip String,
    term_media_ip String,
    term_juris LowCardinality(String),
    term_rate Float64,
    term_billed_duration UInt32,
    term_cost Float64,
    term_state LowCardinality(String),
    term_ratecenter LowCardinality(String),
    term_destination LowCardinality(String),

    stir_x5u String,
    stir_attest LowCardinality(String),
    mos_min Float32,
    mos_avg Float32,
    mos_max Float32
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(event_time)
ORDER BY
(
    toDate(event_time),
    entity,
    file_hour,
    term_media_ip,
    term_trunk_group_name,
    orig_trunk_group_name,
    sip_code,
    record_key
)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS cdr.ingest_log
(
    file_path String,
    entity LowCardinality(String),
    file_hour DateTime('UTC'),
    source_size UInt64,
    source_mtime_ns UInt64,
    rows_ingested UInt64,
    status LowCardinality(String),
    message String,
    updated_at DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY file_path;

CREATE TABLE IF NOT EXISTS cdr.termination_media_ip_watchlist
(
    termination_media_ip String,
    updated_at DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')
)
ENGINE = MergeTree
ORDER BY termination_media_ip;

CREATE TABLE IF NOT EXISTS cdr.termination_media_ip_watch_hits
(
    hour DateTime('UTC'),
    termination_media_ip String,
    entity LowCardinality(String),
    orig_trunk_group_name LowCardinality(String),
    term_carrier_name LowCardinality(String),
    term_trunk_group_name LowCardinality(String),
    term_ip String,
    matching_cdrs UInt64,
    updated_at DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(hour)
ORDER BY
(
    termination_media_ip,
    hour,
    entity,
    orig_trunk_group_name,
    term_carrier_name,
    term_trunk_group_name,
    term_ip
);

CREATE TABLE IF NOT EXISTS cdr.cdr_hourly_media_ip
(
    day Date,
    hour DateTime('UTC'),
    entity LowCardinality(String),
    term_media_ip String,
    term_ip String,
    term_carrier_name LowCardinality(String),
    term_trunk_group_name LowCardinality(String),
    orig_trunk_group_name LowCardinality(String),
    sip_code UInt16,
    reason LowCardinality(String),
    stir_attest LowCardinality(String),
    term_state LowCardinality(String),
    term_ratecenter LowCardinality(String),

    attempts UInt64,
    completions UInt64,
    billed_seconds UInt64,
    revenue Float64,
    cost Float64,
    pdd_sum UInt64,
    pdd_count UInt64,
    mos_sum Float64,
    mos_count UInt64
)
ENGINE = SummingMergeTree
PARTITION BY toYYYYMM(day)
ORDER BY
(
    day,
    entity,
    term_media_ip,
    term_trunk_group_name,
    orig_trunk_group_name,
    hour,
    sip_code,
    reason,
    stir_attest,
    term_state,
    term_ratecenter,
    term_ip,
    term_carrier_name
);

CREATE MATERIALIZED VIEW IF NOT EXISTS cdr.cdr_hourly_media_ip_mv
TO cdr.cdr_hourly_media_ip
AS
SELECT
    toDate(event_time) AS day,
    toStartOfHour(event_time) AS hour,
    entity,
    if(empty(term_media_ip), '(none)', term_media_ip) AS term_media_ip,
    if(empty(term_ip), '(none)', term_ip) AS term_ip,
    if(empty(term_carrier_name), '(none)', term_carrier_name) AS term_carrier_name,
    if(empty(term_trunk_group_name), '(none)', term_trunk_group_name) AS term_trunk_group_name,
    if(empty(orig_trunk_group_name), '(none)', orig_trunk_group_name) AS orig_trunk_group_name,
    sip_code,
    if(empty(reason), '(none)', reason) AS reason,
    if(empty(stir_attest), '?', stir_attest) AS stir_attest,
    if(empty(term_state), '?', term_state) AS term_state,
    if(empty(term_ratecenter), '?', term_ratecenter) AS term_ratecenter,
    count() AS attempts,
    countIf(sip_code = 200) AS completions,
    sumIf(toUInt64(orig_billed_duration), sip_code = 200) AS billed_seconds,
    sum(orig_cost) AS revenue,
    sum(term_cost) AS cost,
    sumIf(toUInt64(pdd), pdd > 0) AS pdd_sum,
    countIf(pdd > 0) AS pdd_count,
    sumIf(toFloat64(mos_avg), mos_avg > 0) AS mos_sum,
    countIf(mos_avg > 0) AS mos_count
FROM cdr.raw_cdr
GROUP BY
    day,
    hour,
    entity,
    term_media_ip,
    term_ip,
    term_carrier_name,
    term_trunk_group_name,
    orig_trunk_group_name,
    sip_code,
    reason,
    stir_attest,
    term_state,
    term_ratecenter;
