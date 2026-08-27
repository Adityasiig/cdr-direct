-- IP -> ISP (netblock owner) lookup for CDR media IPs.
--
-- Source: ip2asn snapshot (iptoasn.com), converted to "CIDR<TAB>ISP" rows and
-- baked into the ClickHouse image at /cdr-source/ip_isp_cidr.tsv by
-- Dockerfile.clickhouse. Uses an IP_TRIE layout so any IPv4 resolves to the
-- owning organization (e.g. 67.231.13.23 -> BANDWIDTH-), distinct from the
-- CDR's own term_carrier_name (the billing vendor).
--
-- Consumed by Grafana panel "Watched IP Individual CDRs" in
-- grafana/dashboards/term-media-ip.json via:
--   dictGetStringOrDefault('cdr.ip_isp','isp',
--       tuple(IPv4StringToNum(trimBoth(term_media_ip))), 'Unknown')
--
-- LIFETIME(0): static snapshot, load once (lazily on first lookup). To refresh
-- the dataset, rebuild the image with a newer clickhouse/data/ip_isp_cidr.tsv.gz.

CREATE DICTIONARY IF NOT EXISTS cdr.ip_isp
(
    prefix String,
    isp    String
)
PRIMARY KEY prefix
SOURCE(FILE(path '/cdr-source/ip_isp_cidr.tsv' format 'TabSeparated'))
LIFETIME(0)
LAYOUT(IP_TRIE());
