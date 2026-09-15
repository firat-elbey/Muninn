"""muninn.store: read/write an OKF-conformant bundle of markdown notes.

A bundle is a directory tree of ``.md`` files, each with YAML frontmatter
(OKF v0.1: ``type`` required, unknown keys preserved). ``index.md`` and
``log.md`` are reserved (OKF). The memory sidecar lives in ``.muninn/``
and is separable: delete it and the bundle is still plain OKF.

Stdlib only. The frontmatter parser handles the subset OKF actually uses
(scalars, flow lists, block lists) rather than full YAML.
"""

from __future__ import annotations

import html
import ntpath
import os
import posixpath
import re
import secrets
import stat
import string
import time
import urllib.parse
from bisect import bisect_right
from dataclasses import dataclass, field

RESERVED = {"index.md", "log.md"}
SIDECAR_DIR = ".muninn"
OKF_VERSION = "0.2"
# the OKF v0.2 actor convention (§7): <producer>/<version> for tools
MUNINN_ACTOR = "muninn/0.1"

_WIKILINK = re.compile(
    r"\[\[((?=[^\]|#]*[^\s\]|#])[^\]|#]+)"
    r"(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]"
)
_ENTITY_REFERENCE = re.compile(
    r"&(?:#[xX][0-9A-Fa-f]{1,8}|#[0-9]{1,8}|[A-Za-z][A-Za-z0-9]{1,31});"
)
_BLOCKQUOTE_LINE = re.compile(r"^[ \t]{0,3}>[ \t]?")
_BLOCK_CONSTRUCT = re.compile(
    r"^[ \t]{0,3}(?:#{1,6}(?:[ \t]|$)|[-*+][ \t]+|"
    r"\d{1,9}[.)][ \t]+|`{3,}|~{3,}|>)"
)
_THEMATIC_OR_SETEXT = re.compile(
    r"^[ \t]{0,3}(?:(?:\*[ \t]*){3,}|(?:_[ \t]*){3,}|"
    r"(?:-[ \t]*){3,}|=+[ \t]*)$"
)
_HTML_BLOCK_TAG = re.compile(
    r"^</?(?:address|article|aside|base|basefont|blockquote|body|caption|"
    r"center|col|colgroup|dd|details|dialog|dir|div|dl|dt|fieldset|"
    r"figcaption|figure|footer|form|frame|frameset|h[1-6]|head|header|hr|"
    r"html|iframe|legend|li|link|main|menu|menuitem|nav|noframes|ol|"
    r"optgroup|option|p|param|section|source|summary|table|tbody|td|tfoot|"
    r"th|thead|title|tr|track|ul)(?:\s|/?>|$)",
    re.IGNORECASE,
)
_COMPLETE_HTML_TAG = re.compile(
    r"^</?[A-Za-z][A-Za-z0-9-]*"
    r"(?:[ \t]+[A-Za-z_:][A-Za-z0-9_.:-]*"
    r"(?:[ \t]*=[ \t]*(?:[^ \t\r\n\"'=<>`]+|'[^']*'|\"[^\"]*\"))?)*"
    r"[ \t]*/?>$"
)
# Typed link: a whole body list line `- <relation> [[Target]] (<confidence>)`
#: exactly what the graphify importer writes under '# Connections'.
# Relation and confidence are both optional (defaults below). Confidence
# is a SINGLE word: a multi-word parenthetical (`- read [[X]] (old draft)`)
# is prose, so the wikilink stays a plain implicit-1.0 link.
_TYPED_LINE = re.compile(
    r"^\s*-\s+(?:(?P<rel>[^\[\]()]+?)\s+)?"
    r"\[\[(?P<target>[^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]"
    r"\s*(?:\((?P<conf>[^()\s]+)\))?\s*$")

# Confidence word -> edge weight (case-insensitive; unknown word -> 0.5).
CONF_WEIGHT = {"extracted": 1.0, "inferred": 0.5, "ambiguous": 0.2}
DEFAULT_RELATION = "related_to"
DEFAULT_CONFIDENCE = "extracted"
IGNORE_FILE = ".muninnignore"
IGNORE_FILE_LIMIT = 65_536
MAX_NOTE_BYTES = 8 * 1024 * 1024
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_BUNDLE_NOTES = 20_000
MAX_BUNDLE_DIRECTORIES = 20_000
MAX_BUNDLE_ENTRIES = 200_000
MAX_BUNDLE_DEPTH = 64
_GLOB_MARKERS = frozenset("*!?[]")


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _read_bounded_descriptor(descriptor: int, limit: int, label: str) -> bytes:
    """Read at most ``limit`` bytes from an already validated descriptor."""
    payload = bytearray()
    while len(payload) <= limit:
        chunk = os.read(descriptor, min(65_536, limit + 1 - len(payload)))
        if not chunk:
            break
        payload.extend(chunk)
        if len(payload) > limit:
            raise ValueError(f"{label} exceeds {limit} bytes")
    return bytes(payload)


def _read_exclusions(root: str) -> tuple[frozenset[str], frozenset[str]]:
    """Read bounded root-relative file and directory exclusions."""
    path = os.path.join(root, IGNORE_FILE)
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return frozenset(), frozenset()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError(f"{IGNORE_FILE} must be a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(f"{IGNORE_FILE} changed during read") from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or not _same_file(before, opened)
        ):
            raise ValueError(f"{IGNORE_FILE} changed during read")
        if opened.st_size > IGNORE_FILE_LIMIT:
            raise ValueError(f"{IGNORE_FILE} exceeds {IGNORE_FILE_LIMIT} bytes")
        payload = _read_bounded_descriptor(
            descriptor,
            IGNORE_FILE_LIMIT,
            IGNORE_FILE,
        )
        try:
            after = os.lstat(path)
        except OSError as error:
            raise ValueError(f"{IGNORE_FILE} changed during read") from error
        if not _same_file(opened, after) or not _same_file(opened, os.fstat(descriptor)):
            raise ValueError(f"{IGNORE_FILE} changed during read")
    finally:
        os.close(descriptor)
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError(f"{IGNORE_FILE} must be UTF-8") from error

    files: set[str] = set()
    directories: set[str] = set()
    for number, raw in enumerate(text.splitlines(), 1):
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        directory = value.endswith("/")
        candidate = value[:-1] if directory else value
        parts = candidate.split("/")
        invalid = (
            not candidate
            or candidate.startswith("/")
            or "\\" in candidate
            or any(part in ("", ".", "..") for part in parts)
            or any(character in _GLOB_MARKERS for character in candidate)
        )
        if invalid:
            raise ValueError(f"invalid {IGNORE_FILE} entry on line {number}")
        (directories if directory else files).add(candidate)
    return frozenset(files), frozenset(directories)


def _excluded(
    relative: str,
    files: frozenset[str],
    directories: frozenset[str],
    *,
    is_directory: bool,
) -> bool:
    """Return whether one normalized bundle path is explicitly excluded."""
    if not is_directory and relative in files:
        return True
    candidate = relative
    while candidate:
        if candidate in directories:
            return True
        candidate, separator, _name = candidate.rpartition("/")
        if not separator:
            break
    return False


def _descriptor_writes_supported() -> bool:
    """Return whether the platform supports race-safe directory traversal."""
    return (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
        and os.rename in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
        and os.listdir in os.supports_fd
        and os.unlink in os.supports_dir_fd
    )


def _new_temporary_file(
    *,
    directory_descriptor: int,
) -> tuple[int, str]:
    """Create a private regular file for one atomic note replacement."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    for _attempt in range(100):
        name = f".muninn-write-{secrets.token_hex(12)}.tmp"
        try:
            descriptor = os.open(
                name,
                flags,
                0o600,
                dir_fd=directory_descriptor,
            )
        except FileExistsError:
            continue
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                raise ValueError("temporary note destination is not private")
            return descriptor, name
        except BaseException:
            os.close(descriptor)
            os.unlink(name, dir_fd=directory_descriptor)
            raise
    raise FileExistsError("could not allocate a private note destination")


def _open_note_destination(
    root: str,
    parts: list[str],
) -> tuple[int, int | None, str, str]:
    """Create parent directories and a private file for atomic replacement."""
    full = os.path.join(os.path.realpath(root), *parts)
    if not _descriptor_writes_supported():
        raise OSError("race-safe bundle writes are unavailable on this platform")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_descriptor = os.open(os.path.realpath(root), directory_flags)
    try:
        for part in parts[:-1]:
            entries = os.listdir(directory_descriptor)
            if part not in entries:
                try:
                    os.mkdir(part, dir_fd=directory_descriptor)
                except FileExistsError:
                    if part not in os.listdir(directory_descriptor):
                        raise ValueError(
                            "note path uses a noncanonical filesystem alias"
                        ) from None
            status = os.stat(
                part,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(status.st_mode):
                raise ValueError("note path passes through a symbolic link")
            if not stat.S_ISDIR(status.st_mode):
                raise ValueError("note path has a non-directory parent")
            child_descriptor = os.open(
                part,
                directory_flags,
                dir_fd=directory_descriptor,
            )
            os.close(directory_descriptor)
            directory_descriptor = child_descriptor

        name = parts[-1]
        entries = os.listdir(directory_descriptor)
        try:
            status = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            status = None
        if name not in entries and status is not None:
            raise ValueError("note path uses a noncanonical filesystem alias")
        if status is not None:
            if stat.S_ISLNK(status.st_mode):
                raise ValueError("note path passes through a symbolic link")
            if not stat.S_ISREG(status.st_mode):
                raise ValueError("note destination is not a regular file")
            if status.st_nlink > 1:
                raise ValueError("note destination has multiple hard links")

        descriptor, temporary = _new_temporary_file(
            directory_descriptor=directory_descriptor,
        )
        if temporary not in os.listdir(directory_descriptor):
            os.close(descriptor)
            os.unlink(temporary, dir_fd=directory_descriptor)
            raise ValueError("note path uses a noncanonical filesystem alias")
        return descriptor, directory_descriptor, full, temporary
    except BaseException:
        os.close(directory_descriptor)
        raise


def _atomic_write_text(root: str, parts: list[str], text: str) -> str:
    """Replace one file through a pinned parent directory descriptor."""
    descriptor, parent_descriptor, full, temporary = _open_note_destination(
        root,
        parts,
    )
    if parent_descriptor is None:  # Defensive: the unsafe fallback is disabled.
        os.close(descriptor)
        raise OSError("race-safe bundle writes are unavailable on this platform")
    opened_status = os.fstat(descriptor)
    replaced = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            opened_status = os.fstat(handle.fileno())
            if opened_status.st_nlink != 1:
                raise ValueError("temporary note destination is not private")
        os.replace(
            temporary,
            parts[-1],
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        replaced = True
        file_status = os.stat(
            parts[-1],
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        parent_status = os.stat(
            os.path.dirname(full),
            follow_symlinks=False,
        )
        if (
            not _same_file(os.fstat(parent_descriptor), parent_status)
            or not _same_file(opened_status, file_status)
        ):
            raise ValueError("note destination changed during write")
        os.fsync(parent_descriptor)
    finally:
        if not replaced:
            try:
                os.unlink(temporary, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
        os.close(parent_descriptor)
    return full


def _markdown_unescape(value: str) -> str:
    """Decode CommonMark backslash escapes and character references."""

    output = []
    cursor = 0
    while cursor < len(value):
        if (
            value[cursor] == "\\"
            and cursor + 1 < len(value)
            and value[cursor + 1] in string.punctuation
        ):
            output.append(value[cursor + 1])
            cursor += 2
            continue
        output.append(value[cursor])
        cursor += 1
    escaped = "".join(output)
    return _ENTITY_REFERENCE.sub(
        lambda match: html.unescape(match.group(0)),
        escaped,
    )


def _reference_label(value: str) -> str:
    """Return the CommonMark-normalized form of one reference label."""

    # CommonMark compares the source spelling of labels.  Character
    # references and backslash escapes are not decoded for this comparison.
    return " ".join(value.casefold().split())


@dataclass(frozen=True)
class _ReferenceDefinition:
    """One parsed CommonMark reference definition."""

    start: int
    end: int
    label: str
    destination: str


def _line_records(text: str) -> list[tuple[int, int, str]]:
    """Return source offsets and content for each physical line."""

    records = []
    offset = 0
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        records.append((offset, offset + len(line), content))
        offset += len(line)
    if not records and text == "":
        return []
    if offset < len(text):
        records.append((offset, len(text), text[offset:]))
    return records


def _reference_destination(value: str) -> tuple[int, str] | None:
    """Parse one destination from the start of a definition line."""

    cursor = 0
    while cursor < len(value) and value[cursor] in " \t":
        cursor += 1
    if cursor >= len(value):
        return None
    if value[cursor] == "<":
        start = cursor + 1
        cursor = start
        while cursor < len(value):
            if value[cursor] == "<" and not _is_escaped(value, cursor):
                return None
            if value[cursor] == ">" and not _is_escaped(value, cursor):
                return cursor + 1, _markdown_unescape(value[start:cursor])
            cursor += 1
        return None

    start = cursor
    depth = 0
    while cursor < len(value) and value[cursor] not in " \t\r\n\v\f":
        if _is_escaped(value, cursor):
            cursor += 1
            continue
        if value[cursor] == "(":
            depth += 1
            if depth > 32:
                return None
        elif value[cursor] == ")":
            if depth == 0:
                return None
            depth -= 1
        cursor += 1
    if cursor == start or depth:
        return None
    return cursor, _markdown_unescape(value[start:cursor])


def _definition_title(value: str) -> bool:
    """Return whether a definition tail is one complete optional title."""

    if not value:
        return False
    leading = len(value) - len(value.lstrip(" \t"))
    if leading == 0:
        return False
    title = value[leading:]
    if len(title) < 2:
        return False
    closing = {"\"": "\"", "'": "'", "(": ")"}.get(title[0])
    if closing is None:
        return False
    cursor = 1
    while cursor < len(title):
        if title[cursor] == closing and not _is_escaped(title, cursor):
            return not title[cursor + 1 :].strip()
        cursor += 1
    return False


def _line_ending_length(text: str, cursor: int) -> int:
    """Return the line-ending width at ``cursor``, or zero."""

    if text.startswith("\r\n", cursor):
        return 2
    if cursor < len(text) and text[cursor] in "\r\n":
        return 1
    return 0


def _skip_spnl(text: str, cursor: int) -> int:
    """Skip spaces and at most one line ending, as CommonMark ``spnl``."""

    while cursor < len(text) and text[cursor] in " \t":
        cursor += 1
    line_ending = _line_ending_length(text, cursor)
    if line_ending:
        cursor += line_ending
        while cursor < len(text) and text[cursor] in " \t":
            cursor += 1
    return cursor


def _title_at(text: str, cursor: int) -> int | None:
    """Return the end of one CommonMark link title."""

    if cursor >= len(text):
        return None
    closing = {"\"": "\"", "'": "'", "(": ")"}.get(text[cursor])
    if closing is None:
        return None
    opening = text[cursor]
    title_start = cursor
    cursor += 1
    while cursor < len(text):
        character = text[cursor]
        if character == "\x00":
            return None
        if _is_escaped(text, cursor):
            cursor += 1
            continue
        if character == closing:
            if re.search(
                r"(?:\r\n?|\n)[ \t]*(?:\r\n?|\n)",
                text[title_start : cursor + 1],
            ):
                return None
            return cursor + 1
        if opening == "(" and character == "(":
            return None
        cursor += 1
    return None


def _line_end_after_spaces(text: str, cursor: int) -> int | None:
    """Return the offset after a line ending when only spaces remain."""

    while cursor < len(text) and text[cursor] in " \t":
        cursor += 1
    line_ending = _line_ending_length(text, cursor)
    if line_ending:
        return cursor + line_ending
    return cursor if cursor == len(text) else None


def _definition_view(text: str) -> tuple[str, list[int], list[int]]:
    """Expose block-quoted definitions while retaining source offsets."""

    characters = list(text)
    content_starts: list[int] = []
    quote_depths: list[int] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        content_end = offset + len(line.rstrip("\r\n"))
        cursor = offset
        depth = 0
        while True:
            marker_start = cursor
            spaces = 0
            while cursor < content_end and text[cursor] in " \t" and spaces < 3:
                cursor += 1
                spaces += 1
            if cursor >= content_end or text[cursor] != ">":
                cursor = marker_start if depth == 0 else cursor
                break
            cursor += 1
            if cursor < content_end and text[cursor] in " \t":
                cursor += 1
            for index in range(marker_start, cursor):
                characters[index] = " "
            depth += 1
        content_starts.append(cursor if depth else offset)
        quote_depths.append(depth)
        offset += len(line)
    return "".join(characters), content_starts, quote_depths


def _parse_reference_definition(
    text: str,
    opening: int,
) -> tuple[int, str, str] | None:
    """Parse one definition beginning at an opening label bracket."""

    label_end = _link_label_end(
        text,
        opening,
        allow_nesting=False,
        max_characters=999,
        max_line_endings=1,
    )
    if label_end is None or label_end + 1 >= len(text):
        return None
    if text[label_end + 1] != ":":
        return None
    label = text[opening + 1 : label_end]
    if not label.strip():
        return None

    destination_start = _skip_spnl(text, label_end + 2)
    parsed = _reference_destination(text[destination_start:])
    if parsed is None:
        return None
    destination_end = destination_start + parsed[0]

    title_start = _skip_spnl(text, destination_end)
    if title_start > destination_end:
        title_end = _title_at(text, title_start)
        if title_end is not None:
            title_text = text[title_start:title_end]
            if not re.search(r"(?:\r\n?|\n)[ \t]*(?:\r\n?|\n)", title_text):
                definition_end = _line_end_after_spaces(text, title_end)
                if definition_end is not None:
                    return definition_end, label, parsed[1]

    definition_end = _line_end_after_spaces(text, destination_end)
    if definition_end is None:
        return None
    return definition_end, label, parsed[1]


def _reference_definitions(text: str) -> tuple[_ReferenceDefinition, ...]:
    """Parse definitions that occur at the beginning of Markdown paragraphs."""

    view, content_starts, quote_depths = _definition_view(text)
    view = _mask_html_blocks(view)
    records = _line_records(view)
    line_starts = [record[0] for record in records]
    output: list[_ReferenceDefinition] = []
    paragraph_open = False
    previous_depth = 0
    index = 0
    while index < len(records):
        line_start, _, content = records[index]
        depth = quote_depths[index]
        if depth != previous_depth:
            paragraph_open = False
        previous_depth = depth
        content_start = content_starts[index]
        visible = view[content_start : line_start + len(content)]
        if not visible.strip():
            paragraph_open = False
            index += 1
            continue

        opening = content_start
        spaces = 0
        while opening < len(view) and view[opening] in " \t" and spaces < 3:
            opening += 1
            spaces += 1
        parsed = None if paragraph_open else _parse_reference_definition(view, opening)
        if parsed is not None:
            definition_end, label, destination = parsed
            output.append(
                _ReferenceDefinition(
                    line_start,
                    definition_end,
                    _reference_label(label),
                    destination,
                )
            )
            consumed_line = bisect_right(line_starts, max(opening, definition_end - 1)) - 1
            index = max(index + 1, consumed_line + 1)
            paragraph_open = False
            continue

        if _BLOCK_CONSTRUCT.match(visible):
            paragraph_open = False
        else:
            paragraph_open = True
        index += 1
    return tuple(output)


def _reference_destinations(text: str) -> dict[str, str]:
    """Return first-definition-wins destinations for reference links."""

    output: dict[str, str] = {}
    for definition in _reference_definitions(text):
        output.setdefault(definition.label, definition.destination)
    return output


@dataclass(frozen=True)
class _MarkdownLink:
    """One parsed Markdown link with source offsets and a destination."""

    start: int
    end: int
    destination: str
    label: str


def _is_escaped(text: str, index: int) -> bool:
    """Return whether the character at ``index`` has an odd escape run."""

    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _link_label_end(
    text: str,
    opening: int,
    *,
    allow_nesting: bool = True,
    max_characters: int | None = None,
    max_line_endings: int | None = None,
    cache: dict[int, int | None] | None = None,
) -> int | None:
    """Return the closing bracket for one balanced Markdown link label."""

    if cache is not None and opening in cache:
        return cache[opening]
    active_openings = [opening]
    line_endings = 0
    backslashes = 0
    cursor = opening + 1
    while cursor < len(text):
        if max_characters is not None and cursor - opening - 1 > max_characters:
            if cache is not None:
                cache.update((active, None) for active in active_openings)
            return None
        character = text[cursor]
        escaped = backslashes % 2 == 1
        if character == "\\":
            backslashes += 1
            cursor += 1
            continue
        backslashes = 0
        if character in "\r\n":
            line_endings += 1
            if max_line_endings is not None and line_endings > max_line_endings:
                if cache is not None:
                    cache.update((active, None) for active in active_openings)
                return None
            cursor += 2 if character == "\r" and text[cursor : cursor + 2] == "\r\n" else 1
            if re.match(r"[ \t]*(?:\r\n?|\n)", text[cursor:]):
                if cache is not None:
                    cache.update((active, None) for active in active_openings)
                return None
            continue
        if escaped:
            cursor += 1
            continue
        if character == "[":
            if not allow_nesting:
                if cache is not None:
                    cache.update((active, None) for active in active_openings)
                return None
            active_openings.append(cursor)
        elif character == "]":
            matched_opening = active_openings.pop()
            if cache is not None:
                cache[matched_opening] = cursor
            if not active_openings:
                return cursor
        cursor += 1
    if cache is not None:
        cache.update((active, None) for active in active_openings)
    return None


def _title_end(text: str, start: int, delimiter: str) -> int | None:
    """Return the first unescaped title delimiter at or after ``start``."""

    cursor = start
    while cursor < len(text):
        if text[cursor] == delimiter and not _is_escaped(text, cursor):
            return cursor
        if text[cursor] in "\r\n":
            return None
        cursor += 1
    return None


def _destination_scan_indexes(
    text: str,
) -> tuple[dict[int, int], _WhitespaceIndex]:
    """Index balanced parentheses and the next whitespace at every position."""

    matches: dict[int, int] = {}
    openings: list[int] = []
    backslashes = 0
    for position, character in enumerate(text):
        if character == "\\":
            backslashes += 1
            continue
        escaped = backslashes % 2 == 1
        backslashes = 0
        if escaped:
            continue
        if character == "(":
            openings.append(position)
        elif character == ")" and openings:
            matches[openings.pop()] = position
    return matches, _WhitespaceIndex(text)


class _WhitespaceIndex:
    """Resolve arbitrary whitespace queries after one linear reverse scan."""

    __slots__ = ("_next",)

    def __init__(self, text: str) -> None:
        next_positions = [-1] * (len(text) + 1)
        nearest = -1
        for position in range(len(text) - 1, -1, -1):
            if text[position] in " \t\r\n":
                nearest = position
            next_positions[position] = nearest
        self._next = tuple(next_positions)

    def first_at_or_after(self, position: int) -> int | None:
        if position < 0 or position >= len(self._next):
            return None
        result = self._next[position]
        return result if result >= 0 else None


def _inline_destination(
    text: str,
    opening: int,
    *,
    cache: dict[int, tuple[int, str] | None] | None = None,
    parenthesis_matches: dict[int, int] | None = None,
    whitespace_index: _WhitespaceIndex | None = None,
) -> tuple[int, str] | None:
    """Parse one inline-link destination and return its end and value."""

    if cache is not None and opening in cache:
        return cache[opening]

    def finish(result: tuple[int, str] | None) -> tuple[int, str] | None:
        if cache is not None:
            cache[opening] = result
        return result

    cursor = _skip_spnl(text, opening + 1)
    if cursor >= len(text):
        return finish(None)
    if text[cursor] == ")":
        return finish((cursor + 1, ""))

    if text[cursor] == "<":
        destination_start = cursor + 1
        cursor = destination_start
        while cursor < len(text):
            if text[cursor] in "\r\n":
                return finish(None)
            if text[cursor] == "<" and not _is_escaped(text, cursor):
                return finish(None)
            if text[cursor] == ">" and not _is_escaped(text, cursor):
                destination = _markdown_unescape(text[destination_start:cursor])
                cursor += 1
                break
            cursor += 1
        else:
            return finish(None)
    else:
        destination_start = cursor
        if (
            parenthesis_matches is not None
            and whitespace_index is not None
        ):
            first_whitespace = whitespace_index.first_at_or_after(cursor)
            matched_closing = parenthesis_matches.get(opening)
            if matched_closing is not None and (
                first_whitespace is None or matched_closing < first_whitespace
            ):
                destination = _markdown_unescape(text[cursor:matched_closing])
                return finish((matched_closing + 1, destination))
            if matched_closing is None and first_whitespace is None:
                return finish(None)
        depth = 0
        while cursor < len(text):
            character = text[cursor]
            if character in "\r\n":
                if depth:
                    return finish(None)
                break
            if _is_escaped(text, cursor):
                cursor += 1
                continue
            if character == "(":
                depth += 1
            elif character == ")":
                if depth == 0:
                    destination = _markdown_unescape(text[destination_start:cursor])
                    return finish((cursor + 1, destination))
                depth -= 1
            elif character in " \t" and depth == 0:
                break
            cursor += 1
        destination = _markdown_unescape(text[destination_start:cursor])
        if not destination:
            return finish(None)

        if cursor >= len(text) and depth:
            return finish(None)

    whitespace_start = cursor
    cursor = _skip_spnl(text, cursor)
    if cursor < len(text) and text[cursor] == ")":
        return finish((cursor + 1, destination))
    if cursor == whitespace_start or cursor >= len(text):
        return finish(None)

    title_end = _title_at(text, cursor)
    if title_end is None:
        return finish(None)
    cursor = _skip_spnl(text, title_end)
    if cursor >= len(text) or text[cursor] != ")":
        return finish(None)
    return finish((cursor + 1, destination))


def _markdown_links(text: str) -> tuple[_MarkdownLink, ...]:
    """Return inline Markdown links, including balanced destinations."""

    output = []
    label_ends: dict[int, int | None] = {}
    destination_cache: dict[int, tuple[int, str] | None] = {}
    parenthesis_matches, whitespace_index = _destination_scan_indexes(text)
    image_ranges = _image_link_ranges(text)
    image_index = 0
    cursor = 0
    while cursor < len(text):
        opening = text.find("[", cursor)
        if opening < 0:
            break
        while (
            image_index < len(image_ranges)
            and image_ranges[image_index][1] <= opening
        ):
            image_index += 1
        inside_image = (
            image_index < len(image_ranges)
            and image_ranges[image_index][0] <= opening < image_ranges[image_index][1]
        )
        if (
            _is_escaped(text, opening)
            or (
                opening > 0
                and text[opening - 1] == "!"
                and not _is_escaped(text, opening - 1)
            )
            or inside_image
        ):
            cursor = opening + 1
            continue
        label_end = _link_label_end(text, opening, cache=label_ends)
        if (
            label_end is None
            or label_end + 1 >= len(text)
            or text[label_end + 1] != "("
        ):
            cursor = opening + 1
            continue
        parsed = _inline_destination(
            text,
            label_end + 1,
            cache=destination_cache,
            parenthesis_matches=parenthesis_matches,
            whitespace_index=whitespace_index,
        )
        if parsed is None:
            cursor = opening + 1
            continue
        end, destination = parsed
        output.append(
            _MarkdownLink(
                opening,
                end,
                destination,
                text[opening + 1 : label_end],
            )
        )
        cursor = end
    return tuple(output)


def _valid_reference_label_source(value: str) -> bool:
    """Return whether source text can serve as a CommonMark link label."""

    if not value.strip() or len(value) > 999:
        return False
    line_endings = 0
    cursor = 0
    while cursor < len(value):
        line_ending = _line_ending_length(value, cursor)
        if line_ending:
            line_endings += 1
            if line_endings > 1:
                return False
            cursor += line_ending
            continue
        if value[cursor] in "[]" and not _is_escaped(value, cursor):
            return False
        cursor += 1
    return True


def _reference_links(
    text: str,
    definition_records: tuple[_ReferenceDefinition, ...] | None = None,
) -> tuple[_MarkdownLink, ...]:
    """Return full, collapsed, and shortcut CommonMark reference links."""

    if definition_records is None:
        definition_records = _reference_definitions(text)
    definitions: dict[str, str] = {}
    for definition in definition_records:
        definitions.setdefault(definition.label, definition.destination)
    if not definitions:
        return ()
    image_ranges = _image_link_ranges(text)
    blocked = sorted(
        [(definition.start, definition.end) for definition in definition_records]
        + [
        (link.start, link.end) for link in _markdown_links(text)
        ]
    )
    candidates = []
    label_ends: dict[int, int | None] = {}
    destination_cache: dict[int, tuple[int, str] | None] = {}
    parenthesis_matches, whitespace_index = _destination_scan_indexes(text)
    image_index = 0
    cursor = 0
    while cursor < len(text):
        opening = text.find("[", cursor)
        if opening < 0:
            break
        while (
            image_index < len(image_ranges)
            and image_ranges[image_index][1] <= opening
        ):
            image_index += 1
        inside_image = (
            image_index < len(image_ranges)
            and image_ranges[image_index][0] <= opening < image_ranges[image_index][1]
        )
        if (
            _is_escaped(text, opening)
            or (
                opening > 0
                and text[opening - 1] == "!"
                and not _is_escaped(text, opening - 1)
            )
            or inside_image
        ):
            cursor = opening + 1
            continue
        label_end = _link_label_end(text, opening, cache=label_ends)
        if label_end is None:
            cursor = opening + 1
            continue
        label = text[opening + 1 : label_end]
        end = label_end + 1
        reference = label
        if (
            end < len(text)
            and text[end] == "("
            and _inline_destination(
                text,
                end,
                cache=destination_cache,
                parenthesis_matches=parenthesis_matches,
                whitespace_index=whitespace_index,
            )
            is not None
        ):
            cursor = end + 1
            continue
        if end < len(text) and text[end] == "[":
            reference_end = _link_label_end(
                text,
                end,
                allow_nesting=False,
                max_characters=999,
                max_line_endings=1,
            )
            if reference_end is None:
                cursor = end + 1
                continue
            reference = text[end + 1 : reference_end] or label
            end = reference_end + 1
        elif not _valid_reference_label_source(label):
            cursor = opening + 1
            continue
        normalized_reference = _reference_label(reference)
        if normalized_reference not in definitions:
            cursor = opening + 1
            continue
        destination = definitions[normalized_reference]
        candidates.append(
            _MarkdownLink(
                opening,
                end,
                destination,
                label,
            )
        )
        cursor = opening + 1
    nested_invalid: set[int] = set()
    active: list[int] = []
    ordered = sorted(
        range(len(candidates)),
        key=lambda index: (
            candidates[index].start,
            -candidates[index].end,
        ),
    )
    for index in ordered:
        candidate = candidates[index]
        while active and candidates[active[-1]].end <= candidate.start:
            active.pop()
        while active:
            parent_index = active[-1]
            parent = candidates[parent_index]
            if candidate.end > parent.end:
                break
            first_label_end = _link_label_end(
                text,
                parent.start,
                cache=label_ends,
            )
            own_reference_label = (
                first_label_end is not None
                and candidate.start == first_label_end + 1
                and candidate.end == parent.end
            )
            if own_reference_label:
                break
            nested_invalid.add(parent_index)
            active.pop()
        active.append(index)
    candidates = [
        candidate
        for index, candidate in enumerate(candidates)
        if index not in nested_invalid
    ]
    candidates.sort(key=lambda link: (link.start, -link.end))
    output = []
    blocked_index = 0
    accepted_end = -1
    for candidate in candidates:
        while (
            blocked_index < len(blocked)
            and blocked[blocked_index][1] <= candidate.start
        ):
            blocked_index += 1
        blocked_overlap = (
            blocked_index < len(blocked)
            and blocked[blocked_index][0] < candidate.end
        )
        if candidate.start < accepted_end or blocked_overlap:
            continue
        output.append(candidate)
        accepted_end = candidate.end
    return tuple(output)


def _image_link_ranges(text: str) -> tuple[tuple[int, int], ...]:
    """Return source ranges occupied by valid CommonMark images."""

    output = []
    definitions = {
        definition.label for definition in _reference_definitions(text)
    }
    label_ends: dict[int, int | None] = {}
    destination_cache: dict[int, tuple[int, str] | None] = {}
    parenthesis_matches, whitespace_index = _destination_scan_indexes(text)
    cursor = 0
    while cursor < len(text):
        marker = text.find("![", cursor)
        if marker < 0:
            break
        opening = marker + 1
        if _is_escaped(text, marker):
            cursor = opening + 1
            continue
        label_end = _link_label_end(text, opening, cache=label_ends)
        if label_end is None:
            cursor = opening + 1
            continue
        end = label_end + 1
        image_end = None
        label = text[opening + 1 : label_end]
        if end < len(text) and text[end] == "(":
            parsed = _inline_destination(
                text,
                end,
                cache=destination_cache,
                parenthesis_matches=parenthesis_matches,
                whitespace_index=whitespace_index,
            )
            if parsed is not None:
                image_end = parsed[0]
            elif (
                _valid_reference_label_source(label)
                and _reference_label(label) in definitions
            ):
                image_end = end
        elif end < len(text) and text[end] == "[":
            reference_end = _link_label_end(
                text,
                end,
                allow_nesting=False,
                max_characters=999,
                max_line_endings=1,
            )
            if reference_end is not None:
                reference = text[end + 1 : reference_end]
                reference = reference or label
                if _reference_label(reference) in definitions:
                    image_end = reference_end + 1
        elif (
            _valid_reference_label_source(label)
            and _reference_label(label) in definitions
        ):
            image_end = end
        if image_end is None:
            cursor = opening + 1
            continue
        output.append((marker, image_end))
        cursor = image_end
    return tuple(output)


def _mask_markdown_comments(text: str) -> str:
    """Mask HTML comments while preserving source offsets."""

    characters = list(text)
    cursor = 0
    while True:
        opening = text.find("<!--", cursor)
        if opening < 0:
            break
        closing = text.find("-->", opening + 4)
        end = len(text) if closing < 0 else closing + 3
        for index in range(opening, end):
            if characters[index] not in "\r\n":
                characters[index] = " "
        cursor = end
    return "".join(characters)


def _html_span_end(text: str, opening: int) -> int | None:
    """Return the end of an inline HTML or autolink span."""

    if text.startswith("<!--", opening):
        closing = text.find("-->", opening + 4)
        return len(text) if closing < 0 else closing + 3
    if text.startswith("<![CDATA[", opening):
        closing = text.find("]]>", opening + 9)
        return None if closing < 0 else closing + 3
    if text.startswith("<?", opening):
        closing = text.find("?>", opening + 2)
        return None if closing < 0 else closing + 2

    cursor = opening + 1
    quote: str | None = None
    while cursor < len(text):
        character = text[cursor]
        if character in "\r\n" and quote is None:
            return None
        if quote is not None:
            if character == quote:
                quote = None
        elif character in {'"', "'"}:
            quote = character
        elif character == ">":
            inner = text[opening + 1 : cursor]
            tag = re.match(r"/?[A-Za-z][A-Za-z0-9-]*", inner)
            valid_tag = bool(
                tag
                and (
                    tag.end() == len(inner)
                    or inner[tag.end()] in " \t/"
                )
            )
            autolink = bool(
                re.fullmatch(
                    r"[A-Za-z][A-Za-z0-9.+-]{1,31}:[^<>\x00-\x20]*",
                    inner,
                )
                or re.fullmatch(
                    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
                    r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?",
                    inner,
                )
            )
            declaration = inner.startswith("!") and len(inner) > 1
            return cursor + 1 if valid_tag or autolink or declaration else None
        cursor += 1
    return None


def _mask_html_blocks(text: str, structural_text: str | None = None) -> str:
    """Mask CommonMark HTML blocks while preserving source offsets."""

    if structural_text is None:
        structural_text = text
    if len(structural_text) != len(text):
        raise ValueError("HTML block views must preserve source offsets")
    characters = list(text)

    def mask(start: int, end: int) -> None:
        for index in range(start, end):
            if characters[index] not in "\r\n":
                characters[index] = " "

    active_close: re.Pattern[str] | None = None
    blank_terminated = False
    active_container_indent = 0
    paragraph_open = False
    list_indents: list[int] = []
    offset = 0
    def physical_lines(value: str) -> list[str]:
        """Split only at CommonMark line endings."""

        output = []
        cursor = 0
        for ending in re.finditer(r"\r\n|\r|\n", value):
            output.append(value[cursor : ending.end()])
            cursor = ending.end()
        if cursor < len(value):
            output.append(value[cursor:])
        return output

    lines = physical_lines(text)
    structural_lines = physical_lines(structural_text)
    for line, structural_line in zip(lines, structural_lines, strict=True):
        content = line.rstrip("\r\n")
        structural_content = structural_line.rstrip("\r\n")
        blank = not structural_content.strip()
        physical_content = structural_content.lstrip(" \t")
        physical_indent = len(structural_content) - len(physical_content)

        if (
            (active_close is not None or blank_terminated)
            and active_container_indent
            and not blank
            and physical_indent < active_container_indent
        ):
            active_close = None
            blank_terminated = False
            active_container_indent = 0
            paragraph_open = False
        if active_close is not None or blank_terminated:
            if blank_terminated and blank:
                blank_terminated = False
                active_container_indent = 0
                paragraph_open = False
            else:
                mask(offset, offset + len(line))
                if active_close is not None and active_close.search(content):
                    active_close = None
                    active_container_indent = 0
                    paragraph_open = False
            offset += len(line)
            continue

        if not blank:
            while list_indents and physical_indent < list_indents[-1]:
                list_indents.pop()
        container_indent = list_indents[-1] if list_indents else 0
        cursor = container_indent
        while (
            cursor < len(structural_content)
            and structural_content[cursor] in " \t"
        ):
            cursor += 1
        indentation = cursor - container_indent
        marker_seen = False
        if indentation <= 3:
            while True:
                list_marker = re.match(
                    r"(?:[-*+]|\d{1,9}[.)])(?:[ \t]+|$)",
                    structural_content[cursor:],
                )
                if list_marker is None:
                    break
                marker_seen = True
                cursor += list_marker.end()
                if not list_indents or cursor > list_indents[-1]:
                    list_indents.append(cursor)
                while (
                    cursor < len(structural_content)
                    and structural_content[cursor] in " \t"
                ):
                    cursor += 1
        block_content = content[cursor:]
        structural_block_content = structural_content[cursor:]
        line_paragraph_open = False if marker_seen else paragraph_open
        if indentation <= 3:
            type_one = re.match(
                r"^<(script|pre|style|textarea)(?:\s|>|$)",
                block_content,
                re.IGNORECASE,
            )
            if type_one is not None:
                tag = re.escape(type_one.group(1))
                active_close = re.compile(rf"</{tag}\s*>", re.IGNORECASE)
            elif block_content.startswith("<!--"):
                active_close = re.compile(r"-->")
            elif block_content.startswith("<?"):
                active_close = re.compile(r"\?>")
            elif re.match(r"^<![A-Z]", block_content) is not None:
                active_close = re.compile(r">")
            elif block_content.startswith("<![CDATA["):
                active_close = re.compile(r"\]\]>")
            elif _HTML_BLOCK_TAG.match(block_content) is not None or (
                not line_paragraph_open
                and _COMPLETE_HTML_TAG.fullmatch(block_content)
            ):
                blank_terminated = True

        if active_close is not None or blank_terminated:
            active_container_indent = list_indents[-1] if list_indents else 0
            mask(offset, offset + len(line))
            if active_close is not None and active_close.search(content):
                active_close = None
                active_container_indent = 0
            paragraph_open = False
        elif blank:
            paragraph_open = False
        elif marker_seen:
            paragraph_open = bool(structural_block_content.strip()) and not (
                _BLOCK_CONSTRUCT.match(structural_block_content)
                or _THEMATIC_OR_SETEXT.match(structural_block_content)
            )
        elif _BLOCK_CONSTRUCT.match(
            structural_block_content
        ) or _THEMATIC_OR_SETEXT.match(structural_block_content):
            paragraph_open = False
        else:
            paragraph_open = True
        offset += len(line)
    return "".join(characters)


def _is_angle_link_destination(text: str, opening: int) -> bool:
    """Return whether an angle span begins an inline-link destination."""

    previous = opening - 1
    while previous >= 0 and text[previous] in " \t":
        previous -= 1
    if previous < 0 or text[previous] != "(":
        return False
    label_end = previous - 1
    while label_end >= 0 and text[label_end] in " \t":
        label_end -= 1
    return label_end >= 0 and text[label_end] == "]"


def _autolinks(text: str) -> tuple[_MarkdownLink, ...]:
    """Return CommonMark URI and email autolinks."""

    output: list[_MarkdownLink] = []
    cursor = 0
    while True:
        opening = text.find("<", cursor)
        if opening < 0:
            break
        if _is_angle_link_destination(text, opening):
            cursor = opening + 1
            continue
        end = _html_span_end(text, opening)
        if end is None:
            cursor = opening + 1
            continue
        inner = text[opening + 1 : end - 1]
        if re.fullmatch(
            r"[A-Za-z][A-Za-z0-9.+-]{1,31}:[^<>\x00-\x20]*",
            inner,
        ):
            destination = inner
        elif re.fullmatch(
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
            r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?",
            inner,
        ):
            destination = "mailto:" + inner
        else:
            cursor = end
            continue
        output.append(_MarkdownLink(opening, end, destination, inner))
        cursor = end
    return tuple(output)


def _mask_inline_html(text: str) -> str:
    """Mask inline HTML syntax without masking angle link destinations."""

    characters = list(text)
    cursor = 0
    while True:
        opening = text.find("<", cursor)
        if opening < 0:
            break
        end = (
            None
            if _is_angle_link_destination(text, opening)
            else _html_span_end(text, opening)
        )
        if end is None:
            cursor = opening + 1
            continue
        for index in range(opening, end):
            if characters[index] not in "\r\n":
                characters[index] = " "
        cursor = end
    return "".join(characters)


def _mask_deleted_markdown(text: str) -> str:
    """Mask paired deletion markers and their content without changing offsets."""

    characters = list(text)
    cursor = 0
    while True:
        opening = text.find("~~", cursor)
        if opening < 0:
            break
        closing = text.find("~~", opening + 2)
        if closing < 0:
            break
        for index in range(opening, closing + 2):
            if characters[index] not in "\r\n":
                characters[index] = " "
        cursor = closing + 2
    return "".join(characters)


def _all_markdown_links(text: str) -> tuple[_MarkdownLink, ...]:
    """Return inline and reference links in source order."""

    code_masked = _mask_markdown_code(text)
    block_masked = _mask_html_blocks(code_masked, structural_text=text)
    definition_source = _mask_markdown_comments(block_masked)
    definitions = _reference_definitions(definition_source)
    occurrence_source = _mask_deleted_markdown(block_masked)
    autolinks = _autolinks(occurrence_source)
    text = _mask_inline_html(occurrence_source)
    return tuple(
        sorted(
            (
                *autolinks,
                *_markdown_links(text),
                *_reference_links(text, definitions),
            ),
            key=lambda link: (link.start, link.end),
        )
    )


def _mask_wikilink_context(text: str) -> str:
    """Mask images and reference definitions while preserving link syntax."""

    characters = list(text)
    ranges = [
        *(_image_link_ranges(text)),
        *(
            (definition.start, definition.end)
            for definition in _reference_definitions(text)
        ),
    ]
    for start, end in ranges:
        for index in range(start, end):
            if characters[index] not in "\r\n":
                characters[index] = " "
    return "".join(characters)


def _mask_wikilink_metadata(text: str) -> str:
    """Mask all non-visible Markdown metadata before wikilink scans."""

    context = _mask_wikilink_context(text)
    characters = list(context)
    link_metadata = []
    label_ends: dict[int, int | None] = {}
    for link in _all_markdown_links(text):
        if link.start >= len(text) or text[link.start] != "[":
            continue
        label_end = _link_label_end(text, link.start, cache=label_ends)
        if label_end is not None and label_end + 1 < link.end:
            link_metadata.append((label_end + 1, link.end))
    for start, end in link_metadata:
        for index in range(start, end):
            if characters[index] not in "\r\n":
                characters[index] = " "
    return "".join(characters)


def _mask_link_graph_source(text: str) -> str:
    """Mask nonprose syntax while preserving links in rendered quotations."""

    code_masked = _mask_markdown_code(text)
    block_masked = _mask_html_blocks(code_masked, structural_text=text)
    return _mask_deleted_markdown(_mask_inline_html(block_masked))


def _recognized_angle_ranges(text: str) -> tuple[tuple[int, int], ...]:
    """Return recognized angle spans with one monotonic source scan."""

    output: list[tuple[int, int]] = []
    opening: int | None = None
    quote: str | None = None
    cursor = 0
    while cursor < len(text):
        character = text[cursor]
        if opening is None:
            if character != "<":
                cursor += 1
                continue
            if text.startswith(("<!--", "<![CDATA[", "<?"), cursor):
                end = _html_span_end(text, cursor)
                if end is not None:
                    output.append((cursor, end))
                    cursor = end
                    continue
            opening = cursor
            quote = None
            cursor += 1
            continue
        if quote is not None:
            if character == quote:
                quote = None
            cursor += 1
            continue
        if character in {'"', "'"}:
            quote = character
            cursor += 1
            continue
        if character in "\r\n":
            opening = None
            cursor += 1
            continue
        if character == "<":
            opening = cursor
            cursor += 1
            continue
        if character == ">":
            end = _html_span_end(text, opening)
            if end is not None:
                output.append((opening, end))
            opening = None
        cursor += 1
    return tuple(output)


def _mask_markdown_code(text: str) -> str:
    """Mask fenced and inline code while preserving character offsets."""

    characters = list(text)

    def mask(start: int, end: int) -> None:
        for index in range(start, end):
            if characters[index] not in "\r\n":
                characters[index] = " "

    fence: tuple[str, int] | None = None
    indented_code = False
    paragraph_open = False
    offset = 0
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        marker = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
        if fence is None:
            if marker is not None:
                token = marker.group(1)
                fence = (token[0], len(token))
                mask(offset, offset + len(line))
                indented_code = False
                paragraph_open = False
            else:
                indentation = 0
                for character in content:
                    if character == " ":
                        indentation += 1
                    elif character == "\t":
                        indentation += 4 - indentation % 4
                    else:
                        break
                blank = not content.strip()
                if indentation >= 4 and (indented_code or not paragraph_open):
                    mask(offset, offset + len(line))
                    indented_code = True
                else:
                    indented_code = False
                    if blank:
                        paragraph_open = False
                    elif _BLOCK_CONSTRUCT.match(content):
                        paragraph_open = bool(
                            re.match(
                                r"^[ \t]{0,3}(?:[-*+]|\d{1,9}[.)])[ \t]+\S",
                                content,
                            )
                        )
                    else:
                        paragraph_open = True
        else:
            fence_character, minimum = fence
            closing = re.match(
                rf"^[ \t]{{0,3}}{re.escape(fence_character)}{{{minimum},}}"
                r"[ \t]*(?:\r?\n)?$",
                line,
            )
            mask(offset, offset + len(line))
            if closing is not None:
                fence = None
                paragraph_open = False
        offset += len(line)

    masked = "".join(characters)
    angle_ranges = _recognized_angle_ranges(masked)
    index = 0
    angle_index = 0
    while index < len(masked):
        opening = masked.find("`", index)
        if opening < 0:
            break
        while (
            angle_index < len(angle_ranges)
            and angle_ranges[angle_index][1] <= opening
        ):
            angle_index += 1
        if (
            angle_index < len(angle_ranges)
            and angle_ranges[angle_index][0] <= opening < angle_ranges[angle_index][1]
        ):
            index = angle_ranges[angle_index][1]
            continue
        run_end = opening
        while run_end < len(masked) and masked[run_end] == "`":
            run_end += 1
        token = masked[opening:run_end]
        closing = masked.find(token, run_end)
        if closing < 0:
            index = run_end
            continue
        mask(opening, closing + len(token))
        index = closing + len(token)
    return "".join(characters)


def _mask_nonassertive_markdown(text: str) -> str:
    """Mask comments, quotations, and deleted prose while preserving offsets."""

    characters = list(text)

    def mask(start: int, end: int) -> None:
        for index in range(start, end):
            if characters[index] not in "\r\n":
                characters[index] = " "

    cursor = 0
    while True:
        opening = text.find("<!--", cursor)
        if opening < 0:
            break
        closing = text.find("-->", opening + 4)
        end = len(text) if closing < 0 else closing + 3
        mask(opening, end)
        cursor = end

    offset = 0
    blockquote_paragraph = False
    for line in text.splitlines(keepends=True):
        marker = _BLOCKQUOTE_LINE.match(line)
        if marker is not None:
            mask(offset, offset + len(line))
            content = line[marker.end() :]
            blockquote_paragraph = bool(content.strip()) and not _BLOCK_CONSTRUCT.match(
                content
            )
        elif (
            blockquote_paragraph
            and line.strip()
            and _BLOCK_CONSTRUCT.match(line) is None
        ):
            mask(offset, offset + len(line))
        else:
            blockquote_paragraph = False
        offset += len(line)

    cursor = 0
    while True:
        opening = text.find("~~", cursor)
        if opening < 0:
            break
        closing = text.find("~~", opening + 2)
        if closing < 0:
            break
        mask(opening, closing + 2)
        cursor = closing + 2
    return "".join(characters)


def _parse_scalar(v: str):
    v = v.strip()
    if not v:
        return ""
    if v.startswith("{") and v.endswith("}"):
        # flow mapping: OKF v0.2 spells its trust/lifecycle families this
        # way (`generated: { by: x, at: y }`). One level deep, scalar
        # values only: exactly the subset the families use.
        out: dict = {}
        for part in v[1:-1].split(","):
            if ":" not in part:
                continue
            k, _, val = part.partition(":")
            if k.strip():
                out[k.strip()] = _parse_scalar(val)
        return out
    if v.startswith(("'", '"')) and v.endswith(v[0]) and len(v) >= 2:
        return v[1:-1]
    low = v.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def _split_flow(inner: str) -> list[str]:
    """Split a flow list's items on commas OUTSIDE braces, so a list of
    flow mappings (`[{ by: x, at: y }, { by: z }]`) keeps each mapping
    whole."""
    parts, cur, depth = [], [], 0
    for ch in inner:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a document into (frontmatter dict, body). Permissive: a file
    with no valid frontmatter yields ({}, text): never an exception."""
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    meta: dict = {}
    key = None
    for raw in lines[1:end]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = raw.strip()
        if stripped.startswith("- ") and key is not None:
            existing = meta.get(key)
            if not isinstance(existing, list):
                existing = [] if existing in ("", None) else [existing]
            existing.append(_parse_scalar(stripped[2:]))
            meta[key] = existing
            continue
        if ":" in stripped and not raw.startswith((" ", "\t")):
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()
            if val.startswith("[") and val.endswith("]"):
                inner = val[1:-1].strip()
                meta[key] = ([_parse_scalar(x) for x in _split_flow(inner)]
                             if inner else [])
            else:
                meta[key] = _parse_scalar(val)
    if not meta:  # a leading '---' that parsed to zero keys is a horizontal rule,
        return {}, text  # not frontmatter: keep the whole document as body
    body = "\n".join(lines[end + 1:])
    return meta, body


def _fm_safe(v) -> str:
    """Frontmatter values must never contain newlines: a value with an
    embedded newline would inject arbitrary keys on reparse (a real attack
    surface when values come from external data like imported graphs)."""
    return str(v).replace("\r", " ").replace("\n", " ")


def _fm_value(v) -> str:
    """One frontmatter value in the flow style the parser round-trips:
    dicts as `{ k: v, ... }` (the OKF v0.2 family spelling), everything
    else newline-scrubbed scalar text."""
    if isinstance(v, dict):
        inner = ", ".join(f"{_fm_safe(k)}: {_fm_safe(x)}"
                          for k, x in v.items())
        return "{ " + inner + " }"
    if isinstance(v, bool):
        return "true" if v else "false"
    return _fm_safe(v)


def render_frontmatter(meta: dict) -> str:
    out = ["---"]
    for k, v in meta.items():
        k = _fm_safe(k)
        if isinstance(v, list):
            items = ", ".join(_fm_value(x) for x in v)
            out.append(f"{k}: [{items}]")
        else:
            out.append(f"{k}: {_fm_value(v)}")
    out.append("---")
    return "\n".join(out)


def generated_stamp(when: float | None = None) -> dict:
    """The OKF v0.2 `generated` family for a note muninn writes now:
    the tool actor plus an ISO instant (§5.2, §7). Every writer uses
    this instead of the retired v0.1 `timestamp` key."""
    return {"by": MUNINN_ACTOR,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                time.localtime(when))}


def render_note(meta: dict, body: str) -> str:
    """The canonical on-disk form of a note: the single source of truth
    for byte-identical comparisons (e.g. re-import hash-skip)."""
    return render_frontmatter(meta) + "\n\n" + body.rstrip() + "\n"


@dataclass
class Note:
    """One concept document, keyed by its bundle-relative posix path."""

    path: str
    meta: dict
    body: str
    links: list[str] = field(default_factory=list)  # resolved bundle-relative targets
    # Keep this field before ``plain_links`` so the original five positional
    # arguments retain their public meaning.
    # typed weighted edges parsed from `- <relation> [[Target]] (<confidence>)`
    # body lines: {target, relation, confidence_word, weight}. Their targets
    # ALSO appear in ``links``: link-only consumers see them as plain links.
    typed_links: list[dict] = field(default_factory=list)
    # Resolved links that occur outside an exact typed-link list line. This
    # preserves the authored 1.0 signal when one pair has both a typed and a
    # plain link while keeping ``links`` as the public deduplicated view.
    plain_links: list[str] = field(default_factory=list)
    _relationship_identity_pattern: str | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _relationship_identity_key: tuple[str, ...] | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    @property
    def title(self) -> str:
        t = self.meta.get("title")
        if t:
            return str(t)
        base = os.path.basename(self.path)[:-3]
        return base.replace("-", " ").replace("_", " ")

    @property
    def description(self) -> str:
        return str(self.meta.get("description", ""))

    @property
    def tags(self) -> list[str]:
        v = self.meta.get("tags", [])
        return [str(x) for x in v] if isinstance(v, list) else [str(v)]

    def supersedes(self) -> list[str]:
        v = self.meta.get("supersedes", [])
        return [str(x) for x in v] if isinstance(v, list) else ([str(v)] if v else [])

    def guards(self) -> list[str]:
        """Paths this note guards (lessons): when a session's changed
        files overlap them, the primed pack carries the note. Empty for
        a guardless lesson (rides along every session) and for ordinary
        notes."""
        v = self.meta.get("guards", [])
        return [str(x) for x in v] if isinstance(v, list) else ([str(v)] if v else [])

    # -- OKF v0.2 trust & lifecycle (§5), legacy-tolerant ----------------

    def generated_at(self) -> str:
        """When the content last meaningfully changed: ``generated.at``
        (OKF v0.2 §5.2), falling back to the legacy v0.1 ``timestamp``
        (§13.1 sanctions exactly this fallback)."""
        g = self.meta.get("generated")
        if isinstance(g, dict) and g.get("at"):
            return str(g["at"])
        return str(self.meta.get("timestamp", ""))

    def status(self) -> str:
        """OKF v0.2 lifecycle §5.4: draft | stable | deprecated; absent
        means stable."""
        return str(self.meta.get("status") or "stable").strip().lower()

    def is_stale(self, today: str | None = None) -> bool:
        """OKF v0.2 §5.5: stale when today >= ``stale_after`` (absolute
        YYYY-MM-DD; ISO strings compare lexically)."""
        s = str(self.meta.get("stale_after") or "").strip()
        if not s:
            return False
        today = today or time.strftime("%Y-%m-%d")
        return today >= s[:10]


class Bundle:
    """An OKF bundle loaded into memory. ``notes`` maps rel-path -> Note."""

    def __init__(
        self,
        root: str,
        *,
        max_note_bytes: int = MAX_NOTE_BYTES,
        max_bundle_bytes: int = MAX_BUNDLE_BYTES,
        max_notes: int = MAX_BUNDLE_NOTES,
        max_directories: int = MAX_BUNDLE_DIRECTORIES,
        max_entries: int = MAX_BUNDLE_ENTRIES,
        max_depth: int = MAX_BUNDLE_DEPTH,
    ):
        limits = {
            "max_note_bytes": max_note_bytes,
            "max_bundle_bytes": max_bundle_bytes,
            "max_notes": max_notes,
            "max_directories": max_directories,
            "max_entries": max_entries,
            "max_depth": max_depth,
        }
        invalid = [name for name, value in limits.items() if value < 1]
        if invalid:
            raise ValueError(f"bundle limit must be positive: {invalid[0]}")
        self.root = os.path.abspath(root)
        self.max_note_bytes = max_note_bytes
        self.max_bundle_bytes = max_bundle_bytes
        self.max_notes = max_notes
        self.max_directories = max_directories
        self.max_entries = max_entries
        self.max_depth = max_depth
        self.notes: dict[str, Note] = {}
        self.backlinks: dict[str, list[str]] = {}
        self._excluded_files, self._excluded_directories = _read_exclusions(self.root)
        self._wikilink_sources_by_name: dict[str, set[str]] = {}
        self._wikilink_names_by_source: dict[str, set[str]] = {}
        self._markdown_sources_by_target: dict[str, set[str]] = {}
        self._markdown_targets_by_source: dict[str, set[str]] = {}
        self._markdown_path_aliases: dict[str, str] = {}
        self._load()

    # -- loading ---------------------------------------------------------

    def _read_note(self, full: str) -> bytes | None:
        """Read one regular note through a bounded, nonblocking descriptor."""
        try:
            before = os.lstat(full)
            mode = before.st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                return None
            flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
            if hasattr(os, "O_NOFOLLOW") and not stat.S_ISLNK(mode):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(full, flags)
        except OSError:
            return None
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                return None
            if not stat.S_ISLNK(mode) and not _same_file(before, opened):
                return None
            if opened.st_size > self.max_note_bytes:
                raise ValueError(
                    f"note exceeds {self.max_note_bytes} bytes: "
                    f"{os.path.relpath(full, self.root)}"
                )
            return _read_bounded_descriptor(
                descriptor,
                self.max_note_bytes,
                f"note {os.path.relpath(full, self.root)!r}",
            )
        finally:
            os.close(descriptor)

    def _load(self) -> None:
        """Load one bounded snapshot and rebuild all derived link state."""
        self.notes.clear()
        self.backlinks.clear()
        self._wikilink_sources_by_name.clear()
        self._wikilink_names_by_source.clear()
        self._markdown_sources_by_target.clear()
        self._markdown_targets_by_source.clear()
        self._markdown_path_aliases.clear()
        self._excluded_files, self._excluded_directories = _read_exclusions(
            self.root
        )
        self.__dict__.pop("_name_index_cache", None)
        self._invalidate_relationship_caches()

        # Mounted directories contribute notes. In-bundle aliases do not
        # rename canonical paths. Every traversal dimension has a hard limit.
        root_real = os.path.realpath(self.root)
        seen_directories: set[str] = set()
        file_candidates: dict[
            tuple[int, int], list[tuple[tuple[bool, str], str, str]]
        ] = {}
        stack = [(self.root, "", 0)]
        directory_count = 0
        entry_count = 0
        while stack:
            dirpath, relative_directory, depth = stack.pop()
            real = os.path.realpath(dirpath)
            if real in seen_directories:
                continue
            seen_directories.add(real)
            directory_count += 1
            if directory_count > self.max_directories:
                raise ValueError(
                    f"bundle exceeds {self.max_directories} directories"
                )
            try:
                with os.scandir(dirpath) as iterator:
                    names = []
                    for entry in iterator:
                        entry_count += 1
                        if entry_count > self.max_entries:
                            raise ValueError(
                                "bundle exceeds "
                                f"{self.max_entries} filesystem entries"
                            )
                        names.append(entry.name)
            except OSError:
                continue

            directories: list[tuple[str, str, int]] = []
            filenames: list[str] = []
            for name in sorted(names):
                full = os.path.join(dirpath, name)
                try:
                    is_directory = os.path.isdir(full)
                except OSError:
                    continue
                rel = (
                    f"{relative_directory}/{name}"
                    if relative_directory
                    else name
                )
                if is_directory:
                    if name == SIDECAR_DIR or name.startswith("."):
                        continue
                    if _excluded(
                        rel,
                        self._excluded_files,
                        self._excluded_directories,
                        is_directory=True,
                    ):
                        continue
                    if os.path.islink(full):
                        target = os.path.realpath(full)
                        if target == root_real or target.startswith(
                            root_real + os.sep
                        ):
                            continue
                    directories.append((full, rel, depth + 1))
                else:
                    filenames.append(name)

            if directories and depth >= self.max_depth:
                raise ValueError(
                    f"bundle exceeds directory depth {self.max_depth}"
                )
            stack.extend(reversed(directories))

            for filename in filenames:
                if not filename.endswith(".md") or filename in RESERVED:
                    continue
                full = os.path.join(dirpath, filename)
                rel = (
                    f"{relative_directory}/{filename}"
                    if relative_directory
                    else filename
                )
                if _excluded(
                    rel,
                    self._excluded_files,
                    self._excluded_directories,
                    is_directory=False,
                ):
                    continue
                if os.path.islink(full):
                    target = os.path.realpath(full)
                    if target == root_real or target.startswith(
                        root_real + os.sep
                    ):
                        continue
                try:
                    status = os.stat(full)
                except OSError:
                    continue
                if not stat.S_ISREG(status.st_mode):
                    continue
                identity = (status.st_dev, status.st_ino)
                rank = (os.path.islink(full), rel)
                file_candidates.setdefault(identity, []).append(
                    (rank, full, rel)
                )

        selected_files = []
        for candidates in file_candidates.values():
            selected = min(candidates, key=lambda item: item[0])
            selected_files.append(selected)
            for _rank, _full, alias in candidates:
                self._markdown_path_aliases[alias] = selected[2]
        if len(selected_files) > self.max_notes:
            raise ValueError(f"bundle exceeds {self.max_notes} notes")

        total_bytes = 0
        for _rank, full, rel in sorted(
            selected_files,
            key=lambda item: item[2],
        ):
            payload = self._read_note(full)
            if payload is None:
                continue
            total_bytes += len(payload)
            if total_bytes > self.max_bundle_bytes:
                raise ValueError(
                    f"bundle exceeds {self.max_bundle_bytes} note bytes"
                )
            text = payload.decode("utf-8")
            meta, body = parse_frontmatter(text)
            self.notes[rel] = Note(path=rel, meta=meta, body=body)

        self._rebuild_link_dependencies()
        by_name = self._name_index()
        for path in sorted(self.notes):
            self._refresh_link_state(self.notes[path], by_name)

    def _name_index(self) -> dict[str, str]:
        """Return only names that identify one note uniquely."""

        cached = getattr(self, "_name_index_cache", None)
        if cached is not None:
            return cached
        candidates: dict[str, set[str]] = {}
        for path in sorted(self.notes):
            for name in self._identity_names(self.notes[path]):
                candidates.setdefault(name, set()).add(path)
        superseded = set(self.superseded_by())
        result = {}
        for name, paths in candidates.items():
            current = {
                path
                for path in paths
                if path not in superseded
                and self.notes[path].status() != "deprecated"
            }
            if len(current) == 1:
                result[name] = min(current)
            elif not current and len(paths) == 1:
                result[name] = min(paths)
        self._name_index_cache = result
        return result

    @staticmethod
    def _identity_names(note: Note) -> set[str]:
        """Return normalized basename, title, and alias identities."""

        aliases = note.meta.get("aliases", [])
        aliases = aliases if isinstance(aliases, list) else [aliases]
        values = {os.path.basename(note.path)[:-3]}
        title = note.meta.get("title")
        if title:
            values.add(str(title))
        values.update(str(alias) for alias in aliases if alias)
        return {Bundle._normalize_name(value) for value in values if value}

    @staticmethod
    def _normalize_name(value: str) -> str:
        """Normalize a note identity or wikilink name for lookup."""

        return " ".join(value.casefold().split())

    @classmethod
    def _wikilink_lookup_names(cls, value: str) -> tuple[str, ...]:
        """Return the exact and space-to-hyphen wikilink lookup names."""

        name = cls._normalize_name(value)
        hyphenated = name.replace(" ", "-")
        return (name,) if hyphenated == name else (name, hyphenated)

    def _resolve_wikilink_name(
        self, value: str, by_name: dict[str, str]
    ) -> str | None:
        """Resolve a wikilink using the same names tracked as dependencies."""

        path = value.strip()
        if path in self.notes:
            return path
        if "/" in path and path + ".md" in self.notes:
            return path + ".md"
        for name in self._wikilink_lookup_names(value):
            target = by_name.get(name)
            if target is not None:
                return target
        return None

    @staticmethod
    def _markdown_target_path(note: Note, raw: str) -> str | None:
        """Return a Markdown link's normalized bundle-relative target path."""

        raw = raw.strip()
        if raw.startswith("<") and raw.endswith(">"):
            raw = raw[1:-1]
        raw = raw.split("#", 1)[0]
        try:
            raw = urllib.parse.unquote(raw, errors="strict")
        except UnicodeDecodeError:
            return None
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", raw):
            return None
        if "\x00" in raw:
            return None
        if raw.startswith("\\"):
            return None
        raw = raw.replace("\\", "/")
        if (
            "://" in raw
            or raw.startswith(("#", "//"))
            or re.match(r"^[A-Za-z]:/", raw)
        ):
            return None
        if not raw:
            return None
        if raw.startswith("/"):
            target = raw.lstrip("/")
        else:
            target = posixpath.join(posixpath.dirname(note.path), raw)
        target = posixpath.normpath(target)
        if (
            target in {"", "."}
            or target == ".."
            or target.startswith("../")
            or re.match(r"^[A-Za-z]:", target)
        ):
            return None
        if not target.endswith(".md"):
            target += ".md"
        return target

    def _resolved_markdown_target_path(self, note: Note, raw: str) -> str | None:
        """Resolve a Markdown path through any deduplicated physical alias."""

        target = self._markdown_target_path(note, raw)
        if target is None:
            return None
        return self._markdown_path_aliases.get(target, target)

    def _resolve_links(
        self,
        note: Note,
        by_name: dict[str, str],
        body: str | None = None,
    ) -> list[str]:
        """Wikilinks resolve by title/basename; md links by path. Broken
        links are dropped (OKF: tolerated, may be not-yet-written)."""
        out: list[str] = []
        body = _mask_link_graph_source(note.body) if body is None else body
        wikilink_body = _mask_wikilink_metadata(body)
        for m in _WIKILINK.finditer(wikilink_body):
            if _is_escaped(wikilink_body, m.start()):
                continue
            hit = self._resolve_wikilink_name(m.group(1), by_name)
            if hit and hit != note.path:
                out.append(hit)
        for link in _all_markdown_links(body):
            rel = self._resolved_markdown_target_path(note, link.destination)
            if rel in self.notes and rel != note.path:
                out.append(rel)
        seen: set[str] = set()
        uniq = []
        for x in out:
            if x not in seen:
                seen.add(x)
                uniq.append(x)
        return uniq

    @staticmethod
    def _without_typed_link_lines(body: str) -> str:
        """Mask exact typed-link lines while preserving all plain links."""

        output = []
        for line in body.splitlines(keepends=True):
            content = line.rstrip("\r\n")
            ending = line[len(content) :]
            output.append((" " * len(content) if _TYPED_LINE.match(content) else content) + ending)
        return "".join(output)

    def _parse_typed_links(self, note: Note,
                           by_name: dict[str, str]) -> list[dict]:
        """Body lines `- <relation> [[Target]] (<confidence>)` become typed
        weighted edges. Missing relation -> 'related_to'; missing confidence
        -> 'extracted' (weight 1.0); unknown confidence word -> 0.5. Broken
        targets are dropped, like any other link."""
        out: list[dict] = []
        positions: dict[tuple[str, str], int] = {}
        masked = _mask_link_graph_source(note.body)
        for line in masked.split("\n"):
            m = _TYPED_LINE.match(line)
            if not m:
                continue
            tgt = self._resolve_wikilink_name(m.group("target"), by_name)
            if not tgt or tgt == note.path:
                continue
            relation = (m.group("rel") or "").strip() or DEFAULT_RELATION
            conf = (m.group("conf") or "").strip() or DEFAULT_CONFIDENCE
            edge = {"target": tgt, "relation": relation,
                    "confidence_word": conf,
                    "weight": CONF_WEIGHT.get(conf.lower(), 0.5)}
            key = (tgt, relation)
            position = positions.get(key)
            if position is None:
                positions[key] = len(out)
                out.append(edge)
            elif edge["weight"] > out[position]["weight"]:
                out[position] = edge
        return out

    def _remove_link_dependencies(self, path: str) -> None:
        """Remove one source from both maintained dependency indexes."""

        for name in self._wikilink_names_by_source.pop(path, set()):
            sources = self._wikilink_sources_by_name.get(name)
            if sources is None:
                continue
            sources.discard(path)
            if not sources:
                self._wikilink_sources_by_name.pop(name, None)
        for target in self._markdown_targets_by_source.pop(path, set()):
            sources = self._markdown_sources_by_target.get(target)
            if sources is None:
                continue
            sources.discard(path)
            if not sources:
                self._markdown_sources_by_target.pop(target, None)

    def _index_link_dependencies(self, note: Note) -> None:
        """Index all links, including links whose targets do not yet exist."""

        body = _mask_link_graph_source(note.body)
        wikilink_body = _mask_wikilink_metadata(body)
        wikilink_names = {
            name
            for match in _WIKILINK.finditer(wikilink_body)
            if not _is_escaped(wikilink_body, match.start())
            for name in self._wikilink_lookup_names(match.group(1))
        }
        if wikilink_names:
            self._wikilink_names_by_source[note.path] = wikilink_names
            for name in wikilink_names:
                self._wikilink_sources_by_name.setdefault(name, set()).add(
                    note.path
                )

        markdown_targets = {
            target
            for link in _all_markdown_links(body)
            if (
                target := self._resolved_markdown_target_path(
                    note,
                    link.destination,
                )
            )
            is not None
        }
        if markdown_targets:
            self._markdown_targets_by_source[note.path] = markdown_targets
            for target in markdown_targets:
                self._markdown_sources_by_target.setdefault(target, set()).add(
                    note.path
                )

    def _rebuild_link_dependencies(self) -> None:
        """Rebuild link dependency indexes once from the loaded note bodies."""

        self._wikilink_sources_by_name.clear()
        self._wikilink_names_by_source.clear()
        self._markdown_sources_by_target.clear()
        self._markdown_targets_by_source.clear()
        for path in sorted(self.notes):
            self._index_link_dependencies(self.notes[path])

    def _remove_source_backlinks(self, note: Note) -> set[str]:
        """Remove one source's prior backlinks and return touched targets."""

        targets = set(note.links)
        for target in targets:
            sources = self.backlinks.get(target)
            if sources is None:
                continue
            remaining = [source for source in sources if source != note.path]
            if remaining:
                self.backlinks[target] = remaining
            else:
                self.backlinks.pop(target, None)
        return targets

    def _refresh_link_state(self, note: Note, by_name: dict[str, str]) -> None:
        """Refresh one source's links, typed links, and sorted backlinks."""

        touched = self._remove_source_backlinks(note)
        masked_body = _mask_link_graph_source(note.body)
        note.links = self._resolve_links(note, by_name, masked_body)
        note.plain_links = self._resolve_links(
            note,
            by_name,
            self._without_typed_link_lines(masked_body),
        )
        note.typed_links = self._parse_typed_links(note, by_name)
        touched.update(note.links)
        for target in note.links:
            sources = self.backlinks.setdefault(target, [])
            if note.path not in sources:
                sources.append(note.path)
        for target in touched:
            sources = self.backlinks.get(target)
            if sources is not None:
                sources.sort()

    def _sources_for_identities(self, identities: set[str]) -> set[str]:
        """Return sources whose wikilinks may resolve through identities."""

        names = {
            lookup
            for identity in identities
            for lookup in self._wikilink_lookup_names(identity)
        }
        return {
            source
            for name in names
            for source in self._wikilink_sources_by_name.get(name, set())
        }

    def _invalidate_relationship_caches(self) -> None:
        """Discard all relationship indexes derived from current links."""

        self.__dict__.pop("_relationship_edges_cache", None)
        self.__dict__.pop("_relationship_edges_historical_cache", None)
        self.__dict__.pop("_relationship_identity_index_cache", None)
        self.__dict__.pop("_relationship_query_identity_index_cache", None)
        self.__dict__.pop(
            "_relationship_query_identity_index_historical_cache",
            None,
        )

    # -- graph views ------------------------------------------------------

    def typed_edges(self) -> dict[str, list[dict]]:
        """Map rel path -> typed weighted edges parsed from its body."""
        return {p: list(n.typed_links)
                for p, n in self.notes.items() if n.typed_links}

    def by_resource(self, prefix: str | None = None) -> dict[str, Note]:
        """Index notes by their ``resource`` frontmatter field (the source
        file a derived note describes). First note per resource wins.
        ``prefix`` restricts the index to one subtree: how a home brain
        keeps identically-named resources in different rooms apart."""
        out: dict[str, Note] = {}
        pre = prefix.rstrip("/") + "/" if prefix else None
        for path in sorted(self.notes):
            if pre and not path.startswith(pre):
                continue
            r = self.notes[path].meta.get("resource")
            if r:
                out.setdefault(str(r), self.notes[path])
        return out

    # -- superseding (the correction graph) ------------------------------

    def superseded_by(self) -> dict[str, str]:
        """Map old-note path -> newest superseding note path. When two
        notes supersede the same note the path-sorted last wins, so the
        map (and anything exported from it) is deterministic."""
        out: dict[str, str] = {}
        for path in sorted(self.notes):
            note = self.notes[path]
            for old in note.supersedes():
                old_rel = old.replace("\\", "/").lstrip("/")
                if not old_rel.endswith(".md"):
                    old_rel += ".md"
                try:
                    old_rel = self._canonical_note_path(old_rel)
                except ValueError:
                    continue
                if old_rel in self.notes:
                    out[old_rel] = note.path
        return out

    # -- writing ----------------------------------------------------------

    @staticmethod
    def _canonical_note_path(rel: str) -> str:
        """Return one safe canonical path accepted by both write and load."""

        if not isinstance(rel, str) or not rel or "\x00" in rel:
            raise ValueError(f"note path escapes the bundle: {rel!r}")
        drive, _tail = ntpath.splitdrive(rel)
        value = rel.replace("\\", "/")
        canonical = posixpath.normpath(value)
        if (
            drive
            or posixpath.isabs(value)
            or value.startswith("//")
            or re.match(r"^[A-Za-z]:", value)
            or ".." in value.split("/")
        ):
            raise ValueError(f"note path escapes the bundle: {rel!r}")
        parts = canonical.split("/")
        if (
            canonical in {"", ".", ".."}
            or canonical.startswith("../")
            or not canonical.endswith(".md")
            or posixpath.basename(canonical) in RESERVED
            or any(part == SIDECAR_DIR or part.startswith(".") for part in parts)
        ):
            raise ValueError(f"note path escapes the bundle: {rel!r}")
        return canonical

    def _disk_spelling(self, rel: str) -> str:
        """Return the existing directory-entry spelling for a safe path."""
        parent = self.root
        output = []
        for requested in rel.split("/"):
            candidate = os.path.join(parent, requested)
            if not os.path.exists(candidate):
                output.append(requested)
                parent = candidate
                continue
            matches = []
            try:
                with os.scandir(parent) as entries:
                    for entry in entries:
                        try:
                            if os.path.samefile(entry.path, candidate):
                                matches.append(entry.name)
                        except OSError:
                            continue
            except OSError:
                matches = []
            actual = (
                requested
                if requested in matches
                else min(matches, default=requested)
            )
            output.append(actual)
            parent = os.path.join(parent, actual)
        return "/".join(output)

    def write_note(self, rel: str, meta: dict, body: str) -> str:
        rel = self._canonical_note_path(rel)
        rel = self._disk_spelling(rel)
        parts = rel.split("/")
        self._excluded_files, self._excluded_directories = _read_exclusions(
            self.root
        )
        if _excluded(
            rel,
            self._excluded_files,
            self._excluded_directories,
            is_directory=False,
        ):
            raise ValueError(f"note path is excluded by {IGNORE_FILE}: {rel!r}")
        meta.setdefault("type", "note")
        text = render_note(meta, body)
        if len(text.encode("utf-8")) > self.max_note_bytes:
            raise ValueError(f"note exceeds {self.max_note_bytes} bytes: {rel}")
        _atomic_write_text(self.root, parts, text)
        m, b = parse_frontmatter(text)
        previous = self.notes.get(rel)
        note = Note(path=rel, meta=m, body=b)
        old_identities = self._identity_names(previous) if previous else set()
        affected = {rel}
        affected.update(self._sources_for_identities({rel, rel[:-3]}))
        affected.update(self._sources_for_identities(old_identities))
        affected.update(self._markdown_sources_by_target.get(rel, set()))
        if previous is not None:
            self._remove_source_backlinks(previous)
        self._remove_link_dependencies(rel)
        self.notes[rel] = note
        self._markdown_path_aliases[rel] = rel
        self._index_link_dependencies(note)
        new_identities = self._identity_names(note)
        affected.update(self._sources_for_identities(new_identities))
        affected.update(self._markdown_sources_by_target.get(rel, set()))
        self.__dict__.pop("_name_index_cache", None)
        by_name = self._name_index()
        for source_path in sorted(affected):
            source = self.notes.get(source_path)
            if source is not None:
                self._refresh_link_state(source, by_name)
        self._invalidate_relationship_caches()
        return rel

    def generate_index(self) -> str:
        """OKF index.md: sections per top-level dir, '* [Title](path) - desc'."""
        groups: dict[str, list[Note]] = {}
        for note in self.notes.values():
            top = note.path.split("/")[0] if "/" in note.path else "."
            groups.setdefault(top, []).append(note)
        lines = ["---", f'okf_version: "{OKF_VERSION}"', "---", ""]
        for top in sorted(groups):
            heading = "Notes" if top == "." else top.replace("-", " ").title()
            lines.append(f"# {heading}")
            lines.append("")
            for n in sorted(groups[top], key=lambda x: x.path):
                desc = f" - {n.description}" if n.description else ""
                lines.append(f"* [{n.title}](/{n.path}){desc}")
            lines.append("")
        text = "\n".join(lines)
        _atomic_write_text(self.root, ["index.md"], text)
        return text
