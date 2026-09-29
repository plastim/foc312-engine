"""PC side of the M5 remote (notes/m5-remote.md): pattern packs, the remote's settings file, and the USB loader.

The M5 runs foc312 from remote/core (C). This package builds what it needs from this PC's own data - the
ET-312 built-in modes from the user's firmware data (et312/fwdata.py), ErosLink routines from the local cache and
routine folder, our own routines - and loads it onto the remote over USB.
"""
