"""Corpus scorecard: runs a fixed *synthetic* signal corpus
through the real analyzer/decoder code paths and records, per case, what each
detector/decoder actually did -- correct, incorrect, abstained, or
unavailable. Nothing here is a field-accuracy claim (see
docs/validation/scorecard-synthetic.md's own header) and nothing here reads
or writes a captured IQ file: every case is generated in code
(msdr.validation.fixtures) each run.
"""

from msdr.validation.scorecard import ScorecardReport, render_markdown, run_scorecard
from msdr.validation.status import validation_status

__all__ = ["ScorecardReport", "render_markdown", "run_scorecard", "validation_status"]
