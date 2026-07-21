"""Qt GUI for browsing and live-watching Omron NX PLC tags over EtherNet/IP.

Read-only: every PLC interaction is an explicit-messaging read, the same
mechanism the EPICS ether_ip driver uses.

Usage: dls-read-plc-gui [ip-address]
"""

import argparse
import re
import sys

from aphyt import omron
from aphyt.cip.cip import CIPException
from aphyt.cip.cip_datatypes import CIPArray, CIPStructure
from openpyxl import Workbook
from PySide6.QtCore import QMetaObject, QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from dls_plc_tools.interrogate import INFO_TAGS, USED_MARKER_MEMBER, dims_str
from dls_plc_tools.portforward import EIP_PORT, PortForward, resolve_host

CHANGED_BRUSH = QBrush(QColor("#ffd54f"))  # amber for values that just changed


def _sheet_name(name):
    """Excel sheet names: max 31 chars, no []:*?/\\ characters."""
    return re.sub(r"[\[\]:*?/\\]", "_", name)[:31]


def _cell(value):
    """Coerce a CIP value into something openpyxl can store."""
    return value if isinstance(value, int | float | str | bool) else str(value)


def _fit_columns(sheet, pad=2, cap=60):
    """Size each column to its longest cell (openpyxl has no auto-fit)."""
    for column in sheet.columns:
        longest = max(len(str(c.value)) for c in column if c.value is not None)
        sheet.column_dimensions[column[0].column_letter].width = min(longest + pad, cap)


def _cell_combo(placeholder, items, width):
    """An editable combo prefilled with per-cell choices. Editable so a value
    not in the list (another cell, an explicit IP, a one-off node) can be typed
    over the selection; starts blank rather than preselecting sr01c."""
    combo = QComboBox()
    combo.setEditable(True)
    combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    combo.addItems(items)
    combo.setMaximumWidth(width)
    combo.setCurrentIndex(-1)
    line = combo.lineEdit()
    if line is not None:
        line.setPlaceholderText(placeholder)
    return combo


class PlcWorker(QObject):
    """Owns the aphyt connection; lives in a QThread so reads never block the UI."""

    connected = Signal(str)
    tags_ready = Signal(list)
    # name, {member: values}, {member: type}, array start index (0- or 1-based)
    struct_ready = Signal(str, object, object, int)
    exported = Signal(str)
    failed = Signal(str)
    progress = Signal(str)  # human-readable phase updates during a slow connect

    def __init__(self):
        super().__init__()
        self._eip = None
        self._pfwd = None  # active PortForward, if we connected through one

    def _open_session(self, host, port):
        """Open a read-only explicit-messaging session to host:port and emit the
        info-tag header. aphyt hardcodes 44818, so a non-default port is pushed
        onto the dispatcher before connecting."""
        eip = omron.NSeries()
        if port != EIP_PORT:
            eip.connected_cip_dispatcher.explicit_message_port = port
        eip.connect_explicit(host, connection_timeout=5.0)
        eip.register_session()
        self._eip = eip
        info = [host]
        for tag in INFO_TAGS:
            try:
                info.append(f"{tag}={eip.read_variable(tag)}")
            except CIPException:
                pass
        self.connected.emit("   ".join(info))

    @Slot(str)
    def connect_plc(self, ip):
        try:
            self.disconnect_plc()
            self._open_session(ip, EIP_PORT)
        except Exception as e:
            self.failed.emit(f"connect: {e}")

    @Slot(str, str, str, int)
    def connect_plc_forward(self, plc_ip, node, service_ip, port):
        try:
            self.disconnect_plc()
            # The Service IP field accepts a literal IP or a DNS name (e.g.
            # sr19c-pfwd); resolve to the literal IP loadBalancerIP/MetalLB and
            # aphyt all need.
            self.progress.emit(f"resolving {service_ip}...")
            service_ip = resolve_host(service_ip)
            pfwd = PortForward()
            pfwd.up(plc_ip, node, service_ip, port, progress=self.progress.emit)
            self._pfwd = pfwd
            self.progress.emit(f"forward up; connecting to {service_ip}:{port}...")
            self._open_session(service_ip, port)
        except Exception as e:
            # Tear down a forward that came up but then failed to connect.
            self.disconnect_plc()
            self.failed.emit(f"connect: {e}")

    @Slot()
    def disconnect_plc(self):
        if self._eip is not None:
            try:
                self._eip.close_explicit()
            except Exception:
                pass
            self._eip = None
        if self._pfwd is not None:
            self.progress.emit("removing port forward...")
            try:
                self._pfwd.down()
            except Exception as e:
                # Surface a failed teardown rather than silently leaking the pod.
                self.failed.emit(f"teardown: {e}")
            else:
                self.progress.emit("port forward removed")
            self._pfwd = None

    @Slot()
    def list_tags(self):
        if self._eip is None:
            self.failed.emit("not connected")
            return
        try:
            self._eip.update_variable_dictionary()
            user = self._eip.connected_cip_dispatcher.user_variables
            rows = []
            for name, dtype in sorted(user.items()):
                if isinstance(dtype, CIPStructure):
                    rows.append((name, dtype.variable_type_name))
                else:
                    rows.append((name, type(dtype).__name__))
            self.tags_ready.emit(rows)
        except Exception as e:
            self.failed.emit(f"list tags: {e}")

    @Slot(str)
    def read_struct(self, name):
        if self._eip is None:
            self.failed.emit("not connected")
            return
        try:
            result = self._eip.read_variable(name)
            columns = {}
            types = {}
            start = 0  # PLC array index of value list position 0 (structs are 1-based)
            if isinstance(result, CIPStructure):
                for member, value in result.members.items():
                    if isinstance(value, CIPArray):
                        columns[member] = value.value()
                        start = value.start_array_elements[0]
                        element = type(value.local_cip_data_type_object).__name__
                        types[member] = f"ARRAY[{dims_str(value)}] OF {element}"
                    else:
                        columns[member] = [value.value()]
                        types[member] = type(value).__name__
            elif isinstance(result, CIPArray):
                start = result.start_array_elements[0]
                element = type(result.local_cip_data_type_object).__name__
                columns[name] = result.value()
                types[name] = f"ARRAY[{dims_str(result)}] OF {element}"
            else:  # scalar tag: one-cell table
                columns[name] = [result]
                types[name] = type(result).__name__
            self.struct_ready.emit(name, columns, types, start)
        except Exception as e:
            self.failed.emit(f"read {name}: {e}")

    @Slot(str)
    def export_structs(self, path):
        """Dump every published structure to an Excel workbook, one sheet per
        structure: rows are members, columns are instance indices."""
        if self._eip is None:
            self.failed.emit("not connected")
            return
        try:
            self._eip.update_variable_dictionary()
            user = self._eip.connected_cip_dispatcher.user_variables
            structs = sorted(n for n, d in user.items() if isinstance(d, CIPStructure))
            workbook = Workbook()
            default_sheet = workbook.active
            if default_sheet is not None:
                workbook.remove(default_sheet)
            for name in structs:
                result = self._eip.read_variable(name)
                if not isinstance(result, CIPStructure):
                    continue
                rows = {}
                width = 1
                start = (
                    0  # PLC index of column 0 (structs are declared ARRAY[start..N])
                )
                for member, value in result.members.items():
                    if isinstance(value, CIPArray):
                        rows[member] = value.value()
                        start = value.start_array_elements[0]
                    else:
                        rows[member] = [value.value()]
                    width = max(width, len(rows[member]))
                sheet = workbook.create_sheet(_sheet_name(name))
                sheet.append([name] + list(range(start, start + width)))
                for member, values in rows.items():
                    sheet.append([member] + [_cell(v) for v in values])
                sheet.freeze_panes = "B2"
                _fit_columns(sheet)
            workbook.save(path)
            self.exported.emit(f"exported {len(structs)} structures to {path}")
        except Exception as e:
            self.failed.emit(f"export: {e}")


class MainWindow(QMainWindow):
    request_connect = Signal(str)
    # (plc_ip, node, service_ip, port)
    request_connect_forward = Signal(str, str, str, int)
    request_tags = Signal()
    request_struct = Signal(str)
    request_export = Signal(str)
    request_disconnect = Signal()

    def __init__(self, ip=None):
        super().__init__()
        self.setWindowTitle("DLS PLC tools")
        self.resize(1100, 650)
        self._waiting = False
        self._current_struct = None
        self._previous = {}

        self._worker = PlcWorker()
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self.request_connect.connect(self._worker.connect_plc)
        self.request_connect_forward.connect(self._worker.connect_plc_forward)
        self.request_tags.connect(self._worker.list_tags)
        self.request_struct.connect(self._worker.read_struct)
        self.request_export.connect(self._worker.export_structs)
        self.request_disconnect.connect(self._worker.disconnect_plc)
        self._worker.connected.connect(self._on_connected)
        self._worker.tags_ready.connect(self._on_tags)
        self._worker.struct_ready.connect(self._on_struct)
        self._worker.exported.connect(self._on_exported)
        self._worker.progress.connect(self._on_progress)
        self._worker.failed.connect(self._on_failed)
        self._thread.start()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)

        # Top bar: connection
        # TEMP (sr19c testing convenience): prefill the sr19c forward so Connect
        # works with no typing. Revert these defaults before merging.
        self.ip_edit = QLineEdit(ip or "192.168.19.10")
        self.ip_edit.setPlaceholderText("e.g. 172.23.241.21")
        self.ip_edit.setMaximumWidth(160)
        # Port forwarding: reach a PLC on a private per-CIA network via a socat
        # forward on the cluster. Off by default; ticking it enables the fields
        # below and routes Connect through the forward (see _connect_clicked).
        self.pfwd_check = QCheckBox("Port forward")
        self.pfwd_check.setChecked(True)  # TEMP: testing default
        # sr01c..sr24c pickers for the two per-cell fields. Both combos are
        # editable so a value not in the list (another cell, an explicit IP, a
        # one-off node) can be typed over the selection.
        self.node_edit = _cell_combo(
            "node (full name)",
            [f"sr{n:02d}c-k8s-serv-01.pri.diamond.ac.uk" for n in range(1, 25)],
            width=260,
        )
        self.node_edit.setEnabled(True)  # TEMP: enabled because pfwd is prechecked
        self.node_edit.setCurrentText(  # TEMP: testing default
            "sr19c-k8s-serv-01.pri.diamond.ac.uk"
        )
        self.service_ip_edit = _cell_combo(
            "service IP or DNS name",
            [f"sr{n:02d}c-pfwd" for n in range(1, 25)],
            width=180,
        )
        self.service_ip_edit.setEnabled(True)  # TEMP
        self.service_ip_edit.setCurrentText("sr19c-pfwd")  # TEMP: testing default
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(EIP_PORT)
        self.port_spin.setEnabled(True)  # TEMP
        self.connect_btn = QPushButton("Connect")
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.setEnabled(False)  # nothing to disconnect until connected
        self.info_label = QLabel("not connected")
        top = QHBoxLayout()
        top.addWidget(QLabel("IP address:"))
        top.addWidget(self.ip_edit)
        top.addWidget(self.pfwd_check)
        top.addWidget(QLabel("Node:"))
        top.addWidget(self.node_edit)
        top.addWidget(QLabel("Service IP:"))
        top.addWidget(self.service_ip_edit)
        top.addWidget(QLabel("Port:"))
        top.addWidget(self.port_spin)
        top.addWidget(self.connect_btn)
        top.addWidget(self.disconnect_btn)
        top.addWidget(self.info_label, stretch=1)

        # Left: tag browser
        self.scan_btn = QPushButton("Scan tags (slow)")
        self.export_btn = QPushButton("Export structures (Excel)")
        self.tag_list = QListWidget()
        left_box = QVBoxLayout()
        left_box.addWidget(self.scan_btn)
        left_box.addWidget(self.export_btn)
        left_box.addWidget(self.tag_list)
        left = QWidget()
        left.setLayout(left_box)

        # Right: live structure table
        self.struct_edit = QLineEdit()
        self.struct_edit.setPlaceholderText("tag to watch, e.g. Seq or PLC_Status")
        self.watch_btn = QPushButton("Watch")
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("filter members, e.g. Desc,State")
        self.used_only = QCheckBox(f"in-use rows only ({USED_MARKER_MEMBER})")
        self.used_only.setChecked(True)
        self.interval = QDoubleSpinBox()
        self.interval.setRange(0.5, 60.0)
        self.interval.setValue(2.0)
        self.interval.setSuffix(" s")
        self.live = QCheckBox("live")
        self.live.setChecked(True)
        controls = QHBoxLayout()
        controls.addWidget(self.struct_edit)
        controls.addWidget(self.watch_btn)
        controls.addWidget(self.filter_edit)
        controls.addWidget(self.used_only)
        controls.addWidget(QLabel("every"))
        controls.addWidget(self.interval)
        controls.addWidget(self.live)
        self.table = QTableWidget()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        right_box = QVBoxLayout()
        right_box.addLayout(controls)
        right_box.addWidget(self.table)
        right = QWidget()
        right.setLayout(right_box)

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 3)

        central_box = QVBoxLayout()
        central_box.addLayout(top)
        central_box.addWidget(splitter, stretch=1)
        central = QWidget()
        central.setLayout(central_box)
        self.setCentralWidget(central)

        self.pfwd_check.toggled.connect(self._pfwd_toggled)
        self.connect_btn.clicked.connect(self._connect_clicked)
        self.disconnect_btn.clicked.connect(self._disconnect_clicked)
        self.ip_edit.returnPressed.connect(self._connect_clicked)
        self.scan_btn.clicked.connect(self._scan_clicked)
        self.export_btn.clicked.connect(self._export_clicked)
        self.tag_list.itemDoubleClicked.connect(self._tag_activated)
        self.watch_btn.clicked.connect(self._watch_clicked)
        self.struct_edit.returnPressed.connect(self._watch_clicked)
        self.interval.valueChanged.connect(self._interval_changed)
        self.live.toggled.connect(self._live_toggled)

    # --- user actions -------------------------------------------------

    def _pfwd_toggled(self, checked):
        # Node/Service IP/Port are only meaningful in port-forward mode.
        self.node_edit.setEnabled(checked)
        self.service_ip_edit.setEnabled(checked)
        self.port_spin.setEnabled(checked)

    def _connect_clicked(self):
        ip = self.ip_edit.text().strip()
        if not ip:
            return
        if self.pfwd_check.isChecked():
            node = self.node_edit.currentText().strip()
            service_ip = self.service_ip_edit.currentText().strip()
            port = self.port_spin.value()
            if not node or not service_ip:
                self.statusBar().showMessage(
                    "Node and Service IP required for port forwarding", 5000
                )
                return
            self.info_label.setText(
                f"port forward {service_ip}:{port} -> {ip} via {node}..."
            )
            self.request_connect_forward.emit(ip, node, service_ip, port)
        else:
            self.info_label.setText(f"connecting to {ip}...")
            self.request_connect.emit(ip)

    def _disconnect_clicked(self):
        # Stop polling first so no read is queued against the closing session.
        self._timer.stop()
        self._waiting = False
        self.request_disconnect.emit()
        self.disconnect_btn.setEnabled(False)
        self.info_label.setText("not connected")
        self.statusBar().showMessage("disconnected", 3000)

    def _scan_clicked(self):
        self.statusBar().showMessage("scanning tags (walks whole dictionary)...")
        self.request_tags.emit()

    def _tag_activated(self, item):
        self.struct_edit.setText(item.data(Qt.ItemDataRole.UserRole))
        self._watch_clicked()

    def _export_clicked(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export structures", "plc_structs.xlsx", "Excel files (*.xlsx)"
        )
        if path:
            self.statusBar().showMessage(
                "exporting (scans all tags, then reads every structure)..."
            )
            self.request_export.emit(path)

    def _watch_clicked(self):
        name = self.struct_edit.text().strip()
        if not name:
            return
        self._current_struct = name
        self._previous = {}
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        self._poll(force=True)
        self._interval_changed()

    def _interval_changed(self):
        self._timer.start(int(self.interval.value() * 1000))

    def _live_toggled(self, checked):
        if checked:
            self._interval_changed()
        else:
            self._timer.stop()

    def _poll(self, force=False):
        if (
            self._current_struct
            and not self._waiting
            and (force or self.live.isChecked())
        ):
            self._waiting = True
            self.request_struct.emit(self._current_struct)

    # --- worker results -----------------------------------------------

    def _on_connected(self, info):
        self.info_label.setText(info)
        self.disconnect_btn.setEnabled(True)
        self.statusBar().showMessage("connected", 3000)

    def _on_exported(self, message):
        self.statusBar().showMessage(message, 10000)

    def _on_progress(self, message):
        # Phase updates while a forward stands up (can take several seconds).
        self.info_label.setText(message)
        self.statusBar().showMessage(message)

    def _on_failed(self, message):
        self._waiting = False
        # A failure while not connected is a connect failure: keep it visible in
        # the top label instead of letting the next action's status overwrite it.
        if not self.disconnect_btn.isEnabled():
            self.info_label.setText(message)
        self.statusBar().showMessage(message)

    def _on_tags(self, rows):
        self.tag_list.clear()
        for name, type_name in rows:
            item = QListWidgetItem(f"{name}  —  {type_name}")
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.tag_list.addItem(item)
        self.statusBar().showMessage(f"{len(rows)} user tags", 5000)

    def _on_struct(self, name, columns, types, start):
        self._waiting = False
        if name != self._current_struct:
            return

        wanted = [
            w.strip().lower() for w in self.filter_edit.text().split(",") if w.strip()
        ]
        members = [
            m for m in columns if not wanted or any(w in m.lower() for w in wanted)
        ]
        labels = columns.get(USED_MARKER_MEMBER)
        # positions index into the value lists; the displayed index is start+position
        # (PLC arrays are declared ARRAY[start..N], commonly 1-based).
        if self.used_only.isChecked() and labels:
            positions = [p for p, v in enumerate(labels) if str(v).strip()]
        else:
            positions = list(range(max((len(v) for v in columns.values()), default=0)))

        self.table.setColumnCount(len(members))
        self.table.setRowCount(len(positions))
        self.table.setHorizontalHeaderLabels(members)
        for col, member in enumerate(members):
            header = self.table.horizontalHeaderItem(col)
            if header is not None:
                header.setToolTip(types.get(member, ""))
        self.table.setVerticalHeaderLabels([str(start + p) for p in positions])

        previous = self._previous
        current = {}
        for col, member in enumerate(members):
            values = columns[member]
            for row, pos in enumerate(positions):
                text = str(values[pos]).strip() if pos < len(values) else "n/a"
                current[(member, pos)] = text
                item = QTableWidgetItem(text)
                if previous.get((member, pos), text) != text:
                    item.setBackground(CHANGED_BRUSH)
                self.table.setItem(row, col, item)
        self._previous = current
        self.statusBar().showMessage(f"{name}: updated", 1500)

    # --- shutdown -------------------------------------------------------

    def closeEvent(self, event):  # noqa: N802 - Qt override
        self._timer.stop()
        # Tear the forward down *synchronously* before quitting the thread. A
        # queued emit races thread shutdown, and the kubectl deletes (which wait
        # out the pod's termination grace) outlast a short wait(), so the process
        # would exit mid-teardown and leak the pod/service. Blocking here holds
        # the close until deletion actually completes.
        QMetaObject.invokeMethod(
            self._worker,
            "disconnect_plc",
            Qt.ConnectionType.BlockingQueuedConnection,
        )
        self._thread.quit()
        self._thread.wait(5000)
        super().closeEvent(event)


def main():
    parser = argparse.ArgumentParser(
        description="Qt GUI for browsing Omron NX PLC tags over EtherNet/IP"
    )
    parser.add_argument("ip", nargs="?", help="PLC IP address to pre-fill")
    args = parser.parse_args()
    app = QApplication(sys.argv)
    window = MainWindow(args.ip)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
