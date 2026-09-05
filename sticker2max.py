#!/usr/bin/env python3
"""Download public Telegram sticker sets and prepare static PNGs for MAX.

The downloader uses Telegram's official Bot API.  MAX sticker-pack creation is
not exposed by the public MAX Bot API, so the generated folders are intended
for upload through the official @stickers bot in MAX.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import re
import shutil
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageOps


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


TELEGRAM_API = "https://api.telegram.org"
APP_VERSION = "1.1.0"
OUTPUT_SIZE = 288
MAX_STICKERS_PER_SET = 100
MAX_STATIC_FILE_BYTES = 10 * 1024 * 1024
DOWNLOAD_LIMIT_BYTES = 20 * 1024 * 1024
TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{20,}$")
SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")
INVALID_WINDOWS_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class StickerToolError(RuntimeError):
    pass


@dataclass
class PreparedSticker:
    telegram_index: int
    output_path: Path
    emoji: str
    source_width: int
    source_height: int
    file_unique_id: str


def eprint(*values: object) -> None:
    print(*values, file=sys.stderr)


def sanitize_name(value: str, fallback: str = "sticker_pack") -> str:
    """Return an ASCII-safe identifier for archives and metadata."""
    cleaned = SAFE_ID_RE.sub("_", value.strip()).strip("._")
    return cleaned or fallback


def sanitize_folder_name(value: str, fallback: str = "Стикерпак", max_length: int = 120) -> str:
    """Return a readable Windows-safe folder name, preserving Unicode."""
    normalized = unicodedata.normalize("NFKC", value)
    cleaned = INVALID_WINDOWS_NAME_RE.sub("_", normalized)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = fallback
    if cleaned.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        cleaned = "_" + cleaned
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(" .")
    return cleaned or fallback


def parse_pack_name(value: str) -> str:
    value = value.strip().strip('"').strip("'")
    if not value:
        raise StickerToolError("Пустое имя стикерпака.")

    if "://" not in value:
        if "/" not in value:
            candidate = value
        else:
            value = "https://" + value
            candidate = ""
    else:
        candidate = ""

    if not candidate:
        parsed = urllib.parse.urlparse(value)
        host = parsed.netloc.lower()
        if host not in {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}:
            raise StickerToolError(f"Неожиданный домен в ссылке: {host or value}")
        parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
        if len(parts) < 2 or parts[0].lower() != "addstickers":
            raise StickerToolError(
                "Нужна ссылка вида https://t.me/addstickers/PackName или короткое имя PackName."
            )
        candidate = parts[1]

    candidate = candidate.replace("\\_", "_").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,128}", candidate):
        raise StickerToolError(f"Некорректное короткое имя Telegram-пака: {candidate!r}")
    return candidate


def split_values(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        for part in re.split(r"[\s,;]+", value.strip()):
            if part and not part.startswith("#"):
                result.append(part)
    return result


def load_default_packs(script_dir: Path) -> list[str]:
    path = script_dir / "packs.txt"
    if not path.exists():
        return []
    values: list[str] = []
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            values.append(line)
    return values


def request_json(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    attempts: int = 5,
) -> dict[str, Any]:
    last_error: Exception | None = None
    request_headers = {"User-Agent": "telegram-to-max-stickers/1.0"}
    if headers:
        request_headers.update(headers)

    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, data=data, headers=request_headers)
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise StickerToolError("API вернул ответ неожиданного формата.")
            return payload
        except urllib.error.HTTPError as exc:
            retry_after = 0
            description = f"HTTP {exc.code}"
            try:
                body = json.loads(exc.read().decode("utf-8"))
                description = body.get("description", description)
                retry_after = int(body.get("parameters", {}).get("retry_after", 0))
            except Exception:
                pass
            if exc.code == 429 and attempt < attempts:
                wait_seconds = max(retry_after, attempt)
                print(f"  Telegram ограничил частоту. Повтор через {wait_seconds} сек.")
                time.sleep(wait_seconds)
                continue
            if exc.code >= 500 and attempt < attempts:
                time.sleep(min(2**attempt, 10))
                continue
            raise StickerToolError(f"Ошибка Telegram API: {description}") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(min(2**attempt, 10))
                continue
    raise StickerToolError(f"Сеть недоступна после {attempts} попыток: {last_error}")


class TelegramBotClient:
    def __init__(self, token: str) -> None:
        token = token.strip()
        if not TOKEN_RE.fullmatch(token):
            raise StickerToolError("Токен Telegram выглядит некорректно.")
        self._token = token
        self._api_root = f"{TELEGRAM_API}/bot{token}"
        self._file_root = f"{TELEGRAM_API}/file/bot{token}"

    def call(self, method: str, **params: object) -> Any:
        encoded = urllib.parse.urlencode(params).encode("utf-8")
        payload = request_json(f"{self._api_root}/{method}", data=encoded)
        if not payload.get("ok"):
            raise StickerToolError(
                f"Telegram API отклонил {method}: {payload.get('description', 'неизвестная ошибка')}"
            )
        return payload.get("result")

    def validate(self) -> str:
        result = self.call("getMe")
        return str(result.get("username") or result.get("first_name") or "bot")

    def get_sticker_set(self, name: str) -> dict[str, Any]:
        result = self.call("getStickerSet", name=name)
        if not isinstance(result, dict):
            raise StickerToolError("Telegram не вернул данные стикерпака.")
        return result

    def download_sticker(self, sticker: dict[str, Any], destination: Path) -> Path:
        declared_size = int(sticker.get("file_size") or 0)
        if declared_size > DOWNLOAD_LIMIT_BYTES:
            raise StickerToolError(
                f"Файл больше лимита скачивания Telegram Bot API (20 МБ): {declared_size} байт."
            )

        file_info = self.call("getFile", file_id=sticker["file_id"])
        file_path = file_info.get("file_path")
        if not file_path:
            raise StickerToolError("Telegram не вернул file_path для стикера.")

        suffix = Path(str(file_path)).suffix.lower() or ".bin"
        output_path = destination.with_suffix(suffix)
        temp_path = output_path.with_suffix(output_path.suffix + ".part")
        request = urllib.request.Request(
            f"{self._file_root}/{urllib.parse.quote(str(file_path), safe='/')}",
            headers={"User-Agent": "telegram-to-max-stickers/1.0"},
        )
        last_error: Exception | None = None
        for attempt in range(1, 5):
            try:
                temp_path.unlink(missing_ok=True)
                with urllib.request.urlopen(request, timeout=90) as response, temp_path.open(
                    "wb"
                ) as handle:
                    shutil.copyfileobj(response, handle, length=1024 * 1024)
                if temp_path.stat().st_size > DOWNLOAD_LIMIT_BYTES:
                    raise StickerToolError("Скачанный файл превысил лимит 20 МБ.")
                temp_path.replace(output_path)
                return output_path
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                temp_path.unlink(missing_ok=True)
                if attempt < 4:
                    time.sleep(min(2**attempt, 8))
            except StickerToolError:
                temp_path.unlink(missing_ok=True)
                raise
        raise StickerToolError(f"Не удалось скачать файл после 4 попыток: {last_error}")


def fit_to_max_png(source: Path, destination: Path, size: int = OUTPUT_SIZE) -> None:
    try:
        with Image.open(source) as image:
            rgba = ImageOps.exif_transpose(image).convert("RGBA")
            fitted = ImageOps.contain(rgba, (size, size), method=Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            left = (size - fitted.width) // 2
            top = (size - fitted.height) // 2
            canvas.alpha_composite(fitted, (left, top))
            destination.parent.mkdir(parents=True, exist_ok=True)
            temp_path = destination.with_suffix(".png.part")
            with temp_path.open("wb") as handle:
                canvas.save(handle, format="PNG", optimize=True, compress_level=9)
            temp_path.replace(destination)
    except Exception as exc:
        raise StickerToolError(f"Не удалось преобразовать {source.name}: {exc}") from exc


def validate_png(path: Path) -> None:
    if not path.exists():
        raise StickerToolError(f"Не создан файл {path}")
    if path.stat().st_size > MAX_STATIC_FILE_BYTES:
        raise StickerToolError(f"{path.name} больше лимита MAX 10 МБ.")
    with Image.open(path) as image:
        if image.format != "PNG" or image.size != (OUTPUT_SIZE, OUTPUT_SIZE):
            raise StickerToolError(
                f"{path.name}: ожидался PNG {OUTPUT_SIZE}x{OUTPUT_SIZE}, получено {image.format} {image.size}."
            )
        image.verify()


def load_previous_file_ids(root: Path) -> dict[str, str]:
    """Map generated relative paths to Telegram stable IDs from a previous run."""
    manifest_path = root / "manifest.csv"
    if not manifest_path.exists():
        return {}
    try:
        with manifest_path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = csv.DictReader(handle)
            return {
                str(row["file"]): str(row["file_unique_id"])
                for row in rows
                if row.get("status") == "ready" and row.get("file") and row.get("file_unique_id")
            }
    except (OSError, csv.Error, KeyError):
        return {}


def prune_stale_generated_pngs(root: Path, expected: set[Path]) -> None:
    """Remove only obsolete PNGs from directories owned by this tool."""
    for part_dir in root.iterdir():
        if not part_dir.is_dir() or not re.fullmatch(r"max_pack_\d{2,}", part_dir.name):
            continue
        for png_path in part_dir.glob("*.png"):
            if png_path not in expected:
                png_path.unlink(missing_ok=True)
        try:
            part_dir.rmdir()
        except OSError:
            pass


def write_manifest(
    root: Path,
    pack: dict[str, Any],
    prepared: list[PreparedSticker],
    skipped: list[dict[str, Any]],
) -> None:
    manifest_path = root / "manifest.csv"
    with manifest_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "telegram_index",
                "file",
                "emoji",
                "source_width",
                "source_height",
                "file_unique_id",
                "status",
            ]
        )
        for item in prepared:
            writer.writerow(
                [
                    item.telegram_index,
                    item.output_path.relative_to(root).as_posix(),
                    item.emoji,
                    item.source_width,
                    item.source_height,
                    item.file_unique_id,
                    "ready",
                ]
            )
        for item in skipped:
            writer.writerow(
                [
                    item["telegram_index"],
                    "",
                    item.get("emoji", ""),
                    item.get("width", ""),
                    item.get("height", ""),
                    item.get("file_unique_id", ""),
                    item["reason"],
                ]
            )

    report = {
        "telegram_name": pack.get("name"),
        "telegram_title": pack.get("title"),
        "total_in_telegram": len(pack.get("stickers", [])),
        "prepared_static_png": len(prepared),
        "skipped": skipped,
        "max_requirements_checked": {
            "format": "PNG",
            "canvas": f"{OUTPUT_SIZE}x{OUTPUT_SIZE}",
            "max_file_bytes": MAX_STATIC_FILE_BYTES,
            "max_stickers_per_set": MAX_STICKERS_PER_SET,
        },
    }
    (root / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def zip_parts(root: Path, pack_name: str, prepared: list[PreparedSticker]) -> list[Path]:
    by_parent: dict[Path, list[Path]] = {}
    for item in prepared:
        by_parent.setdefault(item.output_path.parent, []).append(item.output_path)

    archives: list[Path] = []
    for part_dir, files in sorted(by_parent.items()):
        archive = root / f"{pack_name}__{part_dir.name}.zip"
        temp_archive = archive.with_suffix(".zip.part")
        with zipfile.ZipFile(temp_archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for file_path in sorted(files):
                zf.write(file_path, arcname=file_path.name)
        temp_archive.replace(archive)
        archives.append(archive)
    return archives


def write_max_instructions(
    root: Path,
    pack: dict[str, Any],
    prepared: list[PreparedSticker],
    archives: list[Path],
    skipped: list[dict[str, Any]],
) -> None:
    part_counts: dict[str, int] = {}
    for item in prepared:
        part_counts[item.output_path.parent.name] = part_counts.get(item.output_path.parent.name, 0) + 1

    lines = [
        f"Telegram: {pack.get('title', pack.get('name', ''))}",
        f"Ссылка: https://t.me/addstickers/{pack.get('name', '')}",
        f"Готово PNG {OUTPUT_SIZE}x{OUTPUT_SIZE}: {len(prepared)}",
        f"Пропущено анимированных/видео или файлов с ошибками: {len(skipped)}",
        "",
        "Публикация в MAX:",
        "1. Откройте официальный бот https://max.ru/stickers (@stickers).",
        "2. Для нового набора: Начать -> Создать набор.",
        "3. Для существующего: Открыть -> выберите набор -> меню -> Редактировать.",
        "4. Нажмите Добавить стикеры -> С устройства и откройте папку max_pack_XX.",
        "5. Нажмите Ctrl+A, затем Открыть. Если интерфейс зависает, выбирайте по 20-25 файлов.",
        "6. Добавьте название/эмодзи и сохраните набор.",
        "",
        "Подготовленные части (лимит MAX: не больше 100 стикеров в наборе):",
    ]
    for part_name, count in sorted(part_counts.items()):
        lines.append(f"- {part_name}: {count} PNG")
    if archives:
        lines.append("")
        lines.append("Архивы для хранения (в @stickers загружайте распакованные PNG):")
        for archive in archives:
            lines.append(f"- {archive.name}")
    (root / "MAX_UPLOAD.txt").write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def prepare_pack(
    client: TelegramBotClient,
    pack_name: str,
    output_root: Path,
    *,
    overwrite: bool,
    keep_source: bool,
    create_zip: bool,
) -> tuple[Path, int, int]:
    print(f"\n[{pack_name}] Получаю список стикеров...")
    pack = client.get_sticker_set(pack_name)
    stickers = pack.get("stickers", [])
    if not isinstance(stickers, list):
        raise StickerToolError("Telegram вернул некорректный список стикеров.")

    canonical_name = sanitize_name(str(pack.get("name") or pack_name))
    display_title = str(pack.get("title") or canonical_name).strip()
    folder_name = sanitize_folder_name(f"{display_title} — {canonical_name}")
    root = output_root / folder_name
    root.mkdir(parents=True, exist_ok=True)
    source_dir = root / "_source"
    source_dir.mkdir(parents=True, exist_ok=True)
    previous_file_ids = load_previous_file_ids(root)

    print(f"  {pack.get('title', canonical_name)}: {len(stickers)} стикеров")
    prepared: list[PreparedSticker] = []
    skipped: list[dict[str, Any]] = []
    static_position = 0

    for telegram_index, sticker in enumerate(stickers, start=1):
        emoji = str(sticker.get("emoji") or "")
        if sticker.get("is_animated") or sticker.get("is_video"):
            kind = "animated" if sticker.get("is_animated") else "video"
            skipped.append(
                {
                    "telegram_index": telegram_index,
                    "emoji": emoji,
                    "width": sticker.get("width"),
                    "height": sticker.get("height"),
                    "file_unique_id": sticker.get("file_unique_id", ""),
                    "reason": f"skipped_{kind}_not_static_png",
                }
            )
            print(f"  {telegram_index:03d}/{len(stickers):03d}: пропуск {kind}")
            continue

        static_position += 1
        part_number = (static_position - 1) // MAX_STICKERS_PER_SET + 1
        number_in_part = (static_position - 1) % MAX_STICKERS_PER_SET + 1
        part_dir = root / f"max_pack_{part_number:02d}"
        output_path = part_dir / f"{number_in_part:03d}.png"
        output_relative = output_path.relative_to(root).as_posix()
        file_unique_id = str(sticker.get("file_unique_id") or "")

        may_reuse = (
            output_path.exists()
            and not overwrite
            and previous_file_ids.get(output_relative) == file_unique_id
        )
        if may_reuse:
            try:
                validate_png(output_path)
                print(f"  {telegram_index:03d}/{len(stickers):03d}: уже готов {output_path.name}")
            except StickerToolError:
                output_path.unlink(missing_ok=True)
        elif output_path.exists():
            output_path.unlink(missing_ok=True)

        if not output_path.exists():
            print(f"  {telegram_index:03d}/{len(stickers):03d}: скачиваю и конвертирую", end="", flush=True)
            source_base = source_dir / f"{telegram_index:03d}"
            try:
                source_path = client.download_sticker(sticker, source_base)
                fit_to_max_png(source_path, output_path)
                validate_png(output_path)
                if not keep_source:
                    source_path.unlink(missing_ok=True)
                print(" — OK")
            except Exception as exc:
                output_path.unlink(missing_ok=True)
                skipped.append(
                    {
                        "telegram_index": telegram_index,
                        "emoji": emoji,
                        "width": sticker.get("width"),
                        "height": sticker.get("height"),
                        "file_unique_id": sticker.get("file_unique_id", ""),
                        "reason": f"error: {exc}",
                    }
                )
                print(f" — ОШИБКА: {exc}")
                continue

        prepared.append(
            PreparedSticker(
                telegram_index=telegram_index,
                output_path=output_path,
                emoji=emoji,
                source_width=int(sticker.get("width") or 0),
                source_height=int(sticker.get("height") or 0),
                file_unique_id=file_unique_id,
            )
        )

    if not keep_source:
        shutil.rmtree(source_dir, ignore_errors=True)

    prune_stale_generated_pngs(root, {item.output_path for item in prepared})

    write_manifest(root, pack, prepared, skipped)
    archives = zip_parts(root, canonical_name, prepared) if create_zip else []
    write_max_instructions(root, pack, prepared, archives, skipped)

    print(f"  Готово: {len(prepared)} PNG; пропущено/ошибок: {len(skipped)}")
    print(f"  Папка: {root.resolve()}")
    return root, len(prepared), len(skipped)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Скачать Telegram-стикерпак и подготовить PNG 288x288 для MAX."
    )
    parser.add_argument(
        "packs",
        nargs="*",
        help="Ссылки t.me/addstickers/... или короткие имена паков.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="output",
        help="Корневая папка результата (по умолчанию: output рядом со скриптом).",
    )
    parser.add_argument("--overwrite", action="store_true", help="Пересоздать уже готовые PNG.")
    parser.add_argument("--keep-source", action="store_true", help="Сохранить исходные WEBP.")
    parser.add_argument("--zip", action="store_true", help="Дополнительно создать ZIP каждой части.")
    parser.add_argument(
        "--open-folder",
        action="store_true",
        help="После успеха открыть папку с готовыми PNG.",
    )
    parser.add_argument(
        "--open-max",
        action="store_true",
        help="После успеха открыть официальный @stickers в браузере.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    return parser


def open_output_path(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(path.resolve()))  # type: ignore[attr-defined]
        else:
            webbrowser.open(path.resolve().as_uri())
    except OSError as exc:
        eprint(f"Не удалось открыть папку {path}: {exc}")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    script_dir = Path(__file__).resolve().parent

    supplied = split_values(args.packs)
    if not supplied:
        print("Введите ссылки/короткие имена Telegram-паков через пробел.")
        print("Нажмите Enter без ввода, чтобы обработать список из packs.txt.")
        supplied = split_values([input("> ")])
    if not supplied:
        supplied = load_default_packs(script_dir)
    if not supplied:
        eprint("Нет стикерпаков для обработки.")
        return 2

    pack_names: list[str] = []
    for value in supplied:
        try:
            name = parse_pack_name(value)
        except StickerToolError as exc:
            eprint(f"Пропуск {value!r}: {exc}")
            continue
        if name not in pack_names:
            pack_names.append(name)
    if not pack_names:
        return 2

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("Нужен токен любого вашего Telegram-бота от @BotFather.")
        print("Токен используется только для официального Telegram Bot API и не сохраняется.")
        token = getpass.getpass("TELEGRAM_BOT_TOKEN: ").strip()

    try:
        client = TelegramBotClient(token)
        bot_name = client.validate()
        print(f"Telegram API: авторизация успешна (@{bot_name})")
    except StickerToolError as exc:
        eprint(f"Ошибка авторизации: {exc}")
        return 3

    output_root = Path(args.output)
    if not output_root.is_absolute():
        output_root = script_dir / output_root
    output_root.mkdir(parents=True, exist_ok=True)

    success_count = 0
    skipped_count = 0
    failed_packs = 0
    roots: list[Path] = []
    for name in pack_names:
        try:
            root, prepared, skipped = prepare_pack(
                client,
                name,
                output_root,
                overwrite=args.overwrite,
                keep_source=args.keep_source,
                create_zip=args.zip,
            )
            roots.append(root)
            success_count += prepared
            skipped_count += skipped
        except Exception as exc:
            failed_packs += 1
            eprint(f"\n[{name}] НЕ ГОТОВО: {exc}")

    print("\nИТОГ")
    print(f"  Готовых PNG: {success_count}")
    print(f"  Пропущено стикеров: {skipped_count}")
    print(f"  Не обработано паков: {failed_packs}")
    print(f"  Результат: {output_root.resolve()}")
    print("  Публикация: https://max.ru/stickers (официальный @stickers)")
    if roots:
        if args.open_folder:
            first_part = roots[0] / "max_pack_01"
            open_target = first_part if len(roots) == 1 and first_part.exists() else output_root
            open_output_path(open_target)
        if args.open_max:
            webbrowser.open("https://max.ru/stickers")
        return 0 if failed_packs == 0 else 1
    return 4


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        eprint("\nОперация отменена.")
        raise SystemExit(130) from None
