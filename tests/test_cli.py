import subprocess
import sys

from dls_plc_tools import __version__


def test_cli_version():
    cmd = [sys.executable, "-m", "dls_plc_tools", "--version"]
    assert subprocess.check_output(cmd).decode().strip() == __version__
