import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


class LinuxAppRunTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.appdir = self.root / "AppDir"
        self.home = self.root / "home"
        self.bin_dir = self.root / "bin"
        bundle = self.appdir / "usr" / "bin" / "gozik-yt-music-server"
        bundle.mkdir(parents=True)
        binary = bundle / "gozik-yt-music-server"
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        unit_dir = self.appdir / "usr" / "lib" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        project = Path(__file__).resolve().parents[1]
        shutil.copy(project / "packaging" / "linux" / "gozik-ytmusic.service", unit_dir / "gozik-ytmusic.service")
        self.apprun = self.appdir / "AppRun"
        shutil.copy(project / "packaging" / "linux" / "AppRun", self.apprun)
        self.apprun.chmod(0o755)
        self.bin_dir.mkdir()
        self.log = self.root / "systemctl.log"
        systemctl = self.bin_dir / "systemctl"
        systemctl.write_text(
            "#!/bin/bash\n"
            "printf '%s|LD_LIBRARY_PATH=%s|LD_PRELOAD=%s\\n' \"$*\" "
            "\"${LD_LIBRARY_PATH-unset}\" \"${LD_PRELOAD-unset}\" >> \"${SYSTEMCTL_LOG}\"\n"
            "if [[ \"$*\" == *'is-system-running'* ]]; then exit 1; fi\n"
            "if [[ \"$*\" == *'show-environment'* ]]; then exit 0; fi\n"
            "if [[ \"$*\" == *'is-active gozik-ytmusic.service'* ]]; then exit \"${MOCK_ACTIVE:-1}\"; fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        systemctl.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update(
            HOME=str(self.home),
            PATH=f"{self.bin_dir}:{self.env['PATH']}",
            SYSTEMCTL_LOG=str(self.log),
            LD_LIBRARY_PATH="/tmp/appimage/usr/lib",
            LD_PRELOAD="/tmp/appimage/usr/lib/libpreload.so",
            MOCK_ACTIVE="1",
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def run_apprun(self):
        return subprocess.run(
            [str(self.apprun)],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )

    def calls(self):
        return self.log.read_text(encoding="utf-8").splitlines()

    def test_install_uses_systemd_when_degraded_and_migrates_legacy_unit(self):
        result = self.run_apprun()
        calls = self.calls()
        self.assertIn("Daemon registered and started via systemd", result.stdout)
        self.assertTrue(any("disable --now gozik-yt-music-server.service" in call for call in calls))
        self.assertTrue(any("restart gozik-ytmusic.service" in call for call in calls))
        self.assertFalse(any("is-system-running" in call for call in calls))
        for call in calls:
            self.assertIn("LD_LIBRARY_PATH=unset", call)
            self.assertIn("LD_PRELOAD=unset", call)
        installed_unit = self.home / ".config" / "systemd" / "user" / "gozik-ytmusic.service"
        self.assertIn(str(self.home), installed_unit.read_text(encoding="utf-8"))

    def test_unchanged_running_install_does_not_restart_or_start_server(self):
        self.run_apprun()
        self.log.write_text("", encoding="utf-8")
        self.env["MOCK_ACTIVE"] = "0"
        result = self.run_apprun()
        calls = self.calls()
        self.assertIn("Daemon already running via systemd", result.stdout)
        self.assertFalse(any("restart gozik-ytmusic.service" in call for call in calls))
        self.assertFalse(any("start gozik-ytmusic.service" in call for call in calls))


if __name__ == "__main__":
    unittest.main()
