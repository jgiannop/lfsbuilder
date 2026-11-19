"""The form's explanations, for someone who has never built a Linux distribution.

Every field, box, button and option of the form gets one: shown when the
mouse is over it, and an ⓘ mark beside the names of fields, boxes and the main
options says there is one. `explain()` puts them in, after the form is built,
by the words the form shows (so a new field without an entry here simply has
none). Kept apart from form.py so they can be read and improved as text.
"""

from __future__ import annotations

MARK = "ⓘ"

# Field names (the words left of a field), by the words the form shows.
FIELDS = {
    "Distribution name": (
        "The name of your distribution, as people see it: in the boot menu (GRUB), on the login screen's banner, "
        "in the installer (\"Install Odyssey Linux\") and in /etc/os-release, which programs read to know what "
        "system they run on.<br><br>Letters, digits, spaces and . _ + - (up to 40). Example: <i>Odyssey Linux</i>."),
    "Version": (
        "Your distribution's version, like <i>0.1</i> or <i>1.2.3</i> (numbers with dots).<br><br>"
        "<b>Raise it for every release you publish.</b> PCs that already have your distribution installed only "
        "update to a higher version: publish 0.2 after 0.1, and they fetch it; publish 0.1 again with changes, "
        "and they don't see them."),
    "Logo (branding folder)": (
        "Your distribution's logo: a folder in <i>branding/</i> with two drawings, <i>logo.svg</i> (for light "
        "backgrounds: icons, the installer) and <i>logo-light.svg</i> (for dark ones: login screen, wallpaper, "
        "boot menu).<br><br>From the logo and the label, lfsbuilder draws every picture the system shows. "
        "<i>argo</i> is the default.<br><br>No logo yet? <b>Draw a logo with Claude…</b> makes one from a "
        "description. The list is read when the form opens: start it again after adding a folder by hand."),
    "Label beside the logo": (
        "The words written beside the logo on the login screen, the wallpaper and the boot menu. The first word "
        "is drawn <b>bold</b>, the rest regular: <b>Odyssey</b> Linux.<br><br>Empty: the distribution name is "
        "used. A long label is made smaller so it fits."),
    "Server address": (
        "Where PCs with your distribution installed get their updates: the address of your package server, "
        "ending with /. Example: <i>http://192.168.1.93:8080/</i> (this PC on your home network).<br><br>"
        "<b>Publish</b> puts the updates there; <b>Serve updates</b> makes this PC answer at that address."),
    "Signing key (public, beside the recipe)": (
        "The public half of your signing key, a file beside the recipe (<i>keys/binhost.asc</i>). Installed PCs "
        "accept only packages and updates signed with its private half, which stays on this PC "
        "(<i>work/binhost/gnupg</i>: <b>back it up</b>; without it you can't update PCs already installed)."),
    "Published": (
        "When this recipe was last published. Installed PCs refuse your distribution's repository when it is "
        "older than 30 days (so nobody can feed them an old release as if it were new): <b>publish at least "
        "every 30 days</b>, even with no change. Shown in red when the limit is near."),
    "Init system": (
        "The first program the system starts, which then starts everything else (services, the login screen). "
        "<br><br><b>systemd</b>: what most distributions use, and what GNOME needs. Choose it unless you know "
        "why not.<br><b>OpenRC</b>: Gentoo's own, simpler; for servers and small systems. The desktops and the "
        "graphical installer need systemd."),
    "Processor": (
        "Which processors your distribution runs on.<br><br><b>Any 64-bit PC (x86-64)</b>: every 64-bit Intel or "
        "AMD processor. The safe choice.<br><b>Newer PCs (x86-64-v3)</b>: processors from about 2015 on (AVX2); "
        "a bit faster, but the system won't start on older PCs."),
    "Kernel": (
        "Linux itself, the core that talks to the hardware.<br><br><b>Ready-built</b>: Gentoo's, compiled by "
        "Gentoo; installs in a minute. Choose this.<br><b>Compiled here</b>: the same kernel compiled on this "
        "PC (about an hour); only useful with your own kernel settings."),
    "Language (locale)": (
        "The language and formats (dates, numbers, currency) of menus and programs. Example: <i>en_US.UTF-8</i> "
        "(American English), <i>el_GR.UTF-8</i> (Greek). The person installing can't change it in the "
        "installer yet."),
    "Time zone": "The clock's time zone, like <i>Europe/Athens</i> or <i>UTC</i>. The installer lets the user choose another.",
    "Keyboard": (
        "The keyboard layout of the text console (before the desktop starts), like <i>us</i> or <i>gr</i>. "
        "The desktop and the installer have their own keyboard settings."),
    "Host name": (
        "The computer's name on the network, like <i>myxfce</i> or <i>argo-pc</i>: lower-case letters, digits "
        "and -. Shown in terminals (<i>demo@myxfce</i>) and by other PCs on the network."),
    "Domain": "The network domain, added to the host name for its full name. <i>localdomain</i> is fine at home.",
    "Address": (
        "How the system gets its network address.<br><br><b>Automatic (DHCP)</b>: from your router, like "
        "phones and laptops do. Choose this.<br><b>Fixed address</b>: always the same address you set below; "
        "for servers others must find at a known address."),
    "IP address / prefix": "With a fixed address: the address and the network's size, like <i>192.168.1.50/24</i>.",
    "Gateway": "With a fixed address: the router's address, through which the system reaches the internet, like <i>192.168.1.1</i>.",
    "DNS servers": "With a fixed address: who turns names into addresses, comma-separated, like <i>192.168.1.1, 9.9.9.9</i>.",
    "Disk image size": (
        "The size of the virtual disk <b>Boot test</b> makes to try the system in QEMU. It doesn't limit the "
        "ISO or a real install (the installer uses the whole disk it is given). 30 GB is plenty for a desktop; "
        "the file only takes the space actually used."),
    "Stage3 build": (
        "The date of Gentoo's starter system (\"stage3\") the build begins from, like <i>20261115T170104Z</i>. "
        "Pinned, so the same recipe gives the same system next year; checked against Gentoo's signature. "
        "<b>Leave it</b> unless you want a newer Gentoo (then also change the repository commit, and Prepare)."),
    "Repository commit": (
        "The exact version of Gentoo's package list (a commit of its repository), checked against Gentoo's "
        "signature. It decides which version of every program you get. A newer one = newer programs: change "
        "it, Prepare, build, raise the version and publish, and installed PCs update to them."),
    "Compiler flags": (
        "How the compiler optimizes what is compiled here, like <i>-O2 -pipe</i> (the safe default). "
        "<br><br>They apply only to packages compiled on this PC: Gentoo's ready-made ones are built with "
        "generic flags. For them to apply everywhere, untick \"Use Gentoo's binary packages\" (and expect "
        "builds of many hours)."),
    "Make jobs": (
        "How many compile jobs run at once. \"As many as the processors\" is right for almost everyone; fewer "
        "keeps the PC more responsive during a long compile."),
    "USE flags (all packages)": (
        "Gentoo's feature switches for every package, space-separated: <i>pulseaudio</i> switches a feature on, "
        "<i>-bluetooth</i> off. Example: <i>-bluetooth</i> builds everything without Bluetooth support.<br><br>"
        "Changing them makes Gentoo's ready-made packages no longer match, so more is compiled. "
        "<b>Check</b> tells what a change costs before you build."),
    "USE flags per package\n(package flags, one per line)": (
        "Feature switches for one package only, one line each: the package, then its flags. Example: "
        "<i>media-video/ffmpeg -x264</i>.<br><br><b>Check</b> adds the ones Portage asks for by itself, so "
        "you rarely write them."),
    "Other services to start": (
        "Background programs (services) to start at every boot, space-separated, like <i>sshd</i> (remote "
        "login). The desktop's login screen and the network are started already."),
    # The logo window
    "Folder name": "The logo's folder, <i>branding/&lt;name&gt;/</i>: lower-case letters, digits, _ and -. It is the name you pick in Logo afterwards.",
    "Description": (
        "What the logo should show, in your own words. Say the subject, the style and the colours: "
        "<i>an owl in flight over the sea at night, simple flat shapes, gold and dark blue</i>. Simple, flat "
        "logos come out best."),
    # The customization window
    "Kind": (
        "Where the setting lives. <b>GSettings</b>: GNOME and most GTK programs (Files, the text editor, "
        "GNOME's top bar…). <b>KDE</b>: KDE Plasma and its programs, in files of /etc/xdg."),
    "Schema": "GSettings only: the group the key is in, like <i>org.gnome.desktop.interface</i>. <b>Ask Claude</b> fills it in.",
    "File": "KDE only: the configuration file, like <i>kwinrc</i> (windows) or <i>kdeglobals</i> (everything).",
    "Group": "KDE only: the [group] in that file, like <i>Windows</i>.",
    "Key": "The setting's name, like <i>clock-show-seconds</i>.",
    "Value": (
        "The value. GSettings writes them its own way: <i>true</i> / <i>false</i>, numbers as they are, text in "
        "quotes (<i>'prefer-dark'</i>), lists in brackets (<i>['a', 'b']</i>). A wrong one stops the build "
        "with a message."),
    "What it does": "In your own words, for the list: <i>Clock with seconds</i>.",
    "Program": (
        "The program whose source code to change: any program of the system, not only the recipe's own (GNOME, "
        "for example, is ~300 programs: its top bar is <i>gnome-base/gnome-shell</i>, its windows "
        "<i>x11-wm/mutter</i>, Files <i>gnome-base/nautilus</i>, Settings <i>gnome-base/gnome-control-center</i>, "
        "the login screen <i>gnome-base/gdm</i>). Type to search.<br><br>Not sure? Write the instruction and press "
        "<b>Which program?</b>: Claude names it, or tells you it is only a setting."),
    "Instruction": (
        "What to change, in words, as you would tell a programmer: <i>Add the line 'Hello world from Odyssey' "
        "to the output of nano --version</i>. Be specific about where and what."),
    "Check after building": (
        "Optional: a command that must succeed in the built system, proving the change is there. Example: "
        "<i>nano --version | grep -q 'Hello world from Odyssey'</i>. If it fails, the build stops at that "
        "program, so you know at once."),
}

# Boxes, by their titles.
BOXES = {
    "Identity": "Who your distribution is: its name, version, logo and the words beside the logo.",
    "Updates": (
        "How PCs with your distribution installed get updates and new releases: from your own package "
        "server, signed with your key. Untick for a system that never updates from you."),
    "Base": "The foundations: the init system, the processors it runs on, ready-made packages, and the kernel.",
    "Language and place": "Language, time zone and keyboard of the installed system.",
    "Network": "The computer's name and how it connects to the network.",
    "Output": "Settings of the virtual disk used by Boot test.",
    "Desktop": (
        "The graphical desktop, with its login screen:<br><b>None</b>: no desktop, a text console (servers).<br>"
        "<b>GNOME</b>: modern, simple, one big menu. About 40 min to build.<br><b>KDE Plasma</b>: familiar, "
        "like Windows, very configurable. About 40 min.<br><b>Xfce</b>: light and fast, good on older PCs. "
        "About 30 min.<br><br>The times are with Gentoo's ready-made packages."),
    "Find software": "Type a word to see only the programs that match: their name, what they do, or their package name.",
    "Internet": "Web browsers, email, file sharing, chat and remote desktops.",
    "Office": "Office suites, spreadsheets, word processors, PDF viewers and e-books.",
    "Multimedia": "Players and editors for video and music.",
    "Graphics": "Image editors, drawing, 3D, photo libraries and screenshots.",
    "Editors and development": "Text and code editors, programming languages, containers and virtual machines.",
    "System tools": "Disks, backups, printing, passwords, remote login and system monitors.",
    "Terminal and utilities": "Terminals and command-line tools: downloads, archives, searching, networks and VPNs.",
    "Security tools": (
        "Tools for security testing and forensics, like Kali's: network scanners, Wi-Fi testing, password "
        "auditing, reverse engineering, disk and memory forensics (nmap and Wireshark are in Terminal and "
        "utilities).<br><br><b>Only for your own systems, CTFs, learning, or testing you are allowed to do:</b> "
        "using them on networks or computers without permission is illegal in most countries."),
    "KDE apps": (
        "KDE's own applications (KDE Gear): calculator, editors, viewers, games and more. They fit KDE Plasma best "
        "(same look, same settings), and run on other desktops too, pulling in some KDE libraries."),
    "Other packages": (
        "Any other Gentoo package, one per line, as <i>category/name</i> (like <i>games-puzzle/sgt-puzzles</i>). "
        "Find names at <i>packages.gentoo.org</i>. <b>Check</b> tells if it fits your choices."),
    "Default settings (no compiling)": (
        "Settings your programs already have, made your distribution's defaults: every new user starts with "
        "them, and can still change them. Examples: GNOME's clock showing seconds, dark mode, the font size; "
        "KDE's window borders.<br><br>No compiling, so a build takes seconds more. Each setting is checked while "
        "building: a wrong name or value stops the build with a message. Installed PCs get changed settings with "
        "the next update. Xfce's own settings aren't supported yet."),
    "Customizations: changes to a program's source code": (
        "Change what a program does, in words: Claude edits the program's real source code, you read the "
        "change, and the build compiles that program with it (everything else still comes ready-made).<br><br>"
        "Example: <i>nano --version</i> printing \"Hello world from Odyssey\". Small programs compile in a "
        "minute, Firefox in hours. The change is saved beside the recipe, on this PC."),
    "Root": (
        "<i>root</i> is the administrator account. <b>Locked</b> (no password) is safest: your users get "
        "administrator rights with sudo instead (see Accounts)."),
    "Accounts": (
        "The users of the live system (the one on the ISO, before installing) and of Boot test. In the "
        "installer, the person installing creates their own user, which replaces these.<br><br>The demo "
        "user's password is <i>demo</i>: change it for anything real (<b>Set password…</b>)."),
    "Pinned": "Which Gentoo the recipe is built from, fixed so the same recipe always gives the same system. The defaults are tested.",
    "Compiler": "How things compiled on this PC are compiled. The defaults are right for almost everyone.",
    "USE flags and services": "Gentoo's feature switches and extra services. For people who know Gentoo; the defaults work.",
}

# Buttons, check boxes and choices, by their words.
OPTIONS = {
    "Open…": "Open another recipe (a .toml file in recipes/).",
    "Save": "Save the form into the recipe file. The buttons Check, Build, … save first by themselves.",
    "Save as…": (
        "Save the form as a new recipe, under a new name (lower-case letters, digits and _): a new "
        "distribution, starting from this one. The original stays as it was."),
    "Check": (
        "<b>Step 1.</b> Asks Portage (Gentoo's package manager) what the recipe would install, without "
        "installing anything (2–3 min): whether your choices fit together, which feature changes they need "
        "(it offers to add them), and how many packages come ready-made versus compiled here. Also draws the "
        "logo's pictures if they are new."),
    "Build": (
        "<b>Step 2.</b> Builds the system in Docker, step by step, saving each step (a checkpoint): a second "
        "Build redoes only what changed. The line below shows the step and the package being installed or "
        "compiled; the estimate comes from earlier builds. A desktop takes 20–40 min the first time."),
    "Repair failures with Claude": (
        "When a build step fails: lfsbuilder asks Portage first, then Claude (a few cents), for a fix, saves "
        "it beside the recipe, and tries again (up to 3 times). Claude only sees the failing command and the "
        "end of the log. Off: a failure just stops the build, with the log to look at."),
    "Boot test": (
        "Makes a virtual disk of the built system and starts it in QEMU (a virtual PC), waits for the login, "
        "and saves a picture of the screen. A quick way to see it run, without an ISO."),
    "Make ISO": (
        "<b>Step 4</b> (after Publish). Makes the installation ISO (work/build-&lt;recipe&gt;/&lt;recipe&gt;.iso) "
        "for a USB stick or VirtualBox, then tests it by itself: boots it (old and new PCs), installs it on an "
        "empty virtual disk, and boots that (15–20 min). <b>Close VirtualBox first</b>: it slows the tests about "
        "five times."),
    "Publish": (
        "<b>Step 3.</b> Puts your distribution on your package server (a folder on this PC, "
        "work/binhost/www): its packages, its repository and Gentoo's package list, signed with your key. "
        "Needed before Make ISO, and for every release installed PCs should get. Publish at least every 30 days."),
    "Stop": "Stops what is running. A stopped build keeps every finished step: Build again goes on from there.",
    "Compiler output": (
        "While a build runs: a window following the compiler's output of the program being compiled right now "
        "(nothing while ready-made packages are installed)."),
    "Serve updates": (
        "Makes this PC your package server: installed PCs on your network fetch updates from it "
        "(port 8080). Switch it off when you don't need it; closing the form stops it too."),
    "Installed systems take updates from a package server": (
        "On: installed PCs update from your server (Server address), accepting only what your key signed. "
        "Off: they never get updates from you."),
    "Use Gentoo's binary packages where they match (much faster)": (
        "On: packages Gentoo has ready-made for exactly your choices are downloaded (signature-checked) and "
        "installed in seconds; only the rest is compiled here. A desktop builds in about 30 minutes.<br><br>"
        "Off: everything is compiled on this PC from source: 8–30 hours for a desktop. Only worth it when your "
        "compiler flags must apply to every package."),
    "Administrators (the \"wheel\" group) may use sudo": (
        "Users in the <i>wheel</i> group may run commands as administrator with <i>sudo</i> (typing their own "
        "password). Keep it on: it is how you administer a system whose root is locked."),
    "Automatic (DHCP)": "The router gives the system its address. Right for almost every PC.",
    "Fixed address": "The system always uses the address you set below (servers).",
    "Set root's password…": "Give the administrator account a password (kept only as a hash). Usually not needed: use sudo.",
    "Lock root": "No password login as root: the safest; administer with sudo.",
    "Add": "Add a user of the live system and of Boot test.",
    "Remove": "Remove the selected row.",
    "Set password…": "Set the selected user's password (kept only as a hash: the recipe never holds a password).",
    "Draw a logo with Claude…": (
        "Describe a logo in words: Claude draws it (a few cents, about a minute), you see it on the login "
        "screen and as an icon, and Use it picks it. Draw again until you like it."),
    "Add a change with Claude…": "Change a program's source code in words: Claude writes the change, you read it, Keep it saves it.",
    "Show the change": "Show the patch (the exact lines changed) of the selected customization.",
    "Draw": "Ask Claude for the logo (a few cents, about a minute).",
    "Use it": "Use this logo: it is picked in Logo. Save, then Check or Build.",
    "Add a setting…": "Add a default setting: say what you want and Claude finds it, or fill it in yourself.",
    "Remove the setting": "Take the selected setting out (Save, then Build).",
    "Ask Claude": "Claude finds the setting for what you said (a few cents, under a minute) and fills the fields in. Check them, then Add.",
    "Add the setting": "Add it to the recipe's settings (then Save the recipe).",
    "Add it as a default setting": "Claude found that your wish is only a setting: add it as a default setting instead of changing the source.",
    "Which program? (ask Claude)": (
        "Claude reads your instruction and the list of the system's programs, and says where the change belongs: "
        "a program's source (it picks the program for you), a setting the program already has (no compiling), "
        "or an add-on. A few cents, under a minute."),
    "Write the change": "Fetch the program's source and let Claude change it (a few minutes, some cents). Nothing is kept yet.",
    "Keep it": "Keep the change: saved beside the recipe; Build compiles the program with it.",
    "Throw it away": "Close without keeping anything.",
    "Cancel": "Close; a logo drawn here and not used is removed.",
}

DESKTOP_CHOICES = {
    "None (a server or a base to build on)": "No graphical desktop: a text console. For servers, or a small base to add to.",
    "GNOME": "GNOME: modern and simple, one full-screen menu of apps, its own login screen (GDM). Needs systemd.",
    "KDE Plasma": "KDE Plasma: a familiar desktop like Windows (a panel and a start menu), very configurable. Login screen: SDDM.",
    "Xfce": "Xfce: light, fast and classic; good on older PCs. Login screen: LightDM.",
}


def tip(text: str, title: str = "") -> str:
    """A tooltip in rich text (Qt wraps those to a readable width)."""
    return f"<qt>{f'<b>{title}</b><br>' if title else ''}{text}</qt>"


def software_tip(label: str, package: str, group: str, service: str = "") -> str:
    started = f"<br>It starts its service (<i>{service}</i>) at every boot." if service else ""
    return tip(f"Gentoo package: <i>{package}</i> ({group}).{started}<br><br>Tick to put it in the distribution. "
               "If Gentoo has it ready-made for your choices it installs in seconds; otherwise it is compiled on "
               "this PC. <b>Check</b> tells which, before you build.", label)


def explain(root) -> None:
    """Puts the explanations into the widgets under `root` (a window or dialog), by the words they show."""
    from PySide6.QtWidgets import QAbstractButton, QFormLayout, QGroupBox, QLabel, QRadioButton

    for form in root.findChildren(QFormLayout):
        for row in range(form.rowCount()):
            label_item = form.itemAt(row, QFormLayout.LabelRole)
            field_item = form.itemAt(row, QFormLayout.FieldRole)
            label = label_item.widget() if label_item else None
            if not isinstance(label, QLabel):
                continue
            words = label.text()
            if words not in FIELDS:
                continue
            text = tip(FIELDS[words], words.replace("\n", " "))
            label.setText(words.replace("\n", "<br>") + f" <span style='color:#1d6fb8;'>{MARK}</span>")
            label.setToolTip(text)
            field = field_item.widget() if field_item else None
            if field is not None:
                field.setToolTip(text)
                for child in field.findChildren(QAbstractButton):
                    if not child.toolTip():
                        child.setToolTip(text)
    for box in root.findChildren(QGroupBox):
        title = box.title()
        if title in BOXES:
            box.setTitle(f"{title}  {MARK}")
            box.setToolTip(tip(BOXES[title], title))
    for button in root.findChildren(QAbstractButton):
        words = button.text()
        if isinstance(button, QRadioButton) and words.split("  (")[0] in DESKTOP_CHOICES:
            button.setToolTip(tip(DESKTOP_CHOICES[words.split("  (")[0]], words))
        elif words in OPTIONS:
            button.setToolTip(tip(OPTIONS[words], words))
            if words in ("Use Gentoo's binary packages where they match (much faster)", "Repair failures with Claude",
                         "Installed systems take updates from a package server",
                         "Administrators (the \"wheel\" group) may use sudo"):
                button.setText(f"{words}  {MARK}")
