"""Security contracts for untrusted detector-dataset annotations."""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from safe_xml import UnsafeXmlError, parse_untrusted_xml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DatasetImportSecurityTests(unittest.TestCase):
    def test_normal_pascal_voc_annotation_remains_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            annotation_path = Path(directory) / "sample.xml"
            annotation_path.write_text(
                "<annotation><filename>sample.jpg</filename><size><width>640</width><height>480</height></size></annotation>",
                encoding="utf-8",
            )
            annotation = parse_untrusted_xml(annotation_path)

        self.assertEqual(annotation.findtext("filename"), "sample.jpg")
        self.assertEqual(annotation.findtext("size/width"), "640")

    def test_dtd_and_entity_declarations_are_rejected(self):
        malicious = """<!DOCTYPE annotation [<!ENTITY x "expanded">]>
        <annotation><filename>&x;</filename></annotation>"""
        with tempfile.TemporaryDirectory() as directory:
            annotation_path = Path(directory) / "malicious.xml"
            annotation_path.write_text(malicious, encoding="utf-8")
            with self.assertRaises(UnsafeXmlError):
                parse_untrusted_xml(annotation_path)

    def test_uvh26_download_uses_a_fixed_source_revision(self):
        source = (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
        self.assertRegex(source, r'UVH26_DATASET_REVISION\s*=\s*"[0-9a-f]{40}"')
        self.assertIn("revision=UVH26_DATASET_REVISION", source)


if __name__ == "__main__":
    unittest.main()
