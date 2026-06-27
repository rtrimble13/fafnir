-- 0006_corporate_actions.down.sql
BEGIN;
DROP TABLE IF EXISTS core.adjustment_factor;
DROP TABLE IF EXISTS core.corporate_action;
COMMIT;
