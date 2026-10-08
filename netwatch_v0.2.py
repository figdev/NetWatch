
import sys
import re
import socket
import subprocess
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QTableWidget, QTableWidgetItem,
    QLineEdit, QPushButton, QLabel, QHeaderView,
    QMessageBox, QComboBox, QSpinBox
)


# -------------------------------
# Network checking functions
# -------------------------------

def check_ping(host):
    """Perform one ICMP ping on Windows."""
    try:
        result = subprocess.run(
            ["ping", "-n", "1", "-w", "1500", host],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=subprocess.CREATE_NO_WINDOW
        )

        if result.returncode != 0:
            return False, None

        match = re.search(
            r"(?:time|zeit)\s*[=<]\s*(\d+)\s*ms",
            result.stdout,
            re.IGNORECASE
        )

        latency = (
            float(match.group(1))
            if match else None
        )

        return True, latency

    except (OSError, subprocess.TimeoutExpired):
        return False, None


def check_tcp(host, port):
    """Attempt a TCP connection to the specified port."""
    start = time.perf_counter()

    try:
        with socket.create_connection(
            (host, port), timeout=2
        ):
            latency = (
                time.perf_counter() - start
            ) * 1000

            return True, round(latency, 2)

    except OSError:
        return False, None


class WorkerSignals(QObject):
    result = Signal(int, bool, object)


# -------------------------------
# Main GUI
# -------------------------------

class NetWatch(QMainWindow):

    def __init__(self):
        super().__init__()

        self.setWindowTitle(
            "NetWatch v0.2 - Network Monitor"
        )
        self.resize(1000, 550)

        self.hosts = {}
        self.next_id = 0

        self.executor = ThreadPoolExecutor(
            max_workers=10
        )

        self.signals = WorkerSignals()
        self.signals.result.connect(
            self.update_result
        )

        self.setup_ui()

        self.timer = QTimer(self)
        self.timer.timeout.connect(
            self.check_all_hosts
        )
        self.timer.start(3000)

        # Default monitoring targets
        self.add_host("8.8.8.8", "Ping", 0)
        self.add_host("google.com", "TCP", 443)
        self.add_host("1.1.1.1", "TCP", 53)

        self.check_all_hosts()

    def setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        layout = QVBoxLayout(central)

        title = QLabel(
            "NetWatch - Network Connectivity Monitor"
        )
        title.setStyleSheet(
            "font-size: 22px; font-weight: bold;"
        )
        layout.addWidget(title)

        controls = QHBoxLayout()

        self.host_input = QLineEdit()
        self.host_input.setPlaceholderText(
            "IP address or hostname"
        )

        self.check_type = QComboBox()
        self.check_type.addItems([
            "Ping", "TCP"
        ])
        self.check_type.currentTextChanged.connect(
            self.toggle_port
        )

        self.port_input = QSpinBox()
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(443)
        self.port_input.setEnabled(False)

        add_button = QPushButton("Add Target")
        add_button.clicked.connect(
            self.add_from_input
        )

        remove_button = QPushButton(
            "Remove Selected"
        )
        remove_button.clicked.connect(
            self.remove_selected
        )

        controls.addWidget(self.host_input)
        controls.addWidget(self.check_type)
        controls.addWidget(self.port_input)
        controls.addWidget(add_button)
        controls.addWidget(remove_button)

        layout.addLayout(controls)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels([
            "Host",
            "Type",
            "Port",
            "Status",
            "Latency",
            "Last Check"
        ])

        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )

        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )

        self.table.setSelectionMode(
            QTableWidget.SelectionMode.ExtendedSelection
        )

        self.table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )

        layout.addWidget(self.table)

        self.summary = QLabel()
        layout.addWidget(self.summary)

    def toggle_port(self, check_type):
        self.port_input.setEnabled(
            check_type == "TCP"
        )

    def add_from_input(self):
        host = self.host_input.text().strip()
        check_type = self.check_type.currentText()

        if not host:
            return

        if (
            len(host) > 253
            or any(c.isspace() for c in host)
            or host.startswith("-")
        ):
            QMessageBox.warning(
                self,
                "Invalid Host",
                "Enter a valid hostname or IP address."
            )
            return

        port = (
            self.port_input.value()
            if check_type == "TCP"
            else 0
        )

        self.add_host(host, check_type, port)
        self.host_input.clear()
        self.check_all_hosts()

    def add_host(self, host, check_type, port):
        key = (host.lower(), check_type, port)

        if any(
            data["key"] == key
            for data in self.hosts.values()
        ):
            return

        self.next_id += 1
        target_id = self.next_id

        self.hosts[target_id] = {
            "key": key,
            "host": host,
            "type": check_type,
            "port": port,
            "status": "Pending",
            "latency": None,
            "checking": False
        }

        row = self.table.rowCount()
        self.table.insertRow(row)

        values = [
            host,
            check_type,
            str(port) if port else "-",
            "Pending",
            "-",
            "-"
        ]

        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setData(
                Qt.ItemDataRole.UserRole,
                target_id
            )
            self.table.setItem(
                row, column, item
            )

        self.update_summary()

    def remove_selected(self):
        rows = sorted({
            index.row()
            for index in self.table.selectedIndexes()
        }, reverse=True)

        for row in rows:
            target_id = self.table.item(
                row, 0
            ).data(Qt.ItemDataRole.UserRole)

            self.hosts.pop(target_id, None)
            self.table.removeRow(row)

        self.update_summary()

    def check_all_hosts(self):
        for target_id, data in list(
            self.hosts.items()
        ):
            if data["checking"]:
                continue

            data["checking"] = True

            host = data["host"]
            check_type = data["type"]
            port = data["port"]

            if check_type == "Ping":
                future = self.executor.submit(
                    check_ping, host
                )
            else:
                future = self.executor.submit(
                    check_tcp, host, port
                )

            future.add_done_callback(
                lambda f, tid=target_id:
                self.handle_future(tid, f)
            )

    def handle_future(self, target_id, future):
        try:
            online, latency = future.result()
        except Exception:
            online, latency = False, None

        self.signals.result.emit(
            target_id, online, latency
        )

    def update_result(
        self, target_id, online, latency
    ):
        if target_id not in self.hosts:
            return

        data = self.hosts[target_id]
        data["checking"] = False
        data["status"] = (
            "Online" if online else "Offline"
        )
        data["latency"] = latency

        for row in range(
            self.table.rowCount()
        ):
            item = self.table.item(row, 0)

            if item.data(
                Qt.ItemDataRole.UserRole
            ) != target_id:
                continue

            status_item = self.table.item(
                row, 3
            )
            status_item.setText(
                data["status"]
            )

            status_item.setForeground(
                QColor(
                    "#188038"
                    if online else "#d93025"
                )
            )

            latency_text = (
                f"{latency:.2f} ms"
                if latency is not None
                else "-"
            )

            self.table.item(
                row, 4
            ).setText(latency_text)

            self.table.item(
                row, 5
            ).setText(
                datetime.now().strftime(
                    "%H:%M:%S"
                )
            )

            break

        self.update_summary()

    def update_summary(self):
        online = sum(
            d["status"] == "Online"
            for d in self.hosts.values()
        )

        offline = sum(
            d["status"] == "Offline"
            for d in self.hosts.values()
        )

        self.summary.setText(
            f"Targets: {len(self.hosts)} | "
            f"Online: {online} | "
            f"Offline: {offline}"
        )

    def closeEvent(self, event):
        self.timer.stop()
        self.executor.shutdown(
            wait=False,
            cancel_futures=True
        )
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)

    window = NetWatch()
    window.show()

    sys.exit(app.exec())
