"""Optional RF machine learning; importing this package never imports torch.

Dormant since 0.70.0: the pretrained TorchSig models were not useful on real
captures, so the live classifier and detector are switched off for every
install. The code, settings, CLI (`msdr-rfml`), optional extras and tests are
kept so a better model can reuse the runtime. Set AVAILABLE to True to bring
the feature back; nothing else needs to change (docs/rf-ml.md "Status").
"""

# Read at call time (`rfml.AVAILABLE`), never imported by name, so tests can
# monkeypatch it.
AVAILABLE = False
