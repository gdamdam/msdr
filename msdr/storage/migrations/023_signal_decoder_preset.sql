-- Optional PHY profile recalled when a saved signal starts its decoder.
ALTER TABLE signals ADD COLUMN decoder_preset TEXT;
