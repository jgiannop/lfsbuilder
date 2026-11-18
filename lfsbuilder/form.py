"""The form: a window that writes a recipe, and runs lfsbuilder on it.

    .venv/bin/python -m lfsbuilder.form [RECIPE]

Everything the form shows is a field of the recipe (recipe.py), and saving
writes the recipe with recipe.dump(); the form holds no state of its own.
Check, Build, Boot test, Make ISO and Publish run `python3 -m lfsbuilder ...`
on the saved recipe and show its output; when Check finds USE changes the
recipe needs, the form offers to add them. "Serve updates" runs the package
server (`serve --bind 0.0.0.0`) until it is switched off or the form closes.
"""

from __future__ import annotations

import dataclasses
import re
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QProcess, Qt
from PySide6.QtGui import QFont, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QRadioButton, QScrollArea, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
    QWidget,
)

from . import binhost, gentoo, presets, recipe as recipes, steps

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "recipes" / "example.toml"
_STEP_LINE = re.compile(r"^\s*(?P<number>\d+)  (?P<title>.+?)\s+(?:\.\.\.|done before)")
_SUGGESTED = re.compile(r'^"(?P<package>[^"]+)" = \[(?P<flags>[^\]]*)\]$')
WWW = ROOT / "work" / "binhost" / "www"
DEFAULT_KEY = "keys/binhost.asc"


def last_published(stem: str, www: Path = WWW) -> float | None:
    """When the recipe's argo branch was last published (its commit's time), or None."""
    git = www / binhost.OVERLAY
    if not (git / "HEAD").exists():
        return None
    result = subprocess.run(["git", "-C", str(git), "log", "-1", "--format=%ct", f"refs/heads/{stem}", "--"],
                            capture_output=True, text=True)
    return float(result.stdout) if result.returncode == 0 and result.stdout.strip() else None


def published_text(when: float | None, now: float | None = None) -> tuple[str, bool]:
    """What the form says about the last publish, and whether it is a warning."""
    if when is None:
        return "Not published yet.", True
    days = int(((now if now is not None else time.time()) - when) // 86400)
    left = binhost.MAX_AGE_DAYS - days
    date = time.strftime("%Y-%m-%d", time.localtime(when))
    if left <= 0:
        return (f"Last published {date}, {days} days ago: installed systems now refuse this release. Publish again.", True)
    if left <= 7:
        return (f"Last published {date}, {days} days ago: systems refuse it in {left} days. Publish again soon.", True)
    return f"Last published {date} ({days} days ago). Publish at least every {binhost.MAX_AGE_DAYS} days.", False


def _lines(text: str) -> list[str]:
    return [l.strip() for l in text.replace(",", "\n").splitlines() if l.strip()]


class Form(QMainWindow):
    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        self.base: recipes.Recipe | None = None  # what was loaded: the fields the form doesn't show stay as they were
        self.process: QProcess | None = None
        self.output_text = ""
        self.setWindowTitle("lfsbuilder")
        self.resize(1100, 820)

        tabs = QTabWidget()
        tabs.addTab(self._scroll(self._system_page()), "System")
        tabs.addTab(self._scroll(self._software_page()), "Desktop and software")
        tabs.addTab(self._scroll(self._users_page()), "Users")
        tabs.addTab(self._scroll(self._advanced_page()), "Advanced")

        self.output = QPlainTextEdit(readOnly=True)
        self.output.setFont(QFont("monospace", 9))
        self.output.setPlaceholderText("What lfsbuilder says appears here.")

        buttons = QHBoxLayout()
        for label, slot in (("Open…", self.open), ("Save", self.save), ("Save as…", self.save_as)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        buttons.addStretch()
        self.run_buttons = []
        for label, command, tip in (
            ("Check", "check", "Ask Portage what the recipe would build, and which USE changes it needs"),
            ("Build", "build", "Build the system in Docker (a checkpoint after every step)"),
            ("Boot test", "boot", "Make a disk image and boot it in QEMU; saves a picture of the screen"),
            ("Make ISO", "iso", "Make the live ISO with its installer, and test installing from it (BIOS and UEFI)"),
            ("Publish", "publish", "Sign the built packages and the release, and put them on the package server"),
        ):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, c=command: self.run(c))
            buttons.addWidget(b)
            self.run_buttons.append(b)
        self.repair = QCheckBox("Repair failures with Claude")
        self.repair.setToolTip("When a step fails: check, then Claude Code, finds a fix; it is saved beside the recipe and the build goes on")
        buttons.insertWidget(buttons.indexOf(self.run_buttons[1]) + 1, self.repair)  # beside Build
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        buttons.addWidget(self.stop_button)
        self.serve_button = QPushButton("Serve updates", checkable=True)
        self.serve_button.setToolTip("Run the package server on this PC (port 8080, every network) for installed systems")
        self.serve_button.toggled.connect(self.serve)
        self.server: QProcess | None = None
        buttons.addWidget(self.serve_button)

        top = QWidget()
        layout = QVBoxLayout(top)
        self.path_label = QLabel()
        layout.addWidget(self.path_label)
        self.progress = QLabel()
        layout.addWidget(tabs)
        layout.addLayout(buttons)
        layout.addWidget(self.progress)
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(top)
        splitter.addWidget(self.output)
        splitter.setSizes([640, 180])
        self.setCentralWidget(splitter)
        self.load(path)

    # The pages

    @staticmethod
    def _scroll(widget: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(widget)
        return area

    def _system_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        identity = QGroupBox("Identity")
        form = QFormLayout(identity)
        self.name = QLineEdit()
        self.version = QLineEdit()
        form.addRow("Distribution name", self.name)
        form.addRow("Version", self.version)
        form.addRow(QLabel("Raise the version for every release: installed systems update to a higher one only."))
        layout.addWidget(identity)

        updates = QGroupBox("Updates")
        form = QFormLayout(updates)
        self.updates = QCheckBox("Installed systems take updates from a package server")
        self.server_url = QLineEdit(placeholderText="http://192.168.1.93:8080/")
        self.server_key = QLineEdit(placeholderText=DEFAULT_KEY)
        self.published = QLabel(wordWrap=True)
        form.addRow(self.updates)
        form.addRow("Server address", self.server_url)
        form.addRow("Signing key (public, beside the recipe)", self.server_key)
        form.addRow("Published", self.published)
        self.updates.toggled.connect(self._update_fields)
        layout.addWidget(updates)

        base = QGroupBox("Base")
        form = QFormLayout(base)
        self.init = QComboBox()
        self.init.addItem("systemd (needed by GNOME and most desktops)", "systemd")
        self.init.addItem("OpenRC", "openrc")
        self.cpu = QComboBox()
        self.cpu.addItem("Any 64-bit PC (x86-64)", "x86-64")
        self.cpu.addItem("Newer PCs, faster (x86-64-v3: 2015 on, AVX2)", "x86-64-v3")
        self.binary = QCheckBox("Use Gentoo's binary packages where they match (much faster)")
        self.kernel = QComboBox()
        self.kernel.addItem("Gentoo's kernel, ready-built", "gentoo-kernel-bin")
        self.kernel.addItem("Gentoo's kernel, compiled here", "gentoo-kernel")
        form.addRow("Init system", self.init)
        form.addRow("Processor", self.cpu)
        form.addRow("", self.binary)
        form.addRow("Kernel", self.kernel)
        layout.addWidget(base)

        language = QGroupBox("Language and place")
        form = QFormLayout(language)
        self.locale = self._combo(presets.LOCALES)
        self.timezone = self._combo(presets.TIMEZONES)
        self.keymap = self._combo(presets.KEYMAPS)
        form.addRow("Language (locale)", self.locale)
        form.addRow("Time zone", self.timezone)
        form.addRow("Keyboard", self.keymap)
        layout.addWidget(language)

        network = QGroupBox("Network")
        form = QFormLayout(network)
        self.hostname = QLineEdit()
        self.domain = QLineEdit()
        self.dhcp = QRadioButton("Automatic (DHCP)")
        self.static = QRadioButton("Fixed address")
        group = QButtonGroup(self)
        group.addButton(self.dhcp)
        group.addButton(self.static)
        modes = QHBoxLayout()
        modes.addWidget(self.dhcp)
        modes.addWidget(self.static)
        modes.addStretch()
        self.address = QLineEdit(placeholderText="192.168.1.50/24")
        self.gateway = QLineEdit(placeholderText="192.168.1.1")
        self.dns = QLineEdit(placeholderText="192.168.1.1, 9.9.9.9")
        self.static.toggled.connect(self._network_fields)
        form.addRow("Host name", self.hostname)
        form.addRow("Domain", self.domain)
        form.addRow("Address", modes)
        form.addRow("IP address / prefix", self.address)
        form.addRow("Gateway", self.gateway)
        form.addRow("DNS servers", self.dns)
        layout.addWidget(network)

        output = QGroupBox("Output")
        form = QFormLayout(output)
        self.size = QSpinBox(minimum=4, maximum=2000, suffix=" GB")
        form.addRow("Disk image size", self.size)
        layout.addWidget(output)
        layout.addStretch()
        return page

    def _software_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        desktops = QGroupBox("Desktop")
        column = QVBoxLayout(desktops)
        self.desktop_buttons: dict[str, QRadioButton] = {}
        group = QButtonGroup(self)
        for d in presets.DESKTOPS:
            b = QRadioButton(d.label + ("" if d.tested else "  (not tested yet)"))
            b.setEnabled(d.tested)
            group.addButton(b)
            column.addWidget(b)
            self.desktop_buttons[d.key] = b
        layout.addWidget(desktops)

        software = QGroupBox("Software")
        column = QVBoxLayout(software)
        self.software_boxes: dict[str, QCheckBox] = {}
        for s in presets.SOFTWARE:
            box = QCheckBox(s.label)
            box.setToolTip(s.package)
            column.addWidget(box)
            self.software_boxes[s.package] = box
        column.addWidget(QLabel("Other packages (category/name, one per line):"))
        self.other_packages = QPlainTextEdit()
        self.other_packages.setMaximumHeight(110)
        column.addWidget(self.other_packages)
        layout.addWidget(software)
        layout.addStretch()
        return page

    def _users_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        root = QGroupBox("Root")
        row = QHBoxLayout(root)
        self.root_state = QLabel()
        row.addWidget(self.root_state)
        row.addStretch()
        b = QPushButton("Set root's password…")
        b.clicked.connect(self.set_root_password)
        row.addWidget(b)
        b = QPushButton("Lock root")
        b.clicked.connect(lambda: self._set_root(""))
        row.addWidget(b)
        layout.addWidget(root)

        accounts = QGroupBox("Accounts")
        column = QVBoxLayout(accounts)
        self.sudo = QCheckBox("Administrators (the \"wheel\" group) may use sudo")
        column.addWidget(self.sudo)
        self.users = QTableWidget(0, 4)
        self.users.setHorizontalHeaderLabels(["User name", "Full name", "Groups", "Password"])
        self.users.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        column.addWidget(self.users)
        row = QHBoxLayout()
        for label, slot in (("Add", self.add_user), ("Remove", self.remove_user), ("Set password…", self.set_user_password)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch()
        column.addLayout(row)
        column.addWidget(QLabel("Passwords are kept only as hashes; a recipe never holds a password."))
        layout.addWidget(accounts)
        layout.addStretch()
        return page

    def _advanced_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.stage3 = QLineEdit()
        self.repository = QLineEdit()
        self.cflags = QLineEdit()
        self.jobs = QSpinBox(minimum=0, maximum=256, specialValueText="as many as the processors")
        self.use = QLineEdit(placeholderText="-bluetooth pulseaudio")
        self.package_use = QPlainTextEdit(placeholderText="media-video/ffmpeg -x264")
        self.services = QLineEdit(placeholderText="gdm sshd")
        form.addRow(QLabel("What the recipe pins, and Gentoo's own settings. The defaults are tested."))
        form.addRow("Stage3 build", self.stage3)
        form.addRow("Repository commit", self.repository)
        form.addRow("Compiler flags", self.cflags)
        form.addRow("Make jobs", self.jobs)
        form.addRow("USE flags (all packages)", self.use)
        form.addRow("USE flags per package\n(package flags, one per line)", self.package_use)
        form.addRow("Other services to start", self.services)
        return page

    @staticmethod
    def _combo(values: tuple[str, ...]) -> QComboBox:
        box = QComboBox(editable=True)
        box.addItems(values)
        return box

    def _update_fields(self) -> None:
        for field in (self.server_url, self.server_key):
            field.setEnabled(self.updates.isChecked())

    def _show_published(self) -> None:
        if not self.updates.isChecked():
            self.published.setText("")
            return
        text, warning = published_text(last_published(self.path.stem))
        self.published.setText(text)
        self.published.setStyleSheet("color: #b00020;" if warning else "")

    def _network_fields(self) -> None:
        for field in (self.address, self.gateway, self.dns):
            field.setEnabled(self.static.isChecked())

    # Recipe -> form -> recipe

    def load(self, path: Path) -> None:
        try:
            r = recipes.load(path)
        except recipes.RecipeError as error:
            QMessageBox.warning(self, "lfsbuilder", str(error))
            return
        self.path, self.base = path, r
        self.path_label.setText(f"Recipe: {path}")
        self.name.setText(r.name)
        self.version.setText(r.version)
        self.updates.setChecked(bool(r.binhost))
        self.server_url.setText(r.binhost)
        self.server_key.setText(r.binhost_key or DEFAULT_KEY)
        self._update_fields()
        self._show_published()
        self.init.setCurrentIndex(self.init.findData(r.init))
        self.cpu.setCurrentIndex(self.cpu.findData(r.cpu))
        self.binary.setChecked(r.binary_packages)
        self.kernel.setCurrentIndex(self.kernel.findData(r.kernel))
        self.locale.setCurrentText(r.locale)
        self.timezone.setCurrentText(r.timezone)
        self.keymap.setCurrentText(r.keymap)
        self.hostname.setText(r.hostname)
        self.domain.setText(r.domain)
        (self.static if r.network.get("mode") == "static" else self.dhcp).setChecked(True)
        self.address.setText(r.network.get("address", ""))
        self.gateway.setText(r.network.get("gateway", ""))
        self.dns.setText(", ".join(r.network.get("dns", [])))
        self._network_fields()
        self.size.setValue(int(r.output.get("size_gb", 12)))

        desktop = presets.desktop_of(r.profile)
        self.desktop_buttons[desktop.key].setChecked(True)
        chosen = set(r.packages) - set(desktop.packages)
        for package, box in self.software_boxes.items():
            box.setChecked(package in chosen)
        brought = {"net-misc/networkmanager", "app-admin/sudo"}
        self.other_packages.setPlainText("\n".join(p for p in r.packages if p not in self.software_boxes
                                                     and p not in desktop.packages and p not in brought))
        self._set_root(r.root_password)
        self.sudo.setChecked(r.sudo)
        self.users.setRowCount(0)
        for a in r.accounts:
            self._user_row(a)

        self.stage3.setText(r.stage3)
        self.repository.setText(r.repository)
        self.cflags.setText(r.cflags)
        self.jobs.setValue(r.jobs)
        self.use.setText(" ".join(r.use))
        known = dict(desktop.package_use)
        for s in presets.SOFTWARE:
            known.update(dict(s.package_use))
        self.package_use.setPlainText("\n".join(f"{p} {' '.join(f)}" for p, f in r.package_use.items() if p not in known))
        extra = [s for s in r.services if s not in desktop.services and s not in {x.service for x in presets.SOFTWARE}]
        self.services.setText(" ".join(extra))

    def recipe(self) -> recipes.Recipe:
        """The recipe the form shows (fields it doesn't show come from the loaded one)."""
        r = self.base
        desktop = next(d for d in presets.DESKTOPS if self.desktop_buttons[d.key].isChecked())
        init = self.init.currentData()
        packages = list(desktop.packages)
        package_use: dict[str, list[str]] = {p: list(f) for p, f in desktop.package_use}
        services = list(desktop.services)
        for s in presets.SOFTWARE:
            if self.software_boxes[s.package].isChecked():
                packages.append(s.package)
                package_use.update({p: list(f) for p, f in s.package_use})
                if s.service:
                    services.append(s.service)
        packages += [p for p in _lines(self.other_packages.toPlainText()) if p not in packages]
        for line in self.package_use.toPlainText().splitlines():
            if line.split():
                package_use[line.split()[0]] = line.split()[1:]
        services += [s for s in self.services.text().split() if s not in services]
        network = {"manager": desktop.manager if init == "systemd" or desktop.manager == "networkmanager" else "netifrc",
                   "mode": "static" if self.static.isChecked() else "dhcp"}
        if self.static.isChecked():
            network.update(address=self.address.text().strip(), gateway=self.gateway.text().strip(), dns=_lines(self.dns.text()))
        accounts = [recipes.Account(
            name=self.users.item(row, 0).text().strip(),
            full_name=self.users.item(row, 1).text().strip(),
            groups=_lines(self.users.item(row, 2).text()),
            password=self.users.item(row, 3).data(Qt.UserRole) or "",
        ) for row in range(self.users.rowCount())]
        return dataclasses.replace(
            r, name=self.name.text().strip(), version=self.version.text().strip(), init=init,
            stage3=self.stage3.text().strip(), repository=self.repository.text().strip(),
            profile=desktop.profile_for(init) if desktop.key != "none" else
            ("default/linux/amd64/23.0/systemd" if init == "systemd" else "default/linux/amd64/23.0"),
            cpu=self.cpu.currentData(), cflags=self.cflags.text().strip(), binary_packages=self.binary.isChecked(),
            kernel=self.kernel.currentData(), jobs=self.jobs.value(), use=self.use.text().split(),
            package_use=package_use, packages=packages, hostname=self.hostname.text().strip(),
            domain=self.domain.text().strip(), locale=self.locale.currentText().strip(),
            timezone=self.timezone.currentText().strip(), keymap=self.keymap.currentText().strip(),
            network=network, output={**r.output, "size_gb": self.size.value()},
            root_password=self.root_hash, sudo=self.sudo.isChecked(), accounts=accounts, services=services,
            binhost=self.server_url.text().strip() if self.updates.isChecked() else "",
            binhost_key=(self.server_key.text().strip() or DEFAULT_KEY) if self.updates.isChecked() else "",
        )

    # Users

    def _set_root(self, hashed: str) -> None:
        self.root_hash = hashed
        self.root_state.setText("Root has a password." if hashed else "Root is locked (no password login).")

    def _user_row(self, a: recipes.Account) -> None:
        row = self.users.rowCount()
        self.users.insertRow(row)
        self.users.setItem(row, 0, QTableWidgetItem(a.name))
        self.users.setItem(row, 1, QTableWidgetItem(a.full_name))
        self.users.setItem(row, 2, QTableWidgetItem(", ".join(a.groups)))
        item = QTableWidgetItem("set" if a.password else "none (locked)")
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        item.setData(Qt.UserRole, a.password)
        self.users.setItem(row, 3, item)

    def add_user(self) -> None:
        self._user_row(recipes.Account("user", "", ["wheel", "audio", "video", "users"], ""))

    def remove_user(self) -> None:
        if self.users.currentRow() >= 0:
            self.users.removeRow(self.users.currentRow())

    def _ask_password(self, who: str) -> str | None:
        first, ok = QInputDialog.getText(self, "Password", f"Password for {who}:", QLineEdit.Password)
        if not ok or not first:
            return None
        again, ok = QInputDialog.getText(self, "Password", "Again:", QLineEdit.Password)
        if not ok or again != first:
            QMessageBox.warning(self, "lfsbuilder", "The two passwords differ.")
            return None
        tools = gentoo.Tools("lfsbuilder-host:dev", ROOT / "work")
        return gentoo.password_hash(tools, first)

    def set_root_password(self) -> None:
        hashed = self._ask_password("root")
        if hashed:
            self._set_root(hashed)

    def set_user_password(self) -> None:
        row = self.users.currentRow()
        if row < 0:
            return
        hashed = self._ask_password(self.users.item(row, 0).text())
        if hashed:
            self.users.item(row, 3).setData(Qt.UserRole, hashed)
            self.users.item(row, 3).setText("set")

    # Files

    def _write(self, path: Path) -> bool:
        text = recipes.dump(self.recipe())
        path.write_text(text)
        try:
            recipes.load(path)  # the same checks as the command line
        except recipes.RecipeError as error:
            QMessageBox.warning(self, "lfsbuilder", f"Saved, but the recipe isn't right yet:\n\n{error}")
            return False
        self.path = path
        self.path_label.setText(f"Recipe: {path}")
        return True

    def save(self) -> bool:
        return self._write(self.path)

    def save_as(self) -> None:
        name, _ = QFileDialog.getSaveFileName(self, "Save the recipe", str(ROOT / "recipes"), "Recipes (*.toml)")
        if name:
            self._write(Path(name))

    def open(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "Open a recipe", str(ROOT / "recipes"), "Recipes (*.toml)")
        if name:
            self.load(Path(name))

    # Running lfsbuilder

    def run(self, command: str) -> None:
        if not self.save():
            return
        self.output.clear()
        self.output_text = ""
        self.process = QProcess(self)
        self.process.setWorkingDirectory(str(ROOT))
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.finished.connect(lambda code, _status, c=command: self._finished(c, code))
        for b in self.run_buttons:
            b.setEnabled(False)
        self.stop_button.setEnabled(True)
        arguments = [command, *(["--repair"] if command == "build" and self.repair.isChecked() else []), str(self.path)]
        self.total_steps = len(steps.all_steps(self.recipe()))
        self.progress.setText(f"Running: {command} …")
        self.output.appendPlainText(f"$ python3 -m lfsbuilder {' '.join(arguments)}\n")
        self.process.start("python3", ["-u", "-m", "lfsbuilder", *arguments])

    def _read(self) -> None:
        text = bytes(self.process.readAllStandardOutput()).decode(errors="replace")
        self.output_text += text
        self.output.moveCursor(QTextCursor.End)
        self.output.insertPlainText(text)
        self.output.moveCursor(QTextCursor.End)
        lines = [m for m in map(_STEP_LINE.match, self.output_text.splitlines()) if m]
        if lines:
            last = lines[-1]
            self.progress.setText(f"Step {last['number']} of {self.total_steps}: {last['title']}")

    def _finished(self, command: str, code: int) -> None:
        for b in self.run_buttons:
            b.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.output.appendPlainText(f"\n(finished, exit code {code})")
        self.progress.setText(f"{command}: {'done' if code == 0 else 'failed (exit code %d)' % code}")
        if command == "publish":
            self._show_published()
        if command == "check":
            suggested = [m for m in map(_SUGGESTED.match, self.output_text.splitlines()) if m]
            if suggested:
                lines = [f"{m['package']} {' '.join(f.strip().strip(chr(34)) for f in m['flags'].split(','))}" for m in suggested]
                answer = QMessageBox.question(self, "lfsbuilder", "Portage needs these USE changes:\n\n"
                                              + "\n".join(lines) + "\n\nAdd them to the recipe?")
                if answer == QMessageBox.Yes:
                    current = self.package_use.toPlainText().rstrip()
                    self.package_use.setPlainText((current + "\n" if current else "") + "\n".join(lines))
                    self.save()
        if command == "boot":
            screen = ROOT / "work" / f"build-{self.path.stem}" / "screen.png"
            if code == 0 and screen.exists():
                self.show_screen(screen)

    def show_screen(self, path: Path) -> None:
        """The boot test's picture of the system's screen."""
        dialog = QDialog(self)
        dialog.setWindowTitle(f"The system's screen ({path.name})")
        label = QLabel()
        label.setPixmap(QPixmap(str(path)))
        layout = QVBoxLayout(dialog)
        layout.addWidget(label)
        dialog.show()

    def stop(self) -> None:
        if self.process:
            self.process.terminate()

    def serve(self, on: bool) -> None:
        """The package server, run beside whatever else runs; its output goes to the output pane too."""
        if not on:
            if self.server:
                self.server.terminate()
                self.server.waitForFinished(3000)
            self.server = None
            self.serve_button.setText("Serve updates")
            return
        self.server = QProcess(self)
        self.server.setWorkingDirectory(str(ROOT))
        self.server.setProcessChannelMode(QProcess.MergedChannels)
        self.server.readyReadStandardOutput.connect(lambda: self.output.appendPlainText(
            bytes(self.server.readAllStandardOutput()).decode(errors="replace").rstrip()) if self.server else None)
        self.server.finished.connect(lambda *_: self.serve_button.setChecked(False))
        self.output.appendPlainText("$ python3 -m lfsbuilder serve --bind 0.0.0.0")
        self.server.start("python3", ["-u", "-m", "lfsbuilder", "serve", "--bind", "0.0.0.0"])
        self.serve_button.setText("Serving updates (on)")

    def closeEvent(self, event) -> None:
        self.serve_button.setChecked(False)
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    path = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT
    window = Form(path)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
