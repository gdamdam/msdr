# Third-party notices

The GPL-3.0-only license applies to msdr's original code. Third-party code and
reference data retain their respective rights and terms; this project does not
relicense them.

## Lit

`msdr/ui/vendor/lit.js` is the vendored Lit 3.2.1 ESM bundle. Its embedded
copyright notices are retained. Lit is licensed under BSD-3-Clause; the full
upstream notice is included in `msdr/ui/vendor/LIT-LICENSE.txt`.

Upstream: https://github.com/lit/lit
License source: https://github.com/lit/lit/blob/main/LICENSE

## HFCC reference data

`msdr/catalog/data/hfcc/admin.txt`, `broadcas.txt`, and `site.txt` are HFCC
reference tables. `languages.json` was extracted from the HFCC LANGUAGE.TXT
table, as documented in `msdr/catalog/parsers/hfcc.py`.

The repository does not currently document a redistribution license for these
tables. They are not covered by msdr's GPL grant. Before a public distribution,
verify and record the applicable redistribution terms, obtain permission, or
replace/remove the bundled data. Public availability alone is not a license.

## Installed dependencies

Python dependencies are installed separately and retain their upstream licenses.
Their license notices must be preserved when redistributing those dependencies.

## RS41 and EOT payload parsing

`msdr/decoder/payloads.py` adapts payload parsing and checksum logic
from SDRangel's `radiosonde.cpp` / `radiosonde.h` (RS41; copyright Jon
Beniston, 2022) and `endoftrainpacket.cpp` / `crc.cpp` (EOT; copyright Jon
Beniston, 2024 and Eric Reuter, 2018). The adaptation is licensed under
GPL-3.0-or-later, consistent with SDRangel. DSC UDP output is retained as
hexadecimal symbols; its message-level decoder was not ported.
The CRC source also credits maintech GmbH (2012), Christian Daniel,
Edouard Griffiths (2015–2020), and Jon Beniston (2020).

Upstream: [SDRangel](https://github.com/f4exb/sdrangel)

Sources: [RS41 parser](https://github.com/f4exb/sdrangel/blob/master/sdrbase/util/radiosonde.cpp),
[EOT parser](https://github.com/f4exb/sdrangel/blob/master/plugins/channelrx/demodendoftrain/endoftrainpacket.cpp),
[CRC implementation](https://github.com/f4exb/sdrangel/blob/master/sdrbase/util/crc.cpp).

## Native LoRa receiver

`msdr/decoder/pydecoders/lora_phy.py` ports the demodulator structure
(dechirp/FFT symbol demodulation, preamble/sync-word/SFD detection and
synchronisation, Gray mapping, diagonal deinterleaving, Hamming FEC,
whitening, header parsing and CRC) from SDRangel's ChirpChat demodulator,
`plugins/channelrx/demodchirpchat/` at commit
`030963609f7a667c62259dbcf3cee7522880885c` (GPL-3.0-or-later, Copyright (C)
2019-2020 Edouard Griffiths, F4EXB; its own LoRa decoder notes "Inspired by:
https://github.com/myriadrf/LoRa-SDR"). Files consulted:
`chirpchatdemodsink.cpp/.h`, `chirpchatdemoddecoderlora.cpp/.h`,
`chirpchatdemoddecoder.cpp`, `chirpchatdemodsettings.*`.

SDRangel's own readme states its LoRa mode is only known to work at SF11/SF12
with low-data-rate optimisation, tested against an RN2483, while LoRa,
Meshtastic and MeshCore traffic mostly uses other settings. The bit
conventions and frame synchronisation are therefore ported from gr-lora_sdr
(Joachim Tapparel et al., EPFL; GPL-3.0 per the repository LICENSE, no
per-file headers) at commit `fbbf6ea6684d00363096929c7ef015cabcfaeb6d`, the
build installed under `.venv-lora` that generated the test vectors:
`lib/frame_sync_impl.cc`, `fft_demod_impl.cc`, `gray_mapping_impl.cc`,
`deinterleaver_impl.cc`, `hamming_dec_impl.cc`, `header_decoder_impl.cc`,
`dewhitening_impl.cc` with `tables.h`, and `crc_verif_impl.cc`. The stage
each file informs is listed in `lora_phy.py`'s module docstring.

The reference vectors in `tests/decoder/data/lora/` are synthetic output of
gr-lora_sdr's transmitter (`scripts/make_lora_vectors.py`); no off-air data.

Upstream: [SDRangel](https://github.com/f4exb/sdrangel),
[gr-lora_sdr](https://github.com/tapparelj/gr-lora_sdr).

## Native ADS-B / Mode S receiver

`msdr/decoder/pydecoders/adsb_phy.py` ports the preamble chip correlation,
PPM bit-slicing and CRC-24/ICAO address-recovery scheme (no single-bit
error correction, matching SDRangel's own policy) from SDRangel's ADS-B
demodulator, `plugins/channelrx/demodadsb/adsbdemodsinkworker.cpp` and
`adsb.h`, at commit `030963609f7a667c62259dbcf3cee7522880885c`
(GPL-3.0-or-later, Copyright (C) 2020 Jon Beniston, M7RCE). The CRC-24
polynomial and per-message-type field decode (identification, airborne
position CPR, airborne velocity) follow the public ICAO Annex 10 Volume IV
Mode S specification as documented in "The 1090 MHz Riddle" by Junzi Sun
(mode-s.org, MIT-licensed) / pyModeS, not SDRangel-specific code; see
[docs/adsb-native.md](docs/adsb-native.md) for the validation gate.
`msdr/decoder/standalone_adsb.py` (the `dump1090-fa` adapter) is unchanged
and remains an alternative backend.

Upstream: [SDRangel](https://github.com/f4exb/sdrangel).

Sources: [ADS-B demod worker](https://github.com/f4exb/sdrangel/blob/master/plugins/channelrx/demodadsb/adsbdemodsinkworker.cpp).

## Native RTTY decoder

`msdr/decoder/pydecoders/rtty.py` ports the mark/space correlator, automatic
threshold correction (ATC), inter-edge-interval baud-rate histogram
estimator (`estimateBaudRate()`) and FFT-based frequency-shift estimator
(`estimateFrequencyShift()`) from SDRangel's RTTY demodulator,
`plugins/channelrx/demodrtty/rttydemodsink.cpp`/`.h` and
`rttydemodsettings.cpp`/`.h`, at commit
`030963609f7a667c62259dbcf3cee7522880885c` (GPL-3.0-or-later, Copyright (C)
2019-2021 Edouard Griffiths F4EXB, 2020-2023 Jon Beniston M7RCE, 2020 Kacper
Michajłow). The ITA2 (Baudot) character tables are from the same commit's
`sdrbase/util/baudot.cpp` (Copyright (C) 2023 Jon Beniston, M7RCE),
`m_ita2Letter`/`m_ita2Figure`. Because this decoder runs on real-valued
SSB/mono audio rather than SDRangel's complex IQ baseband with a user-set
channel centre, it departs from the original in several documented ways
(two absolute-frequency oscillators instead of one +-shift/2 oscillator,
an FFT peak search to locate the tones instead of a tuning knob, dual-
polarity parallel decoding to detect inverted shift instead of a manual
"Mark/Space Frequency" setting, ITA2/LSB-first only, and the sliding-sum
correlator always followed by SDRangel's optional smoothing lowpass stage
("FILTERED_MAV") rather than its plain or raised-cosine filter options) --
see the module's own docstring for the full list. SDRangel's
UK/European/US/Russian/Murray character sets, MSB-first bit order and
squelch threshold are not ported.

Upstream: [SDRangel](https://github.com/f4exb/sdrangel).

Sources: [RTTY demod sink](https://github.com/f4exb/sdrangel/blob/master/plugins/channelrx/demodrtty/rttydemodsink.cpp),
[RTTY demod settings](https://github.com/f4exb/sdrangel/blob/master/plugins/channelrx/demodrtty/rttydemodsettings.cpp),
[Baudot tables](https://github.com/f4exb/sdrangel/blob/master/sdrbase/util/baudot.cpp).

## Leaflet

`msdr/ui/vendor/leaflet.js` and `leaflet.css` are Leaflet 1.9.4, distributed
under the BSD 2-Clause license. Copyright 2010–2023 Volodymyr Agafonkin and
2010–2011 CloudMade. The full notice is in `msdr/ui/vendor/LEAFLET-LICENSE.txt`.
Upstream: https://leafletjs.com/ . Basemap tiles are supplied by OpenStreetMap
with visible attribution; no tiles are bundled or prefetched.

## DSC message semantics and FT8 runtime

`msdr/decoder/pydecoders/dsc_message.py` adapts DSC symbol layouts and field
values from SDRangel's DSC implementation (Jon Beniston, M7RCE, 2023), under
GPL-3.0-or-later. Supported semantic fields are gated by message ECC.

`msdr/decoder/pydecoders/morse_ident.py` ports SDRangel's Morse ident
demodulator and ASCII/Morse code table -- `sdrbase/dsp/morsedemod.cpp`/`.h`
(Copyright Jon Beniston, M7RCE, Edouard Griffiths, F4EXB) and
`sdrbase/util/morse.cpp`/`.h` (additionally maintech GmbH, Christian Daniel)
-- plus the VOR/ILS ident wiring from `plugins/channelrx/demodvor/vordemodsink.cpp`
and `plugins/channelrx/demodils/ilsdemodsink.cpp` (additionally Kacper
Michajlow), commit `030963609f7a667c62259dbcf3cee7522880885c`, under
GPL-3.0-or-later; msdr only exposes an ident after it decodes identically
twice in a row.

FT8 uses the separately installed WSJT-X `jt9` executable. The official
WSJT-X 3.0.2 native arm64 application bundle was used for local validation;
no executable or recording from that distribution is bundled in msdr.

## Frequency allocation data

`msdr/allocations/data/dataset.json` is generated by `msdr/allocations/importer.py`
from the NTIA Manual (Redbook) Table of Frequency Allocations, 2025 edition
(https://redbook.ntia.gov/), and the footnote texts of 47 CFR §2.106 from the
eCFR (https://www.ecfr.gov/), with NTIA's footnote page as a labelled fallback
for 16 footnotes. Both are works of the US government. The ITU columns are the
reproduction of the Radio Regulations (2016 edition) inside the US table; the
ITU Radio Regulations themselves are not bundled. Provenance (URLs, SHA-256,
edition, effective date) is recorded inside the dataset. See
[docs/allocations.md](docs/allocations.md).

## Band plan and satellite list

`msdr/providers/data/bandplan_us.json` and `satellites.json` are hand-compiled
reference tables for this project. Band entries cite NTIA and 47 CFR §90.103
where they carry regulatory context; the satellite list maps labels to NORAD
catalog numbers. Orbital elements are fetched from CelesTrak at runtime and
cached locally; none are bundled.

## Reference catalogs

Dated snapshots under `msdr/catalog/data/`; see
[docs/reference-catalogs.md](docs/reference-catalogs.md).

- `nist_time_stations.json`: from NIST's WWV, WWVH and WWVB station pages.
  NIST states that information on its sites is public and may be distributed
  or copied; a byline is requested: "Source: NIST".
- `faa_nasr_2026-10-01.json.gz`: parsed from the FAA NASR 28-day subscription
  NAV and ILS CSV extracts (cycle 2026-10-01), a US government work. No
  explicit statement of terms was found on the distribution page.
- `ioos_hfradar_2026-10-02.json`: from the IOOS HFRNet station list. The
  data may be used and redistributed for free, without warranty, as stated in
  the IOOS ERDDAP metadata.
- `superdarn_2026-10-01.json`: radar identifiers, locations and hardware
  parameters from the SuperDARN `hdw` repository (no licence file) and
  superdarn.ca radar-info ("all rights reserved"). Only factual identifiers
  and coordinates are stored; SuperDARN radar data use is governed by the
  SuperDARN PI agreement. Verify terms before redistributing this snapshot.

## Passive Wi-Fi capture helper

msdr does not include, link or redistribute driver code. Wi-Fi capture uses
the separately installed `xen-proc/rtl8812au-macos` userspace driver (pinned
commit `347e9f172750e0c0b7c45b6dcfef7e780d70d144`, GPL-2.0-only), which
contains register tables, power sequences and firmware derived from
aircrack-ng/rtl8812au and Realtek sources under GPL-2.0, and a small wrapper
program (`msdr-wifi-capture`, GPL-2.0-only) with its own LICENSE and NOTICE.
msdr starts it as a separate program and reads standard PCAP. Upstream:
https://github.com/xen-proc/rtl8812au-macos . See [docs/wifi.md](docs/wifi.md).

## Passive BLE capture (raw HCI)

BLE reception (`python -m msdr.ble.capture`, docs/ble.md) is msdr's own
GPL-3.0-only code. It talks HCI to a USB Bluetooth dongle through the optional
`pyusb` package (BSD-3-Clause, https://github.com/pyusb/pyusb) and the system
`libusb-1.0` library (LGPL-2.1-or-later, https://libusb.info), loaded at run
time and never bundled. No controller firmware or vendor patch file is shipped
or uploaded; the dongle runs whatever firmware it already has. Company and
service names come from the Bluetooth SIG Assigned Numbers.

## Passive BLE capture (Sniffle)

The SONOFF ZBDongle-P (CC2652P) backend (`msdr/ble/sniffle_proto.py`,
`msdr/ble/sniffle_capture.py`, docs/ble.md) ports the serial wire protocol
and a receive-only subset of the advertising-PDU decoder from
[nccgroup/Sniffle](https://github.com/nccgroup/Sniffle), tag `v1.11.0`,
commit `18252f2c5a05f9ad906865b776652e31c61471c4`, GPL-3.0. Files consulted:
`python_cli/sniffle/sniffle_hw.py` (command encoding, base64/CRLF framing,
`SniffleHW.__init__`/`_send_cmd`/`_recv_msg`), `python_cli/sniffle/constants.py`
(`BLE_ADV_AA`, `BLE_ADV_CRCI`, `PhyMode`), `python_cli/sniffle/measurements.py`
(`VersionMeasurement`), and the advertising-PDU classes of
`python_cli/sniffle/packet_decoder.py` (`AdvertMessage`, `AdvIndMessage` and
siblings, `AdvExtIndMessage`, `AuxAdvIndMessage`/`AuxScanRspMessage`/
`AuxChainIndMessage`, `AuxPtr`, `AdvDataInfo`). The two ported files carry
`SPDX-License-Identifier: GPL-3.0-or-later` headers, matching Sniffle's own
license, rather than msdr's own GPL-3.0-only (THIRD_PARTY_NOTICES.md's
header note: third-party code keeps its own terms).

msdr is receive-only by construction and ports only what passive advertising
sniffing needs: the channel/PHY/RSSI/CRC-validation/MAC-filter-clear/
aux-advertising-follow commands, the marker and version-probe commands, and
connection-following is explicitly disabled (`cmd_follow(False)`) and never
re-enabled. Sniffle's connection-following, initiating, active-scan
(`cmd_scan`), advertise/transmit (`cmd_advertise`, `cmd_advertise_ext`,
`cmd_transmit`), address/IRK and channel-map/interval-preload commands, and
the corresponding decoder state for following a connection
(`ConnectIndMessage`, `AuxConnectReqMessage`/`AuxConnectRspMessage`,
`DataMessage` and its LL subclasses, `ScanReqMessage`/`AuxScanReqMessage`)
are not ported at all -- those code paths do not exist in msdr. Every
received advertising PDU (including each `AUX_CHAIN_IND` continuation of a
long extended advertisement) is decoded and reported independently instead
of reproducing Sniffle's own pending-chain/pending-scan-response state
machine, which exists to support connection-following and active scanning
that msdr does not do; `msdr/ble/observations.py`'s existing per-address
merge handles the result the same way it already merges repeated legacy
reports. `tests/ble/fake_sniffle.py` is an independent fake-firmware test
double speaking the same real wire framing; it is not derived from
Sniffle's own test code.

Sniffle's firmware (`fw/CommandTask.c`, `fw/PacketTask.c`, `fw/messenger.c`)
was consulted for the command/message framing it implements but is not
built, shipped or flashed by msdr. msdr never flashes firmware: a person
installs a Sniffle release build (`sniffle_cc1352p1_cc2652p1.hex` for
CP2102N bridges at 2 Mbaud, or the `_1M.hex` variant with CP2102 (non-N)
bridges at 921600 baud, per the development notes' upstream evidence) onto the dongle
themselves before msdr can use it.

Upstream: https://github.com/nccgroup/Sniffle .

## Remote ID decoding

`msdr/remoteid/` (docs/remote-id.md) ports ASTM F3411 / ASD-STAN Open Drone
ID message encode/decode semantics from `opendroneid/opendroneid-core-c`
(https://github.com/opendroneid/opendroneid-core-c), copyright 2019 Intel
Corporation and copyright 2020 Simon Wunderlich, Marek Sobe and Doodle Labs,
at commit `6484f26545d4f012682524e2d843fab0fbdc0b34`. Files consulted:
`libopendroneid/opendroneid.c`/`.h` and `libopendroneid/wifi.c`/
`odid_wifi.h`.

opendroneid-core-c is licensed Apache License, Version 2.0
(http://www.apache.org/licenses/LICENSE-2.0); no modified copy of its source
is included in msdr -- `msdr/remoteid/messages.py` and `transport.py` are a
hand-written Python port carrying their own
`SPDX-License-Identifier: GPL-3.0-only` and a NOTICE-equivalent file header
naming the project, the commit above and the Apache-2.0 license, per Apache
License 2.0 section 4(b)/(d). As required by section 4, this notice
preserves that attribution; no NOTICE file was present in the upstream
distribution at that commit. msdr remains GPL-3.0-only overall; Apache-2.0
is compatible with inclusion in a GPL-3.0 work. The BLE legacy
advertising framing in `transport.py` (service data UUID 0xFFFA, app code
0x0D) follows the same ASTM F3411 / ASD-STAN specification but was not
copied or ported from opendroneid-core-c's Bluetooth encoder (not among the
files consulted).

`tests/remoteid/gen_vectors.c` links the same opendroneid-core-c sources
unmodified (via a small macOS `<byteswap.h>` compatibility shim, not part of
msdr) to generate `tests/remoteid/data/vectors.json`, an independent
cross-check fixture; see that directory's README for provenance and the
regeneration command. Neither the generator nor the opendroneid-core-c
sources it links are built, shipped or imported by msdr itself.

## Separately installed decoders and tools

WSJT-X `jt9`, multimon-ng, minimodem, Dire Wolf, dsd-fme, dump1090, nextpass
and iqscan are external programs installed by the user and called as
separate processes. None are bundled; each keeps its own licence.

## Test fixtures

- `tests/decoder/data/martin_m1_qsstv_sunset.wav`: "SSTV sunset audio" by
  Mysid, Wikimedia Commons, CC BY 2.5
  (https://creativecommons.org/licenses/by/2.5). Converted to 11,025 Hz 8-bit
  mono PCM; details in `martin_m1_qsstv_sunset.LICENSE.txt`.
- `tests/ble/fake_helper.py` sample reports: four advertising payloads shaped
  after a local passive scan on 2026-10-02, with addresses rewritten.

## SSTV mode specifications

SSTV timings and VIS codes follow JL Barber (N7CXI), "Proposal for SSTV Mode
Specifications" (Dayton SSTV forum, 20 May 2000), cited in [docs/sstv.md](docs/sstv.md).

