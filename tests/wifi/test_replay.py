
from msdr.wifi import pcap
from msdr.wifi.replay import replay_file
from tests.wifi.fixtures import build_beacon_frame, build_radiotap


def test_replay_file_end_to_end(tmp_path):
    rt_bytes = build_radiotap(channel_mhz=2437)
    frame1 = build_beacon_frame(bssid="aa:bb:cc:dd:ee:01", ssid=b"CafeWifi", ds_channel=6)
    frame2 = build_beacon_frame(bssid="aa:bb:cc:dd:ee:02", ssid=b"HomeNet", ds_channel=6, subtype=5)

    data = pcap.write_header(linktype=pcap.LINKTYPE_IEEE802_11_RADIOTAP)
    data += pcap.write_record(1.0, rt_bytes + frame1)
    data += pcap.write_record(2.0, rt_bytes + frame2)

    path = tmp_path / "capture.pcap"
    path.write_bytes(data)

    table = replay_file(path, configured_channel=6)
    rows, total = table.rows()
    assert total == 2
    bssids = {r["bssid"] for r in rows}
    assert bssids == {"aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"}
    beacons_row = next(r for r in rows if r["bssid"] == "aa:bb:cc:dd:ee:01")
    probe_row = next(r for r in rows if r["bssid"] == "aa:bb:cc:dd:ee:02")
    assert beacons_row["beacons"] == 1
    assert probe_row["probe_responses"] == 1
