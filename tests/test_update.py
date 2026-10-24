import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

from muse_cli import update


class SourceUrlTest(unittest.TestCase):
    def test_tracks_fork_not_pypi(self):
        self.assertIn("thoitiettxl-cyber/muse-cli", update.VERSION_URL)
        self.assertEqual(update.INSTALL_SPEC,
                         "git+https://github.com/thoitiettxl-cyber/muse-cli.git")
        self.assertFalse(hasattr(update, "PYPI_URL"))
        self.assertNotIn("pypi", update.VERSION_URL)
        self.assertNotIn("nikships", update.VERSION_URL + update.REPO_URL)

    def test_parse_version(self):
        self.assertEqual(update.parse_version('"""doc"""\n\n__version__ = "0.4.1"\n'), "0.4.1")
        self.assertEqual(update.parse_version("__version__='1.2.3'"), "1.2.3")
        with self.assertRaises(ValueError):
            update.parse_version("nothing here")

    def test_fetch_latest_reads_raw_init(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b'__version__ = "9.9.9"\n'
        with mock.patch.object(update.urllib.request, "urlopen", return_value=resp) as op:
            self.assertEqual(update._fetch_latest(), "9.9.9")
        self.assertEqual(op.call_args[0][0].full_url, update.VERSION_URL)


class SourceCheckoutTest(unittest.TestCase):
    def _tree(self, git):
        root = tempfile.mkdtemp()
        pkg = os.path.join(root, "src", "muse_cli")
        os.makedirs(pkg)
        open(os.path.join(root, "pyproject.toml"), "w").close()
        if git:
            os.makedirs(os.path.join(root, ".git"))
        return root, os.path.join(pkg, "update.py")

    def test_detects_checkout(self):
        root, f = self._tree(git=True)
        self.assertEqual(update.source_checkout(f), os.path.realpath(root))

    def test_site_packages_is_not_checkout(self):
        _root, f = self._tree(git=False)
        self.assertIsNone(update.source_checkout(f))

    def test_checkout_upgrade_is_git_pull(self):
        with mock.patch.object(update, "source_checkout", return_value="/r"):
            self.assertEqual(update.upgrade_argv(), ["git", "-C", "/r", "pull", "--ff-only"])

    def test_update_never_replaces_checkout(self):
        err = io.StringIO()
        with mock.patch.object(update, "source_checkout", return_value="/r"), \
                mock.patch("subprocess.call") as call, redirect_stderr(err):
            update.cmd_update(None)
        call.assert_not_called()
        self.assertIn("source checkout", err.getvalue())
        self.assertIn("git -C /r pull --ff-only", err.getvalue())


class UpgradeArgvTest(unittest.TestCase):
    def _argv(self, prefix):
        with mock.patch.object(update, "source_checkout", return_value=None), \
                mock.patch.object(sys, "prefix", prefix), \
                mock.patch.object(update.os.path, "realpath", side_effect=lambda p: p):
            return update.upgrade_argv()

    def test_uv_tool_reinstalls_from_fork(self):
        argv = self._argv("/home/u/.local/share/uv/tools/muse-cli")
        self.assertEqual(argv, ["uv", "tool", "install", "--upgrade", update.INSTALL_SPEC])

    def test_pipx_reinstalls_from_fork(self):
        argv = self._argv("/home/u/.local/share/pipx/venvs/muse-cli")
        self.assertEqual(argv, ["pipx", "install", "--force", update.INSTALL_SPEC])

    def test_pip_installs_from_fork(self):
        argv = self._argv("/opt/venv")
        self.assertEqual(argv[1:], ["-m", "pip", "install", "-U", update.INSTALL_SPEC])
        self.assertNotIn("muse-cli", argv)  # never the bare PyPI name


class CheckGateTest(unittest.TestCase):
    def test_env_disables_check(self):
        with mock.patch.dict(os.environ, {"MUSE_NO_UPDATE_CHECK": "1"}):
            self.assertIsNone(update.start_update_check())

    def test_non_tty_disables_check(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(sys, "stdout", io.StringIO()):
            os.environ.pop("MUSE_NO_UPDATE_CHECK", None)
            os.environ.pop("CI", None)
            self.assertIsNone(update.start_update_check())

    def test_cached_result_within_a_day(self):
        with mock.patch.object(update, "_should_check", return_value=True), \
                mock.patch.object(update, "_load_state",
                                  return_value={"latest": "9.0.0",
                                                "checked_at": update.time.time()}), \
                mock.patch.object(update, "_fetch_latest") as fetch:
            self.assertEqual(update.start_update_check(), {"latest": "9.0.0"})
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
