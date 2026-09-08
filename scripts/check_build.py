"""Exercise the actual bundled engine before packaging; no GUI or user files."""
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw
import app


def main() -> None:
    processor = app.Processor()
    if not processor.binary.is_file():
        raise SystemExit("The processing engine is missing.")
    with tempfile.TemporaryDirectory(prefix="pixel_forge_build_") as directory:
        folder = Path(directory)
        source = folder / "source.png"
        icon = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(icon)
        draw.rectangle((16, 8, 47, 55), fill=(255, 160, 32, 255))
        draw.rectangle((24, 16, 39, 39), fill=(255, 240, 144, 255))
        icon.save(source)
        for method, grid in [("median", False), ("lanczos", False), ("lanczos", True)]:
            output = folder / f"{method}-{grid}.png"
            settings = app.ProcessSettings.from_payload({
                "method": method, "scale": 2, "snapGrid": grid,
                "quantize": True, "colors": 32, "alphaBinarize": True,
            })
            processor.process(source, output, settings)
            with Image.open(output) as result:
                rgba = result.convert("RGBA")
                if not grid and rgba.size != (32, 32):
                    raise RuntimeError(f"Wrong output size for {method}: {rgba.size}")
                if not rgba.getchannel("A").getbbox():
                    raise RuntimeError(f"Empty output for {method}, grid={grid}")
                if not set(rgba.getchannel("A").get_flattened_data()) <= {0, 255}:
                    raise RuntimeError("Alpha binarization failed.")
            print(f"OK: {method}, Simple Grid={grid}")


if __name__ == "__main__":
    main()
