"""In-process (PythonBackend) decoders: HF fax/FSK/time-code, S7-S10.

Each module here defines a class matching msdr.decoder.backends.PyDecoder
(`input_rate`, `feed(samples) -> list[DecodeEvent]`, `flush() -> list[DecodeEvent]`)
that build_backend() instantiates dynamically from a registry spec's
`args[0]` ("module:Class") -- see msdr/decoder/registry.py.
"""
