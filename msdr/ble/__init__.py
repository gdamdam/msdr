"""Passive BLE reception through a raw-HCI USB dongle (docs/ble.md).

Nothing here imports `msdr.radio`, `msdr.api` or `msdr.recorder`: like
`msdr.wifi`, this is an independent receiver that only produces an in-memory
table of what it heard.
"""
