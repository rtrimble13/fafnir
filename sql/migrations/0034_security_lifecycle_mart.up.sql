-- 0034_security_lifecycle_mart.up.sql
-- mart.v_security_lifecycle -- when a security traded, and why it stopped (#79).
--
-- The dead issuers minted from Sharadar (#71, ADR 0016) make the warehouse free of
-- survivorship bias from 1998. A reader needs the facts that make them usable in
-- research -- when each traded, whether it was acquired, merged away, liquidated or
-- delisted, and by whom -- and a fafnir_app role cannot read core (ADR 0008). This
-- is 0020's answer again (ADR 0009): a definer-rights view in mart.
--
-- A MART VIEW IS A GRANT to every mart reader -- every person, every agent. This one
-- exposes the vendor's terminal lifecycle event for each security that stopped
-- trading: its action, date and other party (the acquirer's ticker and name, as the
-- vendor spells them). These are Sharadar ACTIONS facts of the same class as the
-- corporate actions mart.v_corporate_action already exposes. Nothing else of
-- core.vendor_security_event, core.security_vendor_xref or core.vendor_security is
-- reachable through it: not the vendor keys, not the link evidence.
--
-- The reason. A security's Sharadar key is its open link, else its most recent one.
-- Of that key's terminal events (acquisitionby, mergerto, bankruptcyliquidation,
-- regulatorydelisting, voluntarydelisting, delisted), the view takes those within
-- 30 days of the last, so a reason from an earlier listing is not carried to a later
-- death, and prefers a specific reason to a bare `delisted`: an acquisition is
-- recorded as both, on the same date. `mergerto` is the merged-away side (the other
-- party survives); `mergerfrom` is the survivor's event and is not a reason. The
-- vendor writes 'N/A' for no other party; the view reads it as NULL. The reason is
-- given only for a security that has stopped trading (a delisted_date, or not
-- actively trading).
--
-- first_trade_date and last_trade_date are per-security index probes, not a GROUP
-- BY over core.daily_price, so a filter on security_id stays cheap and a full read
-- costs two probes a security.
--
-- survivorship_complete_from is ADR 0016's floor: from 1998-01-01 the universe
-- holds the issuers that died as well as those that live.
--
-- Do NOT set security_invoker (ADR 0009). SELECT comes from the default privileges
-- 0001 set on mart.

BEGIN;

CREATE OR REPLACE VIEW mart.v_security_lifecycle AS
SELECT
    s.security_id,
    s.primary_symbol                                         AS symbol,
    s.company_name,
    s.source                                                 AS minted_by,
    s.is_actively_trading,
    (SELECT min(p.trade_date) FROM core.daily_price p
      WHERE p.security_id = s.security_id)                   AS first_trade_date,
    (SELECT max(p.trade_date) FROM core.daily_price p
      WHERE p.security_id = s.security_id)                   AS last_trade_date,
    s.delisted_date,
    ev.action                                                AS delist_reason,
    ev.event_date                                            AS delist_event_date,
    CASE WHEN ev.action IN ('acquisitionby', 'mergerto')
         THEN NULLIF(ev.contra_ticker, 'N/A') END            AS acquirer_ticker,
    CASE WHEN ev.action IN ('acquisitionby', 'mergerto')
         THEN NULLIF(ev.contra_name, 'N/A') END              AS acquirer_name,
    CASE WHEN ev.action IN ('acquisitionby', 'mergerto')
         THEN (SELECT ax.security_id FROM core.security_vendor_xref ax
                WHERE ax.vendor = 'sharadar'
                  AND ax.vendor_key = ev.contra_vendor_key
                  AND ax.valid_to IS NULL) END               AS acquirer_security_id,
    DATE '1998-01-01'                                        AS survivorship_complete_from
FROM core.security s
LEFT JOIN LATERAL (
    SELECT x.vendor, x.vendor_key
      FROM core.security_vendor_xref x
     WHERE x.security_id = s.security_id AND x.vendor = 'sharadar'
     ORDER BY (x.valid_to IS NULL) DESC, x.valid_from DESC
     LIMIT 1
) link ON TRUE
LEFT JOIN LATERAL (
    SELECT e.action, e.event_date, e.contra_ticker, e.contra_name,
           e.contra_vendor_key
      FROM core.vendor_security_event e
     WHERE e.vendor = link.vendor AND e.vendor_key = link.vendor_key
       AND e.action IN ('acquisitionby', 'mergerto', 'bankruptcyliquidation',
                        'regulatorydelisting', 'voluntarydelisting', 'delisted')
       AND e.event_date >= (
             SELECT max(t.event_date) - 30
               FROM core.vendor_security_event t
              WHERE t.vendor = link.vendor AND t.vendor_key = link.vendor_key
                AND t.action IN ('acquisitionby', 'mergerto',
                                 'bankruptcyliquidation', 'regulatorydelisting',
                                 'voluntarydelisting', 'delisted'))
     ORDER BY (e.action = 'delisted'), e.event_date DESC, e.action
     LIMIT 1
) ev ON s.delisted_date IS NOT NULL OR NOT s.is_actively_trading;

COMMENT ON VIEW mart.v_security_lifecycle IS
    'One row per security: when it traded, who minted it, and for one that stopped '
    'trading, the vendor''s terminal event (acquired, merged away, liquidated, '
    'delisted) and the acquirer. Survivorship-complete from 1998-01-01 (ADR 0016).';

COMMIT;
