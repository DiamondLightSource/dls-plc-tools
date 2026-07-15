import os

import pytest

# The GUI tests run headless; select the offscreen Qt platform before any
# QApplication is created.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    qtw = pytest.importorskip("PySide6.QtWidgets")
    try:
        app = qtw.QApplication.instance() or qtw.QApplication([])
    except Exception as exc:  # no usable Qt platform (e.g. missing libGL)
        pytest.skip(f"Qt unavailable: {exc}")
    yield app
