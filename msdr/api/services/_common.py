"""Module-level helpers, constants and guarded imports shared by the
AppService domain mixins. Everything here is re-exported from
msdr.api.service so existing `from msdr.api.service import X` keeps working.
"""


try:
    # A second package (msdr/recorder/) built alongside this feature -- guard
    # the import so a partial checkout (or this module under test before it
    # lands) doesn't crash the whole app; the recorder feature just reports
    # itself unavailable (see AppService._build_recorder/_require_recorder_feature).
    from msdr.recorder import Recorder, RecorderConfig, RecorderError
    from msdr.recorder.iq_stream import IQStream
except ImportError:  # pragma: no cover - msdr.recorder ships with this feature
    Recorder = None
    RecorderConfig = None
    IQStream = None

    class RecorderError(Exception):
        """Placeholder so app.py's exception_handler(RecorderError) always has
        a real type to register, even when msdr.recorder isn't importable."""

try:
    # A third package (msdr/decoder/), built alongside this feature -- same
    # guard as msdr.recorder above (see AppService._build_decoder/decoder_*).
    from msdr.decoder import AudioStream, DecoderError, DecoderManager
    from msdr.decoder import describe as describe_decoders
    from msdr.decoder.registry import resolve_binaries
except ImportError:  # pragma: no cover - msdr.decoder ships with this feature
    AudioStream = None
    DecoderManager = None

    class DecoderError(Exception):
        """Placeholder so app.py's exception_handler(DecoderError) always has
        a real type to register, even when msdr.decoder isn't importable."""

    def describe_decoders(bin_dirs: list[str], session: dict | None = None) -> list[dict]:
        return []

    def resolve_binaries(bin_dirs: list[str]) -> dict[str, str | None]:
        return {}

try:
    # A fourth package (msdr/survey/), built alongside this feature -- same
    # guard as msdr.recorder/msdr.decoder above (see AppService._build_survey/
    # survey_*).
    from msdr.survey import SpectrumHub, Survey, SurveyError
except ImportError:  # pragma: no cover - msdr.survey ships with this feature
    Survey = None
    SpectrumHub = None

    class SurveyError(Exception):
        """Placeholder so app.py's exception_handler(SurveyError) always has
        a real type to register, even when msdr.survey isn't importable."""

try:
    # A sixth package (msdr/source/, S3 Listening Workbench), built alongside
    # this feature by a concurrent stream (msdr.source.virtual_receiver) --
    # same guard as msdr.survey/msdr.survey.beacons above. AppService only
    # ever binds a VirtualReceiver to a replay ReplaySource (self.survey._replay),
    # never to the live adapter -- see _attach_virtual_receiver.
    from msdr.source.virtual_receiver import OutOfSpanError, VirtualReceiver
except ImportError:  # pragma: no cover - msdr.source ships with this feature
    VirtualReceiver = None

    class OutOfSpanError(Exception):
        """Placeholder so review_tune's except clause always has a real type
        to catch, even when msdr.source isn't importable."""

try:
    # A seventh package (msdr/decoder/replay_decode.py, S4), built alongside
    # this feature by a concurrent stream -- same guard as the others above.
    # Not available yet on a partial checkout: review_decode_start/stop then
    # raise SurveyError ("decode session unavailable"), same shape as every
    # other 409 this module already returns for "feature not wired up yet".
    from msdr.decoder.replay_decode import ReplayDecodeSession
except ImportError:  # pragma: no cover - msdr.decoder.replay_decode ships with this feature
    ReplayDecodeSession = None

# Signal.list()'s own default (500) is a UI page size, not a real cap -- the
# PASSES section wants every satellite label ever assigned, not just the
# first page (mirrors storage/transfer.py's export-all limit).
_ALL_SIGNALS_LIMIT = 1_000_000

# Receiver props the pre-roll recorder needs mirrored (sidecar settings/gap
# annotations, segment rotation on rate/center changes) -- see
# msdr.recorder.Recorder.on_property. Only "primary" device events count
# (msdr/radio/sdrconnect.py defaults an absent `device` field to "primary").
_RECORDER_FORWARD_PROPS = frozenset(
    {
        "device_sample_rate",
        "device_center_frequency",
        "started",
        "lna_state",
        "active_antenna",
        "device_vfo_frequency",
        "demodulator",
        "filter_bandwidth",
    }
)

class RestoreRunning(Exception):
    """Raised when a restore is requested while one is already in flight.

    A dedicated type so app.py can map exactly this condition to 409, instead
    of a catch-all `RuntimeError -> 409` handler that would also swallow
    unrelated bugs and report them as if they were a client-facing conflict.
    """

class ReceiverStopped(Exception):
    """Raised when a restore is requested while SDRconnect's receiver is stopped.

    On real hardware, a stopped receiver (Play not pressed) reports
    `started=False` and silently ignores every set_property -- no echo, no
    error -- so without this check a restore just times out after its full
    per-step timeout and reports "failed". A dedicated type lets app.py map
    this straight to a 503 with an actionable message instead.
    """
