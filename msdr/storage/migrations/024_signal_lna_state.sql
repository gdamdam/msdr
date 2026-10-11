-- Per-signal LNA state, same shape as 020_signal_antenna. None = leave the
-- current LNA unchanged on tune; existing rows get NULL, same meaning.
ALTER TABLE signals ADD COLUMN lna_state INTEGER;
