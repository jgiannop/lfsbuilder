"""lfsbuilder's tests: the checks that keep recipes, fixes and logs safe.

    python3 -m unittest discover -s tests

No Docker, no network: only what can be checked from the code itself.
"""

from __future__ import annotations

import dataclasses
import shutil
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from lfsbuilder import calamares, check, estimate, fixes, image, overlay, recipe, repair, runner, steps

ROOT = Path(__file__).resolve().parent.parent
RECIPES = ROOT / "recipes"
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
        for path in sorted(RECIPES.glob("*.toml")):
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
        return overlay.files(recipe.load(RECIPES / f"{name}.toml"))[f"argo-base/argo-release/argo-release-0.1.ebuild"].decode()

    def test_the_system_says_its_own_name(self):
        r = recipe.load(RECIPES / "example.toml")
        files = overlay.files(r)
        release = dict(line.split("=", 1) for line in files["argo-base/argo-release/files/os-release"].decode().splitlines())
        self.assertEqual((release["NAME"], release["ID"], release["ID_LIKE"], release["PRETTY_NAME"], release["LOGO"]),
                         ('"Argo Linux"', '"argo-linux"', '"gentoo"', '"Argo Linux 0.1"', '"argo-linux"'))
        self.assertIn(b"Argo Linux 0.1", files["argo-base/argo-release/files/issue"])
        self.assertNotIn("etc/os-release", image.system_files(r))  # the package's, not the export's
        self.assertEqual(r.short_name, "Argo")

    def test_the_profile_carries_the_use_flags_and_the_meta_package_the_packages(self):
        r = recipe.load(RECIPES / "gnome.toml")
        files = overlay.files(r)
        self.assertEqual(files["profiles/argo/gnome/parent"], f"gentoo:{r.profile}\n".encode())
        self.assertIn(b"net-libs/ngtcp2 gnutls", files["profiles/argo/gnome/package.use"])
        self.assertIn(b"sys-kernel/installkernel dracut", files["profiles/argo/gnome/package.use"])
        meta = files["argo-base/argo-gnome/argo-gnome-0.1.ebuild"].decode()
        for atom in ["argo-base/argo-release", *r.packages, "net-misc/networkmanager", "app-admin/sudo"]:
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

    def test_each_desktop_is_a_use_flag_of_one_release_package(self):
        # Recipes share one set of binary packages: Portage tells their argo-release builds apart by USE.
        expected = {"example": [], "gnome": ["gdm"], "plasma": ["sddm", "plasma"], "xfce": ["lightdm", "xfce"], "openrc": []}
        releases = set()
        for name, flags in expected.items():
            with self.subTest(name):
                r = recipe.load(RECIPES / f"{name}.toml")
                self.assertEqual(overlay.release_flags(r), flags)
                use = overlay.files(r)[f"profiles/argo/{name}/package.use"].decode()
                self.assertEqual(f"argo-base/argo-release {' '.join(flags)}" in use, bool(flags))
                releases.add(self.release(name))
        self.assertEqual(len(releases), 1)  # the same ebuild for every recipe
        ebuild = releases.pop()
        for flag in overlay.DESKTOP_FLAGS:
            self.assertIn(f"use {flag}", ebuild)
        self.assertIn("LookAndFeelPackage=org.argolinux.desktop", ebuild)
        self.assertIn("/etc/portage/env/xfce-base", ebuild)

    def test_lightdm_starts_xfce(self):
        # Without it LightDM starts xinit's Xsession (first by name) and the login falls back to the greeter.
        files = overlay.files(recipe.load(RECIPES / "xfce.toml"))
        self.assertEqual(files["argo-base/argo-release/files/lightdm-50-argo.conf"], b"[Seat:*]\nuser-session=xfce\n")
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
    def test_the_logo_files_are_well_formed_and_rendered(self):
        import xml.dom.minidom
        for svg in sorted((ROOT / "branding").glob("*.svg")):
            with self.subTest(svg.name):
                xml.dom.minidom.parse(str(svg))
        for name in ("argo-background-1920x1080.png", "argo-background-plain-1920x1080.png", "argo-wordmark-light-440.png"):
            with self.subTest(name):
                png = (ROOT / "branding" / name).read_bytes()
                self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
                svg = (ROOT / "branding" / name.replace("-1920x1080", "").replace("-440", "")).with_suffix(".svg")
                self.assertGreaterEqual((ROOT / "branding" / name).stat().st_mtime, svg.stat().st_mtime,
                                        f"{name} is older than {svg.name}: run branding/render.sh")


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
