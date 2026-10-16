"""End-to-end tests for the stdlib EPUB translation workbench."""

from __future__ import annotations

import json
import contextlib
import io
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import epub_workbench as ew


CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles><rootfile full-path="OPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
""".encode()

OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:epub="http://www.idpf.org/2007/ops" version="3.0" unique-identifier="book-id">
  <metadata><dc:identifier id="book-id">synthetic</dc:identifier><dc:title>Source &amp; title</dc:title></metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="chapter" href="Text/ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="style" href="style.css" media-type="text/css"/>
  </manifest>
  <spine><itemref idref="chapter"/></spine>
</package>
""".encode()

CHAPTER = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>Head remains source-owned</title></head>
<body>
    <p id="first">Hello &amp; <span class="em">world</span> tail &amp; text.</p>
    <p>Second paragraph.</p>
  </body>
</html>
""".encode()

NESTED_CHAPTER = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>Nested inline test</title></head>
  <body><p id="first"><span>A<em>B</em>C</span>D</p></body>
</html>
""".encode()

NAV = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
  <body><nav epub:type="toc"><ol><li id="nav-label"><a href="#nav-label">Same-document label</a></li><li><a href="Text/ch1.xhtml#first">Chapter &amp; one</a></li></ol></nav></body>
</html>
""".encode()


def make_epub(path: Path, chapter: bytes = CHAPTER, nav: bytes = NAV) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OPS/package.opf", OPF)
        archive.writestr("OPS/nav.xhtml", nav)
        archive.writestr("OPS/Text/ch1.xhtml", chapter)
        archive.writestr("OPS/style.css", b"body { color: black; }\n", compress_type=zipfile.ZIP_STORED)


def corrupt_stored_entry(path: Path, entry_name: str) -> None:
    """Flip one stored payload byte while leaving its ZIP CRC unchanged."""

    data = bytearray(path.read_bytes())
    cursor = 0
    signature = b"PK\x03\x04"
    while True:
        offset = data.find(signature, cursor)
        if offset < 0:
            break
        fields = struct.unpack_from("<IHHHHHIIIHH", data, offset)
        name_length, extra_length, compressed_size = fields[9], fields[10], fields[7]
        name_start = offset + 30
        name = bytes(data[name_start : name_start + name_length]).decode("utf-8")
        payload_start = name_start + name_length + extra_length
        if name == entry_name:
            if compressed_size == 0:
                raise AssertionError("test entry unexpectedly has no payload")
            data[payload_start] ^= 0x01
            path.write_bytes(data)
            return
        cursor = payload_start + compressed_size
    raise AssertionError(f"ZIP local entry not found: {entry_name}")


class EpubWorkbenchTests(unittest.TestCase):
    def test_inspect_extract_partial_build_and_validate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.epub"
            output = root / "translated.epub"
            make_epub(source)

            inspection = ew.inspect_epub(source)
            self.assertEqual(inspection["package"]["opf_path"], "OPS/package.opf")
            self.assertEqual(inspection["package"]["metadata_text"][1]["text"], "Source & title")
            self.assertEqual(inspection["package"]["spine"][0]["archive_path"], "OPS/Text/ch1.xhtml")
            self.assertEqual(inspection["navigation"]["nav_links"][0]["target_path"], "OPS/nav.xhtml")
            self.assertEqual(inspection["navigation"]["nav_links"][0]["fragment"], "nav-label")
            self.assertEqual(inspection["navigation"]["nav_links"][1]["target_path"], "OPS/Text/ch1.xhtml")
            self.assertEqual(inspection["navigation"]["nav_links"][1]["fragment"], "first")

            work = ew.extract_work(source)
            self.assertEqual(work["schema"], ew.WORK_SCHEMA)
            fields = {item["field_id"]: item for item in work["fields"]}
            sources = [item["source"] for item in work["fields"]]
            self.assertIn("Hello & ", sources)
            self.assertIn("world", sources)
            self.assertIn(" tail & text.", sources)
            self.assertNotIn("Head remains source-owned", sources)
            self.assertTrue(any(item["slot"] == "tail" for item in work["fields"]))
            self.assertTrue(any(item["source_lexical"] == "Hello &amp; " for item in work["fields"]))
            self.assertTrue(all(item["block_path"].endswith("p[1]") for item in work["fields"][:3]))
            self.assertEqual(work["fields"][0]["block_source"], "Hello & world tail & text.")

            first_field = work["fields"][0]
            translations = {
                "schema": ew.TRANSLATIONS_SCHEMA,
                "fields": [
                    {
                        "field_id": first_field["field_id"],
                        "source": first_field["source"],
                        "translation": "你好 & ",
                        "reviewed": True,
                    }
                ],
            }
            work_path = root / "work.json"
            translations_path = root / "translations.json"
            report_path = root / "report.json"
            ew.write_json(work_path, work)
            ew.write_json(translations_path, translations)
            report = ew.build_epub(source, work_path, translations_path, output)
            ew.write_json(report_path, report)
            self.assertTrue(report["ok"])
            self.assertEqual(report["status"], "partial")
            self.assertEqual(report["completion_scope"], "extracted non-whitespace body text/tail fields in spine documents")
            self.assertTrue(report["metadata_unchanged"])
            self.assertTrue(report["review_flags_are_attestations"])
            self.assertEqual(report["reviewed_field_count"], 1)
            self.assertGreater(report["untranslated_field_count"], 0)
            self.assertTrue(ew.validate_epub(output)["ok"])

            with zipfile.ZipFile(source) as before, zipfile.ZipFile(output) as after:
                self.assertEqual(before.namelist(), after.namelist())
                self.assertEqual(before.read("OPS/style.css"), after.read("OPS/style.css"))
                self.assertEqual(before.read("OPS/package.opf"), after.read("OPS/package.opf"))
                self.assertEqual(before.read("OPS/nav.xhtml"), after.read("OPS/nav.xhtml"))
                source_xml = before.read("OPS/Text/ch1.xhtml")
                output_xml = after.read("OPS/Text/ch1.xhtml")
                self.assertIn("<span class=\"em\">world</span>", output_xml.decode())
                self.assertIn("你好 &amp; ", output_xml.decode())
                self.assertIn("Second paragraph.", output_xml.decode())
                self.assertIn("Head remains source-owned", output_xml.decode())
                self.assertEqual(ew.raw_markup_tokens(source_xml.decode()), ew.raw_markup_tokens(output_xml.decode()))

    def test_rejects_drift_missing_field_fake_review_and_same_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.epub"
            drifted = root / "drifted.epub"
            make_epub(source)
            work = ew.extract_work(source)
            work_path = root / "work.json"
            ew.write_json(work_path, work)
            field = work["fields"][0]

            valid = {
                "schema": ew.TRANSLATIONS_SCHEMA,
                "fields": [
                    {
                        "field_id": field["field_id"],
                        "source": field["source"],
                        "translation": "审阅文本",
                        "reviewed": True,
                    }
                ],
            }
            translations_path = root / "translations.json"
            ew.write_json(translations_path, valid)

            source_bytes_before = source.read_bytes()
            for report_target in (source, work_path, translations_path):
                with self.assertRaisesRegex(ew.WorkbenchError, "Build report path"):
                    ew.build_epub(
                        source,
                        work_path,
                        translations_path,
                        root / "collision-output.epub",
                        report_path=report_target,
                    )
                self.assertEqual(source.read_bytes(), source_bytes_before)
            collision_output = root / "collision-output.epub"
            with self.assertRaisesRegex(ew.WorkbenchError, "Build report path"):
                ew.build_epub(
                    source,
                    work_path,
                    translations_path,
                    collision_output,
                    report_path=collision_output,
                )
            for input_path in (work_path, translations_path):
                with self.assertRaisesRegex(ew.WorkbenchError, "Output EPUB path"):
                    ew.build_epub(source, work_path, translations_path, input_path)

            existing_output = root / "existing.epub"
            existing_output.write_bytes(b"keep-old-book")
            with self.assertRaisesRegex(ew.WorkbenchError, "already exists"):
                ew.build_epub(source, work_path, translations_path, existing_output)
            self.assertEqual(existing_output.read_bytes(), b"keep-old-book")
            ew.build_epub(source, work_path, translations_path, existing_output, overwrite=True)
            self.assertTrue(ew.validate_epub(existing_output)["ok"])

            whitespace_path = root / "whitespace.json"
            ew.write_json(
                whitespace_path,
                {
                    "schema": ew.TRANSLATIONS_SCHEMA,
                    "fields": [dict(valid["fields"][0], translation="   ")],
                },
            )
            with self.assertRaisesRegex(ew.WorkbenchError, "empty or whitespace-only"):
                ew.build_epub(source, work_path, whitespace_path, root / "whitespace.epub")

            # The CLI preflight protects EPUB and JSON inputs when a report
            # path is accidentally reused as an output path.
            for command in (
                ["inspect", "--epub", str(source), "--output", str(source)],
                ["extract", "--epub", str(source), "--output", str(source)],
                ["validate", "--epub", str(source), "--output", str(source)],
            ):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(ew.main(command), 2)
                self.assertEqual(source.read_bytes(), source_bytes_before)

            # A text edit in the original source is source drift even when the
            # work manifest and the translation JSON were not edited.
            drifted_chapter = CHAPTER.replace(b"Second paragraph.", b"Changed paragraph.")
            make_epub(drifted, drifted_chapter)
            with self.assertRaisesRegex(ew.WorkbenchError, "Source EPUB drifted"):
                ew.build_epub(drifted, work_path, translations_path, root / "out.epub")

            unknown = dict(valid)
            unknown["fields"] = [dict(valid["fields"][0], field_id="missing-field")]
            unknown_path = root / "unknown.json"
            ew.write_json(unknown_path, unknown)
            with self.assertRaisesRegex(ew.WorkbenchError, "unknown/missing work field"):
                ew.build_epub(source, work_path, unknown_path, root / "out.epub")

            fake = dict(valid)
            fake["fields"] = [dict(valid["fields"][0], reviewed=False)]
            fake_path = root / "fake.json"
            ew.write_json(fake_path, fake)
            with self.assertRaisesRegex(ew.WorkbenchError, "reviewed: true"):
                ew.build_epub(source, work_path, fake_path, root / "out.epub")

            with self.assertRaisesRegex(ew.WorkbenchError, "must differ"):
                ew.build_epub(source, work_path, translations_path, source)

    def test_full_build_fragments_and_crc_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.epub"
            output = root / "translated.epub"
            report_path = root / "build-report.json"
            make_epub(source)

            source_report = ew.validate_epub(source)
            self.assertTrue(source_report["ok"], source_report)
            self.assertEqual(source_report["zip"]["testzip_error"], None)

            work = ew.extract_work(source)
            work_path = root / "work.json"
            ew.write_json(work_path, work)
            translations = {
                "schema": ew.TRANSLATIONS_SCHEMA,
                "fields": [
                    {
                        "field_id": item["field_id"],
                        "source": item["source"],
                        "translation": "你好 & " if index == 0 else item["source"],
                        "reviewed": True,
                    }
                    for index, item in enumerate(work["fields"])
                ],
            }
            translations_path = root / "translations.json"
            ew.write_json(translations_path, translations)
            report = ew.build_epub(
                source,
                work_path,
                translations_path,
                output,
                report_path=report_path,
                require_complete=True,
            )
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["untranslated_field_count"], 0)
            self.assertEqual(report["completion_scope"], "extracted non-whitespace body text/tail fields in spine documents")
            self.assertTrue(report["metadata_unchanged"])
            self.assertTrue(ew.validate_epub(output)["ok"])

            bad_nav = root / "bad-nav.epub"
            make_epub(bad_nav, nav=NAV.replace(b'href="#nav-label"', b'href="#missing"'))
            bad_nav_report = ew.validate_epub(bad_nav)
            self.assertFalse(bad_nav_report["ok"])
            self.assertTrue(any("missing XML fragment #missing" in error for error in bad_nav_report["errors"]))

            bad_crc = root / "bad-crc.epub"
            bad_crc.write_bytes(source.read_bytes())
            corrupt_stored_entry(bad_crc, "OPS/style.css")
            bad_crc_report = ew.validate_epub(bad_crc)
            self.assertFalse(bad_crc_report["ok"])
            self.assertIsNotNone(bad_crc_report["zip"]["testzip_error"])
            self.assertTrue(any("CRC" in error for error in bad_crc_report["errors"]))

    def test_nested_inline_fields_follow_rendered_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "nested.epub"
            output = root / "nested-translated.epub"
            make_epub(source, chapter=NESTED_CHAPTER)

            work = ew.extract_work(source)
            fields = work["fields"]
            self.assertEqual([item["source"] for item in fields], ["A", "B", "C", "D"])
            self.assertEqual("".join(item["source"] for item in fields), "ABCD")

            work_path = root / "nested-work.json"
            translations_path = root / "nested-translations.json"
            ew.write_json(work_path, work)
            ew.write_json(
                translations_path,
                {
                    "schema": ew.TRANSLATIONS_SCHEMA,
                    "fields": [
                        {
                            "field_id": item["field_id"],
                            "source": item["source"],
                            "translation": translation,
                            "reviewed": True,
                        }
                        for item, translation in zip(fields, ["甲", "乙", "丙", "丁"])
                    ],
                },
            )
            report = ew.build_epub(
                source,
                work_path,
                translations_path,
                output,
                require_complete=True,
            )
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["validation"]["epub"], str(output))
            with zipfile.ZipFile(output) as archive:
                output_text = archive.read("OPS/Text/ch1.xhtml").decode("utf-8")
                root_element = ew.parse_xml(output_text.encode("utf-8"), path="OPS/Text/ch1.xhtml")
            paragraph = next(element for element in root_element.iter() if ew.local_name(element.tag) == "p")
            self.assertEqual("".join(paragraph.itertext()), "甲乙丙丁")
            self.assertIn("<span>甲<em>乙</em>丙</span>丁", output_text)

    def test_rejects_added_hard_line_break(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.epub"
            make_epub(source)
            work = ew.extract_work(source)
            field = work["fields"][0]
            ew.write_json(root / "work.json", work)
            ew.write_json(
                root / "translations.json",
                {
                    "schema": ew.TRANSLATIONS_SCHEMA,
                    "fields": [
                        {
                            "field_id": field["field_id"],
                            "source": field["source"],
                            "translation": "错误\n换行",
                            "reviewed": True,
                        }
                    ],
                },
            )
            with self.assertRaisesRegex(ew.WorkbenchError, "hard line breaks"):
                ew.build_epub(source, root / "work.json", root / "translations.json", root / "out.epub")


if __name__ == "__main__":
    unittest.main()
