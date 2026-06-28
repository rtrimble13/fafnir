-- seeds/0001_exchanges.sql
-- Core US exchanges. Idempotent (ON CONFLICT DO NOTHING). Applied by
-- `fafnir db seed` after migrations.

INSERT INTO ref.exchange (exchange_code, exchange_name, country, timezone) VALUES
    ('NASDAQ', 'Nasdaq Stock Market',      'US', 'America/New_York'),
    ('NYSE',   'New York Stock Exchange',  'US', 'America/New_York'),
    ('AMEX',   'NYSE American',            'US', 'America/New_York'),
    ('BATS',   'Cboe BZX Exchange',        'US', 'America/New_York'),
    ('OTC',    'Over-the-Counter',         'US', 'America/New_York')
ON CONFLICT (exchange_code) DO NOTHING;
