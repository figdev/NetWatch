import csv
import base64
import colorsys
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
from PySide6.QtGui import QColor, QAction, QIcon, QPixmap
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPushButton, QSpinBox, QDoubleSpinBox, QCheckBox, QSystemTrayIcon, QStyle, QMenu, QSplitter, QTabWidget, QVBoxLayout, QWidget)

DB_PATH = Path(__file__).resolve().parent / 'netwatch.db'
FAILURE_THRESHOLD = 3
# Stable high-contrast sequence: retain the original first 12 colors, then
# generate 988 additional distinct colors with a golden-angle hue sequence.
# After 1,000 slots, colors repeat. Saved slots preserve existing assignments.
BASE_COLORS = ['#00E676', '#FF5252', '#40C4FF', '#FFD740', '#D500F9', '#FF9100',
               '#64FFDA', '#FF80AB', '#B2FF59', '#B388FF', '#FFFFFF', '#80D8FF']
MAX_COLORS = 1000


def make_colors():
    colors = list(BASE_COLORS)
    seen = set(colors)
    index = 0
    while len(colors) < MAX_COLORS:
        # Vary saturation/value to create visually distinguishable bright lines
        # against the dark background; golden-angle hue avoids adjacent similarities.
        hue = (index * 0.618033988749895) % 1.0
        saturation = (0.88, 0.67, 0.98, 0.76)[(index // 12) % 4]
        value = (1.0, 0.86, 0.94)[(index // 48) % 3]
        r, g, b = colorsys.hsv_to_rgb(hue, saturation, value)
        color = '#{:02X}{:02X}{:02X}'.format(round(r * 255), round(g * 255), round(b * 255))
        if color not in seen:
            colors.append(color)
            seen.add(color)
        index += 1
    return colors


COLORS = make_colors()

# Icon is embedded in this file: no separate PNG/ICO file is required.
MASCOT_PNG_BASE64 = (
    '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'
)


def mascot_icon():
    pixmap = QPixmap()
    pixmap.loadFromData(base64.b64decode("".join(MASCOT_PNG_BASE64)), "PNG")
    return QIcon(pixmap)


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
        self.setWindowTitle('NetWatch v0.5.8 — Mascot & 1,000 Graph Colors')
        self.setWindowIcon(mascot_icon())
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
            self.tray = QSystemTrayIcon(mascot_icon(), self)
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
    app.setWindowIcon(mascot_icon())
    window = NetWatch()
    window.show()
    sys.exit(app.exec())
