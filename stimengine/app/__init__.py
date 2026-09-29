"""The PC app hub: one local web app for the boxes, the M5 remote and the player.

    py -3.13 -m stimengine.app [--port 8320] [--no-browser]

It runs with no box connected. The engine (stimengine.tools.serve, which serves foc312 on :8322) runs as a child
process for the box chosen on the Boxes page, so a box's port can be released for flashing and boxes switched without
restarting anything. Firmware flashing, the remote's loader and Wi-Fi pairing run as jobs (subprocesses of the same
tools used from the command line) whose output the page shows line by line.
"""
