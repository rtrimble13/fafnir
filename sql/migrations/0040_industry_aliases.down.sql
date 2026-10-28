-- 0040_industry_aliases.down.sql
-- Restores core.v_security_classification as 0039 defined it, with its two industry
-- aliases. Derived: nothing is lost.

BEGIN;

CREATE OR REPLACE VIEW core.v_security_classification AS
SELECT
    s.security_id,
    CASE WHEN rs.sector_id IS NOT NULL THEN rs.sector_id ELSE s.sector_id END
        AS sector_id,
    CASE WHEN rs.sector_id IS NOT NULL THEN ri.industry_id ELSE s.industry_id END
        AS industry_id,
    CASE WHEN rs.sector_id IS NOT NULL THEN 'sharadar' ELSE s.source END
        AS attributes_source
FROM core.security s
LEFT JOIN core.security_vendor_xref fx
       ON fx.security_id = s.security_id AND fx.vendor = 'sharadar'
      AND fx.is_feed AND fx.valid_to IS NULL
LEFT JOIN core.vendor_security vst
       ON vst.vendor = 'sharadar' AND vst.vendor_table = 'stocks'
      AND vst.vendor_key = fx.vendor_key
LEFT JOIN core.vendor_security vfu
       ON vfu.vendor = 'sharadar' AND vfu.vendor_table = 'funds'
      AND vfu.vendor_key = fx.vendor_key
-- sharadar_map._norm: case, commas and spacing do not distinguish two names.
LEFT JOIN LATERAL (
    SELECT lower(btrim(regexp_replace(replace(
               CASE WHEN vst.vendor_key IS NOT NULL THEN vst.sector ELSE vfu.sector END,
               ',', ' '), '\s+', ' ', 'g'))) AS sector_norm,
           lower(btrim(regexp_replace(replace(
               CASE WHEN vst.vendor_key IS NOT NULL THEN vst.industry ELSE vfu.industry END,
               ',', ' '), '\s+', ' ', 'g'))) AS industry_norm
) n ON true
-- sharadar_map.INDUSTRY_ALIASES: Sharadar's name, normalized, and fafnir's.
LEFT JOIN (VALUES
    ('drug manufacturers - major', 'drug manufacturers - general'),
    ('furnishings', 'furnishings fixtures & appliances')
) al (alias_norm, target_norm) ON al.alias_norm = n.industry_norm
-- One id per normalized name; two names alike once normalized resolve to the later
-- one, as map_classification's dictionary does.
LEFT JOIN (
    SELECT DISTINCT ON (k) sector_id, k
      FROM (SELECT sector_id,
                   lower(btrim(regexp_replace(replace(sector_name, ',', ' '),
                                              '\s+', ' ', 'g'))) AS k
              FROM ref.sector) t
     ORDER BY k, sector_id DESC
) rs ON rs.k = n.sector_norm
LEFT JOIN (
    SELECT DISTINCT ON (k) industry_id, k
      FROM (SELECT industry_id,
                   lower(btrim(regexp_replace(replace(industry_name, ',', ' '),
                                              '\s+', ' ', 'g'))) AS k
              FROM ref.industry) t
     ORDER BY k, industry_id DESC
) ri ON ri.k = coalesce(al.target_norm, n.industry_norm);

COMMIT;
