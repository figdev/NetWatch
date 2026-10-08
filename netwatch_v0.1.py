
import sys
import subprocess
import re
import ipaddress
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QTableWidget, QTableWidgetItem,
    QLineEdit, QPushButton, QLabel, QHeaderView,
    QMessageBox
)


class Signals(QObject):
    result = Signal(str, bool, object)


def ping_host(host):
    """Ping a host once on Windows."""
    try:
        process = subprocess.run(
            ["ping", "-n", "1", "-w", "1500", host],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=subprocess.CREATE_NO_WINDOW
        )

        output = process.stdout

        # Match normal and sub-millisecond responses.
        match = re.search(
            r"(?:time|zeit)[=<]\s*(\d+)\s*ms",
            output,
            re.IGNORECASE
        )

        if process.returncode == 0:
            latency = float(match.group(1)) if match else None
            return True, latency

        return False, None

    except (subprocess.TimeoutExpired, OSError):
        return False, None


class NetWatch(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("NetWatch - Network Monitor")
        self.resize(850, 500)

        self.hosts = {}
        self.executor = ThreadPoolExecutor(max_workers=10)
        self.signals = Signals()
        self.signals.result.connect(self.update_result)

        self.setup_ui()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check_all_hosts)
        self.timer.start(3000)

        self.add_host("8.8.8.8")
        self.add_host("1.1.1.1")
        self.add_host("google.com")

        self.check_all_hosts()

    def setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        layout = QVBoxLayout(central)

        title = QLabel("NetWatch - Live Network Monitor")
        title.setStyleSheet(
            "font-size: 22px; font-weight: bold;"
        )
        layout.addWidget(title)

        controls = QHBoxLayout()

        self.host_input = QLineEdit()
        self.host_input.setPlaceholderText(
            "Enter IP address or hostname"
        )
        self.host_input.returnPressed.connect(
            self.add_host_from_input
        )

        add_button = QPushButton("Add Host")
        add_button.clicked.connect(
            self.add_host_from_input
        )

        remove_button = QPushButton("Remove Selected")
        remove_button.clicked.connect(
            self.remove_selected
        )

        controls.addWidget(self.host_input)
        controls.addWidget(add_button)
        controls.addWidget(remove_button)

        layout.addLayout(controls)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels([
            "Host", "Status", "Latency", "Last Check"
        ])

        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )

        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )

        self.table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )

        layout.addWidget(self.table)

        self.summary = QLabel("Monitoring...")
        layout.addWidget(self.summary)

    def add_host_from_input(self):
        host = self.host_input.text().strip()

        if not host:
            return

        if any(c.isspace() for c in host) or len(host) > 253:
            QMessageBox.warning(
                self, "Invalid Host",
                "Enter a valid IP address or hostname."
            )
            return

        self.add_host(host)
        self.host_input.clear()
        self.check_all_hosts()

    def add_host(self, host):
        if host in self.hosts:
            return

        self.hosts[host] = {
            "status": "Pending",
            "latency": None,
            "checking": False
        }

        row = self.table.rowCount()
        self.table.insertRow(row)

        for column, value in enumerate([
            host, "Pending", "-", "-"
        ]):
            item = QTableWidgetItem(value)
            item.setData(Qt.ItemDataRole.UserRole, host)
            self.table.setItem(row, column, item)

    def remove_selected(self):
        rows = sorted({
            index.row()
            for index in self.table.selectedIndexes()
        }, reverse=True)

        for row in rows:
            host = self.table.item(row, 0).text()
            self.hosts.pop(host, None)
            self.table.removeRow(row)

        self.update_summary()

    def check_all_hosts(self):
        for host, data in self.hosts.items():
            if data["checking"]:
                continue

            data["checking"] = True

            future = self.executor.submit(
                ping_host, host
            )

            # Route worker results through Qt signals.
            future.add_done_callback(
                lambda f, h=host: self.handle_future(h, f)
            )

    def handle_future(self, host, future):
        try:
            online, latency = future.result()
        except Exception:
            online, latency = False, None

        self.signals.result.emit(host, online, latency)

    def update_result(self, host, online, latency):
        if host not in self.hosts:
            return

        from datetime import datetime

        self.hosts[host]["checking"] = False
        self.hosts[host]["status"] = (
            "Online" if online else "Offline"
        )
        self.hosts[host]["latency"] = latency

        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).text() != host:
                continue

            status_item = self.table.item(row, 1)
            status_item.setText(
                "Online" if online else "Offline"
            )

            color = (
                "#188038" if online else "#d93025"
            )
            status_item.setForeground(color)

            latency_text = (
                f"{latency:.0f} ms"
                if latency is not None else "-"
            )

            self.table.item(row, 2).setText(
                latency_text
            )

            self.table.item(row, 3).setText(
                datetime.now().strftime("%H:%M:%S")
            )

            break

        self.update_summary()

    def update_summary(self):
        online = sum(
            data["status"] == "Online"
            for data in self.hosts.values()
        )

        total = len(self.hosts)

        self.summary.setText(
            f"Hosts: {total} | "
            f"Online: {online} | "
            f"Offline: "
            f"{sum(d['status'] == 'Offline' for d in self.hosts.values())}"
        )

    def closeEvent(self, event):
        self.timer.stop()
        self.executor.shutdown(wait=False, cancel_futures=True)
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)

    window = NetWatch()
    window.show()

    sys.exit(app.exec())
