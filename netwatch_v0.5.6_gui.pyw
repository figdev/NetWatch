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
from PySide6.QtGui import QColor, QAction
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPushButton, QSpinBox, QDoubleSpinBox, QCheckBox, QSystemTrayIcon, QStyle, QMenu, QSplitter, QTabWidget, QVBoxLayout, QWidget)

DB_PATH = Path(__file__).resolve().parent / 'netwatch.db'
FAILURE_THRESHOLD = 3
# High-contrast sequence for the dark graph. Colors are assigned by stable slot,
# not database ID, and persisted so restarting never reshuffles targets.
COLORS = ['#00E676', '#FF5252', '#40C4FF', '#FFD740', '#D500F9', '#FF9100',
          '#64FFDA', '#FF80AB', '#B2FF59', '#B388FF', '#FFFFFF', '#80D8FF']


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
        self.setWindowTitle('NetWatch v0.5.6 — Background Monitoring & Stable Colors')
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
            CREATE TABLE IF NOT EXISTS target_colors (
                target_id INTEGER PRIMARY KEY REFERENCES targets(id) ON DELETE CASCADE,
                color_slot INTEGER NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS alert_settings (
                target_id INTEGER PRIMARY KEY REFERENCES targets(id) ON DELETE CASCADE,
                latency_threshold_ms REAL NOT NULL DEFAULT 150,
                latency_enabled INTEGER NOT NULL DEFAULT 1,
                offline_enabled INTEGER NOT NULL DEFAULT 1,
                recovery_enabled INTEGER NOT NULL DEFAULT 1,
                desktop_enabled INTEGER NOT NULL DEFAULT 1
            );
        ''')
        cutoff = (datetime.now().astimezone() - timedelta(days=30)).isoformat()
        with self.conn:
            self.conn.execute('DELETE FROM checks WHERE checked_at < ?', (cutoff,))
            self.conn.execute('DELETE FROM incidents WHERE recovered_at IS NOT NULL AND recovered_at < ?', (cutoff,))
        # Migrate existing targets in saved order once. Future assignments reuse
        # freed slots without changing colors for surviving targets.
        with self.conn:
            existing = self.conn.execute('SELECT id FROM targets ORDER BY id').fetchall()
            used = {r[0] for r in self.conn.execute('SELECT color_slot FROM target_colors')}
            for (tid,) in existing:
                if self.conn.execute('SELECT 1 FROM target_colors WHERE target_id=?', (tid,)).fetchone():
                    continue
                slot = next(i for i in range(len(existing) + len(used) + 1) if i not in used)
                self.conn.execute('INSERT INTO target_colors(target_id,color_slot) VALUES(?,?)', (tid,slot))
                used.add(slot)
        self.hosts = {}
        self.executor = ThreadPoolExecutor(max_workers=10)
        self.signals = Signals()
        self.signals.result.connect(self.receive)
        self.closing = False
        self.setup_ui()
        self.tray = None
        self.exit_requested = False
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon), self)
            self.tray.setToolTip('NetWatch — monitoring active')
            tray_menu = QMenu(self)
            show_action = QAction('Open NetWatch', self)
            show_action.triggered.connect(self.restore_window)
            tray_menu.addAction(show_action)
            exit_action = QAction('Exit NetWatch', self)
            exit_action.triggered.connect(self.exit_application)
            tray_menu.addAction(exit_action)
            self.tray.setContextMenu(tray_menu)
            self.tray.activated.connect(self.tray_activated)
            self.tray.show()
            QApplication.instance().setQuitOnLastWindowClosed(False)
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
            QListWidget { background:#202735; border:1px solid #445066; }
            QTabWidget::pane { border:1px solid #445066; }
            QTabBar::tab { background:#252d3b; padding:10px 20px; margin-right:3px; }
            QTabBar::tab:selected { background:#2874c7; }
            QSplitter::handle { background:#445066; }
        ''')
        layout = QVBoxLayout(central)
        heading = QLabel('NetWatch — Live & Historical Latency')
        heading.setStyleSheet('font-size:23px; font-weight:bold;')
        layout.addWidget(heading)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        live_tab = QWidget()
        self.tabs.addTab(live_tab, 'Live Monitor')
        live_layout = QVBoxLayout(live_tab)
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
        live_layout.addLayout(controls)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        live_layout.addWidget(splitter, 1)

        self.graph = pg.PlotWidget()
        self.graph.setBackground('#10151e')
        self.graph.setLabel('left', 'Latency', units='ms')
        self.graph.setLabel('bottom', 'Seconds ago')
        self.graph.showGrid(x=True, y=True, alpha=0.25)
        self.graph.addLegend()
        self.graph.setXRange(-120, 0, padding=0)
        self.graph.setYRange(0, 100)
        self.graph.setMinimumHeight(240)
        splitter.addWidget(self.graph)

        self.host_list = QListWidget()
        self.host_list.setMinimumHeight(80)
        splitter.addWidget(self.host_list)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([570, 125])

        self.summary = QLabel('Loading targets...')
        live_layout.addWidget(self.summary)

        history_tab = QWidget()
        self.tabs.addTab(history_tab, 'Outage History')
        history_layout = QVBoxLayout(history_tab)
        history_heading = QLabel('Saved Outage History (latest 100 incidents)')
        history_heading.setStyleSheet('font-size:16px; font-weight:bold;')
        history_layout.addWidget(history_heading)
        self.incident_list = QListWidget()
        history_layout.addWidget(self.incident_list, 1)
        export_incidents = QPushButton('Export Outage History CSV')
        export_incidents.clicked.connect(self.export_incidents_csv)
        history_layout.addWidget(export_incidents)

        alerts_tab = QWidget()
        self.tabs.addTab(alerts_tab, 'Alerts')
        alerts_layout = QVBoxLayout(alerts_tab)
        alert_heading = QLabel('Connection Alerts (current session)')
        alert_heading.setStyleSheet('font-size:16px; font-weight:bold;')
        alerts_layout.addWidget(alert_heading)
        self.alert_list = QListWidget()
        alerts_layout.addWidget(self.alert_list, 1)

        settings_heading = QLabel('Per-target alert settings')
        settings_heading.setStyleSheet('font-size:16px; font-weight:bold;')
        alerts_layout.addWidget(settings_heading)
        self.alert_target = QComboBox()
        self.alert_target.currentIndexChanged.connect(self.load_alert_controls)
        alerts_layout.addWidget(self.alert_target)
        threshold_row = QHBoxLayout()
        threshold_row.addWidget(QLabel('Latency threshold (ms):'))
        self.latency_threshold = QDoubleSpinBox()
        self.latency_threshold.setRange(1, 60000)
        self.latency_threshold.setDecimals(1)
        self.latency_threshold.setValue(150)
        threshold_row.addWidget(self.latency_threshold)
        alerts_layout.addLayout(threshold_row)
        self.latency_alert_enabled = QCheckBox('Warn when latency exceeds threshold')
        self.latency_alert_enabled.setChecked(True)
        self.offline_alert_enabled = QCheckBox('Notify when target goes offline')
        self.offline_alert_enabled.setChecked(True)
        self.recovery_alert_enabled = QCheckBox('Notify when target recovers')
        self.recovery_alert_enabled.setChecked(True)
        self.desktop_alert_enabled = QCheckBox('Show Windows desktop notifications')
        self.desktop_alert_enabled.setChecked(True)
        for control in (self.latency_alert_enabled, self.offline_alert_enabled,
                        self.recovery_alert_enabled, self.desktop_alert_enabled):
            alerts_layout.addWidget(control)
        save_settings = QPushButton('Save Alert Settings for Selected Target')
        save_settings.clicked.connect(self.save_alert_settings)
        alerts_layout.addWidget(save_settings)
        self.notification_note = QLabel('Desktop notifications require a system tray and Windows notifications enabled.')
        alerts_layout.addWidget(self.notification_note)

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
        row = self.conn.execute('SELECT color_slot FROM target_colors WHERE target_id=?', (target_id,)).fetchone()
        if row is None:
            used = {r[0] for r in self.conn.execute('SELECT color_slot FROM target_colors')}
            slot = next(i for i in range(len(used) + 1) if i not in used)
            with self.conn:
                self.conn.execute('INSERT INTO target_colors(target_id,color_slot) VALUES(?,?)', (target_id, slot))
        else:
            slot = row[0]
        color = COLORS[slot % len(COLORS)]
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
        settings = self.get_alert_settings(target_id)
        self.hosts[target_id]['alert_settings'] = settings
        self.hosts[target_id]['high_latency'] = False
        self.refresh_alert_targets()
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
        self.refresh_alert_targets()

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
            if data['alert_settings']['offline_enabled']:
                self.add_alert(f"OFFLINE: {data['label']} (timer started)", '#ff5252', data)
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
            if data['alert_settings']['recovery_enabled']:
                self.add_alert(f"RECOVERED: {data['label']} — downtime: {duration}", '#00e676', data)
            data['outage_started_at'] = None
            data['incident_id'] = None
            self.refresh_incidents()
        settings = data['alert_settings']
        is_high = (online and latency is not None and settings['latency_enabled']
                   and latency > settings['latency_threshold_ms'])
        if is_high and not data['high_latency']:
            self.add_alert(
                f"HIGH LATENCY: {data['label']} — {latency:.1f} ms "
                f"(threshold {settings['latency_threshold_ms']:.1f} ms)", '#ffab40', data)
        # A single notification per high-latency episode; reset on a normal reading.
        # Failed checks do not reset the high-latency episode.
        if online and latency is not None:
            data['high_latency'] = bool(is_high)
        with self.conn:
            self.conn.execute('INSERT INTO checks(target_id,checked_at,online,latency_ms) VALUES(?,?,?,?)',
                              (target_id, checked_at, int(online), latency))
        data['samples'].append((datetime.fromisoformat(checked_at).timestamp(),
                                latency if online and latency is not None else math.nan))
        self.update_list()
        self.redraw()

    def get_alert_settings(self, target_id):
        row = self.conn.execute(
            'SELECT latency_threshold_ms, latency_enabled, offline_enabled, '
            'recovery_enabled, desktop_enabled FROM alert_settings WHERE target_id=?',
            (target_id,)).fetchone()
        if row is None:
            row = (150.0, 1, 1, 1, 1)
        return dict(zip(('latency_threshold_ms', 'latency_enabled', 'offline_enabled',
                         'recovery_enabled', 'desktop_enabled'), row))

    def refresh_alert_targets(self):
        if not hasattr(self, 'alert_target'):
            return
        selected = self.alert_target.currentData()
        self.alert_target.blockSignals(True)
        self.alert_target.clear()
        for target_id, data in self.hosts.items():
            self.alert_target.addItem(data['label'], target_id)
        if selected is not None:
            index = self.alert_target.findData(selected)
            if index >= 0:
                self.alert_target.setCurrentIndex(index)
        self.alert_target.blockSignals(False)
        self.load_alert_controls()

    def load_alert_controls(self, *_):
        data = self.hosts.get(self.alert_target.currentData())
        enabled = data is not None
        for control in (self.latency_threshold, self.latency_alert_enabled,
                        self.offline_alert_enabled, self.recovery_alert_enabled,
                        self.desktop_alert_enabled):
            control.setEnabled(enabled)
        if not enabled:
            return
        settings = data['alert_settings']
        self.latency_threshold.setValue(settings['latency_threshold_ms'])
        self.latency_alert_enabled.setChecked(bool(settings['latency_enabled']))
        self.offline_alert_enabled.setChecked(bool(settings['offline_enabled']))
        self.recovery_alert_enabled.setChecked(bool(settings['recovery_enabled']))
        self.desktop_alert_enabled.setChecked(bool(settings['desktop_enabled']))

    def save_alert_settings(self):
        target_id = self.alert_target.currentData()
        data = self.hosts.get(target_id)
        if data is None:
            return
        settings = dict(latency_threshold_ms=self.latency_threshold.value(),
                        latency_enabled=int(self.latency_alert_enabled.isChecked()),
                        offline_enabled=int(self.offline_alert_enabled.isChecked()),
                        recovery_enabled=int(self.recovery_alert_enabled.isChecked()),
                        desktop_enabled=int(self.desktop_alert_enabled.isChecked()))
        with self.conn:
            self.conn.execute('''INSERT INTO alert_settings
                (target_id, latency_threshold_ms, latency_enabled, offline_enabled,
                 recovery_enabled, desktop_enabled) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(target_id) DO UPDATE SET
                latency_threshold_ms=excluded.latency_threshold_ms,
                latency_enabled=excluded.latency_enabled,
                offline_enabled=excluded.offline_enabled,
                recovery_enabled=excluded.recovery_enabled,
                desktop_enabled=excluded.desktop_enabled''',
                (target_id, settings['latency_threshold_ms'], settings['latency_enabled'],
                 settings['offline_enabled'], settings['recovery_enabled'], settings['desktop_enabled']))
        data['alert_settings'] = settings
        data['high_latency'] = False
        self.notification_note.setText(f"Saved alert settings for {data['label']}.")

    def add_alert(self, message, color, data=None):
        timestamp = datetime.now().strftime('%H:%M:%S')
        item = QListWidgetItem(f'[{timestamp}] {message}')
        item.setForeground(QColor(color))
        self.alert_list.insertItem(0, item)
        while self.alert_list.count() > 100:
            self.alert_list.takeItem(self.alert_list.count() - 1)
        if data is not None and data['alert_settings']['desktop_enabled'] and self.tray is not None:
            if QSystemTrayIcon.supportsMessages():
                self.tray.showMessage('NetWatch', message, QSystemTrayIcon.MessageIcon.Information, 8000)

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

    def restore_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.restore_window()

    def exit_application(self):
        self.exit_requested = True
        self.close()

    def closeEvent(self, event):
        if self.tray is not None and not self.exit_requested:
            event.ignore()
            self.hide()
            if not getattr(self, '_tray_tip_shown', False):
                self.tray.showMessage('NetWatch is still running',
                    'Monitoring continues in the system tray. Right-click the icon to exit.',
                    QSystemTrayIcon.MessageIcon.Information, 5000)
                self._tray_tip_shown = True
            return
        self.closing = True
        self.timer.stop()
        self.graph_timer.stop()
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.conn.close()
        if self.tray is not None:
            self.tray.hide()
        event.accept()
        QApplication.instance().quit()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = NetWatch()
    window.show()
    sys.exit(app.exec())
