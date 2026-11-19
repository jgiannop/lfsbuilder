"""lfsbuilder's tests: the checks that keep recipes, fixes and logs safe.

    python3 -m unittest discover -s tests

No Docker, no network: only what can be checked from the code itself.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from lfsbuilder import branding, calamares, customize, logo, check, estimate, fixes, image, overlay, recipe, repair, runner, steps

ROOT = Path(__file__).resolve().parent.parent
RECIPES = ROOT / "recipes"
# The example recipes, the ones in git: the owner's own (out of git, .gitignore) may be half-made.
EXAMPLES = [RECIPES / f"{n}.toml" for n in ("example", "gnome", "openrc", "plasma", "xfce")]
HASH = "$6$woOKfvYyg2qIitmP$uwQTom.5BvfCc80CZEQjS6ju6fOVms4TIY7fDqwNuLmUaCL81QIy8pimI6SZ10XYd6kghXygCXG4yeCkIWLPz."


def write(directory: Path, text: str, name: str = "r.toml") -> Path:
    path = directory / name
    path.write_text(text)
    return path


def example_text(**changes: str) -> str:
    """The example recipe with some lines replaced: {old line start: new line}."""
    lines = (RECIPES / "example.toml").read_text().splitlines()
    for start, new in changes.items():
        lines = [new if l.startswith(start) else l for l in lines]
    return "\n".join(lines) + "\n"


class Recipes(unittest.TestCase):
    def test_the_recipes_load(self):
        for path in EXAMPLES:
            if not path.name.endswith(".fixes.toml"):
                with self.subTest(path.name):
                    recipe.load(path)

    def test_dump_then_load_gives_the_same_recipe(self):
        for name in ("example", "gnome", "openrc"):
            with self.subTest(name), tempfile.TemporaryDirectory() as d:
                shutil.copytree(RECIPES / "keys", Path(d) / "keys")  # [updates] key is a file beside the recipe
                original = recipe.load(RECIPES / f"{name}.toml")
                again = recipe.load(write(Path(d), recipe.dump(original)))
                self.assertEqual(dataclasses.replace(original, path=again.path), again)

    def refused(self, text: str, words: str) -> None:
        with tempfile.TemporaryDirectory() as d, self.assertRaises(recipe.RecipeError) as caught:
            shutil.copytree(RECIPES / "keys", Path(d) / "keys")
            write(Path(d), "not a key\n", "keys/bad.asc")
            recipe.load(write(Path(d), text))
        self.assertIn(words, str(caught.exception))

    def test_a_password_is_refused_only_a_hash_is_taken(self):
        self.refused(example_text(**{"password =": 'password = "demo"'}), "must be a password hash")
        self.refused(example_text(**{"root_password =": 'root_password = "secret"'}), "must be a password hash")

    def test_what_reaches_a_script_is_checked(self):
        self.refused(example_text(**{"hostname =": 'hostname = "a;rm -rf /"'}), "isn't a host name")
        self.refused(example_text(**{"cflags =": 'cflags = "-O2 \\"$(id)\\""'}), "compiler options only")
        self.refused(example_text(**{"cflags =": 'cflags = "-march=native"'}), "-march comes from base.cpu")
        self.refused(example_text(**{"install =": 'install = ["nano; reboot"]'}), "isn't a package")
        self.refused(example_text(**{"stage3 =": 'stage3 = "latest"'}), "base.stage3")
        self.refused(example_text(**{"repository =": 'repository = "master"'}), "full commit id")

    def test_the_name_and_version_are_checked(self):
        self.refused(example_text(**{"name =": 'name = "Argo\\" ; reboot"'}), "distribution.name")
        self.refused(example_text(**{"version =": 'version = "0.1 beta"'}), "distribution.version")

    def test_the_package_server_is_checked(self):
        self.refused(example_text(**{"server =": 'server = "http://x/$(id)/"'}), "must be an http(s) URL")
        self.refused(example_text(**{"server =": 'server = "http://10.0.2.2:8080"'}), "must be an http(s) URL")
        self.refused(example_text(**{"key =": 'key = "../../.ssh/id.asc"'}), "path beside the recipe")
        self.refused(example_text(**{"key =": 'key = "keys/bad.asc"'}), "not an armored OpenPGP public key")
        self.refused(example_text(**{"key =": 'key = ""'}), "server and key go together")


class Fixes(unittest.TestCase):
    def fix(self, **changes) -> fixes.Fix:
        base = dict(step="app-misc_abduco", kind="keywords", package="app-misc/abduco", values=["~amd64"], why="w", by="claude")
        return fixes.Fix(**{**base, **changes})

    def test_a_good_fix_has_no_problems(self):
        self.assertEqual(self.fix().problems(), [])
        self.assertEqual(self.fix(kind="use", values=["gnutls", "-doc"]).problems(), [])
        self.assertEqual(self.fix(kind="env", values=["MAKEOPTS=-j1"]).problems(), [])

    def test_bad_fixes_are_refused(self):
        bad = [
            dict(kind="keywords", values=["**"]),  # every keyword, even broken ones
            dict(kind="env", values=["CFLAGS=$(curl evil)"]),
            dict(kind="env", values=["X='a'"]),
            dict(kind="use", values=["a b"]),
            dict(kind="patch", values=["../../etc/passwd.patch"], patch="--- a\n+++ b\n"),
            dict(kind="patch", values=["x.patch"], patch="rm -rf /"),
            dict(kind="patch", values=["x.patch"], patch="--- a\nLFSBUILDER_PATCH_EOF\nrm -rf /\n"),
            dict(kind="shell", values=["anything"]),
            dict(package="abduco; reboot"),
            dict(step="x; reboot"),
        ]
        for changes in bad:
            with self.subTest(changes):
                self.assertTrue(self.fix(**changes).problems())

    def test_save_then_load_keeps_fixes_and_patches(self):
        with tempfile.TemporaryDirectory() as d:
            path = write(Path(d), "")
            saved = [self.fix(), self.fix(kind="patch", values=["x.patch"], patch="--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n")]
            fixes.save(path, saved)
            loaded = fixes.load(path)
            self.assertEqual([(f.kind, f.values, f.patch.strip()) for f in loaded],
                             [(f.kind, f.values, f.patch.strip()) for f in saved])

    def test_a_fix_goes_at_the_start_of_its_step_only(self):
        r = recipe.load(RECIPES / "example.toml")
        before = steps.all_steps(r)
        r.fixes = [self.fix(step="app-misc_tmux", package="app-misc/tmux")]
        after = steps.all_steps(r)
        changed = [b.name for b, a in zip(before, after) if b.script != a.script]
        self.assertEqual(changed, ["app-misc_tmux"])
        self.assertIn("package.accept_keywords", next(s for s in after if s.name == "app-misc_tmux").script)

    def test_a_fix_for_a_step_the_recipe_lacks_is_an_error(self):
        r = recipe.load(RECIPES / "example.toml")
        r.fixes = [self.fix(step="no-such-step")]
        with self.assertRaises(fixes.FixError):
            steps.all_steps(r)


class Steps(unittest.TestCase):
    def test_password_hashes_are_set_with_tracing_off(self):
        script = steps.users(recipe.load(RECIPES / "example.toml")).script
        off, on = script.index("set +x"), script.index("set -x")
        self.assertTrue(off < script.index(HASH) < on)

    def test_the_order_use_changes_are_written_in_doesnt_matter(self):
        r = recipe.load(RECIPES / "gnome.toml")
        reversed_ = dataclasses.replace(r, package_use=dict(reversed(list(r.package_use.items()))))
        self.assertEqual(steps.portage(r).script, steps.portage(reversed_).script)

    def test_the_installer_is_the_last_step_but_updates(self):
        for name in ("example", "gnome", "openrc"):
            with self.subTest(name):
                last = [s for s in steps.all_steps(recipe.load(RECIPES / f"{name}.toml")) if s.name != "updates"][-1]
                self.assertEqual(last.name, "installer")
                self.assertIn("/usr/sbin/lfsbuilder-install", last.script)
                self.assertEqual("systemctl enable lfsbuilder-autoinstall" in last.script, name != "openrc")

    def test_the_installer_script_reaches_the_image_whole(self):
        script = steps.installer(recipe.load(RECIPES / "example.toml")).script
        self.assertIn(steps.INSTALLER.read_text().rstrip(), script)
        self.assertNotIn("LFSBUILDER_EOF", steps.INSTALLER.read_text())

    def test_updates_come_last_and_change_no_other_step(self):
        r = recipe.load(RECIPES / "example.toml")
        with_updates = runner.plan(steps.all_steps(r), "seed")
        without = runner.plan(steps.all_steps(dataclasses.replace(r, binhost="", binhost_key="")), "seed")
        self.assertEqual(with_updates[-1].step.name, "updates")
        self.assertEqual([p.fingerprint for p in with_updates[:-1]], [p.fingerprint for p in without])
        script = with_updates[-1].step.script
        self.assertIn(f"sync-uri = {r.binhost}{r.binary_set}/", script)
        self.assertIn("verify-signature = true", script)
        self.assertIn(steps.TRUST.read_text().rstrip(), script)
        self.assertIn(r.binhost_key_text().rstrip(), script)
        self.assertNotIn("LFSBUILDER_EOF", steps.TRUST.read_text())
        # The repository: the recipe's branch from the same server, its head signed by Gentoo.
        self.assertIn(f"sync-uri = {r.binhost}gentoo.git", script)
        self.assertIn("--branch example --single-branch", script)
        self.assertIn("sync-git-verify-commit-signature = yes", script)

    def test_fingerprints(self):
        r = recipe.load(RECIPES / "example.toml")
        a = runner.plan(steps.all_steps(r), "seed")
        self.assertEqual([p.fingerprint for p in a], [p.fingerprint for p in runner.plan(steps.all_steps(dataclasses.replace(r, jobs=3)), "seed")])
        other = runner.plan(steps.all_steps(dataclasses.replace(r, packages=["app-editors/nano", "app-misc/htop"])), "seed")
        same = sum(x.fingerprint == y.fingerprint for x, y in zip(a, other))
        self.assertEqual(same, 5)  # portage, world, kernel, system, nano: only what follows the change differs
        self.assertNotEqual(a[0].fingerprint, runner.plan(steps.all_steps(r), "another seed")[0].fingerprint)


class Estimates(unittest.TestCase):
    def test_measured_guessed_and_unknown(self):
        r = recipe.load(RECIPES / "example.toml")
        p = runner.plan(steps.all_steps(r), "seed")
        with tempfile.TemporaryDirectory() as d:
            times = Path(d) / "step-times.json"
            self.assertEqual(estimate.load(times), {"fingerprints": {}, "names": {}})  # nothing recorded yet
            estimate.record(times, p[1].fingerprint, p[1].step.name, 120.4)         # this very step
            estimate.record(times, "another recipe's", p[2].step.name, 60)          # the same name elsewhere:
            estimate.record(times, "and a third", p[2].step.name, 300)              # the longest is the guess
            data = estimate.load(times)
        e = estimate.estimate(p, data, built={p[0].image})
        how = [h for _, _, h in e.steps]
        self.assertEqual(how[:3], ["done", "measured", "guess"])
        self.assertEqual(e.seconds, 120 + 300)
        self.assertEqual(e.unknown, [s.step.name for s in p[3:]])
        self.assertIn("partly guessed", e.text())
        self.assertIn(f"{len(p) - 3} with no time recorded", e.text())
        self.assertEqual(estimate.estimate(p, data, built={s.image for s in p}).text(), "Everything is built.")

    def test_names_keep_the_last_few(self):
        with tempfile.TemporaryDirectory() as d:
            times = Path(d) / "t.json"
            for n in range(estimate.KEEP + 3):
                estimate.record(times, f"f{n}", "world", n)
            self.assertEqual(estimate.load(times)["names"]["world"], list(range(3, estimate.KEEP + 3)))

    def test_durations(self):
        self.assertEqual(estimate.duration(20), "under a minute")
        self.assertEqual(estimate.duration(38 * 60), "38 min")
        self.assertEqual(estimate.duration(65 * 60 + 10), "1 h 05 min")


class MachineId(unittest.TestCase):
    def test_first_boot_mark(self):
        # An installed system starts with its root read-only: without the file it has no machine id at all.
        r = recipe.load(RECIPES / "gnome.toml")
        self.assertEqual(image.system_files(r)["etc/machine-id"], ("file", "uninitialized\n"))
        self.assertEqual(image.system_files(r, live=True)["etc/machine-id"], ("file", "uninitialized\n"))
        self.assertNotIn("etc/machine-id", image.system_files(recipe.load(RECIPES / "openrc.toml")))
        installer = (ROOT / "lfsbuilder" / "installer.sh").read_text()
        self.assertNotIn('rm -f "$target/etc/machine-id"', installer)
        self.assertIn("printf 'uninitialized\\n' > \"$target/etc/machine-id\"", installer)


class Screens(unittest.TestCase):
    def test_text_console_or_login_screen(self):
        width, height = 64, 48
        def png(rgb):
            return image.ppm_to_png(b"P6\n%d %d\n255\n" % (width, height) + bytes(rgb) * (width * height))
        self.assertGreater(image.black_share(png((0, 0, 0))), image.TEXT_CONSOLE)    # a console's black
        self.assertEqual(image.black_share(png((34, 34, 38))), 0.0)                 # GDM's grey


class GraphicalInstaller(unittest.TestCase):
    def test_desktops_have_it_and_only_their_installer_step_changes(self):
        for name in ("gnome", "plasma", "xfce"):
            r = recipe.load(RECIPES / f"{name}.toml")
            self.assertTrue(r.graphical_installer, name)
            with_it = runner.plan(steps.all_steps(r), "seed")
            without = runner.plan(steps.all_steps(dataclasses.replace(r, graphical_installer=False)), "seed")
            changed = [a.step.name for a, b in zip(with_it, without) if a.fingerprint != b.fingerprint]
            self.assertEqual(changed, ["installer", "updates"], name)  # (updates comes after it)
            script = next(s for s in steps.all_steps(r) if s.name == "installer").script
            self.assertLess(script.index("sys-boot/grub mount"), script.index("emerge --noreplace --verbose sys-boot/grub"))
            self.assertIn("emerge --noreplace --oneshot --verbose app-admin/calamares", script)
            self.assertIn(f"- {calamares.display_manager(r)}\n", calamares.modules(r)["displaymanager.conf"])
        self.assertFalse(recipe.load(RECIPES / "example.toml").graphical_installer)

    def test_the_root_directory_is_roots(self):
        # sqfstar gives the root directory (no tar entry) its user's uid and 0777: the live system's /
        # was demo's, and every copy's after it (systemd-tmpfiles then refused all, and GNOME failed).
        import inspect
        self.assertIn("-root-uid 0 -root-gid 0 -root-mode 0755", inspect.getsource(image.iso))
        installer = (Path(steps.__file__).parent / "installer.sh").read_text()
        self.assertIn('chown 0:0 "$target"; chmod 0755 "$target"', installer)
        live = calamares.helper(recipe.load(RECIPES / "gnome.toml")).split("live)", 1)[1].split(";;", 1)[0]
        self.assertIn("chown 0:0 /", live)
        self.assertIn("chmod 0755 /", live)

    def test_the_login_test(self):
        self.assertEqual(image.keys_for("aB3 -!"), ["a", "shift-b", "3", "spc", "minus", "shift-1"])
        with self.assertRaises(ValueError):
            image.keys_for("κωδικός")
        # loginctl's answer on the serial console, colours and terminal marks included (as GNOME's gave it).
        answer = ("\x1b]3008;start=x;type=command\x1b\\Name=demo\x1b[m\r\nType=wayland\x1b[m\r\nClass=user\x1b[m\r\n"
                  "State=active\x1b[m\r\n\x1b[K\r\nName=demo\r\nType=unspecified\r\nClass=manager\r\nState=active\r\n\r\n"
                  "Name=demo\r\nType=tty\r\nClass=user\r\nState=active\r\n\r\nEND-42\r\n")
        self.assertEqual(image.graphical_sessions(answer, "demo"),
                         [{"Name": "demo", "Type": "wayland", "Class": "user", "State": "active"}])
        self.assertEqual(image.graphical_sessions(answer, "jason"), [])
        # Back at the greeter: only the serial login's own session.
        self.assertEqual(image.graphical_sessions("Name=demo\nType=tty\nClass=user\nState=active\n\n", "demo"), [])
        self.assertNotIn("END-42", image.SESSIONS_COMMAND)  # the echoed command must not look like the answer's end
        self.assertEqual(set(image.GREETER_KEYS), set(calamares.DISPLAY_MANAGERS))

    def test_the_root_directory_is_checked(self):
        self.assertIsNone(image.root_problem(0, 0, 0o755))
        self.assertIsNone(image.root_problem(0, 0, 0o555))  # Gentoo's own
        self.assertIn("owned by 1000:1000", image.root_problem(1000, 1000, 0o755))
        self.assertIn("others can write", image.root_problem(0, 0, 0o777))
        # The tools' answers, as unsquashfs 4.6 and debugfs 1.47 give them.
        bad = "drwxrwxrwx 1000/1000               3 2026-11-19 06:40 squashfs-root\n"
        self.assertEqual(image.parse_unsquashfs_root(bad), (1000, 1000, 0o777))
        good = "drwxr-xr-x 0/0                     270 2026-11-18 22:26 squashfs-root\n"
        self.assertEqual(image.parse_unsquashfs_root(good), (0, 0, 0o755))
        stat = ("Inode: 2   Type: directory    Mode:  0555   Flags: 0x80000\n"
                "Generation: 0    Version: 0x00000000:00000002\n"
                "User:     0   Group:     0   Project:     0   Size: 4096\n")
        self.assertEqual(image.parse_debugfs_root(stat), (0, 0, 0o555))
        with self.assertRaises(ValueError):
            image.parse_debugfs_root("debugfs: Bad magic number")
        # The root partition of a GPT like the installer's: BIOS boot, ESP, then root (8304) at sector 20480.
        types = [("21686148-6449-6e6f-744e-656564454649", 2048), ("c12a7328-f81f-11d2-ba4b-00a0c93ec93b", 4096),
                 ("4f68bce3-e8cd-4db1-96e7-fbcaf984b709", 20480)]
        disk = bytearray(34 * 512)
        disk[512:512 + 92] = b"EFI PART" + bytes(64) + struct.pack("<QII", 2, 128, 128)
        for i, (guid, start) in enumerate(types):
            a, b, c, d, e = guid.split("-")
            raw = struct.pack("<IHH", int(a, 16), int(b, 16), int(c, 16)) + bytes.fromhex(d + e)
            disk[1024 + i * 128:1024 + i * 128 + 40] = raw + bytes(16) + struct.pack("<QQ", start, start + 100)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "disk.img"
            path.write_bytes(disk)
            self.assertEqual(image.root_partition_offset(path), 20480 * 512)
            path.write_bytes(bytes(34 * 512))
            with self.assertRaises(ValueError):
                image.root_partition_offset(path)

    def test_only_what_was_tested_is_offered(self):
        r = recipe.load(RECIPES / "xfce.toml")
        partition = calamares.modules(r)["partition.conf"]
        self.assertIn("enableLuksAutomatedPartitioning: false", partition)
        self.assertIn('availableFileSystemTypes: [ "ext4" ]', partition)
        script = next(s for s in steps.all_steps(r) if s.name == "installer").script
        self.assertIn(f"rm -f {calamares.OWN_LAUNCHER}\n", script)  # one menu entry: ours

    def test_needs_a_desktop(self):
        with tempfile.TemporaryDirectory() as d:
            path = write(Path(d), example_text() + "\n[installer]\ngraphical = true\n")
            with self.assertRaises(recipe.RecipeError) as error:
                recipe.load(path)
            self.assertIn("installer.graphical needs", str(error.exception))

    def test_files(self):
        r = recipe.load(RECIPES / "gnome.toml")
        files = calamares.files(r)
        self.assertIn(f"region: \"{r.timezone.split('/')[0]}\"", files["/etc/calamares/modules/locale.conf"])
        self.assertIn(f'source: "{calamares.LIVE_ROOT}"\n      sourcefs: "file"', files["/etc/calamares/modules/unpackfs.conf"])
        # The live installer's rule must be read after Gentoo's `wheel` rule (the last match wins).
        self.assertGreater(Path(calamares.SUDO_RULE).name, "wheel")
        # The live accounts go before the new user is made; the boot menu's set-up before GRUB.
        exec_ = calamares.settings().split("- exec:")[1].split("- show:")[0].split()
        self.assertLess(exec_.index("shellprocess@live"), exec_.index("users"))
        self.assertLess(exec_.index("shellprocess@argo"), exec_.index("bootloader"))
        self.assertIn("argo-calamares live demo", files["/etc/calamares/modules/shellprocess-live.conf"])
        self.assertIn(f'productName: "{r.name}"', files["/etc/calamares/branding/argo/branding.desc"])
        helper = files["/usr/libexec/argo-calamares"]
        for live_only in calamares.LIVE_ONLY:
            self.assertIn(live_only, helper)
            if "autoinstall" not in live_only:  # the text installer drops them too
                self.assertIn(live_only.lstrip("/"), (ROOT / "lfsbuilder" / "installer.sh").read_text())


class Logs(unittest.TestCase):
    def test_portage_asking_for_a_use_change_is_read(self):
        m = check._CHANGE.match("- net-libs/ngtcp2-1.25.0::gentoo (Change USE: +gnutls -doc)")
        self.assertEqual((m["package"], m["flags"]), ("net-libs/ngtcp2", "+gnutls -doc"))
        m = check._CHANGE.match("- dev-libs/libsigc++-2.12.1-r1::gentoo (Change USE: +x)")
        self.assertEqual(m["package"], "dev-libs/libsigc++")

    def test_portage_listing_packages_is_read(self):
        m = check._LISTED.match("[binary  N g   ] net-libs/nghttp3-1.18.0-1::gentoo  USE=\"-verify-sig\"")
        self.assertEqual((m["kind"], m["package"]), ("binary", "net-libs/nghttp3-1.18.0-1::gentoo"))

    def test_claude_never_sees_password_hashes(self):
        text = repair._redacted(recipe.load(RECIPES / "example.toml"))
        self.assertNotIn(HASH, text)
        self.assertIn("(hidden)", text)


class Overlay(unittest.TestCase):
    """The argo repository (overlay.py): what carries the recipe to installed systems."""

    def release(self, name: str) -> str:
        return overlay.files(recipe.load(RECIPES / f"{name}.toml"))[
            f"argo-base/argo-{name}-release/argo-{name}-release-0.1.ebuild"].decode()

    def test_the_system_says_its_own_name(self):
        r = recipe.load(RECIPES / "example.toml")
        files = overlay.files(r)
        release = dict(line.split("=", 1) for line in files["argo-base/argo-example-release/files/os-release"].decode().splitlines())
        self.assertEqual((release["NAME"], release["ID"], release["ID_LIKE"], release["PRETTY_NAME"], release["LOGO"]),
                         ('"Argo Linux"', '"argo-linux"', '"gentoo"', '"Argo Linux 0.1"', '"argo-linux"'))
        self.assertIn(b"Argo Linux 0.1", files["argo-base/argo-example-release/files/issue"])
        self.assertNotIn("etc/os-release", image.system_files(r))  # the package's, not the export's
        self.assertEqual(r.short_name, "Argo")

    def test_the_profile_carries_the_use_flags_and_the_meta_package_the_packages(self):
        r = recipe.load(RECIPES / "gnome.toml")
        files = overlay.files(r)
        self.assertEqual(files["profiles/argo/gnome/parent"], f"gentoo:{r.profile}\n".encode())
        self.assertIn(b"net-libs/ngtcp2 gnutls", files["profiles/argo/gnome/package.use"])
        self.assertIn(b"sys-kernel/installkernel dracut", files["profiles/argo/gnome/package.use"])
        meta = files["argo-base/argo-gnome/argo-gnome-0.1.ebuild"].decode()
        for atom in ["argo-base/argo-gnome-release", *r.packages, "net-misc/networkmanager", "app-admin/sudo"]:
            self.assertIn(f"\t{atom}\n", meta)
        self.assertNotIn("USE=", steps.make_conf(r))  # the profile's now
        portage = steps.portage(r).script
        self.assertIn(overlay.profile_digest(r), portage)
        self.assertIn("eselect profile set argo:argo/gnome", portage)

    def test_a_package_added_changes_the_argo_step_not_the_profile(self):
        r = recipe.load(RECIPES / "example.toml")
        more = dataclasses.replace(r, packages=[*r.packages, "app-misc/htop"])
        self.assertEqual(overlay.profile_digest(r), overlay.profile_digest(more))
        self.assertNotEqual(overlay.packages_digest(r), overlay.packages_digest(more))
        self.assertIn(overlay.packages_digest(more), steps.argo(more).script)
        self.assertIn("--oneshot", steps.package("app-misc/htop").script)  # argo-meta holds them in the world file

    def test_the_version_must_be_a_package_version(self):
        r = recipe.load(RECIPES / "example.toml")
        with self.assertRaises(overlay.OverlayError):
            overlay.files(dataclasses.replace(r, version="0.1-beta"))

    def test_each_recipe_has_its_own_release_package_and_desktops_are_its_use_flags(self):
        # Recipes share one set of binary packages: one release package for all would mix up their names
        # and looks (the same name and version, other pictures); each desktop's part is a USE flag.
        expected = {"example": [], "gnome": ["gdm"], "plasma": ["sddm", "plasma"], "xfce": ["lightdm", "xfce"], "openrc": []}
        for name, flags in expected.items():
            with self.subTest(name):
                r = recipe.load(RECIPES / f"{name}.toml")
                self.assertEqual(overlay.release_name(r), f"argo-base/argo-{name}-release")
                self.assertEqual(overlay.release_flags(r), flags)
                use = overlay.files(r)[f"profiles/argo/{name}/package.use"].decode()
                self.assertEqual(f"argo-base/argo-{name}-release {' '.join(flags)}" in use, bool(flags))
                self.assertIn(overlay.release_name(r), steps.argo(r).script)
                ebuild = self.release(name)
                for flag in overlay.DESKTOP_FLAGS:
                    self.assertIn(f"use {flag}", ebuild)
                self.assertIn("LookAndFeelPackage=org.argolinux.desktop", ebuild)
                self.assertIn("/etc/portage/env/xfce-base", ebuild)

    def test_lightdm_starts_xfce(self):
        # Without it LightDM starts xinit's Xsession (first by name) and the login falls back to the greeter.
        files = overlay.files(recipe.load(RECIPES / "xfce.toml"))
        self.assertEqual(files["argo-base/argo-xfce-release/files/lightdm-50-argo.conf"], b"[Seat:*]\nuser-session=xfce\n")
        self.assertIn("/etc/lightdm/lightdm.conf.d", self.release("xfce"))

    def test_the_xfce_wallpaper_hook_is_given_its_root(self):
        # In pkg_postinst ED is read-only (the package's image): setting it did nothing, and the
        # wallpaper went into the image, not the system.
        ebuild = self.release("xfce")
        self.assertNotIn("ED=", ebuild)
        self.assertIn('argo_wallpaper "${EROOT}/"', ebuild)
        self.assertIn('post_src_install() { argo_wallpaper "${ED}"; }', overlay.XFDESKTOP_HOOK)

    def test_the_meta_package_is_named_after_the_recipe(self):
        r = recipe.load(RECIPES / "plasma.toml")
        self.assertEqual(overlay.meta_name(r), "argo-base/argo-plasma")
        self.assertIn("argo-base/argo-plasma", steps.argo(r).script)
        self.assertIn("--usepkg=n", steps.argo(r).script)  # never an older build of the same version
        with self.assertRaises(overlay.OverlayError):
            overlay.files(dataclasses.replace(r, path=r.path.with_name("my-desktop.toml")))

    def test_systems_sync_it_from_the_server_signed(self):
        r = recipe.load(RECIPES / "example.toml")
        script = steps.updates(r).script
        self.assertIn(f"sync-uri = {r.binhost}argo.git", script)
        self.assertIn(f"sync-openpgp-key-path = {steps.BINHOST_KEY}", script)
        self.assertIn("sync-openpgp-key-refresh = no", script)  # ours is on no keyserver

    def test_an_old_release_is_refused(self):
        from lfsbuilder import binhost
        r = recipe.load(RECIPES / "example.toml")
        script = steps.updates(r).script
        self.assertIn(f"sync-git-verify-max-age-days = {binhost.MAX_AGE_DAYS}", script)
        self.assertIn("/etc/portage/postsync.d/argo-gentoo-pin", script)
        self.assertIn("*/*", steps.GENTOO_PIN_HOOK)  # the wrong Gentoo tree masks everything
        self.assertEqual(overlay.files(r)["metadata/gentoo-commit"], f"{r.repository}\n".encode())
        self.assertNotIn("metadata/timestamp.chk", overlay.files(r))  # publish's, so not in the fingerprints

    def test_the_installer_puts_our_picture_behind_grub(self):
        text = steps.INSTALLER.read_text()
        self.assertIn('GRUB_BACKGROUND="/boot/grub/argo.png"', text)
        self.assertIn("s|^GRUB_THEME=|#GRUB_THEME=|", text)  # Gentoo's theme (its artwork) would win
        self.assertIn("background_image /boot/grub/argo.png", image.GRUB_CFG)


class Publishing(unittest.TestCase):
    def test_gentoos_binaries_go_out_with_gentoos_signatures(self):
        from lfsbuilder import binhost
        script = binhost._publish_script("F" * 40, 1000, 1000)
        self.assertIn("/cache/gentoobinhost", script)
        self.assertIn('grep -qxF "CPV: $cpv" /src/Packages && continue', script)  # what we built wins
        self.assertIn("in *image.tar.*.sig*) continue", script)  # Gentoo's aren't signed again
        self.assertNotIn("| grep -q", script)  # pipefail would fail on SIGPIPE
        shared = runner.Shared(Path("/r"), "d", "b", 1, "lfsbuilder-binhost-x").args()
        self.assertIn("lfsbuilder-binhost-x:/var/cache/binhost", shared)


class Branding(unittest.TestCase):
    def test_every_recipe_has_its_pictures_drawn(self):
        # Drawn by `python3 -m lfsbuilder branding RECIPE` (and before any command that plans), kept in git.
        import xml.dom.minidom
        for folder in sorted(p for p in (ROOT / "branding").iterdir() if p.is_dir()):
            for name in recipe.BRANDING_FILES:
                with self.subTest(f"{folder.name}/{name}"):
                    xml.dom.minidom.parse(str(folder / name))
        for path in EXAMPLES:
            with self.subTest(path.name):
                r = recipe.load(path)
                self.assertTrue(branding.current(r), f"python3 -m lfsbuilder branding {path}")
                for name, png in branding.pictures(r).items():
                    self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"), name)

    def test_the_name_logo_and_label_are_the_recipes(self):
        # (Only Argo's logo is in git: a user's logos and recipes stay on their PC, .gitignore.)
        argo = recipe.load(RECIPES / "xfce.toml")
        self.assertEqual((argo.branding, argo.label_text), ("argo", "Argo Linux"))
        self.assertEqual(branding.pictures_dir(argo), ROOT / "branding" / "argo" / "pictures" / "argo-linux")
        # Another name with Argo's look: the texts follow the name, the pictures the label.
        named = dataclasses.replace(argo, name="Odyssey Linux", label="Argo Linux")
        files = overlay.files(named)
        self.assertEqual(files["argo-base/argo-xfce-release/files/argo-256.png"], branding.pictures(argo)["argo-256.png"])
        self.assertEqual(json.loads(files["argo-base/argo-xfce-release/files/wallpaper-metadata.json"])["KPlugin"]["Name"],
                         "Odyssey Linux")
        self.assertIn(b'NAME="Odyssey Linux"', files["argo-base/argo-xfce-release/files/os-release"])
        # A new label or logo: drawn again before anything is planned; until then nothing plans.
        labelled = dataclasses.replace(argo, branding="odyssey", label="Odyssey")
        self.assertEqual(labelled.label_text, "Odyssey")
        self.assertNotEqual(branding.stamp(argo), branding.stamp(dataclasses.replace(argo, label="Argo")))
        dumped = recipe.dump(labelled)  # what the form saves
        self.assertIn('branding = "odyssey"\n', dumped)
        self.assertIn('label = "Odyssey"\n', dumped)
        self.assertNotIn("branding =", recipe.dump(argo))  # Argo's is the default
        with self.assertRaises(branding.BrandingError):
            branding.pictures(dataclasses.replace(argo, label="Not Drawn Yet"))

    def test_the_wordmark_fits_its_label(self):
        logo = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256"/>'
        _, argo = branding.wordmark(logo, "Argo Linux", (2.3, 2.4), "#fff")
        _, odyssey = branding.wordmark(logo, "Odyssey Linux", (3.9, 2.4), "#fff")
        self.assertLess(argo, odyssey)
        one, _ = branding.wordmark(logo, "Odyssey", (3.9, 0), "#fff")
        self.assertNotIn('font-weight="400"', one)  # one word: all bold
        svg, width = branding.wordmark(logo, "Averyveryverylongname Linux Distribution", (14.0, 9.0), "#fff")
        self.assertLessEqual(width, branding.MAX_WIDTH + 2)  # shrunk to fit
        self.assertIn("Averyveryverylongname", svg)
        self.assertEqual(branding.label_parts("Odyssey Linux"), ("Odyssey", "Linux"))
        self.assertEqual(branding.slug("Odyssey Linux"), "odyssey-linux")

    def test_claudes_drawings_are_checked(self):
        # A drawing from Claude is drawn by rsvg here and goes into every system: only shapes, in the file.
        good = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="256" height="256">'
                '<defs><linearGradient id="g"><stop offset="0" stop-color="#fff"/></linearGradient></defs>'
                '<title>Owl</title><circle cx="128" cy="128" r="80" fill="url(#g)"/></svg>')
        self.assertEqual(logo.problems(good), [])
        for bad, why in (
            (good.replace("<title>Owl</title>", "<script>alert(1)</script>"), "<script> isn't allowed"),
            (good.replace("<title>Owl</title>", "<text>Owl</text>"), "<text> isn't allowed"),
            (good.replace("<title>Owl</title>", '<image href="http://x/y.png"/>'), "<image> isn't allowed"),
            (good.replace('fill="url(#g)"', 'fill="url(http://x/y)"'), "points outside the file"),
            (good.replace('r="80"', 'r="80" onclick="x()"'), "'onclick'"),
            (good.replace('viewBox="0 0 256 256"', 'viewBox="0 0 256 128"'), "isn't a square"),
            ('<!DOCTYPE svg [<!ENTITY a "b">]>' + good, "DOCTYPE"),
            (good.replace("</svg>", ""), "not well-formed"),
            (good.replace("<title>Owl</title>", "<g>" + "<path d='M0 0'/>" * 4000 + "</g>"), "bytes"),
        ):
            with self.subTest(why):
                self.assertTrue(any(why in p for p in logo.problems(bad)), logo.problems(bad))
        with self.assertRaises(logo.LogoError):
            logo.write("Bad Name", good, good)

    def test_a_branding_needs_its_drawings(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "r.toml"
            text = (RECIPES / "example.toml").read_text().replace('version = "0.1"', 'version = "0.1"\nbranding = "nowhere"', 1)
            path.write_text(text)
            with self.assertRaises(recipe.RecipeError) as raised:
                recipe.load(path)
            self.assertIn("branding/nowhere/ has no logo.svg", str(raised.exception))


NANO_PATCH = """diff -ruN --no-dereference a/src/nano.c b/src/nano.c
--- a/src/nano.c\t2026-07-31 09:07:59.000000000 +0300
+++ b/src/nano.c\t2026-11-19 12:38:40.101695517 +0200
@@ -660,6 +660,7 @@
 \tprintf(_(" (C) %s the Free Software Foundation and various contributors\\n"), "2026");
 #endif
+\tprintf(" Hello world from Odyssey\\n");
 \tprintf(_(" Compiled options:"));
 
 #ifdef NANO_TINY
"""


class Customizations(unittest.TestCase):
    """A package's source changed in words (customize.py): the patch kept beside the recipe."""

    def recipe(self, d: Path, entry: str, patch: str | None = NANO_PATCH) -> Path:
        shutil.copytree(RECIPES / "keys", Path(d) / "keys")
        path = Path(d) / "mine.toml"
        path.write_text((RECIPES / "example.toml").read_text() + "\n" + entry)
        if patch is not None:
            target = Path(d) / "mine.patches" / "app-editors" / "nano" / "customization.patch"
            target.parent.mkdir(parents=True)
            target.write_text(patch)
        return path

    NANO = ('[[customizations]]\npackage = "app-editors/nano"\n'
            'instruction = "Add the line \'Hello world from Odyssey\' to nano --version"\n'
            'check = "nano --version | grep -q \'Hello world from Odyssey\'"\n')

    def test_the_package_is_compiled_with_the_patch_and_checked(self):
        with tempfile.TemporaryDirectory() as d:
            r = recipe.load(self.recipe(d, self.NANO))
            self.assertEqual(r.customizations[0].patch, NANO_PATCH)
            self.assertIn("app-editors/nano", r.packages)
            step = next(s for s in steps.all_steps(r) if s.name == "app-editors_nano")
            self.assertIn("cat > /etc/portage/patches/app-editors/nano/customization.patch", step.script)
            self.assertIn('printf(" Hello world from Odyssey', step.script)
            self.assertIn("--usepkg-exclude app-editors/nano", step.script)  # no binary in its place
            self.assertNotIn("--noreplace", step.script)  # nano comes with the stage3: install it again
            self.assertTrue(step.script.rstrip().endswith("nano --version | grep -q 'Hello world from Odyssey'"))
            plain = next(s for s in steps.all_steps(recipe.load(RECIPES / "example.toml")) if s.name == "app-editors_nano")
            self.assertNotEqual(step.script, plain.script)  # a fingerprint of its own
            (Path(d) / "mine.toml").write_text(recipe.dump(r))  # what the form saves
            again = recipe.load(Path(d) / "mine.toml")
            self.assertEqual([(c.package, c.instruction, c.check) for c in again.customizations],
                             [(c.package, c.instruction, c.check) for c in r.customizations])

    def test_bad_customizations_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(recipe.RecipeError) as raised:
                recipe.load(self.recipe(d, self.NANO, patch=None))
            self.assertIn("no patch yet", str(raised.exception))
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(recipe.RecipeError) as raised:
                recipe.load(self.recipe(d, self.NANO.replace("check = \"", "check = \"true\\nrm -rf / #")))
            self.assertIn("one line of shell", str(raised.exception))
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(recipe.RecipeError) as raised:
                recipe.load(self.recipe(d, self.NANO, patch="not a diff at all\n"))
            self.assertIn("isn't a unified diff", str(raised.exception))

    def test_a_program_inside_a_bundle_is_compiled_before_it(self):
        # GNOME's top bar is gnome-shell, inside gnome-light: compiled (with its patch, never a binary)
        # in a step of its own before the recipe's packages, which then find it installed.
        r = recipe.load(RECIPES / "gnome.toml")
        shell = recipe.Customization("gnome-base/gnome-shell", "Activities should say Odyssey",
                                     "grep -rq Odyssey /usr/share/gnome-shell", NANO_PATCH)
        planned = steps.all_steps(dataclasses.replace(r, customizations=[shell]))
        names = [s.name for s in planned]
        self.assertEqual(names.index("customizations"), names.index("system") + 1)
        self.assertLess(names.index("customizations"), names.index("gnome-base_gnome-light"))
        step = planned[names.index("customizations")]
        self.assertIn("/etc/portage/patches/gnome-base/gnome-shell/customization.patch", step.script)
        self.assertIn("--usepkg-exclude gnome-base/gnome-shell", step.script)
        self.assertIn("emerge --oneshot --verbose gnome-base/gnome-shell\n", step.script)
        self.assertTrue(step.script.rstrip().endswith("grep -rq Odyssey /usr/share/gnome-shell"))
        self.assertNotIn("customizations", [s.name for s in steps.all_steps(r)])  # none: no step, nothing changes

    def test_package_names_lose_their_versions(self):
        for cpv, name in (("gnome-base/gnome-shell-49.9", "gnome-base/gnome-shell"), ("acct-group/gdm-0-r2", "acct-group/gdm"),
                          ("gnome-base/gdm-49.4-5", "gnome-base/gdm"),  # a binary's build number (binpkg-multi-instance)
                          ("x11-themes/sound-theme-freedesktop-0.8-r1-3", "x11-themes/sound-theme-freedesktop"),
                          ("media-libs/libvpx-1.15.2", "media-libs/libvpx"), ("dev-lang/python-3.13.11_p1", "dev-lang/python")):
            self.assertEqual(customize.unversioned(cpv), name)

    def test_patches_are_made_here_and_checked(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d) / "a", Path(d) / "b"
            for root in (a, b):
                (root / "src").mkdir(parents=True)
                (root / "src" / "nano.c").write_text("int main(void) { return 0; }\n")
            with self.assertRaises(customize.CustomizeError):
                customize.make_patch(a, b)  # nothing changed
            (b / "src" / "nano.c").write_text('int main(void) { puts("Hello world"); return 0; }\n')
            patch = customize.make_patch(a, b)
            self.assertIn("--- a/src/nano.c", patch)
            self.assertIn('+int main(void) { puts("Hello world"); return 0; }', patch)
        for bad in ("Binary files a/x.png and b/x.png differ\n--- a/x\n+++ b/x\n",
                    "--- a/../../etc/passwd\n+++ b/../../etc/passwd\n@@ -1 +1 @@\n-x\n+y\n",
                    NANO_PATCH + "LFSBUILDER_PATCH_EOF\n"):
            with self.subTest(bad[:30]):
                with self.assertRaises(customize.CustomizeError):
                    customize.check_patch(bad)
        self.assertEqual(customize.check_patch(NANO_PATCH), NANO_PATCH)

    def test_a_repair_patch_is_compiled_too(self):
        fix = fixes.Fix(step="app-editors_nano", kind="patch", package="app-editors/nano", values=["fix.patch"],
                        why="", by="claude", patch=NANO_PATCH)
        self.assertIn("--usepkg-exclude app-editors/nano", fixes.script([fix]))


class DefaultSettings(unittest.TestCase):
    """Settings the programs already have, made the distribution's defaults (no compiling)."""

    CLOCK = recipe.Setting("gsettings", "clock-show-seconds", "true", schema="org.gnome.desktop.interface",
                           about="Clock with seconds")
    DARK = recipe.Setting("gsettings", "color-scheme", "'prefer-dark'", schema="org.gnome.desktop.interface")
    BORDERS = recipe.Setting("kde", "BorderSize", "Large", file="kwinrc", group="org.kde.kdecoration2")

    def test_they_are_installed_by_the_release_package_and_checked(self):
        r = dataclasses.replace(recipe.load(RECIPES / "gnome.toml"), settings=[self.CLOCK, self.DARK, self.BORDERS])
        files = overlay.files(r)
        d = "argo-base/argo-gnome-release"
        self.assertEqual(files[f"{d}/files/90_argo-settings.gschema.override"].decode(),
                         "[org.gnome.desktop.interface]\nclock-show-seconds=true\ncolor-scheme='prefer-dark'\n\n")
        self.assertEqual(files[f"{d}/files/kde-settings"], b"kwinrc\torg.kde.kdecoration2\tBorderSize\tLarge\n")
        ebuild = files[f"{d}/argo-gnome-release-0.1.ebuild"].decode()
        self.assertIn('doins "${FILESDIR}"/90_argo-settings.gschema.override', ebuild)
        self.assertIn('glib-compile-schemas "${EROOT}"/usr/share/glib-2.0/schemas', ebuild)
        self.assertIn('done < "${EROOT}"/usr/lib/argo/kde-settings', ebuild)  # values read as data, never as code
        script = steps.argo(r).script
        self.assertIn("glib-compile-schemas --strict --dry-run /usr/share/glib-2.0/schemas", script)  # a wrong one fails
        self.assertIn("gsettings get org.gnome.desktop.interface clock-show-seconds", script)

    def test_without_settings_nothing_changes(self):
        r = recipe.load(RECIPES / "gnome.toml")
        ebuild = overlay.files(r)["argo-base/argo-gnome-release/argo-gnome-release-0.1.ebuild"].decode()
        self.assertNotIn("glib-compile-schemas", ebuild)
        self.assertNotIn("kwriteconfig6", ebuild)
        self.assertNotIn("gsettings", steps.argo(r).script)

    def test_they_round_trip_and_bad_ones_are_refused(self):
        r = dataclasses.replace(recipe.load(RECIPES / "example.toml"), settings=[self.CLOCK, self.BORDERS])
        with tempfile.TemporaryDirectory() as d:
            shutil.copytree(RECIPES / "keys", Path(d) / "keys")
            (Path(d) / "mine.toml").write_text(recipe.dump(r))
            self.assertEqual(recipe.load(Path(d) / "mine.toml").settings, [self.CLOCK, self.BORDERS])
        for bad in (dataclasses.replace(self.CLOCK, schema="not a schema"), dataclasses.replace(self.CLOCK, key="Bad_Key"),
                    dataclasses.replace(self.CLOCK, value="true\nrm -rf /"), dataclasses.replace(self.BORDERS, value="a\tb"),
                    dataclasses.replace(self.BORDERS, file="../../etc/passwd"), dataclasses.replace(self.BORDERS, group="a]b"),
                    dataclasses.replace(self.CLOCK, kind="xfconf"), dataclasses.replace(self.CLOCK, value="")):
            with self.subTest(bad):
                self.assertTrue(recipe.setting_problems(bad))
        self.assertEqual(recipe.setting_problems(self.CLOCK), [])


class Pictures(unittest.TestCase):
    def test_ppm_to_png(self):
        ppm = b"P6\n# a comment\n2 1\n255\n" + bytes([255, 0, 0, 0, 0, 255])
        png = image.ppm_to_png(ppm)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        width, height = struct.unpack(">II", png[16:24])
        self.assertEqual((width, height), (2, 1))
        start = png.index(b"IDAT") + 4
        length = struct.unpack(">I", png[start - 8:start - 4])[0]
        self.assertEqual(zlib.decompress(png[start:start + length]), b"\x00" + bytes([255, 0, 0, 0, 0, 255]))


if __name__ == "__main__":
    unittest.main()
