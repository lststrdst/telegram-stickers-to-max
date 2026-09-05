import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import sticker2max


class PackNameTests(unittest.TestCase):
    def test_parses_full_link(self) -> None:
        self.assertEqual(
            sticker2max.parse_pack_name("https://t.me/addstickers/test_pack"),
            "test_pack",
        )

    def test_parses_markdown_escaped_underscore(self) -> None:
        self.assertEqual(
            sticker2max.parse_pack_name("https://t.me/addstickers/test\\_pack"),
            "test_pack",
        )

    def test_parses_short_name(self) -> None:
        self.assertEqual(sticker2max.parse_pack_name("TestPack_42"), "TestPack_42")

    def test_rejects_unexpected_domain(self) -> None:
        with self.assertRaises(sticker2max.StickerToolError):
            sticker2max.parse_pack_name("https://example.com/addstickers/test")


class FilenameTests(unittest.TestCase):
    def test_preserves_readable_unicode(self) -> None:
        self.assertEqual(
            sticker2max.sanitize_folder_name('Котики: лучшие?  '),
            "Котики_ лучшие_",
        )

    def test_avoids_windows_reserved_names(self) -> None:
        self.assertEqual(sticker2max.sanitize_folder_name("CON"), "_CON")


class ImageTests(unittest.TestCase):
    def test_converts_to_exact_transparent_canvas_without_distortion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "wide.webp"
            destination = root / "ready.png"
            Image.new("RGBA", (512, 256), (255, 0, 0, 255)).save(source, "WEBP", lossless=True)

            sticker2max.fit_to_max_png(source, destination)
            sticker2max.validate_png(destination)

            with Image.open(destination) as result:
                self.assertEqual(result.size, (288, 288))
                self.assertEqual(result.getpixel((144, 10))[3], 0)
                self.assertEqual(result.getpixel((144, 144))[:3], (255, 0, 0))


class FakeTelegramClient:
    def __init__(self, source: Path, count: int) -> None:
        self.source = source
        self.count = count
        self.downloads = 0

    def get_sticker_set(self, name: str) -> dict:
        return {
            "name": name,
            "title": "Тестовый пак",
            "stickers": [
                {
                    "file_id": f"file-{index}",
                    "file_unique_id": f"unique-{index}",
                    "width": 512,
                    "height": 512,
                    "emoji": "🙂",
                    "is_animated": False,
                    "is_video": False,
                }
                for index in range(self.count)
            ],
        }

    def download_sticker(self, sticker: dict, destination: Path) -> Path:
        self.downloads += 1
        output = destination.with_suffix(".webp")
        output.write_bytes(self.source.read_bytes())
        return output


class PreparePackTests(unittest.TestCase):
    def test_splits_reuses_and_prunes_generated_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.webp"
            Image.new("RGBA", (64, 64), (0, 120, 255, 255)).save(source, "WEBP", lossless=True)
            client = FakeTelegramClient(source, count=3)

            with mock.patch.object(sticker2max, "MAX_STICKERS_PER_SET", 2):
                pack_root, prepared, skipped = sticker2max.prepare_pack(
                    client,
                    "TestPack",
                    root / "output",
                    overwrite=False,
                    keep_source=False,
                    create_zip=False,
                )

                self.assertEqual((prepared, skipped), (3, 0))
                self.assertEqual(client.downloads, 3)
                self.assertTrue((pack_root / "max_pack_01" / "002.png").exists())
                self.assertTrue((pack_root / "max_pack_02" / "001.png").exists())

                client.downloads = 0
                sticker2max.prepare_pack(
                    client,
                    "TestPack",
                    root / "output",
                    overwrite=False,
                    keep_source=False,
                    create_zip=False,
                )
                self.assertEqual(client.downloads, 0)

                client.count = 2
                sticker2max.prepare_pack(
                    client,
                    "TestPack",
                    root / "output",
                    overwrite=False,
                    keep_source=False,
                    create_zip=False,
                )
                self.assertFalse((pack_root / "max_pack_02").exists())


if __name__ == "__main__":
    unittest.main()
