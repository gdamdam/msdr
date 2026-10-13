"""Static registry of signal decoders: what each one needs and how to invoke it.

Kept data-only (no process/IO logic here -- that's backends.py) so the API and
UI can describe the catalog without starting anything.
"""

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_MULTIMON_INSTALL_HINT = (
    "Build from source: git clone https://github.com/EliasOenal/multimon-ng "
    "&& cmake && make; copy multimon-ng to ~/.local/bin"
)
_MINIMODEM_INSTALL_HINT = "brew install minimodem"
_DIREWOLF_INSTALL_HINT = "brew install direwolf"
_DSDFME_INSTALL_HINT = (
    "Build dsd-fme from source (mbelib, itpp, libsndfile, ncurses via Homebrew); "
    "copy dsd-fme to ~/.local/bin"
)

_RTL433_INSTALL_HINT = "brew install rtl_433"
# freedv_rx ships in codec2's bin/ on Homebrew (not verified on this machine).
_FREEDV_INSTALL_HINT = "brew install codec2 (provides freedv_rx; check it is on PATH)"
_SONDE_INSTALL_HINT = (
    "Build radiosonde_auto_rx's demodulators: git clone "
    "https://github.com/projecthorus/radiosonde_auto_rx, cd demod/mod && make; "
    "copy {binary} to ~/.local/bin"
)

# All P1 decoders run multimon-ng/minimodem on mono s16 audio at this rate
# (spec "Measured facts": multimon raw input is 22,050 Hz).
_P1_INPUT_RATE = 22050

# dsd-fme (digital voice) wants raw 48 kHz mono s16 on stdin.
_DSDFME_INPUT_RATE = 48000

# In-process (PythonBackend) decoders run on the SSB/AM audio path at the
# same 22,050 Hz... except these five settled on 11,025 Hz -- half the P1
# rate is plenty for a <=3 kHz fax/FSK signal and keeps their per-sample numpy work cheaper.
_PYTHON_INPUT_RATE = 11025


@dataclass(frozen=True)
class DecoderSpec:
    # "pocsag" | "flex" | "eas" | "aprs" | "dtmf" | "cw" | "rtty" | "rtty_minimodem" |
    # "packet9600" | "packet300" | "dmr" | "p25" | "nxdn" | "nxdn96" | ... | "ais"
    id: str
    label: str
    kind: Literal["text", "image", "time", "call"]
    demodulators: tuple[str, ...]  # compatible SDRconnect demodulator names
    default_bandwidth_hz: int
    input_rate: int
    backend: Literal[
        "multimon",
        "minimodem",
        "direwolf",
        "dsdfme",
        "python",
        "standalone_adsb",
        "standalone_ft8",
        "standalone_dsc",
        "standalone_navigation",
        "standalone_protocol",
        "external_iq",
        "freedv",
    ]
    binaries: tuple[str, ...]  # ("multimon-ng",) or ("minimodem",) or ("direwolf",)
    args: tuple[str, ...]  # backend-specific extra args
    phase: int = 1
    normalize: bool = True
    install_hint: str = ""  # shown in UI when a binary is missing
    # 9600-baud FSK (G3RUH) needs the flat discriminator output from the IQ
    # path -- SDRconnect's audio chain corrupts it the same way it garbles
    # POCSAG/FLEX's first codeword. Set True for decoders that must not run
    # on the audio path; the manager skips them (with a warning) rather than
    # starting them somewhere they can't work.
    iq_only: bool = False
    # Preferred alternative backend as (binary_name, extra_args_str), tried
    # before falling back to this spec's own `backend`/`binaries`/`args` --
    # e.g. direwolf decodes AX.25 packet more robustly than multimon-ng when
    # it's installed, but multimon-ng remains the zero-extra-install default.
    alt_backend: tuple[str, str] | None = None


def _multimon(argv_tail: tuple[str, ...]) -> tuple[str, ...]:
    # -c clears multimon-ng's default decoder set so only what we ask for runs.
    return ("-q", "-c", *argv_tail)


DECODERS: dict[str, DecoderSpec] = {
    spec.id: spec
    for spec in (
        DecoderSpec(
            id="pocsag",
            label="POCSAG",
            kind="text",
            demodulators=("NFM",),
            default_bandwidth_hz=12500,
            input_rate=_P1_INPUT_RATE,
            backend="multimon",
            binaries=("multimon-ng",),
            args=_multimon(
                (
                    "--json",
                    "-t",
                    "raw",
                    "-e",
                    "-a",
                    "POCSAG512",
                    "-a",
                    "POCSAG1200",
                    "-a",
                    "POCSAG2400",
                    "-",
                )
            ),
            install_hint=_MULTIMON_INSTALL_HINT,
        ),
        DecoderSpec(
            id="flex",
            label="FLEX",
            kind="text",
            demodulators=("NFM",),
            default_bandwidth_hz=12500,
            input_rate=_P1_INPUT_RATE,
            backend="multimon",
            binaries=("multimon-ng",),
            args=_multimon(("--json", "-t", "raw", "-a", "FLEX", "-")),
            install_hint=_MULTIMON_INSTALL_HINT,
        ),
        DecoderSpec(
            id="eas",
            label="EAS",
            kind="text",
            demodulators=("NFM",),
            default_bandwidth_hz=12500,
            input_rate=_P1_INPUT_RATE,
            backend="multimon",
            binaries=("multimon-ng",),
            args=_multimon(("--json", "-t", "raw", "-a", "EAS", "-")),
            install_hint=_MULTIMON_INSTALL_HINT,
        ),
        DecoderSpec(
            id="aprs",
            label="AX.25 packet 1200 (APRS)",
            kind="text",
            demodulators=("NFM",),
            default_bandwidth_hz=12500,
            input_rate=_P1_INPUT_RATE,
            backend="multimon",
            binaries=("multimon-ng",),
            # Plain TNC2 lines ("AFSK1200: <tnc2>"), not --json.
            args=_multimon(("-A", "-t", "raw", "-a", "AFSK1200", "-")),
            install_hint=_MULTIMON_INSTALL_HINT,
            alt_backend=("direwolf", "-B 1200"),
        ),
        DecoderSpec(
            id="packet9600",
            label="AX.25 packet 9600 (G3RUH)",
            kind="text",
            demodulators=("NFM",),
            default_bandwidth_hz=20000,
            input_rate=_P1_INPUT_RATE,
            backend="multimon",
            binaries=("multimon-ng",),
            # Plain TNC2 lines ("FSK9600: <tnc2>"), not --json.
            args=_multimon(("-A", "-t", "raw", "-a", "FSK9600", "-")),
            install_hint=_MULTIMON_INSTALL_HINT,
            iq_only=True,
            alt_backend=("direwolf", "-B 9600"),
        ),
        DecoderSpec(
            id="packet300",
            label="AX.25 packet 300 (HF)",
            kind="text",
            demodulators=("USB", "LSB"),
            default_bandwidth_hz=2800,
            input_rate=_P1_INPUT_RATE,
            backend="direwolf",
            binaries=("direwolf",),
            args=("-B", "300"),
            install_hint=_DIREWOLF_INSTALL_HINT,
        ),
        DecoderSpec(
            id="dtmf",
            label="DTMF",
            kind="text",
            demodulators=("NFM",),
            default_bandwidth_hz=12500,
            input_rate=_P1_INPUT_RATE,
            backend="multimon",
            binaries=("multimon-ng",),
            args=_multimon(("--json", "-t", "raw", "-a", "DTMF", "-")),
            install_hint=_MULTIMON_INSTALL_HINT,
        ),
        DecoderSpec(
            id="cw",
            label="CW",
            kind="text",
            demodulators=("CW",),
            default_bandwidth_hz=500,
            input_rate=_P1_INPUT_RATE,
            backend="multimon",
            binaries=("multimon-ng",),
            # Preserve the keyed envelope: general AGC raises noise in gaps.
            normalize=False,
            # Unprefixed streaming Morse characters; MorseBackend frames them.
            args=_multimon(("-t", "raw", "-a", "MORSE_CW", "-")),
            install_hint=_MULTIMON_INSTALL_HINT,
        ),
        DecoderSpec(
            id="rtty",
            label="RTTY",
            kind="text",
            demodulators=("USB", "LSB"),
            default_bandwidth_hz=2800,
            input_rate=_PYTHON_INPUT_RATE,
            backend="python",
            binaries=(),
            args=("msdr.decoder.pydecoders.rtty:RttyDecoder",),
        ),
        # Optional cross-check backend: minimodem's own baud/shift autodetect,
        # kept under a separate id (not installed by default; native "rtty"
        # above needs nothing installed).
        DecoderSpec(
            id="rtty_minimodem",
            label="RTTY (minimodem)",
            kind="text",
            demodulators=("USB", "LSB"),
            default_bandwidth_hz=2800,
            input_rate=_P1_INPUT_RATE,
            backend="minimodem",
            binaries=("minimodem",),
            args=("--rx", "-q", "-f", "-", "rtty"),
            install_hint=_MINIMODEM_INSTALL_HINT,
        ),
        DecoderSpec(
            id="dmr",
            label="DMR (dsd-fme)",
            kind="call",
            demodulators=("NFM",),
            default_bandwidth_hz=12500,
            input_rate=_DSDFME_INPUT_RATE,
            backend="dsdfme",
            binaries=("dsd-fme",),
            args=("-fs",),
            iq_only=True,
            install_hint=_DSDFME_INSTALL_HINT,
        ),
        DecoderSpec(
            id="p25",
            label="P25 Phase 1 (dsd-fme)",
            kind="call",
            demodulators=("NFM",),
            default_bandwidth_hz=12500,
            input_rate=_DSDFME_INPUT_RATE,
            backend="dsdfme",
            binaries=("dsd-fme",),
            args=("-f1",),
            iq_only=True,
            install_hint=_DSDFME_INSTALL_HINT,
        ),
        DecoderSpec(
            id="nxdn",
            label="NXDN48 · 6.25 kHz (dsd-fme)",
            kind="call",
            demodulators=("NFM",),
            default_bandwidth_hz=6250,
            input_rate=_DSDFME_INPUT_RATE,
            backend="dsdfme",
            binaries=("dsd-fme",),
            args=("-fi",),  # NXDN cannot be auto-detected by dsd-fme
            iq_only=True,
            install_hint=_DSDFME_INSTALL_HINT,
        ),
        DecoderSpec(
            id="nxdn96",
            label="NXDN96 · 12.5 kHz (dsd-fme)",
            kind="call",
            demodulators=("NFM",),
            default_bandwidth_hz=12500,
            input_rate=_DSDFME_INPUT_RATE,
            backend="dsdfme",
            binaries=("dsd-fme",),
            args=("-fn",),
            iq_only=True,
            install_hint=_DSDFME_INSTALL_HINT,
        ),
        DecoderSpec(
            id="dstar",
            label="D-STAR (dsd-fme)",
            kind="call",
            demodulators=("NFM",),
            default_bandwidth_hz=12500,
            input_rate=_DSDFME_INPUT_RATE,
            backend="dsdfme",
            binaries=("dsd-fme",),
            args=("-fd",),
            iq_only=True,
            install_hint=_DSDFME_INSTALL_HINT,
        ),
        DecoderSpec(
            id="ysf",
            label="Yaesu System Fusion (YSF, dsd-fme)",
            kind="call",
            demodulators=("NFM",),
            default_bandwidth_hz=12500,
            input_rate=_DSDFME_INPUT_RATE,
            backend="dsdfme",
            binaries=("dsd-fme",),
            args=("-fy",),
            iq_only=True,
            install_hint=_DSDFME_INSTALL_HINT,
        ),
        DecoderSpec(
            id="dpmr",
            label="dPMR (dsd-fme)",
            kind="call",
            demodulators=("NFM",),
            default_bandwidth_hz=12500,
            input_rate=_DSDFME_INPUT_RATE,
            backend="dsdfme",
            binaries=("dsd-fme",),
            args=("-fm",),
            iq_only=True,
            install_hint=(
                _DSDFME_INSTALL_HINT
                + "; dPMR is fixed-mode (not auto-detected), and inverted Icom dPMR needs -xd"
            ),
        ),
        DecoderSpec(
            id="m17",
            label="M17 (dsd-fme)",
            kind="call",
            demodulators=("NFM",),
            default_bandwidth_hz=12500,
            input_rate=_DSDFME_INPUT_RATE,
            backend="dsdfme",
            binaries=("dsd-fme",),
            args=("-fz",),
            iq_only=True,
            install_hint=(
                _DSDFME_INSTALL_HINT
                + "; M17 is fixed-mode and dsd-fme cannot auto-detect its signal polarity"
            ),
        ),
        # -- HF decoders, PythonBackend (in-process, no binary) ---------
        DecoderSpec(
            id="wefax",
            label="Radiofax (WEFAX 120/576)",
            kind="image",
            demodulators=("USB",),
            # Tune 1.9 kHz below the listed fax frequency (USB dial offset);
            # noted again in the preset's install_hint/description.
            default_bandwidth_hz=3000,
            input_rate=_PYTHON_INPUT_RATE,
            backend="python",
            binaries=(),
            args=("msdr.decoder.pydecoders.wefax:WefaxDecoder",),
        ),
        DecoderSpec(
            id="sstv",
            label="SSTV (Robot 36, Martin, Scottie, PD)",
            kind="image",
            # HF SSTV is USB (14.230 / 7.171 / 3.730 MHz); 2 m (144.500,
            # ISS 145.800) is NFM. Video sits in 1200-2300 Hz either way.
            demodulators=("USB", "NFM"),
            default_bandwidth_hz=3000,
            input_rate=_PYTHON_INPUT_RATE,
            backend="python",
            binaries=(),
            args=("msdr.decoder.pydecoders.sstv:SstvDecoder",),
        ),
        DecoderSpec(
            id="navtex",
            label="NAVTEX (SITOR-B 100 Bd)",
            kind="text",
            demodulators=("USB",),
            # 518 / 490 / 4209.5 kHz; centre a 1.7 kHz tone pair (1615/1785 Hz).
            default_bandwidth_hz=1000,
            input_rate=_PYTHON_INPUT_RATE,
            backend="python",
            binaries=(),
            args=("msdr.decoder.pydecoders.navtex:NavtexDecoder",),
        ),
        DecoderSpec(
            id="acars",
            label="ACARS (VHF 2400 bd MSK)",
            kind="text",
            demodulators=("AM",),
            # 131.550 / 131.725 / 131.825 MHz etc.; the 1200/2400 Hz MSK tones need
            # ~3 kHz of audio, and 2400 bd wants >=9 samples/bit, hence 22,050 Hz
            # rather than the 11,025 Hz of the HF text decoders.
            default_bandwidth_hz=8000,
            input_rate=_P1_INPUT_RATE,
            backend="python",
            binaries=(),
            args=("msdr.decoder.pydecoders.acars:AcarsDecoder",),
        ),
        # wwv/dcf77/msf: round 2 (time signals), pydecoders/timecode.py.
        DecoderSpec(
            id="wwv",
            label="WWV / WWVH time code",
            kind="time",
            demodulators=("AM",),
            default_bandwidth_hz=6000,
            input_rate=_PYTHON_INPUT_RATE,
            backend="python",
            binaries=(),
            args=("msdr.decoder.pydecoders.timecode:WwvDecoder",),
            phase=1,
        ),
        DecoderSpec(
            id="dcf77",
            label="DCF77 time code",
            kind="time",
            demodulators=("AM",),
            # 77.5 kHz -- needs HDR (Ant B).
            default_bandwidth_hz=1000,
            input_rate=_PYTHON_INPUT_RATE,
            backend="python",
            binaries=(),
            args=("msdr.decoder.pydecoders.timecode:Dcf77Decoder",),
            phase=1,
        ),
        DecoderSpec(
            id="msf",
            label="MSF time code",
            kind="time",
            demodulators=("AM",),
            # 60 kHz -- needs HDR (Ant B).
            default_bandwidth_hz=1000,
            input_rate=_PYTHON_INPUT_RATE,
            backend="python",
            binaries=(),
            args=("msdr.decoder.pydecoders.timecode:MsfDecoder",),
            phase=1,
        ),
        # jjy/wwvb/chu: same module. LF time signals need HDR (Ant B) like
        # DCF77/MSF; CHU is a USB-audio FSK burst, not an AM pulse.
        DecoderSpec(
            id="jjy",
            label="JJY time code",
            kind="time",
            demodulators=("AM",),
            default_bandwidth_hz=1000,  # 40 / 60 kHz
            input_rate=_PYTHON_INPUT_RATE,
            backend="python",
            binaries=(),
            args=("msdr.decoder.pydecoders.timecode:JjyDecoder",),
            phase=1,
        ),
        DecoderSpec(
            id="wwvb",
            label="WWVB time code",
            kind="time",
            demodulators=("AM",),
            default_bandwidth_hz=1000,  # 60 kHz
            input_rate=_PYTHON_INPUT_RATE,
            backend="python",
            binaries=(),
            args=("msdr.decoder.pydecoders.timecode:WwvbDecoder",),
            phase=1,
        ),
        DecoderSpec(
            id="chu",
            label="CHU time code",
            kind="time",
            demodulators=("USB",),
            default_bandwidth_hz=3000,  # 3330 / 7850 / 14670 kHz; tones at 2025/2225 Hz
            input_rate=_PYTHON_INPUT_RATE,
            backend="python",
            binaries=(),
            args=("msdr.decoder.pydecoders.timecode:ChuDecoder",),
            phase=1,
        ),
        # psk31/ale: 8 kHz so a 31.25 baud / 125 baud symbol is a whole number
        # of samples (256 / 64).
        DecoderSpec(
            id="psk31",
            label="PSK31",
            kind="text",
            demodulators=("USB",),
            default_bandwidth_hz=3000,  # finds the strongest carrier in the passband
            input_rate=8000,
            backend="python",
            binaries=(),
            args=("msdr.decoder.pydecoders.psk31:Psk31Decoder",),
            phase=1,
        ),
        DecoderSpec(
            id="ale",
            label="HF ALE (2G)",
            kind="text",
            demodulators=("USB",),
            default_bandwidth_hz=3000,  # 8-FSK tones 750-2500 Hz
            input_rate=8000,
            backend="python",
            binaries=(),
            args=("msdr.decoder.pydecoders.ale:AleDecoder",),
            phase=1,
        ),
        # AIS on the flat NFM discriminator (48 kHz = 5 samples/symbol),
        # in-process (pydecoders/ais.py). Synthetic-validated only; field
        # validation pending. normalize=False: the decoder is scale-free, and
        # the Normalizer's instant-attack AGC would step the gain mid-burst
        # on an FM click, after the DC estimate was taken from the training
        # sequence.
        DecoderSpec(
            id="ais",
            label="AIS (GMSK 9600)",
            kind="text",
            demodulators=("NFM",),
            default_bandwidth_hz=25_000,
            input_rate=48_000,
            backend="python",
            binaries=(),
            args=("msdr.decoder.pydecoders.ais:AisDecoder",),
            normalize=False,
            iq_only=True,
            phase=1,
        ),
    )
}


# Raw-IQ decoders are each implemented by their own process or in-process
# decoder. They never depend on an SDRangel server.
for _spec in (
    DecoderSpec(
        id="adsb",
        label="ADS-B / Mode S",
        kind="text",
        demodulators=("IQ",),
        default_bandwidth_hz=2_400_000,
        input_rate=2_400_000,
        # msdr's own native numpy demodulator (adsb_phy.py/adsb.py), ported
        # from SDRangel's ADS-B demod -- no external dependency. Gate passed
        # (docs/adsb-native.md): bit-exact on published reference frames,
        # CRC-24 rejects single-bit corruption, real-time factor 0.016 at
        # 2 MS/s. dump1090-fa remains available via StandaloneADSBBackend
        # (standalone_adsb.py, backend="standalone_adsb"); not currently
        # wired to a live runtime toggle (same precedent as LoRa's
        # GrLoRaBackend cross-check).
        backend="standalone_protocol",
        binaries=(),
        args=("msdr.decoder.pydecoders.adsb:NativeADSBBackend",),
        normalize=False,
        iq_only=True,
        install_hint=(
            "Built in; no install needed. Alternative: FlightAware dump1090-fa "
            "(brew install dump1090-fa)."
        ),
    ),
    DecoderSpec(
        id="rs41",
        label="RS41 radiosonde",
        kind="text",
        demodulators=("IQ",),
        default_bandwidth_hz=20_000,
        input_rate=57_600,
        backend="standalone_protocol",
        binaries=(),
        args=("msdr.decoder.pydecoders.rs41:RS41Backend",),
        normalize=False,
        iq_only=True,
    ),
    DecoderSpec(
        id="orbcomm",
        label="Orbcomm satellite downlink",
        kind="text",
        demodulators=("IQ",),
        default_bandwidth_hz=25_000,
        input_rate=57_600,
        backend="standalone_protocol",
        binaries=(),
        args=("msdr.decoder.pydecoders.orbcomm:OrbcommBackend",),
        normalize=False,
        iq_only=True,
    ),
    DecoderSpec(
        id="lora",
        label="LoRa / Meshtastic / MeshCore",
        kind="text",
        demodulators=("IQ",),
        default_bandwidth_hz=250_000,
        input_rate=250_000,
        backend="standalone_protocol",
        binaries=(),
        args=("msdr.decoder.pydecoders.lora:LoRaBackend",),
        normalize=False,
        iq_only=True,
        # LoRaBackend is msdr's own native numpy receiver (no external
        # dependency); gr-lora_sdr only backs the optional GrLoRaBackend
        # cross-check (see msdr/decoder/pydecoders/lora.py).
        install_hint=(
            "Built in; no install needed. Optional gr-lora_sdr cross-check backend: "
            "install GNU Radio 3.10 and gr-lora_sdr in the same Python environment "
            "(https://github.com/tapparelj/gr-lora_sdr#installation)."
        ),
    ),
    DecoderSpec(
        id="ft8",
        label="FT8",
        kind="text",
        demodulators=("IQ",),
        default_bandwidth_hz=3_000,
        input_rate=48_000,
        backend="standalone_ft8",
        binaries=("jt9",),
        args=(),
        normalize=False,
        iq_only=True,
        install_hint=(
            "Install WSJT-X; jt9 is discovered on PATH or in "
            "~/Applications/wsjtx.app and /Applications/wsjtx.app."
        ),
    ),
    DecoderSpec(
        id="dsc",
        label="DSC",
        kind="text",
        demodulators=("IQ",),
        default_bandwidth_hz=3_000,
        input_rate=9_600,
        backend="standalone_dsc",
        binaries=(),
        args=(),
        normalize=False,
        iq_only=True,
        install_hint=(
            "In-process MF/HF and VHF channel 70 demodulator; semantic decoding is in progress."
        ),
    ),
    DecoderSpec(
        id="endoftrain",
        label="End-of-Train",
        kind="text",
        demodulators=("IQ",),
        default_bandwidth_hz=12_500,
        input_rate=48_000,
        backend="standalone_protocol",
        binaries=(),
        args=("msdr.decoder.pydecoders.endoftrain:EndOfTrainBackend",),
        normalize=False,
        iq_only=True,
    ),
    DecoderSpec(
        id="vor",
        label="VOR",
        kind="text",
        demodulators=("IQ",),
        default_bandwidth_hz=20_000,
        input_rate=48_000,
        backend="standalone_navigation",
        binaries=(),
        args=(),
        normalize=False,
        iq_only=True,
    ),
    DecoderSpec(
        id="ils",
        label="ILS",
        kind="text",
        demodulators=("IQ",),
        default_bandwidth_hz=15_000,
        input_rate=48_000,
        backend="standalone_navigation",
        binaries=(),
        args=(),
        normalize=False,
        iq_only=True,
    ),
):
    DECODERS[_spec.id] = _spec


# Decoders backed by external programs (backends in external_decoders.py).
_EXTERNAL_SPECS: list[DecoderSpec] = [
    # 250 kS/s is rtl_433's default and what most 433/868/915 MHz protocols are tuned for.
    DecoderSpec(
        id="rtl433",
        label="ISM sensors (rtl_433)",
        kind="text",
        demodulators=("IQ",),
        default_bandwidth_hz=200_000,
        input_rate=250_000,
        backend="external_iq",
        binaries=("rtl_433",),
        args=(),
        normalize=False,
        iq_only=True,
        install_hint=_RTL433_INSTALL_HINT,
    ),
    # Wireless M-Bus modes S/T/C&T need 1-1.2 MS/s in rtl_433 (protocols 105, 238, 104).
    DecoderSpec(
        id="rtl433_wmbus",
        label="Wireless M-Bus (rtl_433)",
        kind="text",
        demodulators=("IQ",),
        default_bandwidth_hz=200_000,
        input_rate=1_000_000,
        backend="external_iq",
        binaries=("rtl_433",),
        args=("-R", "105", "-R", "238"),
        normalize=False,
        iq_only=True,
        install_hint=_RTL433_INSTALL_HINT,
    ),
    *(
        DecoderSpec(
            id=f"freedv{mode.lower()}",
            label=f"FreeDV {mode}",
            kind="text",
            demodulators=("USB",),
            default_bandwidth_hz=2_500,
            input_rate=8_000,
            backend="freedv",
            binaries=("freedv_rx",),
            args=(mode,),
            install_hint=_FREEDV_INSTALL_HINT,
        )
        for mode in ("700D", "700E", "1600")
    ),
    # Flags per each demodulator's own option parser: --iq0 reads IQ WAV (FM demod inside);
    # rs92mod positions need an ephemeris/almanac file we do not supply, so RS92 usually
    # reports telemetry without a map position.
    *(
        DecoderSpec(
            id=f"sonde_{name}",
            label=f"{label} radiosonde",
            kind="text",
            demodulators=("IQ",),
            default_bandwidth_hz=20_000,
            input_rate=48_000,
            backend="external_iq",
            binaries=(binary,),
            args=args,
            normalize=False,
            iq_only=True,
            install_hint=_SONDE_INSTALL_HINT.format(binary=binary),
        )
        for name, label, binary, args in (
            ("dfm", "DFM-06/09/17", "dfm09mod", ("--json", "--ecc", "--iq0")),
            ("m10", "Meteomodem M10", "m10mod", ("--json", "--ptu", "--iq0")),
            ("m20", "Meteomodem M20", "m20mod", ("--json", "--ptu", "--iq0")),
            ("rs92", "Vaisala RS92", "rs92mod", ("--json", "--crc", "--ecc", "--vel", "--iq0")),
        )
    ),
]
for _spec in _EXTERNAL_SPECS:
    DECODERS[_spec.id] = _spec


def _jt9_bundle_candidates() -> list[Path]:
    # Native WSJT-X macOS packages keep dependent libraries beside the executable.
    return [
        root / name / "Contents/MacOS/jt9"
        for root in (Path.home() / "Applications", Path("/Applications"))
        for name in ("wsjtx.app", "WSJT-X.app")
    ]


def resolve_binaries(bin_dirs: list[str]) -> dict[str, str | None]:
    """Resolve every binary name referenced by DECODERS to an absolute path.

    bin_dirs are checked first (in order, ~ expanded), then $PATH via
    shutil.which -- so a user-built multimon-ng in ~/.local/bin wins over
    nothing being on PATH, but a system minimodem from brew still resolves.
    """
    names = sorted({name for spec in DECODERS.values() for name in spec.binaries})
    dirs = [Path(d).expanduser() for d in bin_dirs]
    resolved: dict[str, str | None] = {}
    for name in names:
        candidates = ("dump1090-fa", "dump1090") if name == "dump1090" else (name,)
        found = None
        for d in dirs:
            for candidate_name in candidates:
                candidate = d / candidate_name
                if candidate.is_file() and shutil.os.access(candidate, shutil.os.X_OK):
                    found = str(candidate)
                    break
            if found is not None:
                break
        if found is None:
            for candidate_name in candidates:
                found = shutil.which(candidate_name)
                if found is not None:
                    break
        if found is None and name == "jt9":
            found = next(
                (
                    str(path)
                    for path in _jt9_bundle_candidates()
                    if path.is_file() and shutil.os.access(path, shutil.os.X_OK)
                ),
                None,
            )
        resolved[name] = found
    return resolved


def effective_binaries(spec: DecoderSpec, binaries: dict[str, str | None]) -> tuple[str, ...]:
    """Which binary name(s) actually gate `spec` being usable.

    A spec with `alt_backend` prefers it (e.g. direwolf) when that binary
    resolves; otherwise its own `backend`/`binaries` apply. Mirrors the
    preference build_backend() applies when it picks an argv.
    """
    if spec.alt_backend is not None:
        alt_name, _ = spec.alt_backend
        if binaries.get(alt_name):
            return (alt_name,)
    return spec.binaries


def compatibility_issue(spec: DecoderSpec) -> str:
    return ""


def available(bin_dirs: list[str]) -> dict[str, bool]:
    binaries = resolve_binaries(bin_dirs)
    return {
        spec.id: not compatibility_issue(spec)
        and all(binaries.get(name) for name in effective_binaries(spec, binaries))
        for spec in DECODERS.values()
    }


def describe(bin_dirs: list[str], session: dict | None = None) -> list[dict]:
    """API payload for GET /api/decoders.

    `session` is manager.status() (or None), forwarded to readiness_for() so
    each entry's `readiness.session` reflects what this decoder is actually
    doing right now rather than always reporting idle.
    """
    # Imported here, not at module level: readiness.py imports capability_matrix
    # imports msdr.contracts.readiness (pydantic) -- kept out of registry.py's
    # own import chain (dataclass-only, no pydantic) except inside this call.
    from msdr.decoder.readiness import readiness_for

    binaries = resolve_binaries(bin_dirs)
    result = []
    for spec in DECODERS.values():
        names = effective_binaries(spec, binaries)
        issue = compatibility_issue(spec)
        ok = not issue and all(binaries.get(name) for name in names)
        result.append(
            {
                "id": spec.id,
                "label": spec.label,
                "kind": spec.kind,
                "demodulators": list(spec.demodulators),
                "default_bandwidth_hz": spec.default_bandwidth_hz,
                "phase": spec.phase,
                "available": ok,
                "binary": binaries.get(names[0]) if names else None,
                "install_hint": issue or spec.install_hint,
                "readiness": readiness_for(spec, binaries, session).as_api(),
            }
        )
    return result
