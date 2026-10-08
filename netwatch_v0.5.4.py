import csv
import ipaddress
import math
import re
import socket
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPushButton, QSpinBox, QVBoxLayout, QWidget)

DB_PATH = Path(__file__).resolve().parent / 'netwatch.db'
FAILURE_THRESHOLD = 3
COLORS = ['#00e676', '#40c4ff', '#ffab40', '#ea80fc', '#ff5252', '#ffff00', '#64ffda', '#ffffff']


def valid_host(host):
    if not host or len(host) > 253 or host.startswith('-') or any(c.isspace() for c in host):
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(re.fullmatch(r'(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*', host))


def ping(host):
    try:
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        result = subprocess.run(['ping', '-n', '1', '-w', '1500', host], capture_output=True,
                                text=True, timeout=3, creationflags=flags)
        if result.returncode != 0:
            return False, None
        match = re.search(r'time\s*[=<]\s*(\d+(?:\.\d+)?)\s*ms', result.stdout, re.I)
        return True, float(match.group(1)) if match else None
    except (OSError, subprocess.TimeoutExpired):
        return False, None


def tcp(host, port):
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=2):
            return True, round((time.perf_counter() - start) * 1000, 2)
    except OSError:
        return False, None


class Signals(QObject):
    result = Signal(int, bool, object, str)


class NetWatch(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('NetWatch v0.5.4 — Persistent Outage History')
        self.resize(1120, 730)
        self.conn = sqlite3.connect(DB_PATH)
        self.conn.execute('PRAGMA foreign_keys = ON')
        self.conn.execute('PRAGMA journal_mode = WAL')
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS targets (
                id INTEGER PRIMARY KEY, host TEXT NOT NULL, kind TEXT NOT NULL,
                port INTEGER NOT NULL, UNIQUE(host, kind, port));
            CREATE TABLE IF NOT EXISTS checks (
                id INTEGER PRIMARY KEY, target_id INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
                checked_at TEXT NOT NULL, online INTEGER NOT NULL, latency_ms REAL);
            CREATE INDEX IF NOT EXISTS checks_target_time ON checks(target_id, checked_at);
            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY,
                target_id INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
                started_at TEXT NOT NULL,
                recovered_at TEXT,
                duration_seconds REAL
            );
            CREATE INDEX IF NOT EXISTS incidents_target_time
                ON incidents(target_id, started_at);
        ''')
        cutoff = (datetime.now().astimezone() - timedelta(days=30)).isoformat()
        with self.conn:
            self.conn.execute('DELETE FROM checks WHERE checked_at < ?', (cutoff,))
            self.conn.execute('DELETE FROM incidents WHERE recovered_at IS NOT NULL AND recovered_at < ?', (cutoff,))
        self.hosts = {}
        self.executor = ThreadPoolExecutor(max_workers=10)
        self.signals = Signals()
        self.signals.result.connect(self.receive)
        self.closing = False
        self.setup_ui()
        rows = self.conn.execute('SELECT id, host, kind, port FROM targets ORDER BY id').fetchall()
        if not rows:
            for host, kind, port in [('8.8.8.8', 'Ping', 0), ('google.com', 'TCP', 443), ('1.1.1.1', 'TCP', 53)]:
                self.add_target(host, kind, port)
        else:
            for target_id, host, kind, port in rows:
                self.load_target(target_id, host, kind, port)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(3000)
        self.graph_timer = QTimer(self)
        self.graph_timer.timeout.connect(self.tick)
        self.graph_timer.start(1000)
        self.refresh_incidents()
        self.poll()

    def setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        central.setStyleSheet('''QWidget { background:#151a24; color:#eee; font-size:13px; }
            QLineEdit, QComboBox, QSpinBox { background:#252d3b; padding:7px; border:1px solid #445066; border-radius:5px; }
            QPushButton { background:#2874c7; padding:8px; border-radius:5px; }
            QPushButton:hover { background:#398de8; }
            QListWidget { background:#202735; border:1px solid #445066; }''')
        layout = QVBoxLayout(central)
        heading = QLabel('NetWatch — Live & Historical Latency')
        heading.setStyleSheet('font-size:23px; font-weight:bold;')
        layout.addWidget(heading)
        controls = QHBoxLayout()
        self.host_input = QLineEdit()
        self.host_input.setPlaceholderText('IP address or hostname')
        self.host_input.returnPressed.connect(self.add_from_input)
        self.kind = QComboBox()
        self.kind.addItems(['Ping', 'TCP'])
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(443)
        self.port.setEnabled(False)
        self.kind.currentTextChanged.connect(lambda kind: self.port.setEnabled(kind == 'TCP'))
        add = QPushButton('Add Target')
        add.clicked.connect(self.add_from_input)
        remove = QPushButton('Remove Selected')
        remove.clicked.connect(self.remove_selected)
        export = QPushButton('Export CSV')
        export.clicked.connect(self.export_csv)
        for widget in (self.host_input, self.kind, self.port, add, remove, export):
            controls.addWidget(widget, 3 if widget is self.host_input else 1)
        layout.addLayout(controls)
        self.graph = pg.PlotWidget()
        self.graph.setBackground('#10151e')
        self.graph.setLabel('left', 'Latency', units='ms')
        self.graph.setLabel('bottom', 'Seconds ago')
        self.graph.showGrid(x=True, y=True, alpha=0.25)
        self.graph.addLegend()
        self.graph.setXRange(-120, 0, padding=0)
        self.graph.setYRange(0, 100)
        layout.addWidget(self.graph, 4)
        self.host_list = QListWidget()
        self.host_list.setMaximumHeight(170)
        layout.addWidget(self.host_list)
        alert_heading = QLabel('Connection Alerts (current session)')
        alert_heading.setStyleSheet('font-size:16px; font-weight:bold;')
        layout.addWidget(alert_heading)
        self.alert_list = QListWidget()
        self.alert_list.setMaximumHeight(125)
        layout.addWidget(self.alert_list)
        history_heading = QLabel('Saved Outage History (latest 100 incidents)')
        history_heading.setStyleSheet('font-size:16px; font-weight:bold;')
        layout.addWidget(history_heading)
        self.incident_list = QListWidget()
        self.incident_list.setMaximumHeight(125)
        layout.addWidget(self.incident_list)
        export_incidents = QPushButton('Export Outage History CSV')
        export_incidents.clicked.connect(self.export_incidents_csv)
        layout.addWidget(export_incidents)
        self.summary = QLabel('Loading targets...')
        layout.addWidget(self.summary)

    def add_from_input(self):
        host = self.host_input.text().strip()
        if not valid_host(host):
            QMessageBox.warning(self, 'Invalid host', 'Enter a valid IP address or hostname.')
            return
        kind = self.kind.currentText()
        port = self.port.value() if kind == 'TCP' else 0
        if not self.add_target(host, kind, port):
            QMessageBox.information(self, 'Duplicate', 'This target is already monitored.')
        self.host_input.clear()
        self.poll()

    def add_target(self, host, kind, port):
        host = host.lower()
        if self.conn.execute('SELECT 1 FROM targets WHERE host=? AND kind=? AND port=?',
                             (host, kind, port)).fetchone():
            return False
        with self.conn:
            cur = self.conn.execute('INSERT INTO targets(host,kind,port) VALUES(?,?,?)', (host, kind, port))
        self.load_target(cur.lastrowid, host, kind, port)
        return True

    def load_target(self, target_id, host, kind, port):
        label = f'{host} ({kind}{":" + str(port) if port else ""})'
        color = COLORS[(target_id - 1) % len(COLORS)]
        curve = self.graph.plot([], [], pen=pg.mkPen(color=color, width=2),
                                name=label, connect='finite')
        since = (datetime.now().astimezone() - timedelta(seconds=120)).isoformat()
        history = self.conn.execute('''SELECT checked_at, online, latency_ms FROM checks
            WHERE target_id=? AND checked_at>=? ORDER BY checked_at''', (target_id, since)).fetchall()
        samples = [(datetime.fromisoformat(ts).timestamp(), lat if online and lat is not None else math.nan)
                   for ts, online, lat in history]
        # Reconstruct consecutive failures from recent saved checks.
        recent = self.conn.execute(
            'SELECT online FROM checks WHERE target_id=? ORDER BY id DESC LIMIT ?',
            (target_id, FAILURE_THRESHOLD)
        ).fetchall()
        failures = 0
        for (was_online,) in recent:
            if was_online:
                break
            failures += 1
        if not recent:
            status = 'Pending'
        elif recent[0][0]:
            status = 'Online'
        elif failures >= FAILURE_THRESHOLD:
            status = 'Offline'
        else:
            status = 'Degraded'
        active = self.conn.execute(
            'SELECT id, started_at FROM incidents WHERE target_id=? AND recovered_at IS NULL ORDER BY id DESC LIMIT 1',
            (target_id,)
        ).fetchone()
        self.hosts[target_id] = dict(host=host, kind=kind, port=port, label=label,
                                     status=status, consecutive_failures=failures,
                                     checking=False, curve=curve, color=color, samples=samples,
                                     outage_started_at=(datetime.fromisoformat(active[1]).timestamp() if active else None),
                                     incident_id=(active[0] if active else None))
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, target_id)
        self.host_list.addItem(item)
        self.update_list()

    def remove_selected(self):
        item = self.host_list.currentItem()
        if not item:
            return
        target_id = item.data(Qt.ItemDataRole.UserRole)
        data = self.hosts.pop(target_id, None)
        if data:
            self.graph.removeItem(data['curve'])
            with self.conn:
                self.conn.execute('DELETE FROM targets WHERE id=?', (target_id,))
        self.host_list.takeItem(self.host_list.row(item))
        self.update_list()
        self.refresh_incidents()

    def poll(self):
        for target_id, data in list(self.hosts.items()):
            if data['checking']:
                continue
            data['checking'] = True
            func = ping if data['kind'] == 'Ping' else tcp
            args = (data['host'],) if data['kind'] == 'Ping' else (data['host'], data['port'])
            future = self.executor.submit(func, *args)
            future.add_done_callback(lambda f, tid=target_id: self.completed(tid, f))

    def completed(self, target_id, future):
        try:
            online, latency = future.result()
        except Exception:
            online, latency = False, None
        if not self.closing:
            self.signals.result.emit(target_id, online, latency, datetime.now().astimezone().isoformat())

    def receive(self, target_id, online, latency, checked_at):
        data = self.hosts.get(target_id)
        if data is None or self.closing:
            return
        data['checking'] = False
        previous_state = data['status']
        if online:
            data['consecutive_failures'] = 0
            data['status'] = 'Online'
        else:
            data['consecutive_failures'] += 1
            data['status'] = ('Offline' if data['consecutive_failures'] >= FAILURE_THRESHOLD
                              else 'Degraded')
        if data['status'] != previous_state:
            print(f"[{datetime.now():%H:%M:%S}] {data['label']}: "
                  f"{previous_state} -> {data['status']}")
        # Notify only on confirmed outages and recoveries, not on transient failures.
        if data['status'] == 'Offline' and previous_state != 'Offline':
            # Confirmed outage: persist its start so the timer survives restarts.
            data['outage_started_at'] = datetime.fromisoformat(checked_at).timestamp()
            with self.conn:
                cursor = self.conn.execute(
                    'INSERT INTO incidents(target_id, started_at) VALUES(?, ?)',
                    (target_id, checked_at)
                )
            data['incident_id'] = cursor.lastrowid
            self.add_alert(f"OFFLINE: {data['label']} (timer started)", '#ff5252')
            self.refresh_incidents()
        elif data['status'] == 'Online' and previous_state == 'Offline':
            started = data.get('outage_started_at')
            if started is None:
                duration = 'duration unavailable (outage began before tracking)'
            else:
                elapsed = max(0, datetime.fromisoformat(checked_at).timestamp() - started)
                duration = self.format_duration(elapsed)
            incident_id = data.get('incident_id')
            if incident_id is not None:
                with self.conn:
                    self.conn.execute(
                        'UPDATE incidents SET recovered_at=?, duration_seconds=? WHERE id=? AND recovered_at IS NULL',
                        (checked_at, elapsed if started is not None else None, incident_id)
                    )
            self.add_alert(f"RECOVERED: {data['label']} — downtime: {duration}", '#00e676')
            data['outage_started_at'] = None
            data['incident_id'] = None
            self.refresh_incidents()
        with self.conn:
            self.conn.execute('INSERT INTO checks(target_id,checked_at,online,latency_ms) VALUES(?,?,?,?)',
                              (target_id, checked_at, int(online), latency))
        data['samples'].append((datetime.fromisoformat(checked_at).timestamp(),
                                latency if online and latency is not None else math.nan))
        self.update_list()
        self.redraw()

    def add_alert(self, message, color):
        timestamp = datetime.now().strftime('%H:%M:%S')
        item = QListWidgetItem(f'[{timestamp}] {message}')
        item.setForeground(QColor(color))
        self.alert_list.insertItem(0, item)
        while self.alert_list.count() > 100:
            self.alert_list.takeItem(self.alert_list.count() - 1)

    @staticmethod
    def format_duration(seconds):
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f'{hours}h {minutes:02d}m {seconds:02d}s' if hours else f'{minutes}m {seconds:02d}s'

    def tick(self):
        self.redraw()
        self.update_list()

    def redraw(self):
        now = time.time()
        for data in self.hosts.values():
            data['samples'] = [(t, v) for t, v in data['samples'] if t >= now - 120]
            data['curve'].setData([t - now for t, _ in data['samples']],
                                  [v for _, v in data['samples']])
        self.graph.setXRange(-120, 0, padding=0)

    def update_list(self):
        online = offline = degraded = 0
        for index in range(self.host_list.count()):
            item = self.host_list.item(index)
            data = self.hosts.get(item.data(Qt.ItemDataRole.UserRole))
            if not data:
                continue
            status = data['status']
            online += status == 'Online'
            offline += status == 'Offline'
            degraded += status == 'Degraded'
            if status == 'Offline':
                started = data.get('outage_started_at')
                elapsed = (self.format_duration(time.time() - started)
                           if started is not None else 'start unknown (before launch)')
                item.setText(f"{data['label']}  |  {status}  |  Downtime: {elapsed}")
            else:
                item.setText(f"{data['label']}  |  {status}")
            item.setForeground(QColor({'Online': '#00e676', 'Degraded': '#ffab40',
                                       'Offline': '#ff5252'}.get(status, '#cccccc')))
        self.summary.setText(f'Targets: {len(self.hosts)}  |  Online: {online}  |  Degraded: {degraded}  |  Offline: {offline}  |  Check interval: 3s  |  Graph: 120s  |  History: 30 days')

    def refresh_incidents(self):
        self.incident_list.clear()
        rows = self.conn.execute('''
            SELECT i.started_at, i.recovered_at, i.duration_seconds,
                   t.host, t.kind, t.port
            FROM incidents i JOIN targets t ON t.id=i.target_id
            ORDER BY i.id DESC LIMIT 100
        ''').fetchall()
        for started, recovered, seconds, host, kind, port in rows:
            label = f'{host} ({kind}{":" + str(port) if port else ""})'
            if recovered is None:
                status = 'ONGOING'
                color = '#ff5252'
            else:
                status = f'Recovered {recovered[:19]} | {self.format_duration(seconds)}' if seconds is not None else f'Recovered {recovered[:19]}'
                color = '#00e676'
            item = QListWidgetItem(f'{started[:19]} | {label} | {status}')
            item.setForeground(QColor(color))
            self.incident_list.addItem(item)

    def export_incidents_csv(self):
        filename, _ = QFileDialog.getSaveFileName(
            self, 'Export outage history',
            str(DB_PATH.parent / 'netwatch_outages.csv'), 'CSV Files (*.csv)'
        )
        if not filename:
            return
        try:
            with open(filename, 'w', newline='', encoding='utf-8') as file:
                writer = csv.writer(file)
                writer.writerow(['host', 'type', 'port', 'started_at', 'recovered_at', 'duration_seconds'])
                writer.writerows(self.conn.execute('''
                    SELECT t.host, t.kind, t.port, i.started_at, i.recovered_at, i.duration_seconds
                    FROM incidents i JOIN targets t ON t.id=i.target_id
                    ORDER BY i.id DESC
                '''))
            QMessageBox.information(self, 'Export complete', f'Saved to {filename}')
        except OSError as exc:
            QMessageBox.critical(self, 'Export failed', str(exc))

    def export_csv(self):
        filename, _ = QFileDialog.getSaveFileName(self, 'Export monitoring history',
                                                   str(DB_PATH.parent / 'netwatch_history.csv'), 'CSV Files (*.csv)')
        if not filename:
            return
        try:
            with open(filename, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(['timestamp', 'host', 'type', 'port', 'online', 'latency_ms'])
                for row in self.conn.execute('''SELECT c.checked_at,t.host,t.kind,t.port,c.online,c.latency_ms
                    FROM checks c JOIN targets t ON t.id=c.target_id ORDER BY c.id'''):
                    writer.writerow(row)
            QMessageBox.information(self, 'Export complete', f'Saved to {filename}')
        except OSError as exc:
            QMessageBox.critical(self, 'Export failed', str(exc))

    def closeEvent(self, event):
        self.closing = True
        self.timer.stop()
        self.graph_timer.stop()
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.conn.close()
        event.accept()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = NetWatch()
    window.show()
    sys.exit(app.exec())
