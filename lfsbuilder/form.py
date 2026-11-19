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
import shutil
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QProcess, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPalette, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QRadioButton, QScrollArea, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
    QWidget,
)

from . import __main__ as cli, binhost, branding, customize, estimate, fixes as fixes_, formhelp, gentoo, presets, recipe as recipes, steps

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "recipes" / "example.toml"
_STEP_LINE = re.compile(r"^\s*(?P<number>\d+)  (?P<title>.+?)\s+(?:\.\.\.|done before)")
# Portage's progress in a step's log: which package it is on, and whether it compiles it or installs a binary.
_EMERGING = re.compile(r"^>>> Emerging (?P<binary>binary )?\((?P<n>\d+) of (?P<total>\d+)\) (?P<package>\S+?)(::\S+)?$")
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


class Columns(QGridLayout):
    """A page's boxes in three columns: left to right, then the next row (a box may span columns)."""

    def __init__(self, parent: QWidget, columns: int = 3):
        super().__init__(parent)
        self.columns, self.row, self.column = columns, 0, 0
        for c in range(columns):
            self.setColumnStretch(c, 1)

    def addWidget(self, widget: QWidget, span: int = 1) -> None:  # noqa: N802 (Qt's name)
        if self.column + span > self.columns:
            self.row, self.column = self.row + 1, 0
        super().addWidget(widget, self.row, self.column, 1, span)
        self.column += span
        if self.column >= self.columns:
            self.row, self.column = self.row + 1, 0

    def addStretch(self) -> None:  # noqa: N802
        self.setRowStretch(self.row + 1, 1)


class CustomizeDialog(QDialog):
    """A change to a program's source: `customize` (draft only) in the background, the patch shown;
    Keep it saves it with the recipe (customize.keep)."""

    def __init__(self, parent: QWidget, recipe_path: Path, packages: list[str]):
        super().__init__(parent)
        self.setWindowTitle("Change a program's source with Claude")
        self.resize(1000, 700)
        self.recipe_path, self.kept, self.patch = recipe_path, "", ""
        self.process: QProcess | None = None
        self.output = ""
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.package = QComboBox(editable=False)
        self.package.addItems(packages)
        self.instruction = QPlainTextEdit(placeholderText="e.g. Add the line 'Hello world from Odyssey' to the output "
                                                          "of nano --version")
        self.instruction.setMaximumHeight(80)
        self.check = QLineEdit(placeholderText="optional, e.g. nano --version | grep -q 'Hello world from Odyssey'")
        form.addRow("Program", self.package)
        form.addRow("Instruction", self.instruction)
        form.addRow("Check after building", self.check)
        layout.addLayout(form)
        layout.addWidget(QLabel("Portage fetches exactly the version the build compiles; Claude may only read and edit "
                                "that copy. It takes a few minutes and costs some cents (more for a big program).",
                                wordWrap=True))
        self.status = QLabel(wordWrap=True)
        layout.addWidget(self.status)
        self.text = QPlainTextEdit(readOnly=True)
        self.text.setFont(QFont("monospace", 9))
        self.text.setStyleSheet(OUTPUT_STYLE)
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)
        layout.addWidget(self.text)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.write_button = QPushButton("Write the change")
        self.write_button.clicked.connect(self.write)
        self.keep_button = QPushButton("Keep it")
        self.keep_button.setEnabled(False)
        self.keep_button.clicked.connect(self.keep)
        cancel = QPushButton("Throw it away")
        cancel.clicked.connect(self.reject)
        for b in (self.write_button, self.keep_button, cancel):
            buttons.addWidget(b)
        layout.addLayout(buttons)
        formhelp.explain(self)

    def write(self) -> None:
        instruction = self.instruction.toPlainText().strip()
        if not instruction:
            self.status.setText("Say what to change first.")
            return
        self.write_button.setEnabled(False)
        self.keep_button.setEnabled(False)
        self.status.setText("Fetching the source, then Claude reads and changes it… (a few minutes)")
        self.text.clear()
        self.output = ""
        self.process = QProcess(self)
        self.process.setWorkingDirectory(str(ROOT))
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read)
        self.process.finished.connect(lambda code, _status: self._written(code))
        self.process.start("python3", ["-u", "-m", "lfsbuilder", "customize", str(self.recipe_path),
                                       self.package.currentText(), instruction])

    def _read(self) -> None:
        self.output += bytes(self.process.readAllStandardOutput()).decode(errors="replace")
        self.text.setPlainText(self.output)
        self.text.moveCursor(QTextCursor.End)

    def _written(self, code: int) -> None:
        self.write_button.setEnabled(True)
        self.write_button.setText("Write it again")
        start, end = "----- the change -----\n", "----- end of the change -----"
        if code != 0 or start not in self.output:
            self.status.setText("No change: " + (self.output.strip().splitlines() or ["Claude Code didn't answer."])[-1])
            return
        self.patch = self.output.split(start, 1)[1].split(end, 1)[0]
        said = next((l for l in self.output.splitlines() if l.startswith("Claude changed:")), "")
        self.status.setText(f"{said}\nRead the change below: Keep it, or change the instruction and write it again.")
        self.text.setPlainText(self.patch)
        self.keep_button.setEnabled(True)

    def keep(self) -> None:
        try:
            customize.keep(self.recipe_path, self.package.currentText(), self.instruction.toPlainText(),
                           self.check.text(), self.patch)
        except (customize.CustomizeError, recipes.RecipeError) as error:
            self.status.setText(f"Not kept: {error}")
            return
        self.kept = self.package.currentText()
        self.accept()

    def reject(self) -> None:
        if self.process and self.process.state() != QProcess.NotRunning:
            self.process.kill()
            self.process.waitForFinished(3000)
        super().reject()


class LogoDialog(QDialog):
    """Draw a logo with Claude: `draw-logo` (logo.py) in the background, then its pictures shown.
    A folder this window made and didn't use is removed again."""

    def __init__(self, parent: QWidget, label: str):
        super().__init__(parent)
        self.setWindowTitle("Draw a logo with Claude")
        self.resize(900, 640)
        self.label, self.chosen, self.made = label, "", ""
        self.process: QProcess | None = None
        self.output = ""
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(placeholderText="e.g. odyssey2 (lower-case letters, digits, _ and -)")
        self.description = QPlainTextEdit(placeholderText="e.g. An owl in flight over the sea at night, "
                                                          "simple flat shapes, gold and dark blue")
        self.description.setMaximumHeight(90)
        form.addRow("Folder name", self.name)
        form.addRow("Description", self.description)
        layout.addLayout(form)
        layout.addWidget(QLabel(f"Shown with the label \"{label}\". Each drawing costs a few cents (Claude Code) "
                                "and takes up to a minute.", wordWrap=True))
        previews = QHBoxLayout()
        self.screen = QLabel("The login screen appears here.", alignment=Qt.AlignCenter)
        self.screen.setMinimumSize(560, 315)
        self.screen.setStyleSheet("background: #0d1b2e; color: #8a8a8a;")
        self.icon = QLabel("The icon", alignment=Qt.AlignCenter)
        self.icon.setMinimumSize(256, 256)
        self.icon.setStyleSheet("background: #ffffff; color: #8a8a8a;")
        previews.addWidget(self.screen, 2)
        previews.addWidget(self.icon, 1)
        layout.addLayout(previews)
        self.status = QLabel(wordWrap=True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.draw_button = QPushButton("Draw")
        self.draw_button.clicked.connect(self.draw)
        self.use_button = QPushButton("Use it")
        self.use_button.setEnabled(False)
        self.use_button.clicked.connect(self.use)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        for b in (self.draw_button, self.use_button, cancel):
            buttons.addWidget(b)
        layout.addLayout(buttons)
        formhelp.explain(self)

    def draw(self) -> None:
        name, description = self.name.text().strip(), self.description.toPlainText().strip()
        if not logo_name_ok(name):
            self.status.setText("The folder name: lower-case letters, digits, _ and - (it becomes branding/<name>/).")
            return
        if not description:
            self.status.setText("Describe the logo first.")
            return
        if (recipes.BRANDING_ROOT / name).exists() and name != self.made:
            self.status.setText(f"branding/{name}/ is there already: choose another name.")
            return
        if self.made and self.made != name:
            self._remove(self.made)  # an earlier drawing under another name, not used
        self.draw_button.setEnabled(False)
        self.use_button.setEnabled(False)
        self.status.setText("Claude is drawing… (up to a minute)")
        self.output = ""
        self.process = QProcess(self)
        self.process.setWorkingDirectory(str(ROOT))
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(
            lambda: setattr(self, "output", self.output + bytes(self.process.readAllStandardOutput()).decode(errors="replace")))
        self.process.finished.connect(lambda code, _status, n=name: self._drawn(n, code))
        self.process.start("python3", ["-u", "-m", "lfsbuilder", "draw-logo", name, description, "--label", self.label,
                                       *(["--replace"] if name == self.made else [])])

    def _drawn(self, name: str, code: int) -> None:
        self.draw_button.setEnabled(True)
        self.draw_button.setText("Draw again")
        lines = dict(line.split(": ", 1) for line in self.output.splitlines() if line.startswith(("pictures: ", "icon: ")))
        said = next((line for line in self.output.splitlines() if line.startswith(("Claude drew:", "no logo:"))), "")
        if code != 0 or "pictures" not in lines:
            self.status.setText(said or self.output.strip()[-400:] or "Claude Code didn't answer.")
            return
        self.made = name
        self.screen.setPixmap(QPixmap(str(ROOT / lines["pictures"])).scaled(
            self.screen.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.icon.setPixmap(QPixmap(str(ROOT / lines["icon"])))
        self.status.setText(f"{said}\nUse it, or change the description and draw again.")
        self.use_button.setEnabled(True)

    def use(self) -> None:
        self.chosen = self.made
        self.accept()

    def reject(self) -> None:
        if self.process and self.process.state() != QProcess.NotRunning:
            self.process.kill()
            self.process.waitForFinished(3000)
        if self.made and not self.chosen:
            self._remove(self.made)
        super().reject()

    @staticmethod
    def _remove(name: str) -> None:
        shutil.rmtree(recipes.BRANDING_ROOT / name, ignore_errors=True)


def step_package(logs: Path, number: int) -> str:
    """What Portage is doing in the step's log: "compiling dev-lang/rust-1.91 (3 of 12)" or
    "installing binaries: … (37 of 107)"; empty when it isn't installing packages."""
    log = next(iter(sorted(logs.glob(f"{number:02}-*.log"))), None) if logs.is_dir() else None
    if not log:
        return ""
    with log.open("rb") as f:
        f.seek(max(0, log.stat().st_size - 200_000))
        tail = f.read().decode(errors="replace").splitlines()
    last = next((m for m in map(_EMERGING.match, reversed(tail)) if m), None)
    if not last:
        return ""
    what = "installing the binary" if last["binary"] else "compiling"
    return f"{what} {last['package']} ({last['n']} of {last['total']})"


def compiler_output(lines: int = 300) -> tuple[str, str]:
    """(what is compiled, the end of its build log): the newest Portage build.log in a running step's
    container. While binaries are installed there is none."""
    try:
        names = subprocess.run(["docker", "ps", "--filter", "name=lfsbuilder-step-", "--format", "{{.Names}}"],
                               capture_output=True, text=True, timeout=10).stdout.split()
        if not names:
            return "No build step is running.", ""
        script = ("log=$(ls -t /var/tmp/portage/*/*/temp/build.log 2>/dev/null | head -n1); "
                  f'[ -n "$log" ] && echo "$log" && tail -n {lines} "$log"')
        out = subprocess.run(["docker", "exec", names[0], "sh", "-c", script],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "Docker didn't answer.", ""
    if not out.strip():
        return "Nothing is being compiled right now (binaries are being installed, or the step compiles nothing).", ""
    path, _, body = out.partition("\n")
    package = "/".join(path.split("/")[4:6])  # /var/tmp/portage/<category>/<package-version>/temp/build.log
    return f"Compiling {package} (the newest build log; the window follows it every 2 seconds)", body


def logo_name_ok(name: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", name))


# The output box: light words on black, like a terminal.
OUTPUT_STYLE = "QPlainTextEdit { background-color: #000000; color: #e6e6e6; }"


class Form(QMainWindow):
    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        self.base: recipes.Recipe | None = None  # what was loaded: the fields the form doesn't show stay as they were
        self.process: QProcess | None = None
        self.output_text = ""
        self.setWindowTitle("lfsbuilder")
        self.resize(1400, 860)

        tabs = QTabWidget()
        tabs.addTab(self._scroll(self._system_page()), "System")
        tabs.addTab(self._scroll(self._software_page()), "Desktop and software")
        tabs.addTab(self._scroll(self._users_page()), "Users")
        tabs.addTab(self._scroll(self._advanced_page()), "Advanced")

        self.output = QPlainTextEdit(readOnly=True)
        self.output.setFont(QFont("monospace", 9))
        self.output.setStyleSheet(OUTPUT_STYLE)
        palette = self.output.palette()
        palette.setColor(QPalette.PlaceholderText, QColor("#8a8a8a"))
        self.output.setPalette(palette)
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
        self.compiler_button = QPushButton("Compiler output")
        self.compiler_button.setToolTip("Follow the compiler's output of the package being compiled now")
        self.compiler_button.setEnabled(False)
        self.compiler_button.clicked.connect(self.show_compiler)
        buttons.addWidget(self.compiler_button)
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
        self.estimate = QLabel(wordWrap=True)
        self.estimate.setToolTip("From how long the same steps took before (work/step-times.json); "
                                 "a step never built anywhere has no estimate")
        self.run_estimate: estimate.Estimate | None = None
        layout.addWidget(tabs)
        layout.addLayout(buttons)
        layout.addWidget(self.estimate)
        layout.addWidget(self.progress)
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(top)
        splitter.addWidget(self.output)
        splitter.setSizes([640, 180])
        self.setCentralWidget(splitter)
        # While a command runs: the package Portage is on, from the current step's log, every 2 seconds.
        self.step_text, self.step_number = "", 0
        self.watch = QTimer(self, interval=2000)
        self.watch.timeout.connect(self._watch_step)
        self.load(path)
        formhelp.explain(self)  # an ⓘ and a plain-words tooltip on every field, box and button

    # The pages

    @staticmethod
    def _scroll(widget: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(widget)
        return area

    def _system_page(self) -> QWidget:
        page = QWidget()
        layout = Columns(page)

        identity = QGroupBox("Identity")
        form = QFormLayout(identity)
        self.name = QLineEdit()
        self.version = QLineEdit()
        form.addRow("Distribution name", self.name)
        form.addRow("Version", self.version)
        form.addRow(QLabel("Raise the version for every release: installed systems update to a higher one only.", wordWrap=True))
        # The look: a folder of branding/ with the logo's two drawings (logo.svg, logo-light.svg), and the
        # words beside the logo. The pictures are drawn from them when the recipe is next built or checked.
        self.branding = QComboBox()
        self.branding.addItems(sorted(p.name for p in recipes.BRANDING_ROOT.iterdir()
                                      if all((p / f).is_file() for f in recipes.BRANDING_FILES)))
        self.label = QLineEdit()
        row = QHBoxLayout()
        row.addWidget(self.branding, 1)
        draw = QPushButton("Draw a logo with Claude…")
        draw.setToolTip("Describe a logo: Claude draws it (a few cents through Claude Code), you see it before using it")
        draw.clicked.connect(self.draw_logo)
        row.addWidget(draw)
        form.addRow("Logo (branding folder)", row)
        form.addRow("Label beside the logo", self.label)
        form.addRow(QLabel("The label's first word is bold. Empty: the distribution name. A new logo is a folder "
                           "branding/<name>/ with logo.svg (for light backgrounds) and logo-light.svg (for dark).",
                           wordWrap=True))
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
        layout = Columns(page)
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

        # The software, a box per group; the search field hides what doesn't match (name or package).
        search_box = QGroupBox("Find software")
        column = QVBoxLayout(search_box)
        self.software_search = QLineEdit(placeholderText="Type to filter: e.g. video, pdf, editor, gimp")
        self.software_search.textChanged.connect(self._filter_software)
        column.addWidget(self.software_search)
        column.addWidget(QLabel(f"{len(presets.SOFTWARE)} programs, each checked to exist in the pinned Gentoo "
                                "(hover one for its package name). Check tells how many come as binaries "
                                "and what must be compiled.", wordWrap=True))
        layout.addWidget(search_box, span=2)
        self.software_boxes: dict[str, QCheckBox] = {}
        self.software_groups: list[tuple[QGroupBox, list[QCheckBox]]] = []
        for name in presets.GROUPS:
            members = [s for s in presets.SOFTWARE if s.group == name]
            box = QGroupBox(name)
            grid = QGridLayout(box)
            rows = (len(members) + 1) // 2 if len(members) > 10 else len(members)  # long groups: two columns
            checks = []
            for i, s in enumerate(members):
                check = QCheckBox(s.label)
                check.setToolTip(formhelp.software_tip(s.label, s.package, s.group, s.service))
                grid.addWidget(check, i % rows, i // rows)
                self.software_boxes[s.package] = check
                checks.append(check)
            grid.setRowStretch(rows, 1)
            layout.addWidget(box)
            self.software_groups.append((box, checks))
        other = QGroupBox("Other packages")
        column = QVBoxLayout(other)
        column.addWidget(QLabel("category/name, one per line (names on packages.gentoo.org):", wordWrap=True))
        self.other_packages = QPlainTextEdit()
        self.other_packages.setMaximumHeight(110)
        column.addWidget(self.other_packages)
        layout.addWidget(other)

        changes = QGroupBox("Customizations: changes to a program's source code")
        column = QVBoxLayout(changes)
        column.addWidget(QLabel("Say in words what to change in one of the recipe's programs: Claude writes the change, "
                                "you read it, and the build compiles that program with it (only that one). Saved "
                                "beside the recipe, on this PC.", wordWrap=True))
        self.custom_table = QTableWidget(0, 3)
        self.custom_table.setHorizontalHeaderLabels(["Package", "Instruction", "Check after building"])
        self.custom_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.custom_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.custom_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.custom_table.setMaximumHeight(130)
        column.addWidget(self.custom_table)
        row = QHBoxLayout()
        for label, slot in (("Add a change with Claude…", self.add_customization),
                            ("Show the change", self.show_customization), ("Remove", self.remove_customization)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch()
        column.addLayout(row)
        layout.addWidget(changes, span=3)
        layout.addStretch()
        return page

    def _filter_software(self, text: str) -> None:
        words = text.lower().split()
        for box, checks in self.software_groups:
            shown = 0
            for check in checks:
                hay = f"{check.text()} {check.toolTip()} {box.title()}".lower()
                visible = all(w in hay for w in words) or (check.isChecked() and not words)
                check.setVisible(visible)
                shown += visible
            box.setVisible(shown > 0)

    def _users_page(self) -> QWidget:
        page = QWidget()
        layout = Columns(page)
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
        layout.addWidget(accounts, span=2)
        layout.addStretch()
        return page

    def _advanced_page(self) -> QWidget:
        page = QWidget()
        layout = Columns(page)
        self.stage3 = QLineEdit()
        self.repository = QLineEdit()
        self.cflags = QLineEdit()
        self.jobs = QSpinBox(minimum=0, maximum=256, specialValueText="as many as the processors")
        self.use = QLineEdit(placeholderText="-bluetooth pulseaudio")
        self.package_use = QPlainTextEdit(placeholderText="media-video/ffmpeg -x264")
        self.services = QLineEdit(placeholderText="gdm sshd")
        pinned = QGroupBox("Pinned")
        form = QFormLayout(pinned)
        form.addRow(QLabel("What the recipe pins. The defaults are tested.", wordWrap=True))
        form.addRow("Stage3 build", self.stage3)
        form.addRow("Repository commit", self.repository)
        layout.addWidget(pinned)
        compiler = QGroupBox("Compiler")
        form = QFormLayout(compiler)
        form.addRow("Compiler flags", self.cflags)
        form.addRow("Make jobs", self.jobs)
        layout.addWidget(compiler)
        gentoo = QGroupBox("USE flags and services")
        form = QFormLayout(gentoo)
        form.addRow("USE flags (all packages)", self.use)
        form.addRow("USE flags per package\n(package flags, one per line)", self.package_use)
        form.addRow("Other services to start", self.services)
        layout.addWidget(gentoo)
        layout.addStretch()
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

    def _estimate_of(self, r: recipes.Recipe) -> estimate.Estimate | None:
        try:
            p = cli.planned(r)
        except (OSError, recipes.RecipeError, branding.BrandingError):
            return None
        return estimate.estimate(p, estimate.load(cli.TIMES), estimate.built_images()) if p else None

    def _show_estimate(self, r: recipes.Recipe) -> None:
        e = self._estimate_of(r)
        if not branding.current(r):
            self.estimate.setText(f"Build: the pictures of the logo {r.branding!r} with the label {r.label_text!r} "
                                  "aren't drawn yet: Check or Build draws them first.")
            return
        self.estimate.setText(f"Build: {e.text()}" if e else "Build: Prepare first (the stage3 and the repository aren't fetched yet).")

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
        self.custom_table.setRowCount(0)
        for c in r.customizations:
            row = self.custom_table.rowCount()
            self.custom_table.insertRow(row)
            for column, text in enumerate((c.package, c.instruction, c.check)):
                self.custom_table.setItem(row, column, QTableWidgetItem(text))
        self.name.setText(r.name)
        self.version.setText(r.version)
        if self.branding.findText(r.branding) < 0:
            self.branding.addItem(r.branding)
        self.branding.setCurrentText(r.branding)
        self.label.setText(r.label)
        self.label.setPlaceholderText(r.name)
        self.updates.setChecked(bool(r.binhost))
        self.server_url.setText(r.binhost)
        self.server_key.setText(r.binhost_key or DEFAULT_KEY)
        self._update_fields()
        self._show_published()
        self._show_estimate(r)
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
            branding=self.branding.currentText(), label=self.label.text().strip(),
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
        r = self.recipe()
        text = recipes.dump(r)
        old, new = fixes_.paths(self.path)[1], fixes_.paths(path)[1]
        if r.customizations and path != self.path and old.is_dir() and not new.exists():
            shutil.copytree(old, new)  # (Save as: the customizations' patches go with the recipe)
        path.write_text(text)
        try:
            recipes.load(path)  # the same checks as the command line
        except recipes.RecipeError as error:
            QMessageBox.warning(self, "lfsbuilder", f"Saved, but the recipe isn't right yet:\n\n{error}")
            return False
        self.path = path
        self.path_label.setText(f"Recipe: {path}")
        self._show_estimate(recipes.load(path))
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
        self.compiler_button.setEnabled(True)
        self.step_text, self.step_number = "", 0
        self.watch.start()
        arguments = [command, *(["--repair"] if command == "build" and self.repair.isChecked() else []), str(self.path)]
        try:
            self.total_steps = len(steps.all_steps(self.recipe()))
        except branding.BrandingError:
            self.total_steps = 0  # (the command draws the pictures first; the progress line counts without a total)
        self.run_estimate = self._estimate_of(recipes.load(self.path)) if command == "build" else None
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
            total = f" of {self.total_steps}" if self.total_steps else ""
            text = f"Step {last['number']}{total}: {last['title']}"
            if self.run_estimate:
                left = [(sec, how) for p, sec, how in self.run_estimate.steps if p.number >= int(last['number']) and how != "done"]
                if left and all(sec is not None for sec, _ in left):
                    text += f" (about {estimate.duration(sum(sec for sec, _ in left))} left, this step included)"
            self.step_text, self.step_number = text, int(last["number"])
            self._watch_step()

    def _finished(self, command: str, code: int) -> None:
        for b in self.run_buttons:
            b.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.compiler_button.setEnabled(False)
        self.watch.stop()
        self.output.appendPlainText(f"\n(finished, exit code {code})")
        self.progress.setText(f"{command}: {'done' if code == 0 else 'failed (exit code %d)' % code}")
        if command == "publish":
            self._show_published()
        if command in ("build", "check"):
            self._show_estimate(recipes.load(self.path))
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

    def _watch_step(self) -> None:
        """The progress line, with the package Portage is on in the current step (its log)."""
        if not self.step_text:
            return
        package = step_package(ROOT / "work" / f"build-{self.path.stem}" / "logs", self.step_number)
        self.progress.setText(f"{self.step_text} — {package}" if package else self.step_text)

    def show_compiler(self) -> None:
        """A window following the compiler's output of the package being compiled now (Portage's
        build.log in the step's container), every 2 seconds."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Compiler output")
        dialog.resize(1000, 560)
        layout = QVBoxLayout(dialog)
        title = QLabel(wordWrap=True)
        text = QPlainTextEdit(readOnly=True)
        text.setFont(QFont("monospace", 9))
        text.setStyleSheet(OUTPUT_STYLE)
        text.setLineWrapMode(QPlainTextEdit.NoWrap)
        layout.addWidget(title)
        layout.addWidget(text)

        def refresh() -> None:
            where, lines = compiler_output()
            title.setText(where)
            at_end = text.verticalScrollBar().value() >= text.verticalScrollBar().maximum() - 4
            if text.toPlainText() != lines:
                text.setPlainText(lines)
                if at_end:
                    text.moveCursor(QTextCursor.End)

        timer = QTimer(dialog, interval=2000)
        timer.timeout.connect(refresh)
        timer.start()
        refresh()
        dialog.exec()

    def _selected_customization(self) -> str:
        row = self.custom_table.currentRow()
        return self.custom_table.item(row, 0).text() if row >= 0 and self.custom_table.item(row, 0) else ""

    def add_customization(self) -> None:
        if not self.save():
            return
        r = recipes.load(self.path)
        dialog = CustomizeDialog(self, self.path, sorted(set(r.packages)))
        if dialog.exec() == QDialog.Accepted:
            self.load(self.path)
            self.progress.setText(f"{dialog.kept} is customized: Build compiles it with the change.")

    def show_customization(self) -> None:
        package = self._selected_customization()
        if not package:
            return
        patch = fixes_.paths(self.path)[1] / package / recipes.CUSTOM_PATCH
        dialog = QDialog(self)
        dialog.setWindowTitle(f"The change to {package}")
        dialog.resize(900, 520)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit(readOnly=True)
        text.setFont(QFont("monospace", 9))
        text.setStyleSheet(OUTPUT_STYLE)
        text.setPlainText(patch.read_text() if patch.is_file() else "(no patch)")
        layout.addWidget(QLabel(str(patch)))
        layout.addWidget(text)
        dialog.exec()

    def remove_customization(self) -> None:
        package = self._selected_customization()
        if not package or not self.save():
            return
        if QMessageBox.question(self, "lfsbuilder", f"Remove the change to {package}? "
                                "The next build installs it as Gentoo ships it.") != QMessageBox.Yes:
            return
        customize.remove(self.path, package)
        self.load(self.path)

    def draw_logo(self) -> None:
        """The logo window: a description, Claude's drawing, its pictures; Use it picks it."""
        dialog = LogoDialog(self, self.label.text().strip() or self.name.text().strip() or "Argo Linux")
        if dialog.exec() == QDialog.Accepted and dialog.chosen:
            if self.branding.findText(dialog.chosen) < 0:
                self.branding.addItem(dialog.chosen)
            self.branding.setCurrentText(dialog.chosen)
            self.progress.setText(f"Logo {dialog.chosen!r} chosen: Save, then Check or Build.")

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
