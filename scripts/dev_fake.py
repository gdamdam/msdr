"""Run a fake SDRconnect server so the UI can be exercised without SDRconnect.

    .venv/bin/python scripts/dev_fake.py

Listens on port 5455 by default -- NOT 5454, msdr's default and the port real
SDRconnect binds to -- so this can run alongside a real SDRconnect instance
instead of silently shadowing it. Pass --port to use a different one. Two
receiver profiles are registered, so "apply profile" has real device changes
to confirm against. Runs until interrupted.
"""

import argparse
import asyncio

from msdr.radio.fake_server import FakeSDRconnect


async def main(port: int) -> None:
    fake = FakeSDRconnect(port=port)
    fake.profiles["VHF Sat"] = {
        "active_antenna": "Antenna A",
        "device_sample_rate": 2_000_000,
        "demodulator": "USB",
    }
    fake.profiles["Airband"] = {
        "active_antenna": "Antenna B",
        "demodulator": "AM",
        "filter_bandwidth": 8_000,
    }
    bound_port = await fake.start()
    print(f"fake SDRconnect listening on ws://127.0.0.1:{bound_port}")
    print(f"Run msdr with --port {bound_port}")
    try:
        await asyncio.Event().wait()  # run until Ctrl-C
    except asyncio.CancelledError:
        pass
    finally:
        await fake.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5455, help="port to listen on (default: 5455)")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.port))
    except KeyboardInterrupt:
        pass
