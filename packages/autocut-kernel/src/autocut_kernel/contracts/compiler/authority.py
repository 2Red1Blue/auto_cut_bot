"""Verification of the frozen Markdown authority behind machine contract sources."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from .canonical import sha256_bytes
from .errors import AuthorityIntegrityError
from .source import SourceInput

_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)


def verify_source_authority(*, source: SourceInput, authority_root: Path) -> None:
    """Prove that ``source`` is still licensed by the exact authority bytes it cites.

    The compiler deliberately never parses prose into a schema.  It does, however,
    fail closed when the document frozen in ``SourceMetadata`` is absent, escapes the
    supplied authority root, is a symlink, or has changed since transcription.
    """

    document = authority_root / source.metadata.contract_path.document
    try:
        document.relative_to(authority_root)
    except ValueError as error:  # Defensive: ContractPath already rejects traversal.
        raise AuthorityIntegrityError("authority document escapes authority_root") from error
    try:
        raw = _read_regular_authority_bytes(
            authority_root=authority_root, relative_document=document.relative_to(authority_root)
        )
    except OSError as error:
        raise AuthorityIntegrityError("authority document must be a regular non-symlink file") from error
    actual = sha256_bytes(raw)
    if actual != source.metadata.source_document_sha256:
        raise AuthorityIntegrityError(
            "authority document digest differs from source_metadata.source_document_sha256"
        )
    try:
        markdown = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AuthorityIntegrityError("authority document must be valid UTF-8 Markdown") from error
    matches = _matching_contract_anchors(markdown, source.metadata.contract_path.anchor)
    if len(matches) != 1:
        if matches:
            raise AuthorityIntegrityError("contract_path.anchor identifies multiple authority headings")
        raise AuthorityIntegrityError("contract_path.anchor does not identify a heading in authority document")


def _read_regular_authority_bytes(*, authority_root: Path, relative_document: Path) -> bytes:
    """Open an authority file below one trusted directory without path races."""

    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("platform does not provide O_NOFOLLOW")
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(authority_root, os.O_RDONLY | directory_flag | nofollow)
    try:
        _require_directory(descriptor)
        for component in relative_document.parts[:-1]:
            next_descriptor = os.open(component, os.O_RDONLY | directory_flag | nofollow, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            _require_directory(descriptor)
        file_descriptor = os.open(
            relative_document.parts[-1], os.O_RDONLY | nofollow, dir_fd=descriptor
        )
        try:
            if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
                raise OSError("authority document is not a regular file")
            with os.fdopen(file_descriptor, "rb", closefd=True) as stream:
                file_descriptor = -1
                return stream.read()
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)
    finally:
        os.close(descriptor)


def _require_directory(descriptor: int) -> None:
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        raise OSError("authority root component is not a directory")


def _matching_contract_anchors(markdown: str, anchor: str) -> tuple[str, ...]:
    """Accept an explicit section number or a deterministic heading fragment.

    Numeric fragments such as ``#4.2`` are the stable notation used by the frozen
    Chinese contracts.  Named fragments use a deliberately small heading-slug
    profile: Unicode letters/numbers are preserved, whitespace becomes ``-``, and
    punctuation is discarded.  We reject an unknown fragment rather than silently
    treating the whole-document hash as sufficient provenance.
    """

    fragment = anchor.removeprefix("#")
    matches: list[str] = []
    for match in _HEADING.finditer(markdown):
        heading = match.group(1)
        section_number = heading.split(maxsplit=1)[0]
        if fragment == section_number or fragment == _heading_slug(heading):
            matches.append(heading)
    return tuple(matches)


def _heading_slug(heading: str) -> str:
    parts: list[str] = []
    pending_separator = False
    for character in heading.casefold():
        if character.isalnum() or character in {".", "_"}:
            if pending_separator and parts:
                parts.append("-")
            parts.append(character)
            pending_separator = False
        elif character.isspace() or character == "-":
            pending_separator = True
        # Other punctuation is discarded, matching the small documented profile.
    return "".join(parts)
