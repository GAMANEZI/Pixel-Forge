
from __future__ import annotations

import atexit
import base64
import errno
import json
import os
import shutil
import subprocess
import sys
import stat
import tempfile
import zipfile
import re
import hashlib
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import webview
from PIL import Image, ImageFilter


APP_NAME = "Pixel Forge"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

MAX_IMAGE_PIXELS = 120_000_000
MAX_ARCHIVE_FILES = 20_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 250 * 1024 * 1024
UNFAKE_TIMEOUT_SECONDS = 90
MIN_FREE_SPACE_BYTES = 128 * 1024 * 1024
DISK_SAFETY_FACTOR = 2.5


def resource_path(relative: str) -> Path:
    """
    Works in source mode and after Nuitka onefile/standalone compilation.
    """
    candidates: list[Path] = []

    # Nuitka onefile extraction directory.
    onefile_parent = os.environ.get("NUITKA_ONEFILE_PARENT")
    if onefile_parent:
        candidates.append(Path(onefile_parent))

    # Nuitka/normal executable directory.
    candidates.append(Path(sys.argv[0]).resolve().parent)

    # Source directory.
    candidates.append(Path(__file__).resolve().parent)

    for base in candidates:
        candidate = base / relative
        if candidate.exists():
            return candidate

    return candidates[-1] / relative


def bundled_unfake_path() -> Path:
    if sys.platform == "darwin":
        return resource_path("bin/macos-arm64/unfake")
    if os.name == "nt":
        return resource_path("bin/windows-x64/unfake.exe")
    return resource_path("bin/linux-x64/unfake")


def image_to_data_url(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


@dataclass(frozen=True)
class ProcessSettings:
    detect_method: str = "manual"
    scale: int = 2
    method: str = "nearest"
    colors: int = 256
    snap_grid: bool = True
    quantize: bool = True
    morph_cleanup: bool = False
    jaggy_cleanup: bool = False
    alpha_binarize: bool = True
    alpha_threshold: int = 128
    transparent_background: bool = False
    background_tolerance: int = 10

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ProcessSettings":
        scale = max(1, min(32, int(payload.get("scale", 2))))
        colors = max(2, min(256, int(payload.get("colors", 256))))
        alpha_threshold = max(0, min(255, int(payload.get("alphaThreshold", 128))))
        background_tolerance = max(0, min(100, int(payload.get("backgroundTolerance", 10))))

        method = str(payload.get("method", "nearest"))
        allowed_methods = {"nearest", "median", "mode", "dominant", "qvote"}
        if method not in allowed_methods:
            method = "nearest"

        detect_method = str(payload.get("detectMethod", "manual"))
        if detect_method not in {"manual", "auto", "runs", "edge"}:
            detect_method = "manual"

        return cls(
            detect_method=detect_method,
            scale=scale,
            method=method,
            colors=colors,
            snap_grid=bool(payload.get("snapGrid", True)),
            quantize=bool(payload.get("quantize", True)),
            morph_cleanup=bool(payload.get("morphCleanup", False)),
            jaggy_cleanup=bool(payload.get("jaggyCleanup", False)),
            alpha_binarize=bool(payload.get("alphaBinarize", True)),
            alpha_threshold=alpha_threshold,
            transparent_background=bool(payload.get("transparentBackground", False)),
            background_tolerance=background_tolerance,
        )


class Processor:
    def __init__(self) -> None:
        self.binary = bundled_unfake_path()

    def validate(self) -> None:
        if not self.binary.is_file():
            raise FileNotFoundError(
                f"Встроенный движок не найден:\n{self.binary}\n\n"
                "Скопируй собранный unfake в папку bin перед запуском."
            )

        if os.name != "nt":
            try:
                self.binary.chmod(self.binary.stat().st_mode | stat.S_IEXEC)
            except Exception:
                pass

    @staticmethod
    def _run(command: list[str]) -> str:
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW

        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=creationflags,
                timeout=UNFAKE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                f"Движок unfake не ответил за {UNFAKE_TIMEOUT_SECONDS} секунд"
            ) from error
        except OSError as error:
            raise RuntimeError(f"Не удалось запустить unfake: {error}") from error

        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(message or "Ошибка встроенного движка unfake")
        return result.stdout.strip()

    def _detect_scale(self, source: Path, method: str, fallback: int) -> int:
        if method == "manual":
            return fallback

        commands = [
            [str(self.binary), "detect", str(source), "--method", method],
            [str(self.binary), "detect", str(source), "--detect-method", method],
            [str(self.binary), "detect", str(source)],
        ]
        for command in commands:
            try:
                output = self._run(command)
            except Exception:
                continue

            json_match = re.search(r'"(?:scale|detected_scale)"\s*:\s*(\d+)', output)
            if json_match:
                return max(1, min(32, int(json_match.group(1))))

            explicit_match = re.search(
                r"(?:detected[_ ]?scale|scale)\s*[:=]\s*(\d+)",
                output,
                flags=re.IGNORECASE,
            )
            if explicit_match:
                return max(1, min(32, int(explicit_match.group(1))))

            if output.strip().isdigit():
                return max(1, min(32, int(output.strip())))

        raise RuntimeError(
            "Не удалось автоматически определить масштаб. "
            "Переключись на ручной режим и укажи Pixel size."
        )

    @staticmethod
    def _remove_background(image: Image.Image, tolerance: int) -> Image.Image:
        rgba = image.convert("RGBA")
        pixels = rgba.load()
        width, height = rgba.size
        if width == 0 or height == 0:
            return rgba

        corners = [
            pixels[0, 0][:3],
            pixels[width - 1, 0][:3],
            pixels[0, height - 1][:3],
            pixels[width - 1, height - 1][:3],
        ]
        bg = tuple(sum(c[i] for c in corners) // len(corners) for i in range(3))

        def similar(rgb):
            return max(abs(rgb[i] - bg[i]) for i in range(3)) <= tolerance

        queue = deque()
        visited = set()

        for x in range(width):
            queue.append((x, 0))
            queue.append((x, height - 1))
        for y in range(height):
            queue.append((0, y))
            queue.append((width - 1, y))

        while queue:
            x, y = queue.popleft()
            if (x, y) in visited:
                continue
            visited.add((x, y))
            if not similar(pixels[x, y][:3]):
                continue

            r, g, b, _ = pixels[x, y]
            pixels[x, y] = (r, g, b, 0)

            if x > 0:
                queue.append((x - 1, y))
            if x + 1 < width:
                queue.append((x + 1, y))
            if y > 0:
                queue.append((x, y - 1))
            if y + 1 < height:
                queue.append((x, y + 1))

        return rgba

    @staticmethod
    def _morph_cleanup(image: Image.Image) -> Image.Image:
        rgba = image.convert("RGBA")
        r, g, b, a = rgba.split()
        # Closing alpha holes and removing single-pixel noise.
        a = a.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))
        return Image.merge("RGBA", (r, g, b, a))

    @staticmethod
    def _jaggy_cleanup(image: Image.Image) -> Image.Image:
        rgba = image.convert("RGBA")
        src = rgba.load()
        result = rgba.copy()
        dst = result.load()
        width, height = rgba.size

        for y in range(1, height - 1):
            for x in range(1, width - 1):
                center = src[x, y]
                orthogonal = [
                    src[x - 1, y],
                    src[x + 1, y],
                    src[x, y - 1],
                    src[x, y + 1],
                ]
                diagonal = [
                    src[x - 1, y - 1],
                    src[x + 1, y - 1],
                    src[x - 1, y + 1],
                    src[x + 1, y + 1],
                ]

                # Remove an isolated diagonal "stair-step" pixel only when
                # the surrounding orthogonal pixels agree.
                if all(pixel == orthogonal[0] for pixel in orthogonal):
                    if center != orthogonal[0] and sum(pixel == center for pixel in diagonal) <= 1:
                        dst[x, y] = orthogonal[0]
        return result

    def process(self, source: Path, destination: Path, settings: ProcessSettings) -> dict[str, Any]:
        self.validate()
        destination.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="pixel_forge_job_") as tmp:
            tmp_dir = Path(tmp)
            normalized = tmp_dir / "input.png"
            downscaled = tmp_dir / "downscaled.png"
            quantized = tmp_dir / "quantized.png"

            with Image.open(source) as image:
                image.convert("RGBA").save(normalized, "PNG")

            scale = self._detect_scale(normalized, settings.detect_method, settings.scale)

            command = [
                str(self.binary),
                "downscale",
                str(normalized),
                "--output",
                str(downscaled),
                "--scale",
                str(scale),
                "--method",
                settings.method,
            ]

            if not settings.snap_grid:
                command.append("--no-align")

            self._run(command)
            current = downscaled

            if settings.quantize:
                self._run([
                    str(self.binary),
                    "quantize",
                    str(current),
                    "--output",
                    str(quantized),
                    "--colors",
                    str(settings.colors),
                ])
                current = quantized

            with Image.open(current) as image:
                rgba = image.convert("RGBA")

                if settings.transparent_background:
                    rgba = self._remove_background(rgba, settings.background_tolerance)

                if settings.morph_cleanup:
                    rgba = self._morph_cleanup(rgba)

                if settings.jaggy_cleanup:
                    rgba = self._jaggy_cleanup(rgba)

                if settings.alpha_binarize:
                    red, green, blue, alpha = rgba.split()
                    threshold = settings.alpha_threshold
                    alpha = alpha.point(lambda value: 255 if value >= threshold else 0)
                    rgba = Image.merge("RGBA", (red, green, blue, alpha))

                temporary_destination = destination.with_suffix(destination.suffix + ".tmp")
                temporary_destination.unlink(missing_ok=True)
                try:
                    rgba.save(temporary_destination, "PNG", optimize=True)
                    os.replace(temporary_destination, destination)
                finally:
                    temporary_destination.unlink(missing_ok=True)

        return {"scale": scale}


class Api:
    def __init__(self) -> None:
        self.window: webview.Window | None = None
        self.processor = Processor()
        self.temp_root = Path(tempfile.mkdtemp(prefix="pixel_forge_"))
        self.source_kind: str | None = None
        self.source_path: Path | None = None
        self.archive_root: Path | None = None
        self.files: list[Path] = []
        self.cancel_requested = False
        self.active_job_id = 0
        atexit.register(self.cleanup)

    def cleanup(self) -> None:
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def reset_source(self, *, preserve_archive_root: Path | None = None) -> None:
        """
        Fully discard the previous source without touching a newly prepared
        archive directory that is about to become active.
        """
        old_archive_root = self.archive_root

        self.active_job_id += 1
        self.cancel_requested = True
        self.source_kind = None
        self.source_path = None
        self.archive_root = None
        self.files.clear()

        for relative in ("preview", "normalized", "batch_output"):
            shutil.rmtree(self.temp_root / relative, ignore_errors=True)

        if (
            old_archive_root is not None
            and old_archive_root != preserve_archive_root
        ):
            shutil.rmtree(old_archive_root, ignore_errors=True)

        self.cancel_requested = False

    @staticmethod
    def _friendly_exception(error: BaseException) -> str:
        if isinstance(error, MemoryError):
            return (
                "Недостаточно оперативной памяти для этой операции. "
                "Закрой лишние приложения, уменьши размер изображения или "
                "обрабатывай архив частями."
            )

        if isinstance(error, OSError):
            if error.errno == errno.ENOSPC:
                return (
                    "Недостаточно свободного места на диске для сохранения "
                    "результата и временных файлов."
                )
            if error.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
                return (
                    "Нет доступа для записи в выбранную папку. "
                    "Выбери другую папку, например «Загрузки» или «Документы»."
                )

        message = str(error).strip()
        return message or error.__class__.__name__

    @staticmethod
    def _ensure_free_space(directory: Path, required_bytes: int, purpose: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        free_bytes = shutil.disk_usage(directory).free
        required_bytes = max(MIN_FREE_SPACE_BYTES, int(required_bytes))

        if free_bytes < required_bytes:
            required_gb = required_bytes / (1024 ** 3)
            free_gb = free_bytes / (1024 ** 3)
            raise OSError(
                errno.ENOSPC,
                f"Для операции «{purpose}» требуется примерно "
                f"{required_gb:.2f} ГБ свободного места, доступно {free_gb:.2f} ГБ.",
            )

    def _estimate_single_work_bytes(self, source: Path) -> int:
        try:
            with Image.open(source) as image:
                width, height = image.size
            rgba_bytes = width * height * 4
        except Exception:
            rgba_bytes = source.stat().st_size * 4

        return int(max(source.stat().st_size * 4, rgba_bytes * DISK_SAFETY_FACTOR))

    def _estimate_archive_work_bytes(self) -> int:
        total_source_bytes = sum(
            file.stat().st_size for file in self.files if file.exists()
        )
        return int(max(total_source_bytes * 4, MIN_FREE_SPACE_BYTES))

    def _next_job_id(self) -> int:
        self.active_job_id += 1
        return self.active_job_id

    @staticmethod
    def _safe_error(message: str) -> dict[str, str]:
        return {"error": message}

    @staticmethod
    def _validate_image_file(path: Path) -> None:
        try:
            with Image.open(path) as image:
                width, height = image.size
                if width <= 0 or height <= 0:
                    raise ValueError("Некорректный размер изображения")
                if width * height > MAX_IMAGE_PIXELS:
                    raise ValueError(
                        f"Изображение слишком большое: {width} × {height}. "
                        f"Лимит — {MAX_IMAGE_PIXELS:,} пикселей."
                    )
                image.verify()
        except Image.DecompressionBombError as error:
            raise ValueError("Изображение слишком большое или подозрительно сжато") from error
        except Exception as error:
            raise ValueError("Файл повреждён или имеет неподдерживаемый формат") from error

    @staticmethod
    def _unique_output_path(root: Path, relative: Path, used: set[str]) -> Path:
        base = (root / relative).with_suffix(".png")
        candidate = base
        counter = 2

        while candidate.relative_to(root).as_posix().lower() in used:
            candidate = base.with_name(f"{base.stem}_{counter}{base.suffix}")
            counter += 1

        used.add(candidate.relative_to(root).as_posix().lower())
        return candidate

    def _dialog(self, dialog_type: int, **kwargs) -> Path | None:
        if self.window is None:
            raise RuntimeError("Окно ещё не инициализировано")

        result = self.window.create_file_dialog(dialog_type, **kwargs)
        if not result:
            return None

        # pywebview backends are inconsistent here:
        # SAVE may return a plain string, while OPEN often returns a tuple/list.
        if isinstance(result, (str, os.PathLike)):
            return Path(result)

        if isinstance(result, (tuple, list)) and result:
            return Path(result[0])

        raise RuntimeError("Диалог выбора файла вернул неизвестный формат пути")

    def choose_image(self) -> dict[str, Any]:
        path = self._dialog(
            webview.FileDialog.OPEN,
            allow_multiple=False,
            file_types=("Images (*.png;*.jpg;*.jpeg;*.webp)",),
        )
        if path is None:
            return {"cancelled": True}

        try:
            self._validate_image_file(path)
            self.reset_source()
            self.source_kind = "image"
            self.source_path = path
            self.archive_root = None
            self.files = [path]
            return self._source_payload(0)
        except Exception as error:
            return self._safe_error(self._friendly_exception(error))

    def choose_archive(self) -> dict[str, Any]:
        path = self._dialog(
            webview.FileDialog.OPEN,
            allow_multiple=False,
            file_types=("ZIP archives (*.zip)",),
        )
        if path is None:
            return {"cancelled": True}
        return self._load_archive(path)

    def _load_archive(self, path: Path) -> dict[str, Any]:
        archive_root = Path(
            tempfile.mkdtemp(prefix="archive_", dir=self.temp_root)
        )

        try:
            with zipfile.ZipFile(path, "r") as archive:
                members: list[zipfile.ZipInfo] = []
                total_uncompressed = 0

                for member in archive.infolist():
                    name = member.filename.replace("\\", "/")
                    parts = Path(name).parts

                    if member.is_dir() or name.startswith("/") or ".." in parts:
                        continue
                    if "__MACOSX" in parts or Path(name).name.startswith("._"):
                        continue
                    if Path(name).suffix.lower() not in IMAGE_EXTENSIONS:
                        continue
                    if member.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                        raise ValueError(f"Файл в архиве слишком большой: {name}")

                    total_uncompressed += member.file_size
                    if total_uncompressed > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                        raise ValueError("Архив слишком большой после распаковки")

                    members.append(member)
                    if len(members) > MAX_ARCHIVE_FILES:
                        raise ValueError(
                            f"В архиве больше {MAX_ARCHIVE_FILES} изображений"
                        )

                self._ensure_free_space(
                    self.temp_root,
                    int(total_uncompressed * 1.25),
                    "распаковка ZIP",
                )

                for member in members:
                    archive.extract(member, archive_root)

            files = sorted(
                file for file in archive_root.rglob("*")
                if file.is_file() and file.suffix.lower() in IMAGE_EXTENSIONS
            )

            valid_files: list[Path] = []
            for file in files:
                try:
                    self._validate_image_file(file)
                    valid_files.append(file)
                except ValueError:
                    continue

            if not valid_files:
                raise ValueError(
                    "В ZIP-архиве не найдено корректных поддерживаемых изображений"
                )

            self.reset_source(preserve_archive_root=archive_root)
            self.source_kind = "archive"
            self.source_path = path
            self.archive_root = archive_root
            self.files = valid_files
            return self._source_payload(0)

        except zipfile.BadZipFile:
            shutil.rmtree(archive_root, ignore_errors=True)
            return self._safe_error(
                "ZIP-архив повреждён или имеет неверный формат"
            )
        except Exception as error:
            shutil.rmtree(archive_root, ignore_errors=True)
            return self._safe_error(self._friendly_exception(error))

    def _normalized_preview(self, source: Path) -> Path:
        stat_info = source.stat()
        cache_key = hashlib.sha256(
            f"{source.resolve()}|{stat_info.st_size}|{stat_info.st_mtime_ns}".encode()
        ).hexdigest()[:24]
        destination = self.temp_root / "normalized" / f"{cache_key}.png"
        destination.parent.mkdir(parents=True, exist_ok=True)

        if not destination.exists():
            with Image.open(source) as image:
                preview = image.convert("RGBA")
                preview.thumbnail((2400, 2400), Image.Resampling.NEAREST)
                preview.save(destination, "PNG")

        return destination

    def _relative_name(self, path: Path) -> str:
        if self.archive_root is not None:
            return path.relative_to(self.archive_root).as_posix()
        return path.name

    def _source_payload(self, index: int) -> dict[str, Any]:
        source = self.files[index]
        with Image.open(source) as image:
            width, height = image.size

        return {
            "kind": self.source_kind,
            "index": index,
            "count": len(self.files),
            "files": [self._relative_name(file) for file in self.files],
            "fileName": self._relative_name(source),
            "width": width,
            "height": height,
            "original": image_to_data_url(self._normalized_preview(source)),
        }

    def select_file(self, index: int) -> dict[str, Any]:
        if index < 0 or index >= len(self.files):
            return {"error": "Некорректный номер изображения"}
        return self._source_payload(index)

    def preview(self, index: int, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.files:
            return self._safe_error("Сначала выбери изображение или ZIP-архив")
        if index < 0 or index >= len(self.files):
            return self._safe_error("Некорректный номер изображения")

        job_id = self._next_job_id()
        settings = ProcessSettings.from_payload(payload)
        destination = self.temp_root / "preview" / f"result_{job_id}.png"

        try:
            self._ensure_free_space(
                self.temp_root,
                self._estimate_single_work_bytes(self.files[index]),
                "создание предпросмотра",
            )
            process_info = self.processor.process(self.files[index], destination, settings)

            if job_id != self.active_job_id:
                return {"cancelled": True, "stale": True}

            with Image.open(destination) as image:
                width, height = image.size

            return {
                "result": image_to_data_url(destination),
                "width": width,
                "height": height,
                "scale": process_info.get("scale", settings.scale),
            }
        except Exception as error:
            return self._safe_error(self._friendly_exception(error))

    @staticmethod
    def _normalize_save_destination(
        destination: Path,
        suggested_name: str,
        required_suffix: str,
        fallback_directory: Path,
    ) -> Path:
        """
        Normalize pywebview save-dialog results.

        On macOS some backend versions can return only a directory, or even
        the filesystem root. The root must never be used as an output folder,
        because it is read-only for normal applications.
        """
        destination = destination.expanduser()
        fallback_directory = fallback_directory.expanduser()

        is_root = destination == Path(destination.anchor)
        has_empty_name = destination.name == ""

        if is_root or has_empty_name:
            destination = fallback_directory / suggested_name
        elif destination.exists() and destination.is_dir():
            destination = destination / suggested_name

        if destination.suffix.lower() != required_suffix.lower():
            destination = destination.with_suffix(required_suffix)

        destination.parent.mkdir(parents=True, exist_ok=True)
        return destination

    @staticmethod
    def _reveal_exported_file(path: Path) -> None:
        """
        Show the completed export in the native file manager.
        Failure to reveal the file must never invalidate a successful export.
        """
        try:
            if sys.platform == "darwin":
                subprocess.Popen(
                    ["open", "-R", str(path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            elif os.name == "nt":
                subprocess.Popen(
                    ["explorer", "/select,", str(path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                subprocess.Popen(
                    ["xdg-open", str(path.parent)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass

    def export_current(self, index: int, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.files:
            return self._safe_error("Сначала выбери изображение")
        if index < 0 or index >= len(self.files):
            return self._safe_error("Некорректный номер изображения")

        suggested = self.files[index].stem + "_unfaked.png"
        destination = self._dialog(
            webview.FileDialog.SAVE,
            save_filename=suggested,
            file_types=("PNG image (*.png)",),
        )
        if destination is None:
            return {"cancelled": True}

        try:
            fallback_directory = (
                self.source_path.parent
                if self.source_path is not None
                else Path.home() / "Downloads"
            )
            destination = self._normalize_save_destination(
                destination,
                suggested,
                ".png",
                fallback_directory,
            )
            estimated = self._estimate_single_work_bytes(self.files[index])
            self._ensure_free_space(
                self.temp_root,
                estimated,
                "временная обработка PNG",
            )
            self._ensure_free_space(
                destination.parent,
                max(MIN_FREE_SPACE_BYTES, estimated // 3),
                "сохранение PNG",
            )
            self.processor.process(
                self.files[index],
                destination,
                ProcessSettings.from_payload(payload),
            )
            self._reveal_exported_file(destination)
            return {"ok": True, "path": str(destination)}
        except Exception as error:
            return self._safe_error(self._friendly_exception(error))

    def export_archive(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.source_kind != "archive" or not self.files or self.archive_root is None:
            return self._safe_error("Сначала выбери ZIP-архив")

        if self.source_path is None:
            return self._safe_error("Не удалось определить имя исходного архива")

        suggested = f"{self.source_path.stem}_unfaked.zip"
        destination = self._dialog(
            webview.FileDialog.SAVE,
            save_filename=suggested,
            file_types=("ZIP archive (*.zip)",),
        )
        if destination is None:
            return {"cancelled": True}

        try:
            fallback_directory = self.source_path.parent
            destination = self._normalize_save_destination(
                destination,
                suggested,
                ".zip",
                fallback_directory,
            )
            return self._export_archive_to(destination, payload)
        except Exception as error:
            return self._safe_error(self._friendly_exception(error))

    def _export_archive_to(self, destination: Path, payload: dict[str, Any]) -> dict[str, Any]:
        if self.source_kind != "archive" or not self.files or self.archive_root is None:
            return self._safe_error("Сначала выбери ZIP-архив")

        settings = ProcessSettings.from_payload(payload)
        output_root = self.temp_root / "batch_output"
        shutil.rmtree(output_root, ignore_errors=True)
        output_root.mkdir(parents=True, exist_ok=True)

        self.cancel_requested = False
        total = len(self.files)
        used_output_names: set[str] = set()

        temporary_zip: Path | None = None

        try:
            estimated = self._estimate_archive_work_bytes()
            self._ensure_free_space(
                self.temp_root,
                estimated,
                "обработка ZIP",
            )
            self._ensure_free_space(
                destination.parent,
                max(MIN_FREE_SPACE_BYTES, estimated // 2),
                "сохранение ZIP",
            )

            for current, source in enumerate(self.files, start=1):
                if self.cancel_requested:
                    return {"cancelled": True, "processed": current - 1}

                relative = source.relative_to(self.archive_root)
                output = self._unique_output_path(
                    output_root,
                    relative,
                    used_output_names,
                )
                self.processor.process(source, output, settings)

                if self.window:
                    name_json = json.dumps(relative.as_posix())
                    self.window.evaluate_js(
                        f"window.updateBatchProgress({current}, {total}, {name_json})"
                    )

            temporary_zip = destination.with_suffix(destination.suffix + ".tmp")
            temporary_zip.unlink(missing_ok=True)
            with zipfile.ZipFile(temporary_zip, "w", zipfile.ZIP_DEFLATED) as archive:
                for file in output_root.rglob("*.png"):
                    archive.write(file, file.relative_to(output_root).as_posix())

            os.replace(temporary_zip, destination)
            self._reveal_exported_file(destination)
            return {"ok": True, "path": str(destination), "count": total}

        except Exception as error:
            return self._safe_error(self._friendly_exception(error))
        finally:
            if temporary_zip is not None:
                temporary_zip.unlink(missing_ok=True)

    def cancel_batch(self) -> dict[str, bool]:
        self.cancel_requested = True
        return {"ok": True}

    def engine_status(self) -> dict[str, Any]:
        return {
            "exists": self.processor.binary.is_file(),
            "path": str(self.processor.binary),
        }


APP_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Pixel Forge</title>
  <style>
    :root {
      color-scheme: dark;
      font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      --bg: #0b0d12;
      --panel: #11141b;
      --panel-2: #171b24;
      --panel-3: #1d222d;
      --border: #252b37;
      --border-strong: #343c4b;
      --text: #fafafa;
      --muted: #fafafa;
      --accent: #6d73ff;
      --accent-hover: #7d82ff;
      --danger: #d85f72;
    }

    * { box-sizing: border-box; }
    html, body { width: 100%; height: 100%; margin: 0; overflow: hidden; }
    body { background: var(--bg); color: var(--text); }
    button, input { font: inherit; color: var(--text); }

    .shell {
      height: 100%;
      display: grid;
      grid-template-rows: 72px minmax(0, 1fr);
    }

    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      min-width: 0;
      padding: 0 18px;
      border-bottom: 1px solid var(--border);
      background: #0f1219;
    }

    .brand { display: flex; align-items: center; gap: 12px; min-width: 0; }
    .brand-mark {
      position: relative;
      width: 42px;
      height: 42px;
      flex: 0 0 auto;
      overflow: hidden;
      border-radius: 12px;
      background:
        linear-gradient(135deg, #7b80ff 0%, #6269f3 58%, #555de0 100%);
      color: #fafafa;
      isolation: isolate;
    }

    .brand-glyph {
      position: absolute;
      inset: 0;
      display: grid;
      place-items: center;
      color: #fafafa;
      font-size: 13px;
      font-weight: 900;
      letter-spacing: -1px;
      line-height: 1;
      user-select: none;
      pointer-events: none;
    }

    /* Левая половина — слегка размытое "до". */
    .brand-glyph-blur {
      clip-path: inset(0 50% 0 0);
      filter: blur(1.15px);
      opacity: .88;
      transform: scale(1.035);
    }

    /* Правая половина — резкое пиксельное "после". */
    .brand-glyph-pixel {
      clip-path: inset(0 0 0 50%);
      font-family: "Courier New", ui-monospace, monospace;
      font-weight: 900;
      letter-spacing: -1.5px;
      text-rendering: geometricPrecision;
      -webkit-font-smoothing: none;
      image-rendering: pixelated;
      transform: translateX(.25px);
    }

    .brand-glyph-pixel::after {
      content: "";
      position: absolute;
      inset: 11px 7px 10px 22px;
      opacity: .2;
      background-image:
        linear-gradient(to right, rgba(255,255,255,.45) 1px, transparent 1px),
        linear-gradient(to bottom, rgba(255,255,255,.45) 1px, transparent 1px);
      background-size: 3px 3px;
      mix-blend-mode: screen;
      pointer-events: none;
    }

    .brand-divider {
      position: absolute;
      z-index: 3;
      top: 8px;
      bottom: 8px;
      left: 50%;
      width: 1px;
      background: rgba(255,255,255,.22);
      transform: translateX(-.5px);
      pointer-events: none;
    }
    .brand h1 { margin: 0; font-size: 18px; color: #fafafa; }
    .brand p { margin: 3px 0 0; font-size: 11px; color: #fafafa; opacity: .72; }

    .top-actions {
      display: flex;
      flex-wrap: nowrap;
      align-items: center;
      justify-content: flex-end;
      gap: 8px;
      min-width: 0;
    }

    button {
      border: 1px solid var(--border-strong);
      border-radius: 10px;
      background: var(--panel-3);
      padding: 9px 12px;
      white-space: nowrap;
      cursor: pointer;
      transition: background .15s ease, border-color .15s ease;
    }
    button:hover:not(:disabled) { background: #272e3b; }
    button:disabled { opacity: .38; cursor: default; }
    button.primary { background: var(--accent); border-color: var(--accent); color: #fafafa; }
    button.primary:hover:not(:disabled) { background: var(--accent-hover); }
    button.secondary { background: var(--panel-2); }
    button.ghost { background: transparent; }
    button.danger { background: #351c23; border-color: #683744; }
    button.full { width: 100%; }

    .workspace {
      min-height: 0;
      display: grid;
      grid-template-columns: 340px minmax(0, 1fr);
    }

    .sidebar {
      min-height: 0;
      display: flex;
      flex-direction: column;
      border-right: 1px solid var(--border);
      background: var(--panel);
      overflow: hidden;
    }

    .source-card {
      position: relative;
      flex: 0 0 auto;
      padding: 15px 14px 10px;
    }

    .section-label {
      margin-bottom: 7px;
      color: #fafafa;
      opacity: .66;
      font-size: 9px;
      text-transform: uppercase;
      letter-spacing: .8px;
    }

    .file-picker {
      width: 100%;
      display: grid;
      grid-template-columns: 38px minmax(0, 1fr);
      align-items: center;
      gap: 9px;
      text-align: left;
      padding: 9px;
      background: var(--panel-2);
    }

    .file-icon {
      width: 38px; height: 38px;
      display: grid; place-items: center;
      border-radius: 9px;
      background: #292f3c;
      color: #fafafa;
      font-size: 9px; font-weight: 800;
    }

    .file-meta { min-width: 0; }
    .file-meta strong, .file-meta span {
      display: block;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: #fafafa;
    }
    .file-meta strong { font-size: 12px; }
    .file-meta span { margin-top: 3px; font-size: 9px; opacity: .66; }

    .floating-menu {
      position: absolute;
      z-index: 50;
      left: 14px; right: 14px; top: 88px;
      max-height: 240px;
      overflow-y: auto;
      padding: 5px;
      border: 1px solid var(--border-strong);
      border-radius: 10px;
      background: #191d26;
      box-shadow: 0 18px 50px rgba(0,0,0,.45);
    }
    .menu-item {
      padding: 8px 9px;
      border-radius: 7px;
      color: #fafafa;
      font-size: 11px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      cursor: pointer;
    }
    .archive-browser {
      flex: 0 0 auto;
      margin: 0 14px 10px;
      overflow: hidden;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: #0f1218;
    }

    .archive-browser.collapsed .archive-tree {
      display: none;
    }

    .archive-browser-header {
      min-height: 42px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 8px 9px 8px 11px;
      border-bottom: 1px solid var(--border);
    }

    .archive-browser.collapsed .archive-browser-header {
      border-bottom: 0;
    }

    .archive-browser-header strong,
    .archive-browser-header span {
      display: block;
      color: #fafafa;
    }

    .archive-browser-header strong {
      font-size: 10px;
    }

    .archive-browser-header span {
      margin-top: 3px;
      opacity: .58;
      font-size: 9px;
    }

    .archive-browser-collapse {
      width: 25px;
      height: 25px;
      display: grid;
      place-items: center;
      flex: 0 0 auto;
      padding: 0;
      border: 0;
      border-radius: 7px;
      background: transparent;
      color: #fafafa;
      opacity: .65;
      font-size: 14px;
    }

    .archive-tree {
      max-height: 190px;
      overflow: auto;
      padding: 5px;
    }

    .tree-row {
      min-width: 0;
      height: 28px;
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 0 7px;
      border-radius: 7px;
      color: #fafafa;
      font-size: 10px;
      cursor: pointer;
      user-select: none;
    }

    .tree-row:hover {
      background: #1b202a;
    }

    .tree-row.active {
      background: #292f3c;
    }

    .tree-indent {
      width: calc(var(--tree-depth, 0) * 14px);
      flex: 0 0 auto;
    }

    .tree-chevron {
      width: 12px;
      flex: 0 0 12px;
      color: #fafafa;
      opacity: .55;
      text-align: center;
      transition: transform .14s ease;
    }

    .tree-folder.open > .tree-row .tree-chevron {
      transform: rotate(90deg);
    }

    .tree-icon {
      width: 14px;
      flex: 0 0 14px;
      opacity: .72;
      text-align: center;
    }

    .tree-name {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .tree-children {
      display: none;
    }

    .tree-folder.open > .tree-children {
      display: block;
    }

    .tool-tabs {
      flex: 0 0 auto;
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 3px;
      padding: 0 14px 10px;
    }
    .tool-tab {
      min-width: 0;
      display: grid;
      gap: 3px;
      justify-items: center;
      padding: 8px 2px;
      border-color: transparent;
      background: transparent;
      color: #fafafa;
      opacity: .62;
      font-size: 9px;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .tool-tab span { font-size: 14px; }
    .tool-tab.active {
      opacity: 1;
      background: var(--panel-3);
      border-color: var(--border);
    }

    .tool-panels {
      flex: 1 1 auto;
      min-height: 0;
      overflow-y: auto;
      padding: 0 13px 14px;
    }
    .tool-panel { display: none; }
    .tool-panel.active { display: block; }

    .panel-title { margin: 8px 0 22px; }
    .panel-title strong, .panel-title span { display: block; color: #fafafa; }
    .panel-title strong { font-size: 14px; }
    .panel-title span { margin-top: 6px; opacity: .66; font-size: 9px; line-height: 1.45; }


    .custom-select {
      position: relative;
      width: 100%;
    }

    .custom-select-trigger {
      width: 100%;
      height: 38px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 0 10px;
      border: 1px solid var(--border-strong);
      border-radius: 8px;
      background: #0f1218;
      color: #fafafa;
      text-align: left;
    }

    .custom-select-trigger:hover {
      background: #151a23;
    }

    .custom-select-caret {
      color: #fafafa;
      opacity: .65;
      transition: transform .14s ease;
    }

    .custom-select.open .custom-select-caret {
      transform: rotate(180deg);
    }

    .custom-select-menu {
      position: absolute;
      z-index: 700;
      left: 0;
      right: 0;
      top: calc(100% + 6px);
      display: grid;
      gap: 3px;
      padding: 5px;
      border: 1px solid #3a4352;
      border-radius: 10px;
      background: #181d26;
      box-shadow: 0 18px 50px rgba(0, 0, 0, .5);
    }

    .custom-select-menu button {
      width: 100%;
      min-height: 34px;
      padding: 7px 9px;
      border: 0;
      border-radius: 7px;
      background: transparent;
      color: #fafafa;
      text-align: left;
    }

    .custom-select-menu button:hover,
    .custom-select-menu button.selected {
      background: #292f3c;
    }

    .field {
      display: grid;
      gap: 8px;
      margin-bottom: 16px;
      color: #fafafa;
      font-size: 11px;
    }
    .field-heading { display: flex; justify-content: space-between; align-items: center; }
    .field-grid { display: grid; grid-template-columns: minmax(0,1fr) minmax(0,1fr); gap: 10px; }

    input {
      width: 100%;
      min-width: 0;
      border: 1px solid var(--border-strong);
      border-radius: 8px;
      background: #0f1218;
      color: #fafafa;
      height: 38px;
      padding: 0 10px;
      outline: none;
    }
    input:focus, select:focus { border-color: var(--accent); }
    input[type="range"] { padding: 0; accent-color: var(--accent); }


    #colors {
      -moz-appearance: textfield;
    }

    #colors::-webkit-outer-spin-button,
    #colors::-webkit-inner-spin-button {
      margin: 0;
      -webkit-appearance: none;
    }

    .switch-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      min-height: 70px;
      margin-bottom: 16px;
      padding: 12px;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: var(--panel-2);
    }
    .switch-row strong, .switch-row span { display: block; color: #fafafa; }
    .switch-row strong { font-size: 11px; line-height: 1.35; }
    .switch-row span { margin-top: 5px; opacity: .65; font-size: 9px; line-height: 1.4; }
    .switch-row input { flex: 0 0 auto; }


    .switch-control {
      position: relative;
      width: 38px !important;
      height: 22px !important;
      flex: 0 0 38px !important;
      appearance: none;
      -webkit-appearance: none;
      border: 1px solid #414a5a !important;
      border-radius: 999px !important;
      background: #252b36 !important;
      padding: 0 !important;
      cursor: pointer;
      transition: background .16s ease, border-color .16s ease;
    }

    .switch-control::after {
      content: "";
      position: absolute;
      top: 3px;
      left: 3px;
      width: 14px;
      height: 14px;
      border-radius: 50%;
      background: #fafafa;
      box-shadow: 0 1px 3px rgba(0, 0, 0, .35);
      transition: transform .16s ease;
    }

    .switch-control:checked {
      border-color: var(--accent) !important;
      background: var(--accent) !important;
    }

    .switch-control:checked::after {
      transform: translateX(16px);
    }

    .switch-control:focus-visible {
      outline: 2px solid rgba(109, 115, 255, .65);
      outline-offset: 2px;
    }

    .sidebar-footer {
      flex: 0 0 auto;
      display: grid;
      gap: 7px;
      padding: 12px 13px 14px;
      border-top: 1px solid var(--border);
      background: #0f1218;
    }

    .stage {
      min-width: 0; min-height: 0;
      display: grid;
      grid-template-rows: 68px minmax(0, 1fr) auto 4px;
      background: #0c0f15;
    }

    .stage-toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 15px;
      min-width: 0;
      padding: 0 17px;
      border-bottom: 1px solid var(--border);
    }
    .stage-toolbar > div:first-child {
      min-width: 0;
      flex: 1 1 auto;
      overflow: hidden;
    }
    .stage-toolbar strong, .stage-toolbar span {
      display: block;
      color: #fafafa;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .stage-toolbar strong { font-size: 13px; }
    .stage-toolbar span { margin-top: 3px; opacity: .65; font-size: 9px; }



    .control-group {
      display: grid;
      grid-template-rows: 12px 22px;
      gap: 8px;
      align-items: start;
    }
    .control-group:not(.preview-grid-group){justify-items:start;}

    .control-group-label {
      height: 12px;
      padding-left: 2px;
      color: #fafafa;
      opacity: .68;
      font-size: 10px;
      font-weight: 600;
      text-align: left;
      line-height: 12px;
    }

    .stage-controls {
      flex: 0 0 auto;
      min-width: 360px;
      display: flex;
      align-items: flex-start;
      gap: 14px;
      padding-top: 2px;
    }

    .preview-scale-switch {
      display: flex;
      align-items: center;
      gap: 3px;
      height: 22px;
      padding: 0;
      border: 0;
      border-radius: 0;
      background: transparent;
    }

    .preview-scale-switch button {
      height: 22px;
      display: flex;
      align-items: center;
      justify-content: center;
      border: 0;
      padding: 0 6px;
      background: transparent;
      color: #fafafa;
      opacity: .6;
      font-size: 9px;
      line-height: 1;
    }

    .preview-scale-switch button {
      border-radius: 7px;
    }

    .preview-scale-switch button.active {
      background: var(--panel-3);
      opacity: 1;
    }

    .view-switch {
      flex: 0 0 auto;
      display: flex;
      align-items: center;
      gap: 3px;
      height: 22px;
      padding: 0;
      border: 0;
      border-radius: 0;
      background: transparent;
    }
    .view-switch button {
      height: 22px;
      display: flex;
      align-items: center;
      justify-content: center;
      border: 0;
      padding: 0 6px;
      background: transparent;
      color: #fafafa;
      opacity: .6;
      font-size: 9px;
      line-height: 1;
    }
    .view-switch button {
      border-radius: 7px;
    }

    .view-switch button.active {
      background: var(--panel-3);
      opacity: 1;
    }


    .preview-grid-group {
      justify-items: start;
    }

    .preview-grid-toggle {
      display: flex;
      align-items: center;
      justify-content: flex-start;
      width: 38px;
      height: 22px;
      padding: 0;
      margin: 0;
      border: 0;
      background: transparent;
    }

    .preview-grid-toggle .switch-control {
      align-self: center;
    }

    .preview-grid-toggle .switch-control {
      width: 34px !important;
      height: 20px !important;
      flex-basis: 34px !important;
      margin: 0;
    }

    .preview-grid-toggle .switch-control::after {
      width: 12px;
      height: 12px;
    }

    .preview-grid-toggle .switch-control:checked::after {
      transform: translateX(14px);
    }

    .pixel-grid-overlay {
      position: absolute;
      z-index: 20;
      display: none;
      pointer-events: none;
      box-sizing: border-box;
      background-image:
        linear-gradient(to right, rgba(20, 24, 31, .42) 1px, transparent 1px),
        linear-gradient(to bottom, rgba(20, 24, 31, .42) 1px, transparent 1px);
      background-repeat: repeat;
      transform-origin: top left;
    }

    .preview-area.show-grid .pixel-grid-overlay.grid-visible {
      display: block;
    }

    .drop-zone { min-height: 0; padding: 16px; overflow: hidden; }
    .drop-zone.empty { display: grid; place-items: center; }

    .empty-state { text-align: center; color: #fafafa; }
    .drop-icon {
      width: 66px; height: 66px;
      display: grid; place-items: center;
      margin: 0 auto 14px;
      border: 1px dashed #495164;
      border-radius: 18px;
      background: var(--panel);
      color: #fafafa;
      font-size: 25px;
    }
    .empty-state h2 { margin: 0; color: #fafafa; font-size: 18px; }
    .empty-state p { margin: 7px 0 17px; color: #fafafa; opacity: .66; font-size: 11px; }
    .empty-actions { display: flex; justify-content: center; gap: 8px; }

    .preview-area {
      height: 100%;
      min-height: 0;
      display: grid;
      grid-template-columns: minmax(0,1fr) minmax(0,1fr);
      gap: 12px;
    }
    .preview-area.view-before, .preview-area.view-after { grid-template-columns: minmax(0,1fr); }
    .preview-area.view-before .after-card { display: none; }
    .preview-area.view-after .before-card { display: none; }

    .canvas-card {
      min-width: 0; min-height: 0;
      display: grid;
      grid-template-rows: 42px minmax(0,1fr);
      overflow: hidden;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: var(--panel);
    }
    .canvas-label {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 0 11px;
      border-bottom: 1px solid var(--border);
      color: #fafafa;
      font-size: 10px;
    }

    .canvas-label > span {
      opacity: .72;
      flex: 0 0 auto;
    }

    .canvas-zoom {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 8px;
      min-width: 0;
    }

    .canvas-zoom input[type="range"] {
      width: 112px;
      height: 4px;
      padding: 0;
      border: 0;
      border-radius: 999px;
      background: transparent;
      accent-color: var(--accent);
    }

    .canvas-zoom output {
      width: 38px;
      color: #fafafa;
      opacity: .72;
      font-size: 9px;
      font-variant-numeric: tabular-nums;
      text-align: right;
    }
    .checkerboard {
      position: relative;
      min-height: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      overflow: auto;
      padding: 18px;
      background-color: #d1d1d1;
      background-image:
        linear-gradient(45deg,#b7b7b7 25%,transparent 25%),
        linear-gradient(-45deg,#b7b7b7 25%,transparent 25%),
        linear-gradient(45deg,transparent 75%,#b7b7b7 75%),
        linear-gradient(-45deg,transparent 75%,#b7b7b7 75%);
      background-size: 18px 18px;
      background-position: 0 0,0 9px,9px -9px,-9px 0;
    }
    .checkerboard img {
      display: block;
      flex: 0 0 auto;
      width: auto;
      height: auto;
      max-width: none;
      max-height: none;
      object-fit: contain;
      image-rendering: pixelated;
    }
    .result-placeholder {
      position: absolute;
      inset: 0;
      display: grid;
      place-items: center;
      background: rgba(225,225,225,.45);
      color: #20242c;
      font-size: 11px;
    }

    .statusbar {
      display: flex;
      justify-content: space-between;
      gap: 15px;
      padding: 9px 16px;
      border-top: 1px solid var(--border);
      color: #fafafa;
      font-size: 10px;
    }
    .status-left {
      display: flex;
      align-items: center;
      min-width: 0;
      flex: 1 1 auto;
      gap: 7px;
      overflow: hidden;
    }

    #status {
      display: block;
      min-width: 0;
      max-width: 100%;
      overflow: hidden;
      white-space: nowrap;
    }
    .status-right { flex: 0 0 auto; white-space: nowrap; opacity: .66; }
    .status-dot { width: 7px; height: 7px; flex: 0 0 auto; border-radius: 50%; background: #677082; }
    .status-dot.ready { background: #65c88b; }
    .status-dot.busy { background: #e4b95f; }
    .status-dot.error { background: #e56d7f; }
    .progress-track { background: #202530; overflow: hidden; }
    #progressBar { width: 0; height: 100%; background: var(--accent); transition: width .17s ease; }


    .tooltip-target {
      position: relative;
      display: inline-block;
      width: max-content;
      max-width: 100%;
      cursor: default;
    }


    .floating-tooltip {
      position: fixed;
      z-index: 9999;
      width: 270px;
      padding: 11px 12px;
      border: 1px solid #3b4453;
      border-radius: 10px;
      background: #1b202a;
      color: #fafafa;
      box-shadow: 0 16px 44px rgba(0, 0, 0, .52);
      font-size: 11px;
      font-weight: 400;
      line-height: 1.45;
      pointer-events: none;
    }

    .number-stepper {
      display: grid;
      grid-template-columns: 38px minmax(0, 1fr) 38px;
      align-items: stretch;
      width: 100%;
      height: 38px;
      border: 1px solid var(--border-strong);
      border-radius: 8px;
      overflow: hidden;
      background: #0f1218;
    }

    .number-stepper button {
      min-width: 0;
      padding: 0;
      border: 0;
      border-radius: 0;
      background: #191e28;
      color: #fafafa;
      font-size: 17px;
      line-height: 1;
    }

    .number-stepper button:first-child {
      border-right: 1px solid var(--border-strong);
    }

    .number-stepper button:last-child {
      border-left: 1px solid var(--border-strong);
    }

    .number-stepper input {
      border: 0;
      border-radius: 0;
      text-align: center;
      -moz-appearance: textfield;
    }

    .number-stepper input::-webkit-outer-spin-button,
    .number-stepper input::-webkit-inner-spin-button {
      margin: 0;
      -webkit-appearance: none;
    }

    .number-stepper.disabled {
      opacity: .45;
      pointer-events: none;
    }


    .switch-row > div {
      min-width: 0;
      flex: 1 1 auto;
    }

    .switch-row .label-with-help {
      width: 100%;
    }

    .tool-panel {
      padding-top: 2px;
    }

    .tool-panel > :last-child {
      margin-bottom: 0;
    }

    .hidden { display: none !important; }

    @media (max-width: 1030px) {
      .workspace { grid-template-columns: 320px minmax(0,1fr); }
      .topbar { padding-inline: 14px; }
      .top-actions button { padding-inline: 10px; }
      .brand p { display: none; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <div class="brand">
        <div class="brand-mark" aria-label="Pixel Forge">
          <span class="brand-glyph brand-glyph-blur">PF</span>
          <span class="brand-glyph brand-glyph-pixel">PF</span>
          <span class="brand-divider"></span>
        </div>
        <div>
          <h1>Pixel Forge</h1>
          <p>Подготовка пиксельных ассетов</p>
        </div>
      </div>

      <div class="top-actions">
        <button class="ghost" id="openImageBtn">Изображение</button>
        <button class="ghost" id="openArchiveBtn">ZIP-архив</button>
      </div>
    </header>

    <section class="workspace">
      <aside class="sidebar">
        <div class="source-card">
          <div class="section-label">Источник</div>
          <button class="file-picker" id="filePickerBtn" disabled>
            <div class="file-icon" id="sourceKindIcon">PNG</div>
            <div class="file-meta">
              <strong id="selectedFileName">Файл не выбран</strong>
              <span id="selectedFileMeta">Откройте изображение или ZIP</span>
            </div>
          </button>
        </div>

        <section id="archiveBrowser" class="archive-browser hidden">
          <div class="archive-browser-header">
            <div>
              <strong>Содержимое архива</strong>
              <span id="archiveSummary">0 изображений</span>
            </div>
            <button id="collapseArchiveBrowserBtn" class="archive-browser-collapse" type="button" aria-label="Свернуть список">−</button>
          </div>
          <div id="archiveTree" class="archive-tree"></div>
        </section>

        <nav class="tool-tabs">
          <button class="tool-tab active" data-panel="scalePanel"><span>⌗</span>Масштаб</button>
          <button class="tool-tab" data-panel="palettePanel"><span>◈</span>Палитра</button>
          <button class="tool-tab" data-panel="cleanupPanel"><span>✦</span>Очистка</button>
          <button class="tool-tab" data-panel="alphaPanel"><span>◐</span>Прозрачность</button>
        </nav>

        <div class="tool-panels">
          <section id="scalePanel" class="tool-panel active">
            <div class="panel-title">
              <strong>Масштабирование</strong>
              <span>Восстановление исходной пиксельной сетки</span>
            </div>

            <label class="field">
              <span class="tooltip-target" data-tooltip="В ручном режиме используется заданный Pixel size. Автоматические режимы пытаются определить размер увеличенного пикселя по структуре изображения.">Определение масштаба</span>
              <div class="custom-select" data-select-id="detectMethod">
                <button class="custom-select-trigger" type="button">
                  <span class="custom-select-value">Вручную</span>
                  <span class="custom-select-caret">⌄</span>
                </button>
                <div class="custom-select-menu hidden">
                  <button class="method-tooltip" type="button" data-value="manual" data-tooltip="Использует Pixel size, заданный вручную. Самый предсказуемый вариант для изображений с известным коэффициентом увеличения.">Вручную</button>
                  <button class="method-tooltip" type="button" data-value="auto" data-tooltip="Пытается автоматически определить размер увеличенного пикселя, анализируя изображение несколькими способами.">Автоматически</button>
                  <button class="method-tooltip" type="button" data-value="runs" data-tooltip="Оценивает масштаб по длинам повторяющихся цветовых участков. Хорошо работает на чистой пиксельной графике.">Runs-based</button>
                  <button class="method-tooltip" type="button" data-value="edge" data-tooltip="Определяет масштаб по структуре контуров и переходов цвета. Может быть полезен для изображений со сложными границами.">Edge-aware</button>
                </div>
                <input id="detectMethod" type="hidden" value="manual">
              </div>
            </label>

            <div class="field-grid">
              <label class="field">
                <span class="tooltip-target" data-tooltip="Во сколько раз изображение было искусственно увеличено. Значение 2 уменьшит ширину и высоту вдвое, 3 — втрое.">Pixel size</span>
                <div id="scaleStepper" class="number-stepper">
                  <button id="scaleMinus" type="button" aria-label="Уменьшить Pixel size">−</button>
                  <input id="scale" type="number" min="1" max="32" value="2">
                  <button id="scalePlus" type="button" aria-label="Увеличить Pixel size">+</button>
                </div>
              </label>
              <label class="field">
                <span class="tooltip-target" data-tooltip="Определяет, как цвет итогового пикселя выбирается из блока исходных пикселей. Nearest сохраняет один ближайший пиксель; Dominant и QVote лучше учитывают преобладающие цвета.">Метод</span>
                <div class="custom-select" data-select-id="method">
                  <button class="custom-select-trigger" type="button">
                    <span class="custom-select-value">Nearest</span>
                    <span class="custom-select-caret">⌄</span>
                  </button>
                  <div class="custom-select-menu hidden">
                    <button class="method-tooltip" type="button" data-value="nearest" data-tooltip="Берёт ближайший пиксель из каждого блока. Быстро и предсказуемо, но может сохранять случайный цвет блока.">Nearest</button>
                    <button class="method-tooltip" type="button" data-value="dominant" data-tooltip="Выбирает преобладающий цвет блока. Часто лучше сохраняет крупные однородные области.">Dominant</button>
                    <button class="method-tooltip" type="button" data-value="median" data-tooltip="Использует медианный цвет блока и устойчив к единичному цветовому шуму.">Median</button>
                    <button class="method-tooltip" type="button" data-value="mode" data-tooltip="Выбирает цвет, который встречается в блоке чаще всего. Подходит для ограниченных палитр.">Mode</button>
                    <button class="method-tooltip" type="button" data-value="qvote" data-tooltip="Квантизует цвета блока и выбирает результат голосованием. Полезен для сложных или слегка испорченных изображений.">QVote</button>
                  </div>
                  <input id="method" type="hidden" value="nearest">
                </div>
              </label>
            </div>

            <label class="switch-row">
              <div>
                <strong class="tooltip-target" data-tooltip="Привязывает выбор блоков к простой равномерной сетке. Полезно, когда изображение увеличено целым коэффициентом без смещения.">Simple Grid</strong>
                <span>Выравнивать пиксельную сетку</span>
              </div>
              <input id="snapGrid" class="switch-control" type="checkbox" checked>
            </label>
          </section>

          <section id="palettePanel" class="tool-panel">
            <div class="panel-title">
              <strong>Палитра</strong>
              <span>Сокращение количества оттенков</span>
            </div>
            <label class="switch-row">
              <div>
                <strong class="tooltip-target" data-tooltip="Сокращает количество уникальных цветов. Чем меньше лимит, тем компактнее и стилизованнее палитра, но тем выше риск потери оттенков.">Quantize</strong>
                <span>Ограничить цветовую палитру</span>
              </div>
              <input id="quantize" class="switch-control" type="checkbox" checked>
            </label>
            <label class="field">
              <span class="tooltip-target" data-tooltip="Верхний предел числа цветов после квантизации. 256 почти не ограничивает палитру; 16–64 заметно упрощают её.">Максимум цветов</span>
              <input id="colors" type="number" min="2" max="256" value="256">
            </label>
          </section>

          <section id="cleanupPanel" class="tool-panel">
            <div class="panel-title">
              <strong>Очистка артефактов</strong>
              <span>Удаление шумов и неровных краёв</span>
            </div>
            <label class="switch-row">
              <div>
                <strong class="tooltip-target" data-tooltip="Закрывает небольшие отверстия и разрывы в альфа-маске. Может помочь с шумом, но иногда утолщает мелкие детали.">Morphological cleanup</strong>
                <span>Закрыть мелкие разрывы в маске</span>
              </div>
              <input id="morphCleanup" class="switch-control" type="checkbox">
            </label>
            <label class="switch-row">
              <div>
                <strong class="tooltip-target" data-tooltip="Удаляет отдельные пиксели, создающие случайные ступеньки на контурах. Для намеренно неровного пиксель-арта лучше оставлять выключенным.">Jaggy cleanup</strong>
                <span>Сгладить единичные ступеньки</span>
              </div>
              <input id="jaggyCleanup" class="switch-control" type="checkbox">
            </label>
          </section>

          <section id="alphaPanel" class="tool-panel">
            <div class="panel-title">
              <strong>Прозрачность</strong>
              <span>Очистка фона и альфа-канала</span>
            </div>
            <label class="switch-row">
              <div>
                <strong class="tooltip-target" data-tooltip="Убирает полупрозрачные пиксели: каждый становится либо полностью прозрачным, либо полностью непрозрачным.">Alpha binarization</strong>
                <span>Оставить только 0% и 100%</span>
              </div>
              <input id="alphaBinarize" class="switch-control" type="checkbox" checked>
            </label>
            <label class="field">
              <div class="field-heading">
                <span class="tooltip-target" data-tooltip="Пиксели с прозрачностью ниже порога удаляются, а выше или равной порогу становятся непрозрачными.">Порог Alpha</span>
                <output id="alphaValue">128</output>
              </div>
              <input id="alphaThreshold" type="range" min="0" max="255" value="128">
            </label>
            <label class="switch-row">
              <div>
                <strong class="tooltip-target" data-tooltip="Определяет цвет по углам изображения и удаляет связанную с краями область похожего цвета. Не затрагивает изолированные области внутри объекта.">Удалить фон</strong>
                <span>Заменить связанный фон по краям</span>
              </div>
              <input id="transparentBackground" class="switch-control" type="checkbox">
            </label>
            <label class="field">
              <div class="field-heading">
                <span class="tooltip-target" data-tooltip="Определяет, насколько цвет может отличаться от фонового и всё ещё считаться фоном. Большие значения удаляют более неоднородный фон.">Допуск цвета</span>
                <output id="backgroundValue">10</output>
              </div>
              <input id="backgroundTolerance" type="range" min="0" max="100" value="10">
            </label>
          </section>
        </div>

        <div class="sidebar-footer">
          <button class="primary full" id="previewBtn" disabled>Обновить предпросмотр</button>
          <button class="secondary full" id="savePngBtn" disabled>Обработать PNG</button>
          <button class="secondary full" id="saveZipBtn" disabled>Обработать весь ZIP</button>
          <button class="danger full hidden" id="cancelBtn">Остановить обработку</button>
        </div>
      </aside>

      <section class="stage">
        <div class="stage-toolbar">
          <div>
            <strong id="stageFileName">Предпросмотр</strong>
            <span id="stageDimensions">Файл не выбран</span>
          </div>
          <div class="stage-controls">
            <div class="control-group preview-grid-group">
              <span class="control-group-label">Сетка</span>
              <label class="preview-grid-toggle" title="Показать пиксельную сетку поверх предпросмотра">
                <input id="previewGrid" class="switch-control" type="checkbox">
              </label>
            </div>
            <div class="control-group">
              <span class="control-group-label">Масштаб</span>
              <div class="preview-scale-switch" aria-label="Масштаб предпросмотра">
                <button class="active" data-preview-scale="match">Сравнять</button>
                <button data-preview-scale="actual">100%</button>
              </div>
            </div>
            <div class="control-group">
              <span class="control-group-label">Режим</span>
              <div class="view-switch">
                <button class="active" data-view="split">Сравнение</button>
                <button data-view="before">Исходник</button>
                <button data-view="after">Результат</button>
              </div>
            </div>
          </div>
        </div>

        <div id="dropZone" class="drop-zone empty">
          <div id="emptyState" class="empty-state">
            <div class="drop-icon">▧</div>
            <h2>Откройте изображение или ZIP</h2>
            <p>Файл будет выбран через стандартное окно macOS</p>
            <div class="empty-actions">
              <button class="primary" id="emptyImageBtn">Открыть изображение</button>
              <button class="secondary" id="emptyArchiveBtn">Открыть ZIP</button>
            </div>
          </div>

          <div id="previewArea" class="preview-area hidden">
            <div class="canvas-card before-card">
              <div class="canvas-label">
                <span>До</span>
                <div class="canvas-zoom">
                  <input id="beforeZoom" type="range" min="25" max="400" value="100" step="5">
                  <output id="beforeZoomValue">100%</output>
                </div>
              </div>
              <div class="checkerboard"><img id="originalImage"><div id="beforeGridOverlay" class="pixel-grid-overlay"></div></div>
            </div>
            <div class="canvas-card after-card">
              <div class="canvas-label">
                <span>После</span>
                <div class="canvas-zoom">
                  <input id="afterZoom" type="range" min="25" max="400" value="100" step="5">
                  <output id="afterZoomValue">100%</output>
                </div>
              </div>
              <div class="checkerboard">
                <img id="resultImage">
                <div id="afterGridOverlay" class="pixel-grid-overlay"></div>
                <div id="resultPlaceholder" class="result-placeholder">Нажмите «Обновить предпросмотр»</div>
              </div>
            </div>
          </div>
        </div>

        <footer class="statusbar">
          <div class="status-left">
            <span id="statusDot" class="status-dot"></span>
            <span id="status">Готово к работе</span>
          </div>
          <div class="status-right"><span id="engineStatus">Проверка движка…</span></div>
        </footer>
        <div class="progress-track"><div id="progressBar"></div></div>
      </section>
    </section>
  </main>

  <script>
    const state = {
      loaded: false,
      kind: null,
      index: 0,
      files: [],
      previewScale: "match",
      previewGrid: false,
      beforeZoom: 100,
      afterZoom: 100,
      busy: false,
      archiveTreeCollapsed: false,
      sourceRevision: 0
    };
    const $ = (id) => document.getElementById(id);

    window.addEventListener("error", event => {
      const message = event?.error?.message || event?.message || "Неизвестная ошибка интерфейса";
      setStatus("Ошибка интерфейса: " + message, "error");
    });

    window.addEventListener("unhandledrejection", event => {
      const reason = event?.reason?.message || event?.reason || "Неизвестная ошибка";
      setStatus("Ошибка операции: " + reason, "error");
      event.preventDefault();
    });

    window.addEventListener("pywebviewready", async () => {
      bindEvents();
      const engine = await pywebview.api.engine_status();
      $("engineStatus").textContent = engine.exists ? "Движок unfake подключён" : "Движок unfake не найден";
      setStatus(engine.exists ? "Готово к работе" : "Движок не найден: " + engine.path, engine.exists ? "ready" : "error");
      syncManualScale();
    });

    function bindEvents() {
      $("openImageBtn").addEventListener("click", chooseImage);
      $("openArchiveBtn").addEventListener("click", chooseArchive);
      $("emptyImageBtn").addEventListener("click", chooseImage);
      $("emptyArchiveBtn").addEventListener("click", chooseArchive);
      $("previewBtn").addEventListener("click", makePreview);
      $("savePngBtn").addEventListener("click", savePng);
      $("saveZipBtn").addEventListener("click", saveZip);
      $("cancelBtn").addEventListener("click", cancelBatch);
      $("filePickerBtn").addEventListener("click", () => {
        if (state.kind === "archive") {
          state.archiveTreeCollapsed = !state.archiveTreeCollapsed;
          $("archiveBrowser").classList.toggle("collapsed", state.archiveTreeCollapsed);
          $("collapseArchiveBrowserBtn").textContent = state.archiveTreeCollapsed ? "+" : "−";
        }
      });
      $("collapseArchiveBrowserBtn").addEventListener("click", event => {
        event.stopPropagation();
        state.archiveTreeCollapsed = !state.archiveTreeCollapsed;
        $("archiveBrowser").classList.toggle("collapsed", state.archiveTreeCollapsed);
        $("collapseArchiveBrowserBtn").textContent = state.archiveTreeCollapsed ? "+" : "−";
      });
      initializeCustomSelects();
      $("scaleMinus").addEventListener("click", () => changeScale(-1));
      $("scalePlus").addEventListener("click", () => changeScale(1));
      $("scale").addEventListener("change", normalizeScale);
      $("alphaThreshold").addEventListener("input", e => $("alphaValue").value = e.target.value);
      $("backgroundTolerance").addEventListener("input", e => $("backgroundValue").value = e.target.value);
      $("previewGrid").addEventListener("change", e => {
        state.previewGrid = e.target.checked;
        $("previewArea").classList.toggle("show-grid", state.previewGrid);
        updatePreviewGrids();
      });
      $("beforeZoom").addEventListener("input", e => {
        state.beforeZoom = Number(e.target.value);
        $("beforeZoomValue").value = `${state.beforeZoom}%`;
        applyPreviewSizing();
      });
      $("afterZoom").addEventListener("input", e => {
        state.afterZoom = Number(e.target.value);
        $("afterZoomValue").value = `${state.afterZoom}%`;
        applyPreviewSizing();
      });
      $("originalImage").addEventListener("load", applyPreviewSizing);
      $("resultImage").addEventListener("load", applyPreviewSizing);
      document.querySelectorAll(".checkerboard").forEach(container =>
        container.addEventListener("scroll", updatePreviewGrids)
      );
      window.addEventListener("resize", () =>
        requestAnimationFrame(updatePreviewGrids)
      );

      document.querySelectorAll(".tool-tab").forEach(button =>
        button.addEventListener("click", () => activateToolPanel(button.dataset.panel))
      );
      document.querySelectorAll(".view-switch button").forEach(button =>
        button.addEventListener("click", () => setPreviewView(button.dataset.view))
      );
      document.querySelectorAll(".preview-scale-switch button").forEach(button =>
        button.addEventListener("click", () => setPreviewScale(button.dataset.previewScale))
      );

      document.addEventListener("click", event => {
        if (!event.target.closest(".custom-select")) closeAllCustomSelects();
      });

      initializeFloatingTooltips();
    }

    function initializeCustomSelects() {
      document.querySelectorAll(".custom-select").forEach(select => {
        const trigger = select.querySelector(".custom-select-trigger");
        const menu = select.querySelector(".custom-select-menu");
        const hiddenInput = select.querySelector('input[type="hidden"]');
        const valueLabel = select.querySelector(".custom-select-value");

        trigger.addEventListener("click", event => {
          event.stopPropagation();
          const willOpen = menu.classList.contains("hidden");
          closeAllCustomSelects();
          if (willOpen) {
            menu.classList.remove("hidden");
            select.classList.add("open");
          }
        });

        menu.querySelectorAll("button[data-value]").forEach(option => {
          option.addEventListener("click", () => {
            hiddenInput.value = option.dataset.value;
            valueLabel.textContent = option.textContent;
            menu.querySelectorAll("button").forEach(item =>
              item.classList.toggle("selected", item === option)
            );
            menu.classList.add("hidden");
            select.classList.remove("open");

            if (hiddenInput.id === "detectMethod") syncManualScale();
          });
        });
      });
    }

    function closeAllCustomSelects() {
      document.querySelectorAll(".custom-select").forEach(select => {
        select.classList.remove("open");
        select.querySelector(".custom-select-menu").classList.add("hidden");
      });
    }

    function initializeFloatingTooltips() {
      let tooltip = document.querySelector(".floating-tooltip");
      if (!tooltip) {
        tooltip = document.createElement("div");
        tooltip.className = "floating-tooltip hidden";
        document.body.appendChild(tooltip);
      }

      document.querySelectorAll(".tooltip-target[data-tooltip], .method-tooltip[data-tooltip]").forEach(target => {
        let tooltipTimer = null;

        target.addEventListener("mouseenter", () => {
          tooltipTimer = window.setTimeout(() => {
            tooltip.textContent = target.dataset.tooltip;
            tooltip.classList.remove("hidden");

            const rect = target.getBoundingClientRect();
            const tooltipWidth = 270;
            const tooltipHeight = tooltip.offsetHeight || 70;

            let left = rect.right + 12;
            let top = rect.top + rect.height / 2 - tooltipHeight / 2;

            if (left + tooltipWidth > window.innerWidth - 12) {
              left = rect.left - tooltipWidth - 12;
            }

            top = Math.max(12, Math.min(top, window.innerHeight - tooltipHeight - 12));
            tooltip.style.left = `${left}px`;
            tooltip.style.top = `${top}px`;
          }, 280);
        });

        target.addEventListener("mouseleave", () => {
          if (tooltipTimer !== null) {
            window.clearTimeout(tooltipTimer);
            tooltipTimer = null;
          }
          tooltip.classList.add("hidden");
        });
      });
    }

    function activateToolPanel(panelId) {
      document.querySelectorAll(".tool-tab").forEach(button =>
        button.classList.toggle("active", button.dataset.panel === panelId)
      );
      document.querySelectorAll(".tool-panel").forEach(panel =>
        panel.classList.toggle("active", panel.id === panelId)
      );
    }

    function setPreviewView(view) {
      document.querySelectorAll(".view-switch button").forEach(button =>
        button.classList.toggle("active", button.dataset.view === view)
      );

      const area = $("previewArea");
      area.classList.remove("view-before", "view-after");

      if (view === "before") area.classList.add("view-before");
      if (view === "after") area.classList.add("view-after");

      // Wait until CSS grid has recalculated the visible preview area.
      requestAnimationFrame(() => requestAnimationFrame(applyPreviewSizing));
    }

    function setPreviewScale(mode) {
      state.previewScale = mode;

      document.querySelectorAll(".preview-scale-switch button").forEach(button =>
        button.classList.toggle("active", button.dataset.previewScale === mode)
      );

      requestAnimationFrame(() => requestAnimationFrame(applyPreviewSizing));
    }

    function visiblePreviewContainers() {
      const beforeCard = document.querySelector(".before-card");
      const afterCard = document.querySelector(".after-card");
      const beforeContainer = beforeCard.querySelector(".checkerboard");
      const afterContainer = afterCard.querySelector(".checkerboard");

      return {
        beforeCard,
        afterCard,
        beforeContainer,
        afterContainer,
        beforeVisible: getComputedStyle(beforeCard).display !== "none",
        afterVisible: getComputedStyle(afterCard).display !== "none"
      };
    }

    function fittedSourceSize(container) {
      const source = $("originalImage");

      if (!source.naturalWidth || !source.naturalHeight || !container) {
        return { width: 1, height: 1 };
      }

      const availableWidth = Math.max(1, container.clientWidth - 36);
      const availableHeight = Math.max(1, container.clientHeight - 36);

      const factor = Math.min(
        availableWidth / source.naturalWidth,
        availableHeight / source.naturalHeight
      );

      return {
        width: Math.max(1, Math.round(source.naturalWidth * factor)),
        height: Math.max(1, Math.round(source.naturalHeight * factor))
      };
    }

    function commonMatchedSize(containers) {
      const candidates = [];

      if (containers.beforeVisible) {
        candidates.push(fittedSourceSize(containers.beforeContainer));
      }

      if (containers.afterVisible) {
        candidates.push(fittedSourceSize(containers.afterContainer));
      }

      if (!candidates.length) {
        return { width: 1, height: 1 };
      }

      // In comparison mode both images must fit into both visible panels.
      return candidates.reduce((smallest, current) => (
        current.width < smallest.width ? current : smallest
      ));
    }

    function applyImageSize(image, baseWidth, baseHeight, zoom) {
      if (!image || !image.naturalWidth) return;

      const width = Math.max(1, Math.round(baseWidth * zoom / 100));
      const height = Math.max(1, Math.round(baseHeight * zoom / 100));

      image.style.width = `${width}px`;
      image.style.height = `${height}px`;
      image.style.visibility = "visible";
    }

    function updateGridForImage(image, overlay) {
      if (!state.previewGrid || !image || !image.naturalWidth || !overlay) {
        if (overlay) {
          overlay.classList.remove("grid-visible");
        }
        return;
      }

      const container = image.closest(".checkerboard");
      const imageRect = image.getBoundingClientRect();
      const containerRect = container.getBoundingClientRect();

      const cellWidth = imageRect.width / image.naturalWidth;
      const cellHeight = imageRect.height / image.naturalHeight;

      if (cellWidth < 2 || cellHeight < 2) {
        overlay.classList.remove("grid-visible");
        return;
      }

      const left = image.offsetLeft;
      const top = image.offsetTop;

      overlay.style.left = `${left}px`;
      overlay.style.top = `${top}px`;
      overlay.style.width = `${image.offsetWidth}px`;
      overlay.style.height = `${image.offsetHeight}px`;
      overlay.style.backgroundSize = `${cellWidth}px ${cellHeight}px`;
      overlay.style.backgroundPosition = "0 0";
      overlay.classList.add("grid-visible");
    }

    function updatePreviewGrids() {
      updateGridForImage($("originalImage"), $("beforeGridOverlay"));
      updateGridForImage($("resultImage"), $("afterGridOverlay"));
    }

    function applyPreviewSizing() {
      const before = $("originalImage");
      const after = $("resultImage");

      if (!before.naturalWidth) return;

      const containers = visiblePreviewContainers();

      if (state.previewScale === "actual") {
        applyImageSize(
          before,
          before.naturalWidth,
          before.naturalHeight,
          state.beforeZoom
        );

        if (after.naturalWidth) {
          applyImageSize(
            after,
            after.naturalWidth,
            after.naturalHeight,
            state.afterZoom
          );
        }

        requestAnimationFrame(updatePreviewGrids);
        return;
      }

      // "Сравнять": the processed image is deliberately rendered at exactly
      // the same base display size as the source, regardless of its smaller
      // native dimensions after downscaling.
      const matched = commonMatchedSize(containers);

      applyImageSize(
        before,
        matched.width,
        matched.height,
        state.beforeZoom
      );

      if (after.naturalWidth) {
        applyImageSize(
          after,
          matched.width,
          matched.height,
          state.afterZoom
        );
      }

      requestAnimationFrame(updatePreviewGrids);
    }

    function currentSettings() {
      return {
        detectMethod: $("detectMethod").value,
        scale: Number($("scale").value),
        colors: Number($("colors").value),
        method: $("method").value,
        snapGrid: $("snapGrid").checked,
        quantize: $("quantize").checked,
        morphCleanup: $("morphCleanup").checked,
        jaggyCleanup: $("jaggyCleanup").checked,
        alphaBinarize: $("alphaBinarize").checked,
        alphaThreshold: Number($("alphaThreshold").value),
        transparentBackground: $("transparentBackground").checked,
        backgroundTolerance: Number($("backgroundTolerance").value)
      };
    }

    function normalizeScale() {
      const input = $("scale");
      const min = Number(input.min) || 1;
      const max = Number(input.max) || 32;
      const value = Number.isFinite(Number(input.value)) ? Number(input.value) : 2;
      input.value = Math.max(min, Math.min(max, Math.round(value)));
    }

    function changeScale(delta) {
      if ($("detectMethod").value !== "manual") return;
      normalizeScale();
      $("scale").value = Math.max(1, Math.min(32, Number($("scale").value) + delta));
    }

    function syncManualScale() {
      const isManual = $("detectMethod").value === "manual";
      $("scale").disabled = !isManual;
      $("scaleStepper").classList.toggle("disabled", !isManual);
    }

    function middleEllipsis(value, maxLength = 56) {
      const text = String(value ?? "");
      if (text.length <= maxLength) return text;

      const remaining = maxLength - 1;
      const leftLength = Math.ceil(remaining / 2);
      const rightLength = Math.floor(remaining / 2);
      return `${text.slice(0, leftLength)}…${text.slice(-rightLength)}`;
    }

    function setBusy(isBusy) {
      state.busy = isBusy;
      [
        "openImageBtn",
        "openArchiveBtn",
        "emptyImageBtn",
        "emptyArchiveBtn",
        "previewBtn",
        "savePngBtn",
        "saveZipBtn",
        "filePickerBtn"
      ].forEach(id => {
        const element = $(id);
        if (!element) return;

        if (id === "saveZipBtn") {
          element.disabled = isBusy || state.kind !== "archive";
        } else if (["previewBtn", "savePngBtn", "filePickerBtn"].includes(id)) {
          element.disabled = isBusy || !state.loaded;
        } else {
          element.disabled = isBusy;
        }
      });
    }

    function setStatus(text, type = "") {
      const status = $("status");
      status.textContent = middleEllipsis(text, 82);
      status.title = text;
      $("statusDot").className = "status-dot" + (type ? " " + type : "");
    }

    function handleError(data) {
      if (data && data.error) {
        setStatus("Ошибка: " + data.error, "error");
        return true;
      }
      return false;
    }

    function resetSourceInterface() {
      state.sourceRevision += 1;
      state.loaded = false;
      state.kind = null;
      state.index = 0;
      state.files = [];

      $("archiveBrowser").classList.add("hidden");
      $("archiveBrowser").classList.remove("collapsed");
      $("archiveTree").innerHTML = "";
      $("archiveSummary").textContent = "0 изображений";
      $("sourceKindIcon").textContent = "PNG";

      $("selectedFileName").textContent = "Файл не выбран";
      $("selectedFileName").title = "";
      $("selectedFileMeta").textContent = "Откройте изображение или ZIP";
      $("stageFileName").textContent = "Предпросмотр";
      $("stageFileName").title = "";
      $("stageDimensions").textContent = "Файл не выбран";

      ["originalImage", "resultImage"].forEach(id => {
        const image = $(id);
        image.removeAttribute("src");
        image.style.width = "";
        image.style.height = "";
        image.style.visibility = id === "resultImage" ? "hidden" : "";
      });

      ["beforeGridOverlay", "afterGridOverlay"].forEach(id => {
        const overlay = $(id);
        overlay.classList.remove("grid-visible");
        overlay.removeAttribute("style");
      });

      document.querySelectorAll(".checkerboard").forEach(container => {
        container.scrollLeft = 0;
        container.scrollTop = 0;
      });

      $("resultPlaceholder").classList.remove("hidden");
      $("progressBar").style.width = "0%";
      $("cancelBtn").classList.add("hidden");

      $("beforeZoom").value = 100;
      $("afterZoom").value = 100;
      $("beforeZoomValue").value = "100%";
      $("afterZoomValue").value = "100%";
      state.beforeZoom = 100;
      state.afterZoom = 100;

      $("dropZone").classList.add("empty");
      $("emptyState").classList.remove("hidden");
      $("previewArea").classList.add("hidden");

      setBusy(false);
    }

    async function chooseImage() {
      if (state.busy) return;
      setBusy(true);
      setStatus("Открытие изображения…", "busy");

      try {
        const data = await pywebview.api.choose_image();
        if (!data || data.cancelled || handleError(data)) return;
        resetSourceInterface();
        applySource(data);
        await makePreview();
      } finally {
        setBusy(false);
      }
    }

    async function chooseArchive() {
      if (state.busy) return;
      setBusy(true);
      setStatus("Открытие ZIP…", "busy");

      try {
        const data = await pywebview.api.choose_archive();
        if (!data || data.cancelled || handleError(data)) return;
        resetSourceInterface();
        applySource(data);
        await makePreview();
      } finally {
        setBusy(false);
      }
    }

    function applySource(data) {
      state.loaded = true;
      state.kind = data.kind;
      state.index = data.index;
      state.files = data.files;

      $("sourceKindIcon").textContent = data.kind === "archive" ? "ZIP" : "PNG";
      $("archiveBrowser").classList.toggle("hidden", data.kind !== "archive");

      if (data.kind === "archive") {
        state.archiveTreeCollapsed = false;
        $("archiveBrowser").classList.remove("collapsed");
        $("collapseArchiveBrowserBtn").textContent = "−";
        $("archiveSummary").textContent = `${data.count} изображений`;
        renderArchiveTree(data.files);
      } else {
        $("archiveTree").innerHTML = "";
      }

      showSource(data);

      $("previewBtn").disabled = false;
      $("savePngBtn").disabled = false;
      $("saveZipBtn").disabled = data.kind !== "archive";
      $("filePickerBtn").disabled = false;
      $("dropZone").classList.remove("empty");
      $("emptyState").classList.add("hidden");
      $("previewArea").classList.remove("hidden");
    }

    function showSource(data) {
      state.index = data.index;
      markActiveArchiveFile(data.index);
      $("selectedFileName").textContent = middleEllipsis(data.fileName, 46);
      $("selectedFileName").title = data.fileName;
      $("selectedFileMeta").textContent = `${data.width} × ${data.height} • ${data.index + 1} из ${data.count}`;
      $("savePngBtn").textContent = data.kind === "archive"
        ? "Обработать выбранный PNG"
        : "Обработать PNG";
      $("stageFileName").textContent = middleEllipsis(data.fileName, 72);
      $("stageFileName").title = data.fileName;
      $("stageDimensions").textContent = `${data.width} × ${data.height}`;
      $("originalImage").style.width = "";
      $("originalImage").style.height = "";
      $("resultImage").style.width = "";
      $("resultImage").style.height = "";
      $("resultImage").style.visibility = "hidden";
      $("originalImage").src = data.original;
      $("resultImage").removeAttribute("src");
      $("beforeZoom").value = 100;
      $("afterZoom").value = 100;
      $("beforeZoomValue").value = "100%";
      $("afterZoomValue").value = "100%";
      state.beforeZoom = 100;
      state.afterZoom = 100;
      $("resultPlaceholder").classList.remove("hidden");
      document.querySelectorAll(".checkerboard").forEach(container => {
        container.scrollLeft = 0;
        container.scrollTop = 0;
      });
      requestAnimationFrame(updatePreviewGrids);
      setStatus(`Загружен файл ${data.index + 1} из ${data.count}`, "ready");
    }

    function buildArchiveTree(files) {
      const root = { folders: new Map(), files: [] };

      files.forEach((path, index) => {
        const parts = path.split("/").filter(Boolean);
        const fileName = parts.pop() || path;
        let node = root;

        parts.forEach(part => {
          if (!node.folders.has(part)) {
            node.folders.set(part, { folders: new Map(), files: [] });
          }
          node = node.folders.get(part);
        });

        node.files.push({ name: fileName, path, index });
      });

      return root;
    }

    function renderArchiveTree(files) {
      const container = $("archiveTree");
      container.innerHTML = "";
      const tree = buildArchiveTree(files);

      function renderNode(node, parent, depth) {
        [...node.folders.entries()]
          .sort(([a], [b]) => a.localeCompare(b, "ru", { numeric: true }))
          .forEach(([name, childNode]) => {
            const folder = document.createElement("div");
            folder.className = "tree-folder open";

            const row = document.createElement("div");
            row.className = "tree-row";
            row.innerHTML = `
              <span class="tree-indent" style="--tree-depth:${depth}"></span>
              <span class="tree-chevron">›</span>
              <span class="tree-icon">▣</span>
              <span class="tree-name"></span>
            `;
            row.querySelector(".tree-name").textContent = name;
            row.title = name;

            const children = document.createElement("div");
            children.className = "tree-children";

            row.addEventListener("click", () => {
              folder.classList.toggle("open");
            });

            folder.append(row, children);
            parent.appendChild(folder);
            renderNode(childNode, children, depth + 1);
          });

        [...node.files]
          .sort((a, b) => a.name.localeCompare(b.name, "ru", { numeric: true }))
          .forEach(file => {
            const row = document.createElement("div");
            row.className = "tree-row tree-file";
            row.dataset.index = String(file.index);
            row.innerHTML = `
              <span class="tree-indent" style="--tree-depth:${depth}"></span>
              <span class="tree-chevron"></span>
              <span class="tree-icon">▧</span>
              <span class="tree-name"></span>
            `;
            row.querySelector(".tree-name").textContent = file.name;
            row.title = file.path;

            row.addEventListener("click", async () => {
              if (state.busy || file.index === state.index) return;

              document.querySelectorAll(".tree-file.active").forEach(element => {
                element.classList.remove("active");
              });
              row.classList.add("active");

              setBusy(true);
              setStatus("Загрузка ассета…", "busy");

              try {
                const data = await pywebview.api.select_file(file.index);
                if (handleError(data)) return;
                showSource(data);
                await makePreview();
              } finally {
                setBusy(false);
              }
            });

            parent.appendChild(row);
          });
      }

      renderNode(tree, container, 0);
      markActiveArchiveFile(state.index);
    }

    function markActiveArchiveFile(index) {
      document.querySelectorAll(".tree-file").forEach(element => {
        element.classList.toggle(
          "active",
          Number(element.dataset.index) === Number(index)
        );
      });
    }

    async function makePreview() {
      if (!state.loaded) return;
      const revision = state.sourceRevision;
      const requestedIndex = state.index;
      const ownsBusyState = !state.busy;
      if (ownsBusyState) setBusy(true);
      setStatus("Создание предпросмотра…", "busy");

      try {
        const data = await pywebview.api.preview(requestedIndex, currentSettings());
        if (
          revision !== state.sourceRevision ||
          requestedIndex !== state.index ||
          !data ||
          data.cancelled
        ) return;
        if (handleError(data)) return;
        $("resultImage").style.visibility = "hidden";
        $("resultImage").src = data.result;
        $("resultPlaceholder").classList.add("hidden");
        const original = $("stageDimensions").textContent.split(" → ")[0];
        $("stageDimensions").textContent = `${original} → ${data.width} × ${data.height}`;
        setStatus(`Предпросмотр готов • Pixel size ${data.scale}`, "ready");
      } catch (error) {
        setStatus("Ошибка: " + (error?.message || error), "error");
      } finally {
        if (ownsBusyState) setBusy(false);
      }
    }

    async function savePng() {
      if (!state.loaded || state.busy) return;
      setBusy(true);
      setStatus("Сохранение PNG…", "busy");

      try {
        const data = await pywebview.api.export_current(state.index, currentSettings());
        if (!data) return;
        if (data.cancelled) {
          setStatus("Сохранение отменено", "ready");
          return;
        }
        if (handleError(data)) return;
        setStatus("Готово: " + data.path, "ready");
      } finally {
        setBusy(false);
      }
    }

    async function saveZip() {
      if (state.kind !== "archive" || state.busy) return;
      setBusy(true);
      $("progressBar").style.width = "0%";
      $("cancelBtn").classList.remove("hidden");
      setStatus("Пакетная обработка…", "busy");

      try {
        const data = await pywebview.api.export_archive(currentSettings());
        if (!data) return;
        if (data.cancelled) {
          setStatus("Обработка архива отменена", "ready");
          return;
        }
        if (handleError(data)) return;
        $("progressBar").style.width = "100%";
        setStatus(`Готово: ${data.count} файлов • ${data.path}`, "ready");
      } finally {
        $("cancelBtn").classList.add("hidden");
        setBusy(false);
      }
    }

    async function cancelBatch() {
      await pywebview.api.cancel_batch();
      setStatus("Остановка после текущего изображения…", "busy");
    }

    window.updateBatchProgress = function(current, total, name) {
      const percent = total ? current / total * 100 : 0;
      $("progressBar").style.width = percent + "%";
      setStatus(`${current} / ${total}: ${middleEllipsis(name, 58)}`, "busy");
    };
  </script>
</body>
</html>"""


def main() -> None:
    api = Api()
    window = webview.create_window(
        APP_NAME,
        html=APP_HTML,
        js_api=api,
        width=1140,
        height=720,
        resizable=False,
        background_color="#0b0d12",
    )
    api.window = window
    window.events.closed += api.cleanup
    webview.start(debug=False)


if __name__ == "__main__":
    main()


# Pixel Forge - инструмент для восстановления настоящего pixel art. Удаляет следы некачественного апскейлинга и нейросетевой обработки, возвращая изображениям чистую пиксельную сетку.
# python3 -m nuitka --mode=app --enable-plugin=pywebview --macos-app-name="Pixel Forge" --macos-app-icon=Pixel_Forge_MacOS.icns --macos-app-mode=gui --include-data-dir=bin=bin --output-dir=dist app.py