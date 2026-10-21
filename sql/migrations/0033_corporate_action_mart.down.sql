-- 0033_corporate_action_mart.down.sql
-- Reverse of 0033: drop mart.v_corporate_action.
--
-- A view only: no data is lost. What is lost is a mart-only reader's way to list
-- corporate actions -- portable's `pt ca sync` against fafnir stops working.

BEGIN;

DROP VIEW IF EXISTS mart.v_corporate_action;

COMMIT;
