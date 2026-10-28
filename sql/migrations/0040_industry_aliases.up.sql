-- 0040_industry_aliases.up.sql
-- Sharadar's industry names onto fafnir's (#109 follow-up).
--
-- 0039 mapped a security Sharadar feeds onto fafnir's vocabulary (ref.industry, FMP's
-- names) with two industry aliases. Sharadar spells 69 of its industries otherwise
-- ("Medical Devices", FMP's "Medical - Devices"; "Oil & Gas E&P", "Oil & Gas
-- Exploration & Production"), so the view gave about 1,660 listed securities
-- Sharadar's sector and no industry, and security_missing_classification went from
-- 38 to 1,704 open flags on its first night (2026-10-28).
--
--   core.v_security_classification (VIEW, re-defined)
--       0039's view with the alias list sharadar_map.INDUSTRY_ALIASES holds: every
--       Sharadar industry name FMP's vocabulary does not spell the same way. The same
--       columns, so the views that read it (mart.v_security_attributes,
--       mart.v_security_profile) stand; mart.security_latest picks it up at its next
--       refresh.
--
-- Nothing is written: the view is derived.

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
    ('airlines', 'airlines airports & air services'),
    ('airports & air services', 'airlines airports & air services'),
    ('apparel manufacturing', 'apparel - manufacturers'),
    ('apparel retail', 'apparel - retail'),
    ('auto & truck dealerships', 'auto - dealerships'),
    ('auto manufacturers', 'auto - manufacturers'),
    ('auto parts', 'auto - parts'),
    ('beverages - brewers', 'beverages - alcoholic'),
    ('building materials', 'construction materials'),
    ('building products & equipment', 'construction materials'),
    ('capital markets', 'financial - capital markets'),
    ('coking coal', 'coal'),
    ('computer systems', 'computer hardware'),
    ('confectioners', 'food confectioners'),
    ('credit services', 'financial - credit services'),
    ('diagnostics & research', 'medical - diagnostics & research'),
    ('diversified industrials', 'industrial - machinery'),
    ('drug manufacturers - major', 'drug manufacturers - general'),
    ('electronic components', 'hardware equipment & parts'),
    ('electronics & computer distribution', 'technology distributors'),
    ('farm & heavy construction machinery', 'agricultural - machinery'),
    ('farm products', 'agricultural farm products'),
    ('financial conglomerates', 'financial - conglomerates'),
    ('financial data & stock exchanges', 'financial - data & stock exchanges'),
    ('footwear & accessories', 'apparel - footwear & accessories'),
    ('furnishings', 'furnishings fixtures & appliances'),
    ('gambling', 'gambling resorts & casinos'),
    ('health information services', 'medical - healthcare information services'),
    ('healthcare plans', 'medical - healthcare plans'),
    ('home improvement retail', 'home improvement'),
    ('industrial distribution', 'industrial - distribution'),
    ('industrial metals & minerals', 'industrial materials'),
    ('infrastructure operations', 'industrial - infrastructure operations'),
    ('insurance brokers', 'insurance - brokers'),
    ('internet retail', 'specialty retail'),
    ('lodging', 'travel lodging'),
    ('lumber & wood production', 'paper lumber & forest products'),
    ('medical care facilities', 'medical - care facilities'),
    ('medical devices', 'medical - devices'),
    ('medical distribution', 'medical - distribution'),
    ('medical instruments & supplies', 'medical - instruments & supplies'),
    ('metal fabrication', 'manufacturing - metal fabrication'),
    ('mortgage finance', 'financial - mortgages'),
    ('oil & gas e&p', 'oil & gas exploration & production'),
    ('other industrial metals & mining', 'industrial materials'),
    ('other precious metals & mining', 'other precious metals'),
    ('paper & paper products', 'paper lumber & forest products'),
    ('personal services', 'personal products & services'),
    ('pharmaceutical retailers', 'medical - pharmaceuticals'),
    ('pollution & treatment controls', 'industrial - pollution & treatment controls'),
    ('real estate services', 'real estate - services'),
    ('recreational vehicles', 'auto - recreational vehicles'),
    ('resorts & casinos', 'gambling resorts & casinos'),
    ('savings & cooperative banks', 'banks - regional'),
    ('scientific & technical instruments', 'hardware equipment & parts'),
    ('semiconductor equipment & materials', 'semiconductors'),
    ('shipping & ports', 'marine shipping'),
    ('specialty chemicals', 'chemicals - specialty'),
    ('specialty industrial machinery', 'industrial - machinery'),
    ('telecom services', 'telecommunications services'),
    ('textile manufacturing', 'manufacturing - textiles'),
    ('thermal coal', 'coal'),
    ('tools & accessories', 'manufacturing - tools & accessories'),
    ('utilities - diversified', 'diversified utilities'),
    ('utilities - independent power producers', 'independent power producers'),
    ('utilities - regulated electric', 'regulated electric'),
    ('utilities - regulated gas', 'regulated gas'),
    ('utilities - regulated water', 'regulated water'),
    ('utilities - renewable', 'renewable utilities')
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
