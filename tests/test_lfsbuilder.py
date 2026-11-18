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

from lfsbuilder import check, fixes, image, recipe, repair, runner, steps

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
        self.assertIn(f"sync-uri = http://10.0.2.2:8080/{r.binary_set}/", script)
        self.assertIn("verify-signature = true", script)
        self.assertIn(steps.TRUST.read_text().rstrip(), script)
        self.assertIn(r.binhost_key_text().rstrip(), script)
        self.assertNotIn("LFSBUILDER_EOF", steps.TRUST.read_text())
        # The repository: the recipe's branch from the same server, its head signed by Gentoo.
        self.assertIn("sync-uri = http://10.0.2.2:8080/gentoo.git", script)
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


class Identity(unittest.TestCase):
    def test_the_system_says_its_own_name(self):
        r = recipe.load(RECIPES / "example.toml")
        files = image.system_files(r)
        self.assertEqual(files["etc/os-release"][0], "file")  # a file in place of baselayout's link
        release = dict(line.split("=", 1) for line in files["etc/os-release"][1].splitlines())
        self.assertEqual((release["NAME"], release["ID"], release["ID_LIKE"], release["PRETTY_NAME"]),
                         ('"Argo Linux"', '"argo-linux"', '"gentoo"', '"Argo Linux 0.1"'))
        self.assertIn("Argo Linux 0.1", files["etc/issue"][1])
        self.assertNotIn("Gentoo", files["etc/os-release"][1] + files["etc/issue"][1])
        self.assertEqual(r.short_name, "Argo")

    def test_each_desktop_gets_its_own_login_screen(self):
        marks = {"gdm": "org/gnome/login-screen", "sddm": "Current=breeze", "lightdm": "/etc/lightdm/lightdm-gtk-greeter.conf"}
        for name, dm in (("example", None), ("gnome", "gdm"), ("plasma", "sddm"), ("xfce", "lightdm")):
            with self.subTest(name):
                script = steps.branding(recipe.load(RECIPES / f"{name}.toml")).script
                self.assertIn("/usr/share/icons/hicolor/256x256/apps/argo-linux.png", script)
                for other, mark in marks.items():
                    self.assertEqual(mark in script, other == dm)


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
