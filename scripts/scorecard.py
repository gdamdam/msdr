#!/usr/bin/env python3
"""Regenerate docs/validation/scorecard-synthetic.md from the S4 corpus
scorecard (msdr.validation.scorecard) -- a fixed *synthetic* corpus, nothing
committed as IQ. Run from the repo root:

    .venv/bin/python scripts/scorecard.py
"""

import asyncio
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    # So `from tests.helpers.gmsk import ...` (msdr.validation.scorecard's AIS
    # case) resolves when this is run as a plain script, not just under pytest.
    sys.path.insert(0, str(_REPO_ROOT))

from msdr.validation.scorecard import render_markdown, run_scorecard  # noqa: E402

_OUTPUT = _REPO_ROOT / "docs" / "validation" / "scorecard-synthetic.md"


def main() -> None:
    report = asyncio.run(run_scorecard())
    _OUTPUT.write_text(render_markdown(report))
    print(f"wrote {_OUTPUT}")


if __name__ == "__main__":
    main()
