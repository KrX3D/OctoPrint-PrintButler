########################################################################################################################
# OctoPrint-PrintButler setup.py
#
# All plugin metadata lives in octoprint_printbutler/__init__.py as __plugin_*__ variables.
# This file reads them via regex so there is only one place to edit.
########################################################################################################################

import re, os, sys

def _read(attr):
    """Read a single-line __plugin_<attr>__ string value from __init__.py."""
    path = os.path.join(os.path.dirname(__file__), "octoprint_printbutler", "__init__.py")
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    m = re.search(r'__plugin_' + attr + r'__\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else ""

########################################################################################################################

from setuptools import setup

try:
    import octoprint_setuptools
except ImportError:
    print(
        "Could not import OctoPrint's setuptools. Make sure you are running this with "
        "the same Python installation that OctoPrint is installed in."
    )
    sys.exit(-1)

setup_parameters = octoprint_setuptools.create_plugin_setup_parameters(
    identifier  = _read("identifier"),
    package     = "octoprint_printbutler",
    name        = _read("name"),
    version     = _read("version"),
    description = "Print-finished notifications, light/plug automation, and safe shutdown over MQTT.",
    author      = _read("author"),
    mail        = "",
    url         = _read("url"),
    license     = _read("license"),
    requires    = [],
)

if __name__ == "__main__":
    setup(**setup_parameters)
