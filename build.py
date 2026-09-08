"""Build the Windows application locally or on GitHub Actions."""
from pathlib import Path
import hashlib
import os
import shutil
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
ENGINE_SHA256 = "ba187776ac142beca41ebb405209abd1bcb05e4532a154ba64e9fa8a8214bd93"


def main() -> None:
    if os.name != "nt" or struct.calcsize("P") != 8:
        raise SystemExit("Build with 64-bit Python on Windows.")
    engine = ROOT / "bin/windows-x64/unfake.exe"
    if not engine.is_file():
        raise SystemExit("Missing bin/windows-x64/unfake.exe; include the bundled engine.")
    if hashlib.sha256(engine.read_bytes()).hexdigest() != ENGINE_SHA256:
        raise SystemExit("Engine checksum mismatch. Update the checksum when deliberately upgrading unfake.")
    subprocess.run([
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", "PixelForge",
        "--icon", str(ROOT / "assets/Pixel_Forge_Win.ico"),
        "--add-binary", f"{engine};bin/windows-x64",
        "--add-data", f"{ROOT / 'licenses/UNFAKE-LICENSE.txt'};licenses",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        str(ROOT / "app.py"),
    ], cwd=ROOT, check=True)
    executable = ROOT / "dist/PixelForge.exe"
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    (ROOT / "dist/SHA256SUMS.txt").write_text(
        f"{digest}  PixelForge.exe\n", encoding="utf-8", newline="\n")
    shutil.copyfile(ROOT / "licenses/UNFAKE-LICENSE.txt", ROOT / "dist/UNFAKE-LICENSE.txt")
    print(f"Built {executable.name} ({executable.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
