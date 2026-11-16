"""Static security contract for the Windows-only tunnel dependency bootstrap."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class WindowsLauncherSupplyChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = (ROOT / "start_windows.ps1").read_text(encoding="utf-8")

    def test_managed_cloudflared_download_is_version_and_hash_pinned(self):
        self.assertIn("$cloudflaredVersion = '2026.11.0'", self.script)
        self.assertIn(
            "$cloudflaredSha256 = 'd5c1d82031da6253dcdcff64c944cd0fe063016edeb2e4fd088075fc9bd52d0a'",
            self.script,
        )
        self.assertIn(
            'https://github.com/cloudflare/cloudflared/releases/download/$cloudflaredVersion/cloudflared-windows-amd64.exe',
            self.script,
        )
        self.assertNotIn("releases/latest/download", self.script)

    def test_download_integrity_and_version_are_checked_before_install(self):
        start = self.script.index("$releaseUrl =")
        end = self.script.index("Move-Item -LiteralPath $cloudflaredTemp", start)
        download = self.script[start:end]
        self.assertLess(download.index("Get-FileHash"), download.index("$downloadHash -ne $cloudflaredSha256"))
        self.assertLess(download.index("$downloadHash -ne $cloudflaredSha256"), download.index("$cloudflaredTemp version"))
        self.assertLess(download.index("$cloudflaredTemp version"), download.index("throw \"Could not verify the pinned cloudflared release"))
        self.assertIn("Remove-Item -LiteralPath $cloudflaredTemp", download)

    def test_new_virtual_environment_updates_pip_before_installing_app_packages(self):
        setup = self.script.index("$createdVenv = $false")
        bootstrap = self.script[setup:]
        self.assertIn("$createdVenv = $true", bootstrap)
        self.assertIn("--upgrade pip", bootstrap)
        lowered = self.script.lower()
        self.assertLess(lowered.index("--upgrade pip"), lowered.index("installing required camera and face-recognition packages"))


if __name__ == "__main__":
    unittest.main()
