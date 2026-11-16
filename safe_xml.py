"""Strict parsing helpers for user-supplied XML annotation files."""
from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException


class UnsafeXmlError(ValueError):
    """Raised when annotation XML is malformed or uses forbidden XML features."""


def parse_untrusted_xml(source: str | Path | BinaryIO):
    """Parse a local XML document while rejecting DTD and entity expansion.

    Dataset annotations are supplied by external sources and must be treated
    as untrusted even when they were unpacked locally. Callers can catch this
    stable ValueError subclass without exposing parser details to the UI.
    """
    try:
        return ET.parse(source).getroot()
    except (ET.ParseError, DefusedXmlException) as exc:
        raise UnsafeXmlError("The XML document is malformed or contains forbidden DTD/entity declarations.") from exc
