"""Shared test setup.

ET-312 built-in modes: their program blocks are ErosTek's and are not in the source (stimengine/et312/fwdata.py).
Tests that need them are marked `needs_et312_data` and skip when no firmware data is available, as in a public
checkout. STIM_ENGINE_NO_ET312_DATA=1 runs the suite as if the data were absent.
"""
import os

import pytest

from stimengine.et312 import fwdata

if os.environ.get("STIM_ENGINE_NO_ET312_DATA") == "1":
    fwdata.set_default(None)

# never read or fill the user's real firmware download cache (stimengine/app/updates.py): one temp folder per run
if not os.environ.get("FOC312_FIRMWARE_CACHE"):
    import tempfile
    os.environ["FOC312_FIRMWARE_CACHE"] = tempfile.mkdtemp(prefix="foc312-test-cache-")


def pytest_configure(config):
    config.addinivalue_line("markers", "needs_et312_data: needs the ET-312 built-in mode data (fwdata.py)")


def pytest_collection_modifyitems(config, items):
    if fwdata.default() is not None:
        return
    skip = pytest.mark.skip(reason="no ET-312 firmware data (built-in modes); see stimengine/et312/fwdata.py")
    for item in items:
        if "needs_et312_data" in item.keywords:
            item.add_marker(skip)
