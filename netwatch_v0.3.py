
import sys
import re
import socket
import subprocess
import time
import math
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import pyqtgraph as pg

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QLineEdit, QPushButton, QLabel,
    QComboBox, QSpinBox, QListWidget, QMessageBox
)


# Network checking functions

def check_ping(host):
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
            r"time\s*[=<]\s*(\d+)\s*ms",
            result.stdout,
            re.IGNORECASE
        )

        latency = float(match.group(1)) if match else None
        return True, latency

    except (OSError, subprocess.TimeoutExpired):
        return False, None


def check_tcp(host, port):
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


class NetWatch(QMainWindow):

    COLORS = [
        "#00e676",
        "#40c4ff",
        "#ffab40",
        "#ea80fc",
        "#ff5252",
        "#ffff00",
        "#64ffda",
        "#ffffff"
    ]

    def __init__(self):
        super().__init__()

        self.setWindowTitle(
            "NetWatch v0.3 - Live Network Graph"
        )
        self.resize(1100, 700)

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

        self.add_host("8.8.8.8", "Ping", 0)
        self.add_host("google.com", "TCP", 443)
        self.add_host("1.1.1.1", "TCP", 53)

        self.check_all_hosts()

    def setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        central.setStyleSheet("""
            QWidget {
                background-color: #151a24;
                color: #eeeeee;
                font-size: 13px;
            }
            QLineEdit, QComboBox, QSpinBox {
                background-color: #252d3b;
                padding: 7px;
                border: 1px solid #445066;
                border-radius: 5px;
            }
            QPushButton {
                background-color: #2874c7;
                padding: 8px;
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: #398de8;
            }
            QListWidget {
                background-color: #202735;
                border: 1px solid #445066;
            }
        """)

        layout = QVBoxLayout(central)

        title = QLabel("NetWatch - Live Latency Monitor")
        title.setStyleSheet(
            "font-size: 23px; font-weight: bold;"
        )
        layout.addWidget(title)

        controls = QHBoxLayout()

        self.host_input = QLineEdit()
        self.host_input.setPlaceholderText(
            "IP address or hostname"
        )
        self.host_input.returnPressed.connect(
            self.add_from_input
        )

        self.check_type = QComboBox()
        self.check_type.addItems(["Ping", "TCP"])
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

        remove_button = QPushButton("Remove Selected")
        remove_button.clicked.connect(
            self.remove_selected
        )

        controls.addWidget(self.host_input, 3)
        controls.addWidget(self.check_type)
        controls.addWidget(self.port_input)
        controls.addWidget(add_button)
        controls.addWidget(remove_button)

        layout.addLayout(controls)

        self.graph = pg.PlotWidget()
        self.graph.setBackground("#10151e")
        self.graph.setTitle(
            "Connection Latency Over Time",
            color="w",
            size="14pt"
        )
        self.graph.setLabel(
            "left", "Latency", units="ms"
        )
        self.graph.setLabel(
            "bottom", "Time", units="seconds"
        )
        self.graph.showGrid(
            x=True, y=True, alpha=0.25
        )
        self.graph.addLegend()
        self.graph.setYRange(0, 100)
        self.graph.setXRange(-120, 0)

        layout.addWidget(self.graph, 4)

        self.host_list = QListWidget()
        self.host_list.setMaximumHeight(145)
        layout.addWidget(self.host_list)

        self.summary = QLabel("Monitoring...")
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
            if check_type == "TCP" else 0
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

        color = self.COLORS[
            (target_id - 1) % len(self.COLORS)
        ]

        label = (
            f"{host} ({check_type}"
            + (f":{port}" if port else "")
            + ")"
        )

        curve = self.graph.plot(
            [], [],
            pen=pg.mkPen(color=color, width=2),
            name=label,
            connect="finite"
        )

        self.hosts[target_id] = {
            "key": key,
            "host": host,
            "type": check_type,
            "port": port,
            "status": "Pending",
            "checking": False,
            "times": [],
            "latencies": [],
            "curve": curve,
            "color": color,
            "label": label,
            "checks": 0,
            "failures": 0
        }

        self.refresh_host_list()

    def remove_selected(self):
        item = self.host_list.currentItem()

        if item is None:
            return

        target_id = item.data(
            Qt.ItemDataRole.UserRole
        )

        data = self.hosts.pop(target_id, None)

        if data:
            self.graph.removeItem(data["curve"])

        self.refresh_host_list()

    def check_all_hosts(self):
        for target_id, data in list(
            self.hosts.items()
        ):
            if data["checking"]:
                continue

            data["checking"] = True

            if data["type"] == "Ping":
                future = self.executor.submit(
                    check_ping, data["host"]
                )
            else:
                future = self.executor.submit(
                    check_tcp,
                    data["host"],
                    data["port"]
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

    def update_result(self, target_id, online, latency):
        if target_id not in self.hosts:
            return

        data = self.hosts[target_id]
        data["checking"] = False
        data["status"] = (
            "Online" if online else "Offline"
        )

        data["checks"] += 1

        if not online:
            data["failures"] += 1

        # Relative time in seconds
        now = time.monotonic()

        if not hasattr(self, "start_time"):
            self.start_time = now

        elapsed = now - self.start_time

        data["times"].append(elapsed)

        # Use NaN for failed or unmeasured latency.
        # This produces a gap, not a false zero.
        value = (
            latency if online and latency is not None
            else math.nan
        )
        data["latencies"].append(value)

        # Keep last 120 seconds of samples
        cutoff = elapsed - 120

        while (
            data["times"]
            and data["times"][0] < cutoff
        ):
            data["times"].pop(0)
            data["latencies"].pop(0)

        # Display time relative to current sample
        x_values = [
            t - elapsed for t in data["times"]
        ]

        data["curve"].setData(
            x_values,
            data["latencies"]
        )

        self.graph.setXRange(-120, 0, padding=0)

        self.refresh_host_list()

    def refresh_host_list(self):
        self.host_list.clear()

        online_count = 0
        offline_count = 0

        for target_id, data in self.hosts.items():
            if data["status"] == "Online":
                online_count += 1
                status_color = "#00e676"
            elif data["status"] == "Offline":
                offline_count += 1
                status_color = "#ff5252"
            else:
                status_color = "#cccccc"

            loss = (
                100 * data["failures"] / data["checks"]
                if data["checks"] else 0
            )

            text = (
                f"{data['label']} | "
                f"{data['status']} | "
                f"Loss: {loss:.1f}%"
            )

            self.host_list.addItem(text)

            item = self.host_list.item(
                self.host_list.count() - 1
            )
            item.setData(
                Qt.ItemDataRole.UserRole,
                target_id
            )
            item.setForeground(
                QColor(status_color)
            )

        self.summary.setText(
            f"Targets: {len(self.hosts)} | "
            f"Online: {online_count} | "
            f"Offline: {offline_count} | "
            "Refresh: 3 seconds | "
            "Graph: last 120 seconds"
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
