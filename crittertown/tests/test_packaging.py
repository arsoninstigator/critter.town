"""what a distribution carries. a vendored tree's licence has to travel with it, and a rust workspace we
run as its own image must not ride along as if it were python we import.

both were wrong before this test existed: the package-data globs were written from the repo root while
setuptools reads them from the package, so the wheel shipped none of the vendored licences it promises,
and the package finder walked into the relay's tree and packaged eighteen of upstream's python files as
importable modules. neither shows up as a failure anywhere else, which is why it is checked here."""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def open_text(path: str) -> str:
    with open(path) as handle:
        return handle.read()


class WheelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.out = tempfile.mkdtemp()
        # build from a copy, so a stale build/ in the working tree cannot make this pass or fail
        cls.src = tempfile.mkdtemp()
        ignore = shutil.ignore_patterns(".git", "build", "*.egg-info", "__pycache__", "*.pyc",
                                        "theidea", "demo_data", "*.db")
        # symlinks stay links, as in a checkout: the relay tree has eight that dangle by design
        shutil.copytree(ROOT, os.path.join(cls.src, "src"), ignore=ignore, symlinks=True)
        built = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "-w", cls.out, "-q"],
            cwd=os.path.join(cls.src, "src"), capture_output=True, text=True)
        if built.returncode:
            raise unittest.SkipTest(f"a wheel could not be built here: {built.stderr[-200:]}")
        cls.names = zipfile.ZipFile(glob.glob(cls.out + "/*.whl")[0]).namelist()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.out, ignore_errors=True)
        shutil.rmtree(cls.src, ignore_errors=True)

    def vendored(self) -> list[str]:
        return sorted(d for d in os.listdir(os.path.join(ROOT, "vendor"))
                      if os.path.isdir(os.path.join(ROOT, "vendor", d)) and d != "__pycache__")

    def test_every_vendored_tree_ships_its_licence_and_provenance(self):
        for name in self.vendored():
            for record in ("LICENSE", "PROVENANCE.md"):
                self.assertIn(f"vendor/{name}/{record}", self.names, f"{name} lost its {record}")

    def test_the_relay_ships_its_manifest_so_a_recipient_can_verify_it(self):
        self.assertIn("vendor/relay_workspace/UPSTREAM_SHA256SUMS", self.names)

    def test_the_rust_workspace_is_not_packaged(self):
        carried = {"LICENSE", "PROVENANCE.md", "UPSTREAM_SHA256SUMS"}
        stray = [n for n in self.names if n.startswith("vendor/relay_workspace/")
                 and n.rsplit("/", 1)[-1] not in carried]
        self.assertEqual(stray, [], "the relay is run as its own image, not imported")

    def test_none_of_the_upstreams_python_is_importable_as_ours(self):
        self.assertFalse([n for n in self.names if "relay_workspace" in n and n.endswith(".py")])

    def test_the_wheel_stays_small(self):
        size = os.path.getsize(glob.glob(self.out + "/*.whl")[0])
        self.assertLess(size, 5_000_000, "something large rode along")


class ContextTests(unittest.TestCase):
    def test_the_docker_context_leaves_out_the_rust_but_keeps_the_licence(self):
        lines = open_text(os.path.join(ROOT, ".dockerignore")).splitlines()
        self.assertIn("vendor/relay_workspace/*", lines)
        for keep in ("LICENSE", "PROVENANCE.md", "UPSTREAM_SHA256SUMS"):
            self.assertIn(f"!vendor/relay_workspace/{keep}", lines)

    def test_a_negation_comes_after_the_pattern_it_carves_out(self):
        lines = open_text(os.path.join(ROOT, ".dockerignore")).splitlines()
        block = lines.index("vendor/relay_workspace/*")
        for keep in ("LICENSE", "PROVENANCE.md", "UPSTREAM_SHA256SUMS"):
            self.assertGreater(lines.index(f"!vendor/relay_workspace/{keep}"), block)


if __name__ == "__main__":
    unittest.main()
