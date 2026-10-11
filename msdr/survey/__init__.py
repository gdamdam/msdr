"""Survey package (S1): span picker, spectrum + waterfall, occupancy map.
Sibling of msdr/decoder/ -- see docs/superpowers/specs/2026-09-25-wide-view-
identification-design.md for the full architecture (this package ships only
the S1 slice: SpectrumHub, OccupancyMap, Survey)."""

from msdr.survey.classify import activity_class, bandwidth_class
from msdr.survey.occupancy import OccupancyMap
from msdr.survey.peaks_spectrum import SpectrumPeakTracker, TrackedPeak
from msdr.survey.spectrum_hub import SpectrumHub
from msdr.survey.survey import Survey, SurveyError
from msdr.survey.watch import SpanGroup, WithinBandWatcher, plan_span_groups

__all__ = [
    "Survey",
    "SpectrumHub",
    "SurveyError",
    "OccupancyMap",
    "SpectrumPeakTracker",
    "TrackedPeak",
    "bandwidth_class",
    "activity_class",
    "SpanGroup",
    "WithinBandWatcher",
    "plan_span_groups",
]
