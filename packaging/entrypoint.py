"""PyInstaller entry shim.

Freezing ``dls_plc_tools/__main__.py`` directly makes it the top-level script,
which has no parent package, so its ``from . import ...`` relative imports raise
``ImportError: attempted relative import with no known parent package``. Import
the package *absolutely* here instead: that pulls ``dls_plc_tools`` in as a real
package, so its internal relative imports resolve. The spec freezes this file.
"""

from dls_plc_tools.__main__ import main

if __name__ == "__main__":
    main()
