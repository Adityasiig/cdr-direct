-- Accurate IP -> ISP name and IP -> location lookups (DB-IP Lite, 2026-09).
--
-- Supersedes cdr.ip_isp for display purposes. That dictionary is built from an
-- ip2asn snapshot and stores raw BGP AS handles ("CMCS", "PRLSS", "UUNET",
-- "BANDWIDTH-"), which are unreadable in the CDR tables. These two dictionaries
-- carry the registered organisation name and the geo-location instead:
--
--   CMCS        -> Comcast Cable Communications, LLC   (Salt Lake City, Utah, US)
--   PRLSS       -> Peerless Network Inc                (Chicago (West Loop), Illinois, US)
--   UUNET       -> Verizon Business                    (Boston, Massachusetts, US)
--   BANDWIDTH-  -> Bandwidth Inc.                      (Dallas, Texas, US)
--
-- cdr.ip_isp is left in place so existing deployments and any ad-hoc queries
-- keep working; no materialized view or table depends on it. New queries
-- should use ip_isp_name / ip_location.
--
-- Source files are baked into the image by Dockerfile.clickhouse and live under
-- user_files_path (/cdr-source/) so the FILE() dictionary source may read them.
-- Both use ip_trie, keyed on a CIDR prefix string, and are queried with
-- tuple(IPv4StringToNum(ip)) exactly like cdr.ip_isp.
--
-- To refresh, rebuild the image with newer clickhouse/data/*.tsv.gz files.

CREATE DICTIONARY IF NOT EXISTS cdr.ip_isp_name
(
    prefix String,
    isp    String
)
PRIMARY KEY prefix
SOURCE(FILE(path '/cdr-source/ip_isp_trie.tsv' format 'TabSeparated'))
LIFETIME(MIN 0 MAX 0)
LAYOUT(IP_TRIE());

CREATE DICTIONARY IF NOT EXISTS cdr.ip_location
(
    prefix   String,
    location String,
    country  String
)
PRIMARY KEY prefix
SOURCE(FILE(path '/cdr-source/ip_loc_trie.tsv' format 'TabSeparated'))
LIFETIME(MIN 0 MAX 0)
LAYOUT(IP_TRIE());
