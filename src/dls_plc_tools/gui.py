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
from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from dls_plc_tools.interrogate import INFO_TAGS, USED_MARKER_MEMBER, dims_str

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


class PlcWorker(QObject):
    """Owns the aphyt connection; lives in a QThread so reads never block the UI."""

    connected = Signal(str)
    tags_ready = Signal(list)
    struct_ready = Signal(str, object, object)  # name, {member: values}, {member: type}
    exported = Signal(str)
    failed = Signal(str)

    def __init__(self):
        super().__init__()
        self._eip = None

    @Slot(str)
    def connect_plc(self, ip):
        try:
            self.disconnect_plc()
            eip = omron.NSeries()
            eip.connect_explicit(ip, connection_timeout=5.0)
            eip.register_session()
            self._eip = eip
            info = [ip]
            for tag in INFO_TAGS:
                try:
                    info.append(f"{tag}={eip.read_variable(tag)}")
                except CIPException:
                    pass
            self.connected.emit("   ".join(info))
        except Exception as e:
            self.failed.emit(f"connect: {e}")

    @Slot()
    def disconnect_plc(self):
        if self._eip is not None:
            try:
                self._eip.close_explicit()
            except Exception:
                pass
            self._eip = None

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
            if isinstance(result, CIPStructure):
                for member, value in result.members.items():
                    if isinstance(value, CIPArray):
                        columns[member] = value.value()
                        element = type(value.local_cip_data_type_object).__name__
                        types[member] = f"ARRAY[{dims_str(value)}] OF {element}"
                    else:
                        columns[member] = [value.value()]
                        types[member] = type(value).__name__
            elif isinstance(result, CIPArray):
                element = type(result.local_cip_data_type_object).__name__
                columns[name] = result.value()
                types[name] = f"ARRAY[{dims_str(result)}] OF {element}"
            else:  # scalar tag: one-cell table
                columns[name] = [result]
                types[name] = type(result).__name__
            self.struct_ready.emit(name, columns, types)
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
                for member, value in result.members.items():
                    if isinstance(value, CIPArray):
                        rows[member] = value.value()
                    else:
                        rows[member] = [value.value()]
                    width = max(width, len(rows[member]))
                sheet = workbook.create_sheet(_sheet_name(name))
                sheet.append([name] + list(range(width)))
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
        self.request_tags.connect(self._worker.list_tags)
        self.request_struct.connect(self._worker.read_struct)
        self.request_export.connect(self._worker.export_structs)
        self.request_disconnect.connect(self._worker.disconnect_plc)
        self._worker.connected.connect(self._on_connected)
        self._worker.tags_ready.connect(self._on_tags)
        self._worker.struct_ready.connect(self._on_struct)
        self._worker.exported.connect(self._on_exported)
        self._worker.failed.connect(self._on_failed)
        self._thread.start()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)

        # Top bar: connection
        self.ip_edit = QLineEdit(ip or "")
        self.ip_edit.setPlaceholderText("e.g. 172.23.241.21")
        self.ip_edit.setMaximumWidth(160)
        self.connect_btn = QPushButton("Connect")
        self.info_label = QLabel("not connected")
        top = QHBoxLayout()
        top.addWidget(QLabel("IP address:"))
        top.addWidget(self.ip_edit)
        top.addWidget(self.connect_btn)
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

        self.connect_btn.clicked.connect(self._connect_clicked)
        self.ip_edit.returnPressed.connect(self._connect_clicked)
        self.scan_btn.clicked.connect(self._scan_clicked)
        self.export_btn.clicked.connect(self._export_clicked)
        self.tag_list.itemDoubleClicked.connect(self._tag_activated)
        self.watch_btn.clicked.connect(self._watch_clicked)
        self.struct_edit.returnPressed.connect(self._watch_clicked)
        self.interval.valueChanged.connect(self._interval_changed)
        self.live.toggled.connect(self._live_toggled)

    # --- user actions -------------------------------------------------

    def _connect_clicked(self):
        ip = self.ip_edit.text().strip()
        if ip:
            self.info_label.setText(f"connecting to {ip}...")
            self.request_connect.emit(ip)

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
        self.statusBar().showMessage("connected", 3000)

    def _on_exported(self, message):
        self.statusBar().showMessage(message, 10000)

    def _on_failed(self, message):
        self._waiting = False
        self.statusBar().showMessage(message)

    def _on_tags(self, rows):
        self.tag_list.clear()
        for name, type_name in rows:
            item = QListWidgetItem(f"{name}  —  {type_name}")
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.tag_list.addItem(item)
        self.statusBar().showMessage(f"{len(rows)} user tags", 5000)

    def _on_struct(self, name, columns, types):
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
        if self.used_only.isChecked() and labels:
            rows = [i for i, v in enumerate(labels) if str(v).strip()]
        else:
            rows = list(range(max((len(v) for v in columns.values()), default=0)))

        self.table.setColumnCount(len(members))
        self.table.setRowCount(len(rows))
        self.table.setHorizontalHeaderLabels(members)
        for col, member in enumerate(members):
            header = self.table.horizontalHeaderItem(col)
            if header is not None:
                header.setToolTip(types.get(member, ""))
        self.table.setVerticalHeaderLabels([str(i) for i in rows])

        previous = self._previous
        current = {}
        for col, member in enumerate(members):
            values = columns[member]
            for row, idx in enumerate(rows):
                text = str(values[idx]).strip() if idx < len(values) else "n/a"
                current[(member, idx)] = text
                item = QTableWidgetItem(text)
                if previous.get((member, idx), text) != text:
                    item.setBackground(CHANGED_BRUSH)
                self.table.setItem(row, col, item)
        self._previous = current
        self.statusBar().showMessage(f"{name}: updated", 1500)

    # --- shutdown -------------------------------------------------------

    def closeEvent(self, event):  # noqa: N802 - Qt override
        self._timer.stop()
        self.request_disconnect.emit()
        self._thread.quit()
        self._thread.wait(3000)
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
