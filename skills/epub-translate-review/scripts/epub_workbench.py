#!/usr/bin/env python3
"""A small, dependency-free EPUB translation workbench.

The workbench deliberately separates the four operations used by a translation
review workflow:

``inspect``
    Read ``META-INF/container.xml`` and the publication package and report its
    real manifest, spine, and navigation entries.
``extract``
    Write one JSON work manifest containing stable document/node/field IDs,
    exact source text, and the spine mapping.
``build``
    Apply explicitly reviewed translations to the source archive.  XML is
    never re-serialized: only text and tail slots are replaced in the original
    XML byte stream.
``validate``
    Check ZIP/package/XML/reference invariants for an EPUB.

No translation service, model, network, or book-specific path is used here.
The JSON shapes are intentionally plain so that a reviewer can edit them with
any text editor or a small local script.
"""

from __future__ import annotations

import argparse
import copy
import html
import json
import os
import posixpath
import re
import sys
import tempfile
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape


SCHEMA_PREFIX = "epub-translate-review"
WORK_SCHEMA = f"{SCHEMA_PREFIX}/work-v1"
TRANSLATIONS_SCHEMA = f"{SCHEMA_PREFIX}/translations-v1"
REPORT_SCHEMA = f"{SCHEMA_PREFIX}/report-v1"

XML_SUFFIXES = (".xml", ".xhtml", ".html", ".opf", ".ncx", ".svg")
XMLNS_URI = "http://www.w3.org/2000/xmlns/"
XML_URI = "http://www.w3.org/XML/1998/namespace"


class WorkbenchError(ValueError):
    """An expected, actionable workbench error."""


def local_name(tag: str) -> str:
    """Return the local component of a Clark-name or lexical tag name."""

    if tag.startswith("{") and "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag.rsplit(":", 1)[-1]


def namespace_uri(tag: str) -> str | None:
    if tag.startswith("{") and "}" in tag:
        return tag[1:].split("}", 1)[0]
    return None


def expanded_xml_name(name: str, nsmap: Mapping[str, str]) -> str:
    """Expand a lexical XML name for stable node paths."""

    if name.startswith("{"):
        return name
    if ":" in name:
        prefix, local = name.split(":", 1)
        uri = XML_URI if prefix == "xml" else nsmap.get(prefix)
        return f"{{{uri}}}{local}" if uri else name
    uri = nsmap.get("")
    return f"{{{uri}}}{name}" if uri else name


def safe_archive_path(path: str, *, context: str = "archive path") -> str:
    """Normalize one ZIP path and reject traversal/absolute paths."""

    if not isinstance(path, str) or not path:
        raise WorkbenchError(f"{context} must be a non-empty string")
    path = path.replace("\\", "/")
    if path.startswith("/"):
        raise WorkbenchError(f"{context} is absolute: {path!r}")
    normalized = posixpath.normpath(path)
    if normalized in {"", "."} or normalized == ".." or normalized.startswith("../"):
        raise WorkbenchError(f"{context} escapes the EPUB root: {path!r}")
    return normalized


def resolve_href(base_path: str, href: str, *, context: str = "href") -> str | None:
    """Resolve a local EPUB href, returning ``None`` for external URLs."""

    if not isinstance(href, str):
        raise WorkbenchError(f"{context} must be a string")
    parsed = urllib.parse.urlsplit(href)
    if parsed.scheme or parsed.netloc:
        return None
    decoded = urllib.parse.unquote(parsed.path)
    if decoded.startswith("/"):
        raise WorkbenchError(f"{context} is an absolute URL/path: {href!r}")
    # An empty path means the current XML resource (for example ``#part-2``
    # or ``?view=print``), rather than the directory containing it.
    if decoded == "":
        return safe_archive_path(base_path, context=context)
    base_dir = posixpath.dirname(base_path)
    return safe_archive_path(posixpath.join(base_dir, decoded), context=context)


def write_json(path: Path, value: Any) -> None:
    """Write JSON through a neighboring temporary file and replace atomically."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def json_object(value: Any, *, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkbenchError(f"{context} must be a JSON object")
    return value


def read_json(path: Path) -> dict[str, Any]:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json_object(json.load(handle), context=str(path))
    except FileNotFoundError as exc:
        raise WorkbenchError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise WorkbenchError(f"Invalid JSON in {path}: {exc}") from exc


def zip_entry_names(archive: zipfile.ZipFile) -> list[str]:
    names = [info.filename for info in archive.infolist()]
    if len(set(names)) != len(names):
        duplicates = sorted({name for name in names if names.count(name) > 1})
        raise WorkbenchError(f"EPUB contains duplicate ZIP paths: {duplicates}")
    for name in names:
        safe_archive_path(name, context="ZIP entry path")
    return names


def parse_xml(data: bytes, *, path: str) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise WorkbenchError(f"Invalid XML in {path}: {exc}") from exc


def elements(root: ET.Element, name: str) -> list[ET.Element]:
    return [element for element in root.iter() if local_name(element.tag) == name]


def attr_by_local(element: ET.Element, name: str, default: str | None = None) -> str | None:
    if name in element.attrib:
        return element.attrib[name]
    for key, value in element.attrib.items():
        if local_name(key) == name:
            return value
    return default


def text_content(element: ET.Element) -> str:
    return "".join(element.itertext())


@dataclass(frozen=True)
class ManifestItem:
    item_id: str
    href: str
    archive_path: str
    media_type: str
    properties: str


@dataclass(frozen=True)
class SpineItem:
    index: int
    item_id: str
    document_id: str
    archive_path: str
    media_type: str


@dataclass
class Publication:
    opf_path: str
    container_rootfile: str
    package_root: ET.Element
    manifest: dict[str, ManifestItem]
    spine: list[SpineItem]
    nav_item: ManifestItem | None
    ncx_item: ManifestItem | None


def open_publication(archive: zipfile.ZipFile) -> Publication:
    """Read the actual container/rootfile/manifest/spine mapping."""

    names = set(zip_entry_names(archive))
    if "META-INF/container.xml" not in names:
        raise WorkbenchError("EPUB is missing META-INF/container.xml")
    container_root = parse_xml(archive.read("META-INF/container.xml"), path="META-INF/container.xml")
    rootfiles = [element for element in container_root.iter() if local_name(element.tag) == "rootfile"]
    if not rootfiles:
        raise WorkbenchError("META-INF/container.xml has no rootfile element")
    rootfile_paths: list[str] = []
    for rootfile in rootfiles:
        full_path = attr_by_local(rootfile, "full-path")
        if not full_path:
            raise WorkbenchError("container rootfile is missing full-path")
        rootfile_paths.append(safe_archive_path(full_path, context="container rootfile full-path"))
    selected = next(
        (
            path
            for path, rootfile in zip(rootfile_paths, rootfiles)
            if attr_by_local(rootfile, "media-type") in {None, "application/oebps-package+xml"}
        ),
        rootfile_paths[0],
    )
    if selected not in names:
        raise WorkbenchError(f"container rootfile does not exist in ZIP: {selected}")

    package_root = parse_xml(archive.read(selected), path=selected)
    package_items: list[ManifestItem] = []
    for item in elements(package_root, "item"):
        item_id = attr_by_local(item, "id")
        href = attr_by_local(item, "href")
        media_type = attr_by_local(item, "media-type")
        if not item_id or not href or not media_type:
            raise WorkbenchError(f"OPF manifest item is missing id/href/media-type in {selected}")
        if item_id in {candidate.item_id for candidate in package_items}:
            raise WorkbenchError(f"OPF has duplicate manifest id: {item_id}")
        archive_path = resolve_href(selected, href, context=f"manifest href for {item_id}")
        if archive_path is None:
            raise WorkbenchError(f"manifest item {item_id} has an external href: {href!r}")
        if archive_path not in names:
            raise WorkbenchError(f"manifest item {item_id} points to missing ZIP entry: {archive_path}")
        package_items.append(
            ManifestItem(
                item_id=item_id,
                href=href,
                archive_path=archive_path,
                media_type=media_type,
                properties=attr_by_local(item, "properties", "") or "",
            )
        )
    manifest = {item.item_id: item for item in package_items}
    spine_elements = elements(package_root, "itemref")
    spine: list[SpineItem] = []
    for index, itemref in enumerate(spine_elements):
        item_id = attr_by_local(itemref, "idref")
        if not item_id or item_id not in manifest:
            raise WorkbenchError(f"spine itemref {index} has unknown idref: {item_id!r}")
        item = manifest[item_id]
        spine.append(
            SpineItem(
                index=index,
                item_id=item_id,
                document_id=f"manifest:{item_id}",
                archive_path=item.archive_path,
                media_type=item.media_type,
            )
        )
    if not spine:
        raise WorkbenchError("OPF spine is empty")
    nav_item = next((item for item in package_items if "nav" in item.properties.split()), None)
    toc_id = next(
        (
            attr_by_local(itemref, "toc")
            for itemref in spine_elements
            if attr_by_local(itemref, "toc")
        ),
        None,
    )
    ncx_item = manifest.get(toc_id) if toc_id else None
    if ncx_item is None:
        ncx_item = next((item for item in package_items if item.media_type == "application/x-dtbncx+xml"), None)
    return Publication(
        opf_path=selected,
        container_rootfile=selected,
        package_root=package_root,
        manifest=manifest,
        spine=spine,
        nav_item=nav_item,
        ncx_item=ncx_item,
    )


def nav_links(archive: zipfile.ZipFile, item: ManifestItem | None) -> list[dict[str, Any]]:
    if item is None:
        return []
    root = parse_xml(archive.read(item.archive_path), path=item.archive_path)
    links: list[dict[str, Any]] = []
    for anchor in elements(root, "a"):
        href = attr_by_local(anchor, "href")
        if not href:
            continue
        parsed = urllib.parse.urlsplit(href)
        target = resolve_href(item.archive_path, href, context=f"navigation href in {item.archive_path}")
        links.append(
            {
                "label": " ".join(text_content(anchor).split()),
                "href": href,
                "target_path": target,
                "fragment": urllib.parse.unquote(parsed.fragment),
                "external": target is None,
            }
        )
    return links


def ncx_links(archive: zipfile.ZipFile, item: ManifestItem | None) -> list[dict[str, Any]]:
    if item is None:
        return []
    root = parse_xml(archive.read(item.archive_path), path=item.archive_path)
    links: list[dict[str, Any]] = []
    for content in elements(root, "content"):
        src = attr_by_local(content, "src")
        if not src:
            continue
        parsed = urllib.parse.urlsplit(src)
        target = resolve_href(item.archive_path, src, context=f"NCX src in {item.archive_path}")
        links.append(
            {
                "src": src,
                "target_path": target,
                "fragment": urllib.parse.unquote(parsed.fragment),
                "external": target is None,
            }
        )
    return links


def document_text_count(archive: zipfile.ZipFile, spine_item: SpineItem) -> int:
    root = parse_xml(archive.read(spine_item.archive_path), path=spine_item.archive_path)
    return sum(1 for element in root.iter() if (element.text or "").strip()) + sum(
        1 for element in root.iter() for child in element if (child.tail or "").strip()
    )


def package_metadata_text(root: ET.Element) -> list[dict[str, str]]:
    metadata = next((element for element in root.iter() if local_name(element.tag) == "metadata"), None)
    if metadata is None:
        return []
    values: list[dict[str, str]] = []
    for element in list(metadata):
        value = text_content(element).strip()
        if value:
            values.append({"tag": local_name(element.tag), "text": value})
    return values


def inspect_epub(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise WorkbenchError(f"EPUB file not found: {path}")
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = zip_entry_names(archive)
            publication = open_publication(archive)
            manifest = [
                {
                    "id": item.item_id,
                    "href": item.href,
                    "archive_path": item.archive_path,
                    "media_type": item.media_type,
                    "properties": item.properties,
                }
                for item in publication.manifest.values()
            ]
            spine = [
                {
                    "index": item.index,
                    "item_id": item.item_id,
                    "document_id": item.document_id,
                    "archive_path": item.archive_path,
                    "media_type": item.media_type,
                    "text_slot_count": document_text_count(archive, item),
                }
                for item in publication.spine
            ]
            return {
                "schema": f"{SCHEMA_PREFIX}/inspection-v1",
                "epub": str(path),
                "zip": {
                    "entry_count": len(names),
                    "entry_order": names,
                    "mimetype_first_stored": bool(
                        archive.infolist()
                        and archive.infolist()[0].filename == "mimetype"
                        and archive.infolist()[0].compress_type == zipfile.ZIP_STORED
                        and archive.read("mimetype") == b"application/epub+zip"
                    ),
                },
                "package": {
                    "opf_path": publication.opf_path,
                    "metadata_text": package_metadata_text(publication.package_root),
                    "manifest": manifest,
                    "spine": spine,
                },
                "navigation": {
                    "nav_path": publication.nav_item.archive_path if publication.nav_item else None,
                    "nav_links": nav_links(archive, publication.nav_item),
                    "ncx_path": publication.ncx_item.archive_path if publication.ncx_item else None,
                    "ncx_links": ncx_links(archive, publication.ncx_item),
                },
            }
    except zipfile.BadZipFile as exc:
        raise WorkbenchError(f"Invalid EPUB ZIP: {path}: {exc}") from exc


@dataclass
class RawAttribute:
    name: str
    value_start: int
    value_end: int
    quote: str


@dataclass(eq=False)
class RawNode:
    index: int
    name: str
    start: int
    start_end: int
    close_start: int
    close_end: int
    parent: "RawNode | None" = None
    children: list["RawNode"] = field(default_factory=list)
    nsmap: dict[str, str] = field(default_factory=dict)
    node_path: str = ""

    @property
    def self_closing(self) -> bool:
        return self.close_start == self.start_end


NAME_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.:-]*)")
ATTR_RE = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z0-9_.:-]*)(?P<eq>\s*=\s*)"
    r"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.S,
)


def markup_end(text: str, start: int) -> int:
    """Find a tag terminator outside quoted attributes."""

    quote: str | None = None
    for index in range(start + 1, len(text)):
        char = text[index]
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == ">":
            return index + 1
    raise WorkbenchError(f"Unterminated XML markup at character offset {start}")


def declaration_end(text: str, start: int) -> int:
    """Find the end of a declaration, including a DOCTYPE internal subset."""

    quote: str | None = None
    subset_depth = 0
    for index in range(start + 1, len(text)):
        char = text[index]
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "[":
            subset_depth += 1
        elif char == "]" and subset_depth:
            subset_depth -= 1
        elif char == ">" and subset_depth == 0:
            return index + 1
    raise WorkbenchError(f"Unterminated XML declaration at character offset {start}")


def token_end(text: str, start: int) -> int:
    if text.startswith("<!--", start):
        end = text.find("-->", start + 4)
        if end < 0:
            raise WorkbenchError("Unterminated XML comment")
        return end + 3
    if text.startswith("<![CDATA[", start):
        end = text.find("]]>", start + 9)
        if end < 0:
            raise WorkbenchError("Unterminated XML CDATA section")
        return end + 3
    if text.startswith("<?", start):
        end = text.find("?>", start + 2)
        if end < 0:
            raise WorkbenchError("Unterminated XML processing instruction")
        return end + 2
    if text.startswith("<!", start):
        return declaration_end(text, start)
    return markup_end(text, start)


def raw_attributes(start_tag: str) -> list[RawAttribute]:
    body = start_tag[1:-1]
    if re.search(r"/\s*$", body):
        body = re.sub(r"/\s*$", "", body)
    name_match = NAME_RE.match(body)
    if not name_match:
        return []
    result: list[RawAttribute] = []
    for match in ATTR_RE.finditer(body, name_match.end()):
        result.append(
            RawAttribute(
                name=match.group("name"),
                value_start=1 + match.start("value"),
                value_end=1 + match.end("value"),
                quote=match.group("quote"),
            )
        )
    return result


def parse_raw_nodes(text: str) -> list[RawNode]:
    """Tokenize XML elements while retaining byte/character boundaries."""

    nodes: list[RawNode] = []
    stack: list[RawNode] = []
    cursor = 0
    while cursor < len(text):
        start = text.find("<", cursor)
        if start < 0:
            break
        end = token_end(text, start)
        token = text[start:end]
        body = token[1:-1]
        stripped = body.lstrip()
        if not stripped or stripped.startswith(("!", "?")):
            cursor = end
            continue
        if stripped.startswith("/"):
            name_match = NAME_RE.match(stripped[1:])
            if not name_match or not stack:
                raise WorkbenchError(f"Malformed XML end tag: {token!r}")
            node = stack.pop()
            closing_name = name_match.group(1)
            if node.name != closing_name:
                raise WorkbenchError(
                    f"Mismatched XML tags: {node.name!r} and {closing_name!r}"
                )
            node.close_start = start
            node.close_end = end
            cursor = end
            continue
        name_match = NAME_RE.match(stripped)
        if not name_match:
            cursor = end
            continue
        name = name_match.group(1)
        self_closing = bool(re.search(r"/\s*$", body))
        parent = stack[-1] if stack else None
        node = RawNode(
            index=len(nodes),
            name=name,
            start=start,
            start_end=end,
            close_start=end if self_closing else -1,
            close_end=end if self_closing else -1,
            parent=parent,
        )
        if parent is not None:
            parent.children.append(node)
        nodes.append(node)
        if not self_closing:
            stack.append(node)
        cursor = end
    if stack:
        raise WorkbenchError(f"Unclosed XML tag: {stack[-1].name!r}")
    if not nodes:
        raise WorkbenchError("No XML elements found")
    return nodes


def assign_namespaces_and_paths(text: str, nodes: list[RawNode]) -> None:
    for node in nodes:
        inherited = dict(node.parent.nsmap) if node.parent is not None else {}
        start_tag = text[node.start : node.start_end]
        for attribute in raw_attributes(start_tag):
            value = html.unescape(start_tag[attribute.value_start : attribute.value_end])
            if attribute.name == "xmlns":
                inherited[""] = value
            elif attribute.name.startswith("xmlns:"):
                inherited[attribute.name.split(":", 1)[1]] = value
        node.nsmap = inherited
        if node.parent is None:
            node.node_path = f"/{local_name(node.name)}[1]"
        else:
            sibling_position = next(
                index for index, child in enumerate(node.parent.children) if child is node
            )
            prior = [
                child
                for child in node.parent.children[:sibling_position]
                # Count local-name siblings rather than lexical prefixes or
                # namespace URIs.  This keeps locators readable while still
                # giving distinct ``foo[1]``/``foo[2]`` paths when a document
                # mixes namespaces with the same local name.
                if local_name(child.name) == local_name(node.name)
            ]
            ordinal = len(prior) + 1
            node.node_path = f"{node.parent.node_path}/{local_name(node.name)}[{ordinal}]"


def raw_markup_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    cursor = 0
    while cursor < len(text):
        start = text.find("<", cursor)
        if start < 0:
            break
        end = token_end(text, start)
        tokens.append(text[start:end])
        cursor = end
    return tokens


def raw_tail_end(node: RawNode, document_length: int) -> int:
    if node.parent is None:
        return node.close_end
    position = node.parent.children.index(node)
    if position + 1 < len(node.parent.children):
        return node.parent.children[position + 1].start
    return node.parent.close_start


def raw_text_slot(node: RawNode, document_length: int) -> tuple[int, int] | None:
    if node.self_closing:
        return None
    return (node.start_end, node.children[0].start if node.children else node.close_start)


def raw_tail_slot(node: RawNode, document_length: int) -> tuple[int, int] | None:
    if node.parent is None:
        return None
    return (node.close_end, raw_tail_end(node, document_length))


def decode_text_slot(raw: str, *, path: str, node_path: str, slot: str) -> str:
    if "<![CDATA[" in raw or "<!--" in raw or "<?" in raw or "<" in raw:
        raise WorkbenchError(
            f"Unsupported markup inside {slot} slot {path}::{node_path}; "
            "text slots containing CDATA/comments/PIs require manual handling"
        )
    return html.unescape(raw)


def field_id(document_id: str, node_path: str, slot: str) -> str:
    return f"{document_id}::{node_path}::{slot}"


def is_body_descendant(node: RawNode) -> bool:
    """Return whether a lexical node belongs to a spine document's body."""

    cursor: RawNode | None = node
    while cursor is not None:
        if local_name(cursor.name).lower() == "body":
            return True
        cursor = cursor.parent
    return False


BLOCK_TAGS = {
    "address",
    "blockquote",
    "dd",
    "div",
    "figcaption",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "p",
    "pre",
    "section",
    "td",
    "th",
}


def nearest_block(node: RawNode) -> RawNode | None:
    cursor: RawNode | None = node
    while cursor is not None:
        if local_name(cursor.name).lower() in BLOCK_TAGS:
            return cursor
        cursor = cursor.parent
    return None


def extract_document_fields(
    archive: zipfile.ZipFile, spine_item: SpineItem
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    data = archive.read(spine_item.archive_path)
    try:
        source_text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkbenchError(f"Spine document is not UTF-8 XML: {spine_item.archive_path}") from exc
    root = parse_xml(data, path=spine_item.archive_path)
    nodes = parse_raw_nodes(source_text)
    assign_namespaces_and_paths(source_text, nodes)
    source_elements = list(root.iter())
    if len(nodes) != len(source_elements):
        raise WorkbenchError(
            f"Lexical/XML element count mismatch in {spine_item.archive_path}: "
            f"{len(nodes)} lexical vs {len(source_elements)} parsed"
        )
    element_by_node = {id(node): element for node, element in zip(nodes, source_elements)}
    # A preorder node walk is not rendered text order when inline elements
    # nest: a parent tail follows all of its descendants. Keep each slot's
    # lexical offset while collecting, then expose fields in source order.
    fields_with_offsets: list[tuple[int, dict[str, Any]]] = []
    for node, element in zip(nodes, source_elements):
        # Translation scope is intentionally limited to rendered spine body
        # content. OPF metadata/package XML, head/title content, non-spine
        # cover/nav documents, CSS, fonts, images, and other non-selected
        # resources remain source-owned and byte-identical.
        if not is_body_descendant(node):
            continue
        block = nearest_block(node)
        block_element = element_by_node.get(id(block)) if block is not None else None
        block_path = block.node_path if block is not None else None
        block_source = text_content(block_element) if block_element is not None else None
        text_slot = raw_text_slot(node, len(source_text))
        if text_slot is not None:
            start, end = text_slot
            raw = source_text[start:end]
            parsed_value = element.text or ""
            decoded = decode_text_slot(
                raw,
                path=spine_item.archive_path,
                node_path=node.node_path,
                slot="text",
            )
            if decoded != parsed_value:
                raise WorkbenchError(
                    f"Text decoding mismatch in {spine_item.archive_path}::{node.node_path}: "
                    "the XML slot contains an entity or construct the lexical scanner cannot map"
                )
            if decoded and decoded.strip():
                fields_with_offsets.append(
                    (
                        start,
                        {
                            "field_id": field_id(spine_item.document_id, node.node_path, "text"),
                            "document_id": spine_item.document_id,
                            "archive_path": spine_item.archive_path,
                            "node_path": node.node_path,
                            "slot": "text",
                            "source": decoded,
                            "source_lexical": raw,
                            "block_path": block_path,
                            "block_source": block_source,
                        },
                    )
                )
        tail_slot = raw_tail_slot(node, len(source_text))
        if tail_slot is not None:
            start, end = tail_slot
            raw = source_text[start:end]
            parsed_value = element.tail or ""
            decoded = decode_text_slot(
                raw,
                path=spine_item.archive_path,
                node_path=node.node_path,
                slot="tail",
            )
            if decoded != parsed_value:
                raise WorkbenchError(
                    f"Tail decoding mismatch in {spine_item.archive_path}::{node.node_path}: "
                    "the XML slot contains an entity or construct the lexical scanner cannot map"
                )
            if decoded and decoded.strip():
                fields_with_offsets.append(
                    (
                        start,
                        {
                            "field_id": field_id(spine_item.document_id, node.node_path, "tail"),
                            "document_id": spine_item.document_id,
                            "archive_path": spine_item.archive_path,
                            "node_path": node.node_path,
                            "slot": "tail",
                            "source": decoded,
                            "source_lexical": raw,
                            "block_path": block_path,
                            "block_source": block_source,
                        },
                    )
                )
    fields = [record for _, record in sorted(fields_with_offsets, key=lambda pair: pair[0])]
    document = {
        "document_id": spine_item.document_id,
        "manifest_id": spine_item.item_id,
        "archive_path": spine_item.archive_path,
        "media_type": spine_item.media_type,
        "source_xml_size": len(data),
        "source_markup_tokens": raw_markup_tokens(source_text),
        "field_ids": [record["field_id"] for record in fields],
    }
    return document, fields


def archive_entry_manifest(archive: zipfile.ZipFile) -> list[dict[str, Any]]:
    return [
        {"path": info.filename, "size": info.file_size, "is_dir": info.is_dir()}
        for info in archive.infolist()
    ]


def extract_work(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise WorkbenchError(f"EPUB file not found: {path}")
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = zip_entry_names(archive)
            publication = open_publication(archive)
            documents: list[dict[str, Any]] = []
            all_fields: list[dict[str, Any]] = []
            for spine_item in publication.spine:
                document, fields = extract_document_fields(archive, spine_item)
                documents.append(document)
                all_fields.extend(fields)
            spine = [
                {
                    "index": item.index,
                    "item_id": item.item_id,
                    "document_id": item.document_id,
                    "archive_path": item.archive_path,
                    "media_type": item.media_type,
                }
                for item in publication.spine
            ]
            return {
                "schema": WORK_SCHEMA,
                "scope": {
                    "text_fields": "non-whitespace text and tail slots under body descendants of spine documents",
                    "review_guidance": "block_source is the review context; field slots are inline locators to repartition after reviewing a whole block",
                    "unchanged_resources": [
                        "OPF metadata and package XML",
                        "head/title content",
                        "non-spine content documents, including nav/cover documents when outside the spine",
                        "CSS, fonts, images, and other non-spine resources",
                    ],
                },
                "source": {
                    "opf_path": publication.opf_path,
                    "entry_order": names,
                    "entries": archive_entry_manifest(archive),
                },
                "spine": spine,
                "documents": documents,
                "fields": all_fields,
            }
    except zipfile.BadZipFile as exc:
        raise WorkbenchError(f"Invalid EPUB ZIP: {path}: {exc}") from exc


def work_projection(work: Mapping[str, Any]) -> dict[str, Any]:
    """Select all source-bound values used to detect a changed source EPUB."""

    return {
        "source": work.get("source"),
        "spine": work.get("spine"),
        "documents": work.get("documents"),
        "fields": work.get("fields"),
    }


def assert_work_matches(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> None:
    if expected.get("schema") != WORK_SCHEMA:
        raise WorkbenchError(
            f"Unsupported work manifest schema {expected.get('schema')!r}; expected {WORK_SCHEMA!r}"
        )
    if work_projection(expected) != work_projection(actual):
        raise WorkbenchError(
            "Source EPUB drifted from the extracted work manifest: "
            "OPF/spine/document markup/text identities or source values differ"
        )


def newline_sequence(value: str) -> list[str]:
    return re.findall(r"\r\n|\r|\n", value)


def parse_translations(path: Path) -> list[dict[str, Any]]:
    obj = read_json(path)
    if obj.get("schema") != TRANSLATIONS_SCHEMA:
        raise WorkbenchError(
            f"Unsupported translation schema {obj.get('schema')!r}; expected {TRANSLATIONS_SCHEMA!r}"
        )
    raw_fields = obj.get("fields")
    if not isinstance(raw_fields, list):
        raise WorkbenchError("Translation JSON must contain a fields array")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_fields):
        if not isinstance(item, dict):
            raise WorkbenchError(f"Translation fields[{index}] must be an object")
        field_key = item.get("field_id")
        if not isinstance(field_key, str) or not field_key:
            raise WorkbenchError(f"Translation fields[{index}] is missing field_id")
        if field_key in seen:
            raise WorkbenchError(f"Translation JSON repeats field_id: {field_key}")
        seen.add(field_key)
        if item.get("reviewed") is not True:
            raise WorkbenchError(
                f"Translation field {field_key} must explicitly set reviewed: true"
            )
        source = item.get("source")
        translation = item.get("translation")
        if not isinstance(source, str):
            raise WorkbenchError(f"Translation field {field_key} is missing exact source text")
        if not isinstance(translation, str):
            raise WorkbenchError(f"Translation field {field_key} is missing a string translation")
        if not translation.strip() and source.strip():
            raise WorkbenchError(
                f"Translation field {field_key} is empty or whitespace-only; "
                "explicit empty inline repartition is not supported"
            )
        result.append(
            {
                "field_id": field_key,
                "source": source,
                "translation": translation,
                "reviewed": True,
            }
        )
    return result


def field_slots_by_document(work: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    fields = work.get("fields")
    if not isinstance(fields, list):
        raise WorkbenchError("Work manifest is missing its fields array")
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(fields):
        if not isinstance(item, dict):
            raise WorkbenchError(f"Work fields[{index}] must be an object")
        key = item.get("field_id")
        if not isinstance(key, str) or not key:
            raise WorkbenchError(f"Work fields[{index}] is missing field_id")
        if key in result:
            raise WorkbenchError(f"Work manifest repeats field_id: {key}")
        result[key] = item
    return result


def patch_document_text(
    source_data: bytes,
    document_path: str,
    source_fields: Sequence[Mapping[str, Any]],
    translations: Mapping[str, str],
) -> tuple[bytes, dict[str, int]]:
    """Replace text/tail slots in source XML without serializing its tree."""

    try:
        source_text = source_data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkbenchError(f"XML document is not UTF-8: {document_path}") from exc
    source_root = parse_xml(source_data, path=document_path)
    nodes = parse_raw_nodes(source_text)
    assign_namespaces_and_paths(source_text, nodes)
    source_elements = list(source_root.iter())
    if len(nodes) != len(source_elements):
        raise WorkbenchError(f"Cannot map XML nodes lexically in {document_path}")
    by_key: dict[tuple[str, str], tuple[int, int, str]] = {}
    for node, element in zip(nodes, source_elements):
        text_slot = raw_text_slot(node, len(source_text))
        if text_slot is not None:
            start, end = text_slot
            raw = source_text[start:end]
            decoded = decode_text_slot(raw, path=document_path, node_path=node.node_path, slot="text")
            if decoded != (element.text or ""):
                raise WorkbenchError(f"Cannot map text slot in {document_path}::{node.node_path}")
            by_key[(node.node_path, "text")] = (start, end, decoded)
        tail_slot = raw_tail_slot(node, len(source_text))
        if tail_slot is not None:
            start, end = tail_slot
            raw = source_text[start:end]
            decoded = decode_text_slot(raw, path=document_path, node_path=node.node_path, slot="tail")
            if decoded != (element.tail or ""):
                raise WorkbenchError(f"Cannot map tail slot in {document_path}::{node.node_path}")
            by_key[(node.node_path, "tail")] = (start, end, decoded)

    replacements: list[tuple[int, int, str]] = []
    changed = 0
    for field in source_fields:
        key = str(field.get("field_id"))
        if key not in translations:
            continue
        node_path = field.get("node_path")
        slot = field.get("slot")
        if not isinstance(node_path, str) or slot not in {"text", "tail"}:
            raise WorkbenchError(f"Malformed work field {key}")
        mapping = by_key.get((node_path, slot))
        if mapping is None:
            raise WorkbenchError(f"Work field no longer exists in {document_path}: {key}")
        start, end, current_source = mapping
        expected_source = field.get("source")
        if expected_source != current_source:
            raise WorkbenchError(f"Source text drifted for field {key}")
        translation = translations[key]
        if newline_sequence(translation) != newline_sequence(current_source):
            raise WorkbenchError(
                f"Translation {key} changes hard line breaks; preserve the source newline sequence"
            )
        # This is element text, so only XML-significant ``&``, ``<``, and
        # ``>`` need escaping.  Quotes remain lexical text characters.
        replacement = xml_escape(translation)
        replacements.append((start, end, replacement))
        changed += 1

    for start, end, replacement in sorted(replacements, key=lambda value: value[0], reverse=True):
        source_text = source_text[:start] + replacement + source_text[end:]
    try:
        output_data = source_text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise WorkbenchError(f"Translation for {document_path} contains invalid Unicode: {exc}") from exc
    parse_xml(output_data, path=document_path)
    if raw_markup_tokens(source_text) != raw_markup_tokens(source_data.decode("utf-8")):
        raise WorkbenchError(f"Lexical XML markup changed while translating {document_path}")
    return output_data, {"translated_fields": changed}


def same_file_path(first: Path, second: Path) -> bool:
    try:
        if first.resolve() == second.resolve():
            return True
    except OSError:
        pass
    if first.exists() and second.exists():
        try:
            return os.path.samefile(first, second)
        except OSError:
            return False
    return False


def assert_output_distinct(
    output_path: Path,
    inputs: Sequence[tuple[str, Path]],
    *,
    output_label: str = "Output path",
) -> None:
    for input_label, input_path in inputs:
        if same_file_path(output_path, input_path):
            raise WorkbenchError(
                f"{output_label} must differ from {input_label}: {output_path}"
            )


def report_path_is_safe(args: argparse.Namespace) -> bool:
    report_path = getattr(args, "report", None)
    if not report_path:
        return False
    inputs: list[tuple[str, Path]] = []
    if getattr(args, "command", None) == "build":
        inputs = [
            ("source EPUB", args.source),
            ("work manifest", args.work),
            ("translations JSON", args.translations),
            ("output EPUB", args.output),
        ]
    return not any(same_file_path(report_path, path) for _, path in inputs)


def xml_entry_paths(archive: zipfile.ZipFile, publication: Publication) -> list[str]:
    paths: list[str] = ["META-INF/container.xml", publication.opf_path]
    for item in publication.manifest.values():
        if "xml" in item.media_type or item.media_type in {"application/xhtml+xml", "image/svg+xml"}:
            paths.append(item.archive_path)
        elif item.archive_path.lower().endswith(XML_SUFFIXES):
            paths.append(item.archive_path)
    # Include unmanifested XML-like resources too.  They are copied byte for
    # byte, but a malformed resource should still make package validation
    # explicit rather than silently passing through.
    paths.extend(
        name for name in archive.namelist() if name.lower().endswith(XML_SUFFIXES)
    )
    return list(dict.fromkeys(path for path in paths if path in set(archive.namelist())))


def is_xml_resource(path: str, publication: Publication) -> bool:
    if path.lower().endswith(XML_SUFFIXES):
        return True
    item = next((candidate for candidate in publication.manifest.values() if candidate.archive_path == path), None)
    return bool(item and ("xml" in item.media_type or item.media_type == "image/svg+xml"))


def xml_fragment_ids(archive: zipfile.ZipFile, path: str) -> set[str]:
    root = parse_xml(archive.read(path), path=path)
    identifiers: set[str] = set()
    for element in root.iter():
        for key, value in element.attrib.items():
            if local_name(key) in {"id", "name"} and value:
                identifiers.add(value)
    return identifiers


def fragment_reference_error(
    archive: zipfile.ZipFile,
    publication: Publication,
    target_path: str | None,
    fragment: str,
    *,
    context: str,
) -> str | None:
    if not target_path or not fragment or not is_xml_resource(target_path, publication):
        return None
    if target_path not in set(archive.namelist()):
        return None
    if fragment not in xml_fragment_ids(archive, target_path):
        return f"{context} points to missing XML fragment #{fragment} in {target_path}"
    return None


def validate_references(archive: zipfile.ZipFile, publication: Publication) -> list[str]:
    names = set(archive.namelist())
    errors: list[str] = []
    for item in publication.manifest.values():
        if item.archive_path not in names:
            errors.append(f"manifest item {item.item_id} missing target {item.archive_path}")
    for spine_item in publication.spine:
        if spine_item.archive_path not in names:
            errors.append(f"spine item {spine_item.item_id} missing target {spine_item.archive_path}")
    for label, item, links in (
        ("nav", publication.nav_item, nav_links(archive, publication.nav_item)),
        ("ncx", publication.ncx_item, ncx_links(archive, publication.ncx_item)),
    ):
        if item is None:
            continue
        for link in links:
            target = link.get("target_path")
            if target is not None and target not in names:
                errors.append(f"{label} reference points to missing target {target}")
            fragment_error = fragment_reference_error(
                archive,
                publication,
                target,
                str(link.get("fragment") or ""),
                context=f"{label} reference {link.get('href', link.get('src', ''))!r}",
            )
            if fragment_error:
                errors.append(fragment_error)
    # Validate ordinary local href/src links in publication XML as a useful
    # second line of defense. External/data URLs are allowed and skipped.
    for xml_path in xml_entry_paths(archive, publication):
        try:
            root = parse_xml(archive.read(xml_path), path=xml_path)
        except WorkbenchError as exc:
            errors.append(str(exc))
            continue
        for element in root.iter():
            for key, value in element.attrib.items():
                if local_name(key) not in {"href", "src"}:
                    continue
                target = resolve_href(xml_path, value, context=f"{xml_path} {key}")
                if target is not None and target not in names:
                    errors.append(f"{xml_path} reference points to missing target {target}")
                fragment = urllib.parse.unquote(urllib.parse.urlsplit(value).fragment)
                fragment_error = fragment_reference_error(
                    archive,
                    publication,
                    target,
                    fragment,
                    context=f"{xml_path} {key}={value!r}",
                )
                if fragment_error:
                    errors.append(fragment_error)
    return sorted(set(errors))


def validate_epub(path: Path) -> dict[str, Any]:
    path = Path(path)
    report: dict[str, Any] = {
        "schema": f"{SCHEMA_PREFIX}/validation-v1",
        "epub": str(path),
        "ok": False,
        "errors": [],
    }
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = zip_entry_names(archive)
            infos = archive.infolist()
            mimetype_ok = bool(
                infos
                and infos[0].filename == "mimetype"
                and infos[0].compress_type == zipfile.ZIP_STORED
                and archive.read("mimetype") == b"application/epub+zip"
            )
            publication = open_publication(archive)
            xml_paths = xml_entry_paths(archive, publication)
            xml_errors: list[str] = []
            for xml_path in xml_paths:
                try:
                    parse_xml(archive.read(xml_path), path=xml_path)
                except WorkbenchError as exc:
                    xml_errors.append(str(exc))
            ref_errors = validate_references(archive, publication)
            testzip_error = archive.testzip()
            crc_errors = (
                [f"ZIP CRC check failed for entry: {testzip_error}"]
                if testzip_error
                else []
            )
            errors = (
                ([] if mimetype_ok else ["mimetype must be first, stored, and equal application/epub+zip"])
                + xml_errors
                + ref_errors
                + crc_errors
            )
            report.update(
                {
                    "zip": {
                        "entry_count": len(names),
                        "entry_order": names,
                        "mimetype_first_stored": mimetype_ok,
                        "testzip_error": testzip_error,
                    },
                    "package": {
                        "opf_path": publication.opf_path,
                        "manifest_count": len(publication.manifest),
                        "spine_count": len(publication.spine),
                        "nav_path": publication.nav_item.archive_path if publication.nav_item else None,
                        "ncx_path": publication.ncx_item.archive_path if publication.ncx_item else None,
                    },
                    "xml": {"paths": xml_paths, "errors": xml_errors},
                    "references": {"errors": ref_errors},
                    "errors": sorted(set(errors)),
                    "ok": not errors,
                }
            )
    except FileNotFoundError:
        report["errors"] = [f"EPUB file not found: {path}"]
    except zipfile.BadZipFile as exc:
        report["errors"] = [f"Invalid EPUB ZIP: {exc}"]
    except (KeyError, RuntimeError, zipfile.LargeZipFile) as exc:
        report["errors"] = [f"Cannot read EPUB ZIP entries: {exc}"]
    except WorkbenchError as exc:
        report["errors"] = [str(exc)]
    return report


def require_valid_epub(path: Path) -> dict[str, Any]:
    report = validate_epub(path)
    if not report.get("ok"):
        raise WorkbenchError("EPUB validation failed: " + "; ".join(report.get("errors", [])))
    return report


def build_epub(
    source_path: Path,
    work_path: Path,
    translations_path: Path,
    output_path: Path,
    *,
    require_complete: bool = False,
    report_path: Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    source_path = Path(source_path)
    work_path = Path(work_path)
    translations_path = Path(translations_path)
    output_path = Path(output_path)
    report_path = Path(report_path) if report_path is not None else None
    assert_output_distinct(
        output_path,
        [
            ("source EPUB", source_path),
            ("work manifest", work_path),
            ("translations JSON", translations_path),
        ],
        output_label="Output EPUB path",
    )
    if report_path is not None:
        assert_output_distinct(
            report_path,
            [
                ("source EPUB", source_path),
                ("work manifest", work_path),
                ("translations JSON", translations_path),
                ("output EPUB", output_path),
            ],
            output_label="Build report path",
        )
    if output_path.exists() and not overwrite:
        raise WorkbenchError(
            f"Output EPUB already exists: {output_path}; pass --overwrite to replace it"
        )
    expected_work = read_json(work_path)
    translations = parse_translations(translations_path)
    actual_work = extract_work(source_path)
    assert_work_matches(expected_work, actual_work)
    work_fields = field_slots_by_document(expected_work)
    translated_by_id: dict[str, str] = {}
    for item in translations:
        key = item["field_id"]
        if key not in work_fields:
            raise WorkbenchError(f"Translation requests unknown/missing work field: {key}")
        if item["source"] != work_fields[key].get("source"):
            raise WorkbenchError(f"Translation source does not match work manifest for field {key}")
        translated_by_id[key] = item["translation"]
    missing_ids = [item["field_id"] for item in expected_work.get("fields", []) if item["field_id"] not in translated_by_id]
    if require_complete and missing_ids:
        raise WorkbenchError(
            f"Translation batch is incomplete: {len(missing_ids)} reviewed fields are missing"
        )

    source_validation = require_valid_epub(source_path)
    per_document: list[dict[str, Any]] = []
    modified_data: dict[str, bytes] = {}
    actual_docs = {document["document_id"]: document for document in actual_work["documents"]}
    for document in actual_work["documents"]:
        document_fields = [
            field_record
            for field_record in actual_work["fields"]
            if field_record["document_id"] == document["document_id"]
        ]
        local_translations = {
            field_record["field_id"]: translated_by_id[field_record["field_id"]]
            for field_record in document_fields
            if field_record["field_id"] in translated_by_id
        }
        with zipfile.ZipFile(source_path, "r") as source_archive:
            source_data = source_archive.read(document["archive_path"])
        output_data, stats = patch_document_text(
            source_data,
            document["archive_path"],
            document_fields,
            local_translations,
        )
        if output_data != source_data:
            modified_data[document["archive_path"]] = output_data
        per_document.append(
            {
                "document_id": document["document_id"],
                "archive_path": document["archive_path"],
                "translated_fields": stats["translated_fields"],
                "untranslated_fields": len(document_fields) - stats["translated_fields"],
            }
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".staged.epub", dir=str(output_path.parent)
    )
    os.close(fd)
    staged = Path(tmp_name)
    try:
        with zipfile.ZipFile(source_path, "r") as source_archive, zipfile.ZipFile(
            staged, "w", allowZip64=True
        ) as output_archive:
            source_names = zip_entry_names(source_archive)
            for info in source_archive.infolist():
                data = modified_data.get(info.filename, source_archive.read(info.filename))
                copied_info = copy.copy(info)
                output_archive.writestr(copied_info, data, compress_type=info.compress_type)
            if [info.filename for info in output_archive.infolist()] != source_names:
                raise WorkbenchError("Staged EPUB changed ZIP entry order/path list")
        # Validate before the atomic rename, while the staged file is still
        # available. A second validation below makes the returned report point
        # at the final output path rather than this temporary file.
        require_valid_epub(staged)
        with zipfile.ZipFile(source_path, "r") as source_archive, zipfile.ZipFile(
            staged, "r"
        ) as output_archive:
            source_names = zip_entry_names(source_archive)
            output_names = zip_entry_names(output_archive)
            if source_names != output_names:
                raise WorkbenchError("Output EPUB changed original ZIP resource paths or order")
            unchanged_mismatches = [
                name
                for name in source_names
                if name not in modified_data and source_archive.read(name) != output_archive.read(name)
            ]
            if unchanged_mismatches:
                raise WorkbenchError(
                    "Output EPUB changed unmodified resources: " + ", ".join(unchanged_mismatches)
                )
            lexical_mismatches: list[str] = []
            for document_path, output_data in modified_data.items():
                source_text = source_archive.read(document_path).decode("utf-8")
                output_text = output_archive.read(document_path).decode("utf-8")
                if raw_markup_tokens(source_text) != raw_markup_tokens(output_text):
                    lexical_mismatches.append(document_path)
                if len(parse_raw_nodes(source_text)) != len(parse_raw_nodes(output_text)):
                    lexical_mismatches.append(document_path)
            if lexical_mismatches:
                raise WorkbenchError(
                    "Output EPUB changed XML tags/namespaces: " + ", ".join(sorted(set(lexical_mismatches)))
                )
        os.replace(staged, output_path)
    finally:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass

    final_validation = require_valid_epub(output_path)
    status = "complete" if not missing_ids else "partial"
    return {
        "schema": REPORT_SCHEMA,
        "ok": True,
        "status": status,
        "source_epub": str(source_path),
        "output_epub": str(output_path),
        "work_manifest": str(work_path),
        "translations": str(translations_path),
        "reviewed_field_count": len(translated_by_id),
        "untranslated_field_count": len(missing_ids),
        "untranslated_field_ids": missing_ids,
        "completion_scope": "extracted non-whitespace body text/tail fields in spine documents",
        "metadata_unchanged": True,
        "review_flags_are_attestations": True,
        "review_flags_note": "reviewed=true records an explicit caller attestation; it does not prove semantic review",
        "documents": per_document,
        "validation": final_validation,
        "validated_as_staged": True,
        "source_validation": source_validation,
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    subparsers = root.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="inspect the EPUB package/spine/navigation")
    inspect_parser.add_argument("--epub", type=Path, required=True)
    inspect_parser.add_argument("--output", type=Path, help="optional inspection JSON path")

    extract_parser = subparsers.add_parser("extract", help="extract a translation work manifest")
    extract_parser.add_argument("--epub", type=Path, required=True)
    extract_parser.add_argument("--output", type=Path, required=True)

    build_parser = subparsers.add_parser("build", help="build a reviewed translation EPUB")
    build_parser.add_argument("--source", type=Path, required=True)
    build_parser.add_argument("--work", type=Path, required=True)
    build_parser.add_argument("--translations", type=Path, required=True)
    build_parser.add_argument("--output", type=Path, required=True)
    build_parser.add_argument("--report", type=Path, help="optional build report JSON path")
    build_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing output EPUB after staging and validation",
    )
    build_parser.add_argument(
        "--require-complete",
        action="store_true",
        help="reject batches that omit any extracted non-whitespace text field",
    )

    validate_parser = subparsers.add_parser("validate", help="validate an EPUB package")
    validate_parser.add_argument("--epub", type=Path, required=True)
    validate_parser.add_argument("--work", type=Path, help="also reject source drift from a work manifest")
    validate_parser.add_argument("--output", type=Path, help="optional validation report JSON path")
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "inspect":
            if args.output:
                assert_output_distinct(args.output, [("source EPUB", args.epub)], output_label="Inspection JSON path")
            result = inspect_epub(args.epub)
            if args.output:
                write_json(args.output, result)
        elif args.command == "extract":
            assert_output_distinct(args.output, [("source EPUB", args.epub)], output_label="Work manifest path")
            result = extract_work(args.epub)
            write_json(args.output, result)
        elif args.command == "validate":
            if args.output:
                validation_inputs = [("source EPUB", args.epub)]
                if args.work:
                    validation_inputs.append(("work manifest", args.work))
                assert_output_distinct(args.output, validation_inputs, output_label="Validation JSON path")
            result = validate_epub(args.epub)
            if result.get("ok") and args.work:
                try:
                    expected = read_json(args.work)
                    actual = extract_work(args.epub)
                    assert_work_matches(expected, actual)
                    result["work_manifest_match"] = True
                except WorkbenchError as exc:
                    result["ok"] = False
                    result.setdefault("errors", []).append(str(exc))
                    result["work_manifest_match"] = False
            if args.output:
                write_json(args.output, result)
        elif args.command == "build":
            result = build_epub(
                args.source,
                args.work,
                args.translations,
                args.output,
                require_complete=args.require_complete,
                report_path=args.report,
                overwrite=args.overwrite,
            )
            if args.report:
                write_json(args.report, result)
        else:  # pragma: no cover - argparse enforces commands
            raise WorkbenchError(f"Unknown command: {args.command}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok", True) else 2
    except WorkbenchError as exc:
        error = {"schema": f"{SCHEMA_PREFIX}/error-v1", "ok": False, "error": str(exc)}
        if report_path_is_safe(args):
            write_json(args.report, error)
        print(json.dumps(error, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except OSError as exc:
        error = {"schema": f"{SCHEMA_PREFIX}/error-v1", "ok": False, "error": str(exc)}
        if report_path_is_safe(args):
            write_json(args.report, error)
        print(json.dumps(error, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except (KeyError, RuntimeError, UnicodeError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        error = {"schema": f"{SCHEMA_PREFIX}/error-v1", "ok": False, "error": f"Cannot read EPUB ZIP: {exc}"}
        if report_path_is_safe(args):
            write_json(args.report, error)
        print(json.dumps(error, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


def _configure_cli_encoding() -> None:
    """Keep direct CLI diagnostics UTF-8 on Windows regardless of locale."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


if __name__ == "__main__":
    _configure_cli_encoding()
    raise SystemExit(main())
