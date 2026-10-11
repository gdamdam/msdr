"""Balanced sampling preserves narrow frequency detail and shadow semantics."""
from types import SimpleNamespace

import numpy as np
import pytest

from msdr.rfml.detector import RFMLDetector, prepare_spectrogram_image
from msdr.settings import RFMLSettings
from msdr.survey.peaks_spectrum import TrackedPeak
from msdr.survey.survey import Survey


def _proposal_state(survey):
    survey._ml_proposal_cache_key = None
    survey._ml_proposal_cache = []
    survey._ml_proposal_boxes = {}
    survey._ml_proposal_seq = 0


def test_narrow_tones_resolve_at_six_megasamples():
    rate = 6_000_000
    t = np.arange(65536) / rate
    # Integer FFT bins, separated by about 12 kHz: coarse FFT cannot separate these.
    delta = rate / 4096
    iq = np.exp(2j * np.pi * 80 * delta * t) + np.exp(2j * np.pi * 88 * delta * t)
    image, metadata = prepare_spectrogram_image(iq, rate, 160e6, n_fft=4096, hop_length=128)
    ridge = image[..., 0].mean(axis=1)
    assert metadata.bin_width_hz < 1500
    def row(bin_index):
        return 4095 - (2048 + bin_index)
    assert ridge[row(84)] > ridge[row(80)] + 20
    assert ridge[row(84)] > ridge[row(88)] + 20


def test_adaptive_tiles_keep_resolution_and_rotate_search(tmp_path, monkeypatch):
    detector = RFMLDetector(tmp_path / 'unused', target_bin_hz=1500, max_tiles=4)
    monkeypatch.setattr(detector, 'load', lambda: None)
    shapes = []
    monkeypatch.setattr(detector, '_predict', lambda image: shapes.append(image.shape) or [])
    iq = np.ones(65536, dtype=np.complex64)
    first = detector.predict(iq, sample_rate_hz=6e6, center_frequency_hz=160e6,
                             focus_frequencies_hz=[162.4e6])
    assert first['bin_width_hz'] == pytest.approx(1464.84375)
    assert 0 < first['frequency_coverage'] < 1
    assert len(shapes) == 4
    assert all(shape[0] == 512 and shape[1] <= 512 for shape in shapes)
    starts = {tile['start_bin'] for tile in first['tiles']}
    later = set()
    for _ in range(12):
        result = detector.predict(iq, sample_rate_hz=6e6, center_frequency_hz=160e6,
                                  focus_frequencies_hz=[162.4e6])
        later.update(tile['start_bin'] for tile in result['tiles'])
    assert len(later) > len(starts)


def test_classifier_result_age_and_stale(monkeypatch):
    survey = object.__new__(Survey)
    survey._rfml_results = {'p1': {'classification': 'FM', 'status': 'ok'}}
    survey._rfml_received = {'p1': 100.0}
    survey._settings_getter = lambda: SimpleNamespace(ml=RFMLSettings(result_ttl_s=60))
    monkeypatch.setattr('msdr.survey.survey.time.monotonic', lambda: 170)
    assert survey._fresh_ml_result('p1')['stale'] is True
    assert survey._fresh_ml_result('p1')['age_s'] == 70
    assert survey._fresh_ml_result('absent') is None


def test_proposals_join_dsp_observations_without_duplicate():
    survey = object.__new__(Survey)
    survey._generation = 3
    _proposal_state(survey)
    survey._analyzer = SimpleNamespace(detector_result=lambda: {
        'result_age_s': 1, 'result': {'model_id': 'test', 'bin_width_hz': 1500, 'candidates': [
            {'low_hz': 159990000, 'high_hz': 160010000, 'score': 0.8},
            {'low_hz': 159995000, 'high_hz': 160005000, 'score': 0.3},
            {'low_hz': 161990000, 'high_hz': 162010000, 'score': 0.9}]}})
    delivered = []
    survey._on_labels_changed = delivered.append
    survey._notify_labels_changed([{'id': 'p1', 'frequency_hz': 160000000}])
    assert len(delivered[0]) == 2
    assert delivered[0][0]['id'] == 'p1'
    assert delivered[0][0]['rfml_detection']['shadow']
    assert delivered[0][0]['rfml_detection']['score'] == 0.8
    assert delivered[0][1]['ml_only']


def test_expired_detector_proposal_retires_on_survey_publish(monkeypatch):
    survey = object.__new__(Survey)
    survey._last_survey_publish_at = 0
    survey._last_detector_result = None
    status = {'result': {'candidates': [{'low_hz': 100, 'high_hz': 200, 'score': 0.8}]}}
    survey._analyzer = SimpleNamespace(detector_result=lambda: status)
    notifications = []
    survey._notify_labels_changed = lambda: notifications.append(status['result'])
    survey._publish_survey = lambda: None
    monkeypatch.setattr('msdr.survey.survey.time.monotonic', lambda: 10)
    survey._maybe_publish_survey()
    status['result'] = None
    survey._maybe_publish_survey()
    assert len(notifications) == 2
    assert notifications[-1] is None


def test_fresh_narrow_proposal_uses_spare_classifier_slot_and_binds_result():
    survey = object.__new__(Survey)
    survey._center_hz = 160_000_000
    survey._sample_rate_hz = 6_000_000
    survey._generation = 2
    survey._dimensions = []
    survey._units_per_db = 1
    survey._tracker = SimpleNamespace(history_s=600, dc_exclude_bins=-1)  # exclusion off (default)
    survey._latest_peaks = [
        TrackedPeak('t1', 255, 257, 256, 90, 40, 0, 0, profile=[90]),
        # A nearby low-SNR track must not steal the proposal's ML result.
        TrackedPeak('t2', 264, 266, 265, 30, 1, 0, 0, profile=[30]),
    ]
    survey._replay = None
    survey._settings_getter = lambda: SimpleNamespace(
        ml=RFMLSettings(enabled=True), max_analyzed=2, analyze_min_snr_db=8
    )
    proposals = [
        {'id': 'overlap', 'frequency_hz': 160_005_000, 'width_hz': 10_000},
        {'id': 'spare', 'frequency_hz': 160_100_000, 'width_hz': 12_000},
        {'id': 'wide', 'frequency_hz': 160_200_000, 'width_hz': 30_000},
    ]
    survey.ml_proposals = lambda: proposals
    assert survey._analyzer_targets() == [160_000_000, 160_100_000]
    context = survey._ml_context(160_100_000)
    assert context['candidate_id'] == 'spare'
    assert context['display_snr_db'] is None
    assert context['spectrum_bandwidth_hz'] == 12_000
    survey._rfml_results = {}
    survey._rfml_received = {}
    survey._notify_labels_changed = lambda: None
    survey._publish_survey = lambda **_: None
    survey._on_ml_result(context, {'status': 'ok', 'classification': 'FM'})
    assert survey._rfml_results['spare']['classification'] == 'FM'
    survey.ml_proposals = lambda: proposals[:1]
    survey._on_ml_result(context, {'status': 'ok', 'classification': 'PSK'})
    assert survey._rfml_results['spare']['classification'] == 'FM'


def test_ml_only_proposal_exposes_bound_classifier_result(monkeypatch):
    survey = object.__new__(Survey)
    survey._generation = 2
    _proposal_state(survey)
    survey._analyzer = SimpleNamespace(detector_result=lambda: {
        'result_age_s': 1, 'result': {'model_id': 'detector', 'bin_width_hz': 1500,
                                    'candidates': [{'low_hz': 159994000,
                                                    'high_hz': 160006000,
                                                    'score': 0.9}]}})
    survey._settings_getter = lambda: SimpleNamespace(ml=RFMLSettings(enabled=True))
    monkeypatch.setattr('msdr.survey.survey.time.monotonic', lambda: 100)
    proposal_id = survey.ml_proposals()[0]['id']
    survey._rfml_results = {proposal_id: {'status': 'ok', 'classification': 'FM'}}
    survey._rfml_received = {proposal_id: 99}
    [proposal] = survey.ml_proposals()
    assert proposal['rfml']['classification'] == 'FM'
    assert proposal['rfml']['stale'] is False
    assert proposal['ml_only'] is True
