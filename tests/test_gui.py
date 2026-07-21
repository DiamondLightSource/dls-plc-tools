"""Headless tests for the GUI's connection-mode routing and field state.

The worker thread's slots are disconnected and replaced with spies so no real
kubectl/aphyt call happens; we only assert what MainWindow emits.
"""

from types import SimpleNamespace

import pytest

# Skip (rather than error) where the Qt platform libraries are unavailable,
# e.g. a minimal container without libGL. CI runners have them.
try:
    from dls_plc_tools.gui import MainWindow
except ImportError as exc:
    pytest.skip(f"Qt unavailable: {exc}", allow_module_level=True)


@pytest.fixture
def gui(qapp):
    """A MainWindow whose request signals are diverted from the worker thread
    into spy lists, so emitting them records the call instead of running kubectl
    / connecting to a PLC."""
    win = MainWindow()
    win.request_connect.disconnect()
    win.request_connect_forward.disconnect()
    win.request_nodes.disconnect()  # don't let ticking the box run real kubectl
    direct: list = []
    forward: list = []  # tuples of (plc_ip, node, service_ip, port, cluster)
    nodes: list = []  # cluster names the Node-refresh was requested for
    win.request_connect.connect(lambda ip: direct.append(ip))
    win.request_connect_forward.connect(lambda *a: forward.append(a))
    win.request_nodes.connect(lambda c: nodes.append(c))
    yield SimpleNamespace(win=win, direct=direct, forward=forward, nodes=nodes)
    win.close()


def test_forward_fields_start_disabled(gui):
    assert not gui.win.node_edit.isEnabled()
    assert not gui.win.service_ip_edit.isEnabled()
    assert not gui.win.port_spin.isEnabled()


def test_checkbox_enables_fields(gui):
    gui.win.pfwd_check.setChecked(True)
    assert gui.win.node_edit.isEnabled()
    assert gui.win.service_ip_edit.isEnabled()
    assert gui.win.port_spin.isEnabled()
    gui.win.pfwd_check.setChecked(False)
    assert not gui.win.node_edit.isEnabled()


def test_direct_connect_when_unchecked(gui):
    gui.win.ip_edit.setText("192.168.8.10")
    gui.win._connect_clicked()
    assert gui.direct == ["192.168.8.10"]
    assert gui.forward == []


def test_forward_connect_when_checked(gui):
    gui.win.ip_edit.setText("192.168.8.10")
    gui.win.pfwd_check.setChecked(True)
    gui.win.node_edit.setText("sr08c-node.example")
    gui.win.service_ip_edit.setText("172.23.206.158")
    gui.win._connect_clicked()
    assert gui.direct == []
    assert gui.forward == [
        ("192.168.8.10", "sr08c-node.example", "172.23.206.158", 44818, "acastus")
    ]


def test_port_override_passed_through(gui):
    gui.win.ip_edit.setText("192.168.8.10")
    gui.win.pfwd_check.setChecked(True)
    gui.win.node_edit.setText("node")
    gui.win.service_ip_edit.setText("172.23.206.158")
    gui.win.port_spin.setValue(2222)
    gui.win._connect_clicked()
    assert gui.forward[0][3] == 2222


def test_forward_requires_node_and_service_ip(gui):
    gui.win.ip_edit.setText("192.168.8.10")
    gui.win.pfwd_check.setChecked(True)
    gui.win.service_ip_edit.setText("172.23.206.158")  # node left blank
    gui.win._connect_clicked()
    assert gui.forward == []
    assert gui.direct == []


def test_pod_status_led_updates(gui):
    # LED starts inactive with no pod name; each state recolours the dot.
    assert gui.win.pod_status_label.text() == "inactive"
    assert gui.win.pod_name_label.text() == ""
    seen = set()
    for state in ("pending", "good", "inactive"):
        gui.win._set_pod_status(state)
        assert gui.win.pod_status_label.text() == state
        style = gui.win.pod_led.styleSheet()
        assert "background-color" in style
        seen.add(style)
    assert len(seen) == 3  # a distinct colour per state


def test_pod_name_shown_while_active_and_cleared(gui):
    gui.win._set_pod_status("good", "pfwd-tester-44818")
    assert gui.win.pod_name_label.text() == "pfwd-tester-44818"
    gui.win._set_pod_status("inactive", "")  # teardown clears the name
    assert gui.win.pod_name_label.text() == ""


def test_ticking_box_requests_node_refresh(gui):
    # Ticking Port forward asks the worker for the cluster's live node list.
    gui.win.pfwd_check.setChecked(True)
    assert gui.nodes == ["acastus"]


def test_nodes_ready_repopulates_combo_keeping_selection(gui):
    combo = gui.win.node_edit
    typed = "sr19c-k8s-serv-01.pri.diamond.ac.uk"  # a typed/prefilled node
    combo.setCurrentText(typed)
    gui.win._on_nodes_ready(["nodeA", "nodeB"])
    items = [combo.itemText(i) for i in range(combo.count())]
    assert items == ["nodeA", "nodeB"]  # static fallback replaced by live list
    # the typed value is preserved even though it isn't in the fetched list
    assert combo.currentText() == typed
