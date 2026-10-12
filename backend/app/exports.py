"""High-quality PDF and PowerPoint exporters for FrameNotes.

The exporters keep frame paths job-relative, preserve source/review ordering,
and publish only validated files.  PDF text uses registered Unicode fonts and
is emitted as real text (including optional OCR), while PPTX text remains
editable text boxes.  Export layout choices apply to PDF; PowerPoint always
uses one primary frame per slide so a slide remains readable at presentation
size.
"""

from __future__ import annotations

import importlib
import os
import re
import textwrap
import uuid
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from threading import Event
from typing import Any, Iterable

from PIL import Image

from .errors import CancelledProcessing, ExportError
from .models import DownloadArtifact, ExportRequest, FrameMetadata, JobRecord, SourceRecord, utc_now
from .ocr import normalize_ocr_text


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return cleaned or "framenotes-export"


def _timestamp(value: float) -> str:
    total_ms = max(0, int(round(value * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _check_cancel(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledProcessing("Export cancelled by user")


def _image_size(path: Path) -> tuple[int, int]:
    try:
        with Image.open(path) as image:
            return image.size
    except Exception as exc:
        raise ExportError(f"Could not read frame image {path}: {exc}") from exc


def _fit_box(width: float, height: float, max_width: float, max_height: float) -> tuple[float, float]:
    if width <= 0 or height <= 0 or max_width <= 0 or max_height <= 0:
        return max_width, max_height
    scale = min(max_width / width, max_height / height)
    return width * scale, height * scale


def _preferred_aspect_box(max_width: float, max_height: float, aspect_ratio: str) -> tuple[float, float]:
    ratio = 16 / 9 if aspect_ratio == "16:9" else 4 / 3
    return _fit_box(ratio, 1.0, max_width, max_height)


def _source_map(job: JobRecord) -> dict[str, SourceRecord]:
    return {source.id: source for source in job.sources}


def _frame_order(frame: FrameMetadata) -> tuple[float, int, str]:
    # Review order is intentionally per-frame.  Frames without a review order
    # remain timestamp/index ordered and do not get moved by a neighboring
    # frame's manual ordering.
    primary = float(frame.review_order) if frame.review_order is not None else float(frame.timestamp)
    return primary, frame.index, frame.id


def _ordered_frames(job: JobRecord) -> list[FrameMetadata]:
    retained = [frame for frame in job.frames if frame.retained]
    grouped: dict[str, list[FrameMetadata]] = defaultdict(list)
    for frame in retained:
        grouped[frame.source_id].append(frame)
    ordered: list[FrameMetadata] = []
    seen: set[str] = set()
    for source in job.sources:
        ordered.extend(sorted(grouped.get(source.id, []), key=_frame_order))
        seen.add(source.id)
    # A partially restored or hand-built job can contain a frame whose source
    # record is absent.  Keep it deterministic after known source records.
    for source_id in sorted(set(grouped) - seen):
        ordered.extend(sorted(grouped[source_id], key=_frame_order))
    return ordered


def _frames_by_source(job: JobRecord, frames: Iterable[FrameMetadata]) -> list[tuple[SourceRecord | None, list[FrameMetadata]]]:
    grouped: dict[str, list[FrameMetadata]] = defaultdict(list)
    for frame in frames:
        grouped[frame.source_id].append(frame)
    source_by_id = _source_map(job)
    result: list[tuple[SourceRecord | None, list[FrameMetadata]]] = []
    seen: set[str] = set()
    for source in job.sources:
        source_frames = sorted(grouped.get(source.id, []), key=_frame_order)
        if source_frames:
            result.append((source, source_frames))
        seen.add(source.id)
    for source_id in sorted(set(grouped) - seen):
        result.append((source_by_id.get(source_id), sorted(grouped[source_id], key=_frame_order)))
    return result


def _frame_path(job_dir: Path, frame: FrameMetadata) -> Path:
    base = job_dir.resolve(strict=False)
    candidate = (base / frame.path).resolve(strict=False)
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ExportError(f"Frame path escapes the isolated job directory: {frame.path}") from exc
    if not candidate.is_file():
        raise ExportError(f"Frame image is missing: {frame.path}")
    return candidate


def _draw_pdf_image(canvas: Any, path: Path, x: float, y: float, width: float, height: float) -> None:
    try:
        from reportlab.lib.utils import ImageReader

        image_width, image_height = _image_size(path)
        box_width, box_height = _fit_box(image_width, image_height, width, height)
        canvas.drawImage(
            ImageReader(str(path)),
            x + (width - box_width) / 2,
            y + (height - box_height) / 2,
            width=box_width,
            height=box_height,
            preserveAspectRatio=True,
            anchor="c",
            mask="auto",
        )
    except ExportError:
        raise
    except Exception as exc:
        raise ExportError(f"Could not place frame image {path}: {exc}") from exc


def _validate_pdf(path: Path) -> None:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ExportError(f"Could not read generated PDF {path}: {exc}") from exc
    if not payload.startswith(b"%PDF-") or b"%%EOF" not in payload[-2048:]:
        raise ExportError(f"Generated PDF failed its file signature check: {path}")
    for module_name in ("pypdf", "PyPDF2"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        try:
            reader = module.PdfReader(str(path))
            if len(reader.pages) < 1:
                raise ExportError(f"Generated PDF has no pages: {path}")
        except ExportError:
            raise
        except Exception as exc:
            raise ExportError(f"Generated PDF could not be opened: {path}: {exc}") from exc
        break


def _validate_pptx(path: Path) -> None:
    if not zipfile.is_zipfile(path):
        raise ExportError(f"Generated PPTX is not a valid ZIP package: {path}")
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names or "ppt/presentation.xml" not in names:
                raise ExportError(f"Generated PPTX is missing presentation parts: {path}")
    except zipfile.BadZipFile as exc:
        raise ExportError(f"Generated PPTX is corrupt: {path}") from exc
    try:
        pptx = importlib.import_module("pptx")
        presentation = pptx.Presentation(str(path))
        if len(presentation.slides) < 1:
            raise ExportError(f"Generated PPTX has no slides: {path}")
    except ExportError:
        raise
    except ImportError:
        pass
    except Exception as exc:
        raise ExportError(f"Generated PPTX could not be opened: {path}: {exc}") from exc


def _pdf_page_count(path: Path) -> int:
    for module_name in ("pypdf", "PyPDF2"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        try:
            return len(module.PdfReader(str(path)).pages)
        except Exception as exc:
            raise ExportError(f"Could not count PDF pages in {path}: {exc}") from exc
    # ReportLab's output has one /Type /Page object per page.  This fallback
    # avoids adding a PDF reader dependency to the project.
    return len(re.findall(rb"/Type\s*/Page(?:\s|/|>)", path.read_bytes()))


def _pptx_page_count(path: Path) -> int:
    try:
        pptx = importlib.import_module("pptx")
        return len(pptx.Presentation(str(path)).slides)
    except ImportError:
        with zipfile.ZipFile(path) as archive:
            return sum(name.startswith("ppt/slides/slide") and name.endswith(".xml") for name in archive.namelist())
    except Exception as exc:
        raise ExportError(f"Could not count PPTX slides in {path}: {exc}") from exc


@dataclass(frozen=True)
class _PdfFonts:
    regular: str
    bold: str
    cjk: str | None
    cjk_bold: str | None


def _font_path(name: str, *, bold: bool = False, cjk: bool = False) -> Path | None:
    roots = [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".fonts"]
    candidates: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        if cjk:
            candidates.extend(
                path
                for path in root.rglob("*")
                if path.is_file()
                and path.suffix.lower() in {".ttf", ".otf"}
                and any(token in path.name.lower() for token in ("cjk", "wqy", "japanese-gothic", "sourcehan"))
            )
        elif "dejavu" in name.lower():
            exact = root / "truetype" / "dejavu" / name
            if exact.exists():
                candidates.append(exact)
    return candidates[0] if candidates else None


def _register_pdf_fonts() -> _PdfFonts:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    def register(name: str, path: Path | None, fallback: str) -> str:
        if path is None:
            return fallback
        try:
            pdfmetrics.getFont(name)
        except KeyError:
            try:
                pdfmetrics.registerFont(TTFont(name, str(path)))
            except Exception:
                return fallback
        return name

    regular = register("FrameNotes-DejaVu", _font_path("DejaVuSans.ttf"), "Helvetica")
    bold = register("FrameNotes-DejaVu-Bold", _font_path("DejaVuSans-Bold.ttf", bold=True), regular)
    # ReportLab cannot embed the common TTC-shaped Noto CJK files directly,
    # but its built-in Unicode CID font is searchable and covers the Han,
    # kana, and Hangul ranges.  Prefer it when available, with an installed
    # TTF fallback for minimal ReportLab builds.
    cjk: str | None = None
    cjk_bold: str | None = None
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        cjk = "STSong-Light"
        cjk_bold = cjk
    except Exception:
        cjk_path = _font_path("", cjk=True)
        cjk = register("FrameNotes-CJK", cjk_path, regular) if cjk_path else None
        cjk_bold = register("FrameNotes-CJK-Bold", cjk_path, cjk or regular) if cjk_path else None
    return _PdfFonts(regular, bold, cjk, cjk_bold)


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x2E80 <= codepoint <= 0x2FFF
        or 0x3000 <= codepoint <= 0x30FF
        or 0x31F0 <= codepoint <= 0x31FF
        or 0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xAC00 <= codepoint <= 0xD7AF
        or 0xF900 <= codepoint <= 0xFAFF
    )


def _font_for_character(character: str, fonts: _PdfFonts, bold: bool = False) -> str:
    if _is_cjk(character) and fonts.cjk:
        return fonts.cjk_bold if bold and fonts.cjk_bold else fonts.cjk
    return fonts.bold if bold else fonts.regular


def _font_runs(text: str, fonts: _PdfFonts, bold: bool = False) -> list[tuple[str, str]]:
    if not text:
        return [(fonts.bold if bold else fonts.regular, "")]
    runs: list[tuple[str, str]] = []
    current_font = _font_for_character(text[0], fonts, bold)
    current = [text[0]]
    for character in text[1:]:
        font = _font_for_character(character, fonts, bold)
        if font != current_font:
            runs.append((current_font, "".join(current)))
            current_font = font
            current = []
        current.append(character)
    runs.append((current_font, "".join(current)))
    return runs


def _pdf_text_width(text: str, size: float, fonts: _PdfFonts, bold: bool = False) -> float:
    from reportlab.pdfbase.pdfmetrics import stringWidth

    return sum(stringWidth(part, font, size) for font, part in _font_runs(text, fonts, bold))


def _wrap_pdf_text(text: str, width: float, size: float, fonts: _PdfFonts, bold: bool = False) -> list[str]:
    """Wrap by measured Unicode glyph width without dropping characters."""

    if not text:
        return [""]
    lines: list[str] = []
    for paragraph in str(text).splitlines() or [""]:
        if not paragraph:
            lines.append("")
            continue
        current = ""
        tokens = re.findall(r"\s+|\S+", paragraph)
        for token in tokens:
            if token.isspace():
                if current and _pdf_text_width(current + token, size, fonts, bold) <= width:
                    current += token
                elif current:
                    lines.append(current.rstrip())
                    current = ""
                continue
            if not current and _pdf_text_width(token, size, fonts, bold) <= width:
                current = token
                continue
            if current and _pdf_text_width(current + token, size, fonts, bold) <= width:
                current += token
                continue
            if current:
                lines.append(current.rstrip())
                current = ""
            if _pdf_text_width(token, size, fonts, bold) <= width:
                current = token
                continue
            # A URL, equation, or CJK run may have no whitespace.  Break that
            # single token by glyph only when it cannot fit as a unit.
            for character in token:
                proposed = current + character
                if current and _pdf_text_width(proposed, size, fonts, bold) > width:
                    lines.append(current.rstrip())
                    current = character
                else:
                    current = proposed
        lines.append(current.rstrip())
    return lines or [""]


def _draw_pdf_line(canvas: Any, text: str, x: float, y: float, size: float, fonts: _PdfFonts, bold: bool = False) -> None:
    cursor = x
    for font, part in _font_runs(text, fonts, bold):
        if not part:
            continue
        canvas.setFont(font, size)
        canvas.drawString(cursor, y, part)
        from reportlab.pdfbase.pdfmetrics import stringWidth

        cursor += stringWidth(part, font, size)


def _draw_pdf_wrapped(
    canvas: Any,
    text: str,
    x: float,
    y: float,
    width: float,
    size: float,
    leading: float,
    fonts: _PdfFonts,
    *,
    bold: bool = False,
) -> float:
    for line in _wrap_pdf_text(text, width, size, fonts, bold):
        _draw_pdf_line(canvas, line, x, y, size, fonts, bold)
        y -= leading
    return y


def _pdf_layout(options: ExportRequest) -> tuple[int, int, bool]:
    layout = options.layout
    if layout == "single":
        return 1, 1, False
    if layout == "grid":
        return max(1, options.columns), 2, False
    if layout == "filmstrip":
        return max(3, options.columns), 1, False
    if layout == "two":
        return 2, 1, False
    if layout == "four":
        return 2, 2, False
    if layout == "contact":
        return 4, 4, False
    if layout == "notes":
        return 1, 1, True
    raise ExportError(f"unsupported PDF layout: {layout}")


def _pdf_page_size(options: ExportRequest) -> tuple[float, float]:
    from reportlab.lib import pagesizes

    size = pagesizes.A4 if options.page_size == "A4" else pagesizes.LETTER
    if options.orientation == "landscape":
        return max(size), min(size)
    return min(size), max(size)


def _source_label(source: SourceRecord | None, fallback: str = "Unknown source") -> str:
    if source is None:
        return fallback
    return f"{source.title} ({source.id})"


def _pdf_content_page_count(source_frames: list[FrameMetadata], capacity: int, include_sections: bool) -> int:
    return (1 if include_sections else 0) + max(1, (len(source_frames) + capacity - 1) // capacity)


def export_pdf(
    job: JobRecord,
    job_dir: Path | str,
    options: ExportRequest,
    output: Path,
    cancel_event: Event | None = None,
) -> Path:
    """Write and validate an annotated or images-only PDF export."""

    try:
        reportlab_canvas = importlib.import_module("reportlab.pdfgen.canvas")
        reportlab_units = importlib.import_module("reportlab.lib.units")
    except ImportError as exc:
        raise ExportError("PDF export requires reportlab") from exc

    frames = _ordered_frames(job)
    if not frames:
        raise ExportError("No retained frames are available for export")
    output.parent.mkdir(parents=True, exist_ok=True)
    base = Path(job_dir)
    if options.images_only:
        _check_cancel(cancel_event)
        first_path = _frame_path(base, frames[0])
        first_width, first_height = _image_size(first_path)
        canvas = reportlab_canvas.Canvas(
            str(output),
            pagesize=(first_width, first_height),
            pageCompression=1,
        )
        try:
            from reportlab.lib.utils import ImageReader

            for frame_index, frame in enumerate(frames):
                _check_cancel(cancel_event)
                image_path = first_path if frame_index == 0 else _frame_path(base, frame)
                image_width, image_height = _image_size(image_path)
                if frame_index:
                    canvas.setPageSize((image_width, image_height))
                canvas.drawImage(
                    ImageReader(str(image_path)),
                    0,
                    0,
                    width=image_width,
                    height=image_height,
                    preserveAspectRatio=True,
                    anchor="c",
                    mask="auto",
                )
                canvas.showPage()
            _check_cancel(cancel_event)
            canvas.save()
        except Exception as exc:
            if isinstance(exc, (CancelledProcessing, ExportError)):
                raise
            raise ExportError(f"Could not write images-only PDF {output}: {exc}") from exc
        _validate_pdf(output)
        return output
    source_groups = _frames_by_source(job, frames)
    page_width, page_height = _pdf_page_size(options)
    margin = float(options.margin_mm) * float(reportlab_units.mm)
    gap = max(8.0, margin * 0.45)
    fonts = _register_pdf_fonts()
    canvas = reportlab_canvas.Canvas(str(output), pagesize=(page_width, page_height), pageCompression=1)
    canvas.setTitle(options.title)
    canvas.setAuthor("FrameNotes AI")

    columns, rows, notes_layout = _pdf_layout(options)
    capacity = columns * rows
    cell_width = (page_width - 2 * margin - (columns - 1) * gap) / columns
    title_height = 64.0
    cell_height = (page_height - 2 * margin - title_height - (rows - 1) * gap - 12) / rows

    def ocr_cell_settings() -> tuple[float, float, float, int]:
        if notes_layout:
            image_height = cell_height * 0.55
            ocr_area = max(16.0, cell_height - image_height - 20.0)
            size = 8.5
            leading = size + 2
        else:
            image_height = max(20.0, cell_height - 24.0 - 38.0)
            ocr_area = 38.0
            size = 6.7
            leading = size + 2
        return image_height, ocr_area, leading, max(1, int((ocr_area - 3) / leading))

    def continuation_capacity() -> int:
        return max(1, int((page_height - 2 * margin - 86) / 13))

    def source_extra_pages(source_frames: list[FrameMetadata]) -> int:
        if not options.include_ocr_text:
            return 0
        _, _, _, max_lines = ocr_cell_settings()
        extra = 0
        for frame in source_frames:
            text = normalize_ocr_text(frame.ocr_text)
            if not text:
                continue
            lines = _wrap_pdf_text(text, cell_width - 4, 6.7 if not notes_layout else 8.5, fonts)
            if len(lines) <= max_lines:
                continue
            remainder = "\n".join(lines[max_lines:])
            continuation_lines = _wrap_pdf_text(remainder, page_width - 2 * margin, 10, fonts)
            extra += max(1, (len(continuation_lines) + continuation_capacity() - 1) // continuation_capacity())
        return extra

    toc_enabled = len(frames) > 8 or len(source_groups) > 3
    toc_entries: list[tuple[str, int]] = []
    cover_pages = 1 if options.include_cover else 0
    # Reserve several lines per entry so a long Unicode source title cannot
    # push later contents entries into the footer.
    toc_rows_per_page = max(1, int((page_height - 2 * margin - 70) / (18 * 4)))
    toc_pages = (len(source_groups) + toc_rows_per_page - 1) // toc_rows_per_page if toc_enabled else 0
    next_content_page = cover_pages + toc_pages + 1
    for source, source_frames in source_groups:
        toc_entries.append((_source_label(source, source_frames[0].source_title), next_content_page))
        next_content_page += _pdf_content_page_count(source_frames, capacity, options.include_sections)
        next_content_page += source_extra_pages(source_frames)

    page_number = 1

    def footer(source_info: str) -> None:
        text = f"{options.footer} · Page {page_number} · Source: {source_info}"
        _draw_pdf_line(canvas, text, margin, max(12.0, margin * 0.42), 7.5, fonts)

    def finish_page(source_info: str) -> None:
        nonlocal page_number
        _check_cancel(cancel_event)
        footer(source_info)
        canvas.showPage()
        page_number += 1

    if options.include_cover:
        _check_cancel(cancel_event)
        _draw_pdf_wrapped(
            canvas,
            options.title,
            margin,
            page_height - margin - 28,
            page_width - 2 * margin,
            26,
            32,
            fonts,
            bold=True,
        )
        _draw_pdf_wrapped(
            canvas,
            "Extracted visual notes",
            margin,
            page_height - margin - 78,
            page_width - 2 * margin,
            12,
            16,
            fonts,
        )
        metadata = (
            f"Created {utc_now().astimezone(timezone.utc).isoformat()} · "
            f"Sources: {len(job.sources)} · Retained frames: {len(frames)}"
        )
        _draw_pdf_wrapped(canvas, metadata, margin, page_height - margin - 112, page_width - 2 * margin, 9, 13, fonts)
        y = page_height - margin - 155
        _draw_pdf_wrapped(canvas, "Source information", margin, y, page_width - 2 * margin, 13, 17, fonts, bold=True)
        y -= 25
        for source in job.sources:
            _check_cancel(cancel_event)
            details = f"{source.title} · {source.kind} · {source.input}"
            lines = _wrap_pdf_text(details, page_width - 2 * margin, 9, fonts)
            if y - len(lines) * 12 < margin + 18:
                break
            for line in lines:
                _draw_pdf_line(canvas, line, margin, y, 9, fonts)
                y -= 12
        finish_page("all sources")

    if toc_enabled:
        for toc_page in range(toc_pages):
            _check_cancel(cancel_event)
            _draw_pdf_wrapped(canvas, "Contents", margin, page_height - margin - 24, page_width - 2 * margin, 20, 24, fonts, bold=True)
            y = page_height - margin - 65
            start = toc_page * toc_rows_per_page
            end = min(len(toc_entries), start + toc_rows_per_page)
            for title, target_page in toc_entries[start:end]:
                _check_cancel(cancel_event)
                label = f"{title} · page {target_page}"
                y = _draw_pdf_wrapped(canvas, label, margin, y, page_width - 2 * margin, 10, 13, fonts)
                y -= 5
            finish_page("all sources")

    for source, source_frames in source_groups:
        _check_cancel(cancel_event)
        source_info = _source_label(source, source_frames[0].source_title)
        bookmark_key = f"source-{page_number}-{uuid.uuid4().hex}"
        if options.include_sections:
            canvas.bookmarkPage(bookmark_key)
            canvas.addOutlineEntry(source_info, bookmark_key, level=0, closed=False)
            y = _draw_pdf_wrapped(canvas, source.title if source else source_frames[0].source_title, margin, page_height - margin - 32, page_width - 2 * margin, 22, 27, fonts, bold=True)
            y -= 18
            if source is not None:
                y = _draw_pdf_wrapped(canvas, f"Source: {source.input}", margin, y, page_width - 2 * margin, 10, 14, fonts)
                y -= 8
                y = _draw_pdf_wrapped(canvas, f"Type: {source.kind} · {source.id}", margin, y, page_width - 2 * margin, 10, 14, fonts)
            y -= 12
            _draw_pdf_wrapped(canvas, f"Retained frames: {len(source_frames)}", margin, y, page_width - 2 * margin, 10, 14, fonts)
            finish_page(source_info)

        for chunk_start in range(0, len(source_frames), capacity):
            _check_cancel(cancel_event)
            page_frames = source_frames[chunk_start : chunk_start + capacity]
            overflow: list[tuple[FrameMetadata, list[str]]] = []
            if chunk_start == 0 and not options.include_sections:
                canvas.bookmarkPage(bookmark_key)
                canvas.addOutlineEntry(source_info, bookmark_key, level=0, closed=False)
            else:
                canvas.bookmarkPage(f"source-{page_number}")
            heading = source.title if options.include_source_titles and source else options.title
            _draw_pdf_wrapped(canvas, heading, margin, page_height - margin - 18, page_width - 2 * margin, 14, 17, fonts, bold=True)
            for cell_index, frame in enumerate(page_frames):
                _check_cancel(cancel_event)
                column = cell_index % columns
                row = cell_index // columns
                x = margin + column * (cell_width + gap)
                y = page_height - margin - title_height - (row + 1) * cell_height - row * gap
                image_path = _frame_path(base, frame)
                ocr_text = normalize_ocr_text(frame.ocr_text) if options.include_ocr_text else None
                image_height, ocr_area, ocr_leading, max_ocr_lines = ocr_cell_settings()
                if notes_layout:
                    image_y = y + cell_height - image_height
                    caption_y = image_y - 13
                    ocr_top = y + ocr_area - 2
                else:
                    if not ocr_text:
                        image_height = max(20.0, cell_height - 24.0)
                        ocr_area = 0.0
                    image_y = y + (ocr_area if ocr_text else 0.0) + 24.0
                    caption_y = y + (ocr_area if ocr_text else 0.0) + 5.0
                    ocr_top = y + ocr_area - 2
                box_width, box_height = _preferred_aspect_box(cell_width, image_height, options.aspect_ratio)
                _draw_pdf_image(
                    canvas,
                    image_path,
                    x + (cell_width - box_width) / 2,
                    image_y + (image_height - box_height) / 2,
                    box_width,
                    box_height,
                )
                caption = f"Frame {frame.index}"
                if options.include_timestamps:
                    caption += f" · {_timestamp(frame.timestamp)}"
                _draw_pdf_wrapped(canvas, caption, x + 2, caption_y, cell_width - 4, 8, 10, fonts)
                if ocr_text:
                    ocr_size = 8.5 if notes_layout else 6.7
                    lines = _wrap_pdf_text(ocr_text, cell_width - 4, ocr_size, fonts)
                    visible = lines[:max_ocr_lines]
                    _draw_pdf_wrapped(canvas, "\n".join(visible), x + 2, ocr_top, cell_width - 4, ocr_size, ocr_leading, fonts)
                    if len(lines) > max_ocr_lines:
                        remainder = "\n".join(lines[max_ocr_lines:])
                        overflow.append((frame, _wrap_pdf_text(remainder, page_width - 2 * margin, 10, fonts)))
            finish_page(source_info)
            for frame, lines in overflow:
                for continuation_start in range(0, len(lines), continuation_capacity()):
                    _check_cancel(cancel_event)
                    continuation = lines[continuation_start : continuation_start + continuation_capacity()]
                    canvas.bookmarkPage(f"frame-{frame.id}-{page_number}")
                    heading = f"{source.title if source else frame.source_title} · Frame {frame.index} · OCR continuation"
                    y = _draw_pdf_wrapped(canvas, heading, margin, page_height - margin - 24, page_width - 2 * margin, 15, 19, fonts, bold=True)
                    y -= 14
                    _draw_pdf_wrapped(canvas, "\n".join(continuation), margin, y, page_width - 2 * margin, 10, 13, fonts)
                    finish_page(source_info)

    try:
        canvas.save()
    except Exception as exc:
        raise ExportError(f"Could not write PDF {output}: {exc}") from exc
    _validate_pdf(output)
    return output


def _pptx_font_name() -> str:
    # Prefer the installed CJK family when available; DejaVu remains the
    # explicit Latin/Greek/Cyrillic fallback in environments without it.
    if Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc").exists():
        return "Noto Sans CJK JP"
    if Path("/usr/share/fonts/truetype/fonts-japanese-gothic.ttf").exists():
        return "IPAGothic"
    return "DejaVu Sans"


def _add_pptx_picture(slide: Any, path: Path, left: int, top: int, width: int, height: int, cancel_event: Event | None = None) -> None:
    _check_cancel(cancel_event)
    image_width, image_height = _image_size(path)
    box_width, box_height = _fit_box(image_width, image_height, width, height)
    slide.shapes.add_picture(
        str(path),
        int(left + (width - box_width) / 2),
        int(top + (height - box_height) / 2),
        width=int(box_width),
        height=int(box_height),
    )


def _pptx_chunks(text: str, width: int = 88) -> list[str]:
    normalized = str(text)
    chunks: list[str] = []
    for paragraph in normalized.splitlines() or [""]:
        if not paragraph:
            chunks.append("")
            continue
        chunks.extend(textwrap.wrap(paragraph, width=width, break_long_words=True, break_on_hyphens=False) or [""])
    return chunks or [""]


def export_pptx(
    job: JobRecord,
    job_dir: Path | str,
    options: ExportRequest,
    output: Path,
    cancel_event: Event | None = None,
) -> Path:
    """Write and validate an annotated or images-only PowerPoint deck."""

    try:
        pptx = importlib.import_module("pptx")
        util = importlib.import_module("pptx.util")
        enum = importlib.import_module("pptx.enum.text")
    except ImportError as exc:
        raise ExportError("PPTX export requires python-pptx") from exc

    frames = _ordered_frames(job)
    if not frames:
        raise ExportError("No retained frames are available for export")
    output.parent.mkdir(parents=True, exist_ok=True)
    presentation = pptx.Presentation()
    base_height = 7.5
    if options.aspect_ratio == "16:9":
        base_width = 13.333333
    else:
        base_width = 10.0
    if options.orientation == "portrait":
        presentation.slide_width = util.Inches(base_height)
        presentation.slide_height = util.Inches(base_width)
    else:
        presentation.slide_width = util.Inches(base_width)
        presentation.slide_height = util.Inches(base_height)
    blank_layout = presentation.slide_layouts[6]
    base = Path(job_dir)
    if options.images_only:
        _check_cancel(cancel_event)
        first_path = _frame_path(base, frames[0])
        first_width, first_height = _image_size(first_path)
        reference_height = util.Inches(7.5)
        scale = max(1, reference_height // first_height)
        slide_height = first_height * scale
        slide_width = first_width * scale
        presentation.slide_width = slide_width
        presentation.slide_height = slide_height
        for frame in frames:
            _check_cancel(cancel_event)
            slide = presentation.slides.add_slide(blank_layout)
            _add_pptx_picture(
                slide,
                _frame_path(base, frame),
                0,
                0,
                presentation.slide_width,
                presentation.slide_height,
                cancel_event,
            )
        try:
            _check_cancel(cancel_event)
            presentation.save(str(output))
        except CancelledProcessing:
            raise
        except Exception as exc:
            raise ExportError(f"Could not write images-only PPTX {output}: {exc}") from exc
        _validate_pptx(output)
        return output
    source_groups = _frames_by_source(job, frames)
    slide_width = presentation.slide_width
    slide_height = presentation.slide_height
    margin_x = util.Mm(options.margin_mm)
    margin_y = util.Mm(options.margin_mm)
    font_name = _pptx_font_name()
    try:
        alignment = enum.PP_ALIGN.LEFT
    except AttributeError:
        alignment = None

    def add_text(
        slide: Any,
        text: str,
        left: int,
        top: int,
        width: int,
        height: int,
        size: int,
        *,
        bold: bool = False,
        color: tuple[int, int, int] | None = None,
    ) -> Any:
        box = slide.shapes.add_textbox(left, top, width, height)
        text_frame = box.text_frame
        text_frame.clear()
        text_frame.word_wrap = True
        text_frame.margin_left = util.Inches(0.04)
        text_frame.margin_right = util.Inches(0.04)
        text_frame.margin_top = util.Inches(0.03)
        text_frame.margin_bottom = util.Inches(0.03)
        paragraph = text_frame.paragraphs[0]
        if alignment is not None:
            paragraph.alignment = alignment
        run = paragraph.add_run()
        run.text = str(text)
        run.font.name = font_name
        run.font.size = util.Pt(size)
        run.font.bold = bold
        if color is not None:
            from pptx.dml.color import RGBColor

            run.font.color.rgb = RGBColor(*color)
        return box

    def add_footer(slide: Any, source_info: str) -> None:
        slide_number = len(presentation.slides)
        text = f"{options.footer} · Slide {slide_number} · Source: {source_info}"
        add_text(slide, text, margin_x, slide_height - util.Inches(0.32), slide_width - 2 * margin_x, util.Inches(0.22), 7, color=(90, 90, 90))

    def add_cover() -> None:
        _check_cancel(cancel_event)
        slide = presentation.slides.add_slide(blank_layout)
        add_text(slide, options.title, margin_x, util.Inches(1.15), slide_width - 2 * margin_x, util.Inches(1.1), 28, bold=True)
        summary = f"Extracted visual notes · {len(job.sources)} source(s) · {len(frames)} retained frame(s)"
        add_text(slide, summary, margin_x, util.Inches(2.55), slide_width - 2 * margin_x, util.Inches(0.5), 15)
        add_text(slide, utc_now().astimezone(timezone.utc).isoformat(), margin_x, util.Inches(3.1), slide_width - 2 * margin_x, util.Inches(0.4), 10)
        source_text = "\n".join(f"{source.title} · {source.kind} · {source.input}" for source in job.sources)
        if source_text:
            add_text(slide, source_text, margin_x, util.Inches(3.7), slide_width - 2 * margin_x, slide_height - util.Inches(4.4), 10)
        add_footer(slide, "all sources")

    def add_section(source: SourceRecord | None, source_frames: list[FrameMetadata]) -> None:
        _check_cancel(cancel_event)
        slide = presentation.slides.add_slide(blank_layout)
        title = source.title if source else source_frames[0].source_title
        add_text(slide, title, margin_x, util.Inches(1.3), slide_width - 2 * margin_x, util.Inches(1.25), 26, bold=True)
        if source is not None:
            details = f"Source: {source.input}\nType: {source.kind} · {source.id}\nRetained frames: {len(source_frames)}"
        else:
            details = f"Retained frames: {len(source_frames)}"
        add_text(slide, details, margin_x, util.Inches(3.0), slide_width - 2 * margin_x, util.Inches(1.5), 13)
        add_footer(slide, _source_label(source, source_frames[0].source_title))

    def add_frame_slide(source: SourceRecord | None, frame: FrameMetadata, ocr_chunk: str | None, continuation: bool = False) -> None:
        _check_cancel(cancel_event)
        slide = presentation.slides.add_slide(blank_layout)
        source_info = _source_label(source, frame.source_title)
        title = source.title if options.include_source_titles and source else options.title
        suffix = " · OCR continuation" if continuation else ""
        add_text(slide, f"{title}{suffix}", margin_x, margin_y, slide_width - 2 * margin_x, util.Inches(0.55), 16, bold=True)
        caption = f"Frame {frame.index}"
        if options.include_timestamps:
            caption += f" · {_timestamp(frame.timestamp)}"
        if options.orientation == "portrait":
            image_left = margin_x
            image_top = util.Inches(1.05)
            image_width = slide_width - 2 * margin_x
            image_height = slide_height * 0.47
            text_left = margin_x
            text_top = image_top + image_height + util.Inches(0.14)
            text_width = slide_width - 2 * margin_x
            text_height = slide_height - text_top - util.Inches(0.55)
        else:
            image_left = margin_x
            image_top = util.Inches(1.0)
            image_width = slide_width * 0.61
            image_height = slide_height - util.Inches(1.55)
            text_left = image_left + image_width + util.Inches(0.18)
            text_top = image_top
            text_width = slide_width - text_left - margin_x
            text_height = image_height
        # Continuation slides repeat the same contained image so every slide
        # still has exactly one associated frame and the OCR can be reviewed
        # without losing visual context.
        image_path = _frame_path(base, frame)
        _add_pptx_picture(slide, image_path, image_left, image_top, image_width, image_height, cancel_event)
        add_text(slide, caption, text_left, text_top, text_width, util.Inches(0.42), 11, bold=True)
        if ocr_chunk:
            add_text(slide, ocr_chunk, text_left, text_top + util.Inches(0.45), text_width, text_height - util.Inches(0.45), 10)
        elif options.include_ocr_text and not continuation:
            add_text(slide, "OCR text: unavailable for this frame", text_left, text_top + util.Inches(0.45), text_width, util.Inches(0.4), 9, color=(90, 90, 90))
        add_footer(slide, source_info)

    if options.include_cover:
        add_cover()
    for source, source_frames in source_groups:
        _check_cancel(cancel_event)
        if options.include_sections:
            add_section(source, source_frames)
        for frame in source_frames:
            _check_cancel(cancel_event)
            ocr_text = normalize_ocr_text(frame.ocr_text) if options.include_ocr_text else None
            chunks = _pptx_chunks(ocr_text, 88) if ocr_text else [None]
            add_frame_slide(source, frame, chunks[0], continuation=False)
            for chunk in chunks[1:]:
                _check_cancel(cancel_event)
                add_frame_slide(source, frame, chunk, continuation=True)

    try:
        presentation.save(str(output))
    except Exception as exc:
        raise ExportError(f"Could not write PPTX {output}: {exc}") from exc
    _validate_pptx(output)
    return output


def _temporary_output(directory: Path, format_name: str) -> Path:
    return directory / f".framenotes-{uuid.uuid4().hex}.tmp.{format_name}"


def _published_output(directory: Path, stem: str, job_id: str, format_name: str, source_id: str | None, token: str) -> Path:
    suffix = f"-{_safe_filename(source_id)}" if source_id else "-combined"
    return directory / f"{stem}-{job_id[:8]}{suffix}-{token}.{format_name}"


def _source_job(job: JobRecord, source: SourceRecord, frames: list[FrameMetadata]) -> JobRecord:
    return job.model_copy(deep=True, update={"sources": [source.model_copy(deep=True)], "frames": frames})


def create_exports(
    job: JobRecord,
    job_dir: Path | str,
    options: ExportRequest,
    cancel_event: Event | None = None,
) -> list[DownloadArtifact]:
    """Generate validated, atomically published artifacts and metadata.

    A combined export is always generated.  With ``separate_videos`` enabled,
    one additional artifact per source with retained frames is generated for
    every requested format.
    """

    combined_frames = _ordered_frames(job)
    if not combined_frames:
        raise ExportError("No retained frames are available for export")
    directory = Path(job_dir) / "downloads"
    directory.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(options.title)
    token = uuid.uuid4().hex[:12]
    export_jobs: list[tuple[JobRecord, str | None]] = [(job, None)]
    if options.separate_videos:
        for source, source_frames in _frames_by_source(job, combined_frames):
            _check_cancel(cancel_event)
            if source is not None and source_frames:
                export_jobs.append((_source_job(job, source, source_frames), source.id))

    artifacts: list[DownloadArtifact] = []
    published: list[Path] = []
    temporaries: set[Path] = set()
    try:
        for export_job_record, source_id in export_jobs:
            _check_cancel(cancel_event)
            for format_name in options.formats:
                _check_cancel(cancel_event)
                temporary = _temporary_output(directory, format_name)
                temporaries.add(temporary)
                final = _published_output(directory, stem, job.id, format_name, source_id, token)
                try:
                    if format_name == "pdf":
                        export_pdf(export_job_record, job_dir, options, temporary, cancel_event)
                    elif format_name == "pptx":
                        export_pptx(export_job_record, job_dir, options, temporary, cancel_event)
                    else:
                        raise ExportError(f"unsupported export format: {format_name}")
                    _check_cancel(cancel_event)
                    # UUID names are unique, and the existence check prevents
                    # an exceptionally unlucky collision from replacing a file
                    # already exposed through the download API.
                    if final.exists():
                        raise ExportError(f"Generated artifact name already exists: {final.name}")
                    os.replace(temporary, final)
                    temporaries.discard(temporary)
                    published.append(final)
                except Exception:
                    temporary.unlink(missing_ok=True)
                    raise
                page_count = _pdf_page_count(final) if format_name == "pdf" else _pptx_page_count(final)
                artifacts.append(
                    DownloadArtifact(
                        filename=final.name,
                        format=format_name,
                        size_bytes=final.stat().st_size,
                        created_at=utc_now(),
                        page_count=page_count,
                        source_id=source_id,
                    )
                )
    except Exception:
        for path in published:
            path.unlink(missing_ok=True)
        # Remove only this invocation's temp files; another concurrent export
        # may be publishing its own temporary artifact in the same directory.
        for path in temporaries:
            path.unlink(missing_ok=True)
        raise
    return artifacts


# Short aliases for callers that prefer a generic name.
export_job = create_exports
