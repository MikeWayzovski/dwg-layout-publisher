"""Publish PaperSpace layouts from a DWG or DXF file to separate PDF or DXF files.

DWG input is converted with the external ``dwg2dxf`` program (LibreDWG). There is
no dependency on AutoCAD or QCAD. DXF reading and layout rendering use ``ezdxf``.

Limitations:
- LibreDWG does not support every DWG version or every entity.
- Viewports are rendered as a top view. 3D geometry is projected onto the XY plane.
- DXF export is a flattened drawing (lines, polylines, hatches). Text becomes
  outlines, not editable TEXT entities.
"""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import ezdxf
from ezdxf import bbox as ezdxf_bbox
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing.dxf import DXFBackend
from ezdxf.addons.drawing.layout import Page
from ezdxf.addons.drawing.properties import BackendProperties
from ezdxf.addons.drawing.pymupdf import PyMuPdfBackend
from ezdxf.document import Drawing
from ezdxf.layouts import BaseLayout, Modelspace
from ezdxf.path import to_splines_and_polylines

SUPPORTED_FORMATS = ("pdf", "dxf")
A3_WIDTH_MM = 420.0
A3_HEIGHT_MM = 297.0
_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
logger = logging.getLogger("dwg_layout_publisher")


class Dwg2dxfNotFoundError(FileNotFoundError):
    """``dwg2dxf`` is not available on this machine."""


class DwgConversionError(RuntimeError):
    """LibreDWG could not convert a DWG file to DXF."""


class DxfLoadError(RuntimeError):
    """A DXF file is missing, unreadable, or too corrupt to recover."""


class NoLayoutsError(RuntimeError):
    """The drawing has no layouts that match the request."""


class _SheetDxfBackend(DXFBackend):
    """DXF backend that accepts the numpy paths the drawing frontend emits.

    ezdxf 1.4 hands a ``NumpyPath2d`` to ``to_splines_and_polylines``, which only
    unwraps ``ezdxf.path.Path``. Circles and arcs then fail while flattening a
    layout. Convert to a path object first.
    """

    def draw_path(self, path, properties: BackendProperties) -> None:
        attribs = self.resolve_properties(properties)
        if path.has_curves:
            source = path.to_path() if hasattr(path, "to_path") else path
            for entity in to_splines_and_polylines(source, dxfattribs=attribs):
                self.layout.add_entity(entity)
            return
        self.layout.add_lwpolyline(path.control_vertices(), dxfattribs=attribs)


@dataclass(frozen=True)
class LayoutInfo:
    """One layout discovered in a drawing."""

    name: str
    paper_width_mm: float | None
    paper_height_mm: float | None
    entity_count: int
    is_model: bool


def find_dwg2dxf() -> Path:
    """Return the ``dwg2dxf`` executable.

    Search order: ``DWG2DXF`` environment variable, a copy bundled next to a
    frozen executable, ``sys._MEIPASS``, then the system ``PATH``.
    """
    names = ("dwg2dxf.exe", "dwg2dxf") if sys.platform == "win32" else ("dwg2dxf",)

    env_value = os.environ.get("DWG2DXF")
    if env_value:
        env_path = Path(env_value)
        if env_path.is_file():
            return env_path
        raise Dwg2dxfNotFoundError(
            f"DWG2DXF wijst naar een bestand dat niet bestaat: {env_path}"
        )

    for candidate in _bundled_dwg2dxf_candidates(names):
        if candidate.is_file():
            return candidate

    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)

    raise Dwg2dxfNotFoundError(_missing_dwg2dxf_message())


def load_document(path: str | Path) -> Drawing:
    """Load a DWG or DXF file into an ezdxf document.

    A DWG file is converted to a temporary DXF with ``dwg2dxf`` first. The
    temporary file is removed after the document has been read into memory.
    """
    source = Path(path)
    if not source.is_file():
        raise DxfLoadError(f"Bestand niet gevonden: {source}")

    suffix = source.suffix.lower()
    if suffix == ".dwg":
        with tempfile.TemporaryDirectory(prefix="dwg-layout-") as tmp:
            dxf_path = Path(tmp) / f"{source.stem}.dxf"
            _convert_dwg(source, dxf_path)
            return _read_dxf(dxf_path)
    if suffix == ".dxf":
        return _read_dxf(source)
    raise DxfLoadError(
        f"Niet-ondersteund bestandstype '{source.suffix}'. Gebruik .dwg of .dxf."
    )


def list_layouts(doc: Drawing, *, include_model: bool = False) -> list[LayoutInfo]:
    """Return layouts in document order.

    Model space is omitted unless ``include_model`` is true.
    """
    infos: list[LayoutInfo] = []
    for layout in doc.layouts:
        is_model = _is_modelspace(layout)
        if is_model and not include_model:
            continue
        width, height = resolve_paper_mm(layout)
        infos.append(
            LayoutInfo(
                name=layout.name,
                paper_width_mm=width,
                paper_height_mm=height,
                entity_count=sum(1 for _ in layout),
                is_model=is_model,
            )
        )
    return infos


def export_layout_pdf(doc: Drawing, layout_name: str, dest: str | Path) -> Path:
    """Render one layout to a PDF file. Paper size follows the layout sheet."""
    layout = _get_layout(doc, layout_name)
    destination = Path(dest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        backend = PyMuPdfBackend()
        Frontend(RenderContext(doc), backend).draw_layout(layout, finalize=True)
        destination.write_bytes(backend.get_pdf_bytes(_page_for_layout(layout)))
    except OSError as exc:
        raise DxfLoadError(f"Kan PDF niet schrijven: {destination}: {exc}") from exc
    except Exception as exc:
        logger.exception(
            "Layout '%s' kon niet naar PDF worden geëxporteerd.", layout_name
        )
        raise DxfLoadError(
            f"Layout '{layout_name}' kon niet naar PDF worden geëxporteerd: {exc}"
        ) from exc
    return destination


def export_layout_dxf(doc: Drawing, layout_name: str, dest: str | Path) -> Path:
    """Flatten one layout into the model space of a new DXF file.

    Viewport contents are drawn into that model space, so viewers that ignore
    PaperSpace still show the sheet.
    """
    layout = _get_layout(doc, layout_name)
    destination = Path(dest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    output = ezdxf.new(dxfversion="R2010")
    try:
        backend = _SheetDxfBackend(output.modelspace())
        Frontend(RenderContext(doc), backend).draw_layout(layout, finalize=True)
        output.saveas(destination)
    except OSError as exc:
        raise DxfLoadError(f"Kan DXF niet schrijven: {destination}: {exc}") from exc
    except Exception as exc:
        logger.exception(
            "Layout '%s' kon niet naar DXF worden geëxporteerd.", layout_name
        )
        raise DxfLoadError(
            f"Layout '{layout_name}' kon niet naar DXF worden geëxporteerd: {exc}"
        ) from exc
    return destination


def publish(
    path: str | Path,
    output_dir: str | Path,
    formats: Iterable[str],
    layout_names: Iterable[str] | None = None,
    *,
    include_model: bool = False,
) -> list[Path]:
    """Export the requested layouts to ``output_dir``.

    ``formats`` may contain ``pdf`` and ``dxf``. An empty ``layout_names``
    sequence exports every available layout. Files are named
    ``{stem}_{layout}.{ext}``.
    """
    chosen = _normalize_formats(formats)
    doc = load_document(path)
    available = list_layouts(doc, include_model=include_model)
    if not available:
        if include_model:
            raise NoLayoutsError("Geen layouts gevonden in dit bestand.")
        raise NoLayoutsError(
            "Geen PaperSpace-layouts gevonden. "
            "Gebruik --include-model om modelspace mee te nemen."
        )

    selected = _select_layouts(available, layout_names)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    stem = Path(path).stem
    written: list[Path] = []
    used_names: set[str] = set()
    for info in selected:
        safe = _unique_filename(info.name, used_names)
        for fmt in chosen:
            destination = output / f"{stem}_{safe}.{fmt}"
            if fmt == "pdf":
                written.append(export_layout_pdf(doc, info.name, destination))
            else:
                written.append(export_layout_dxf(doc, info.name, destination))
    return written


def format_layout_line(info: LayoutInfo) -> str:
    """One scan line: name, paper size, entity count, and space."""
    if info.paper_width_mm and info.paper_height_mm:
        paper = f"{info.paper_width_mm:g} x {info.paper_height_mm:g} mm"
    else:
        paper = "onbekend formaat"
    kind = "modelspace" if info.is_model else "paperspace"
    return f"{info.name}\t{paper}\t{info.entity_count} objecten\t{kind}"


def _bundled_dwg2dxf_candidates(names: Sequence[str]) -> list[Path]:
    if not getattr(sys, "frozen", False):
        return []
    exe_dir = Path(sys.executable).resolve().parent
    meipass = Path(getattr(sys, "_MEIPASS", exe_dir))
    bases = (exe_dir, exe_dir / "_internal", meipass)
    candidates: list[Path] = []
    for base in bases:
        for folder in (base / "libredwg", base):
            for name in names:
                candidates.append(folder / name)
    return candidates


def _missing_dwg2dxf_message() -> str:
    if sys.platform == "win32":
        return (
            "dwg2dxf is niet gevonden. Zet LibreDWG (dwg2dxf.exe en de bijbehorende "
            "DLL's) in de map libredwg naast deze toepassing, of wijs DWG2DXF naar "
            "dwg2dxf.exe."
        )
    return (
        "dwg2dxf is niet gevonden. Installeer het pakket libredwg-tools, of wijs de "
        "omgevingsvariabele DWG2DXF naar het programma dwg2dxf."
    )


def _convert_dwg(source: Path, dest: Path) -> None:
    tool = find_dwg2dxf()
    env = os.environ.copy()
    env["PATH"] = str(tool.parent) + os.pathsep + env.get("PATH", "")
    command = [str(tool), "-y", "-o", str(dest), str(source)]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            cwd=str(tool.parent),
            env=env,
        )
    except OSError as exc:
        raise DwgConversionError(f"dwg2dxf kon niet worden gestart: {exc}") from exc

    detail = (result.stderr or result.stdout or "").strip()
    if result.returncode != 0 or not dest.is_file():
        extra = f" {detail}" if detail else ""
        raise DwgConversionError(
            "DWG-conversie mislukt "
            f"(exit {result.returncode}). Het bestand is mogelijk corrupt of een "
            f"niet-ondersteunde DWG-versie.{extra}"
        )


def _read_dxf(path: Path) -> Drawing:
    try:
        return ezdxf.readfile(path)
    except ezdxf.DXFStructureError:
        return _recover_dxf(path)
    except OSError as exc:
        raise DxfLoadError(f"Kan DXF niet lezen: {path}: {exc}") from exc
    except ezdxf.DXFError as exc:
        raise DxfLoadError(f"Ongeldig DXF-bestand: {path.name}: {exc}") from exc


def _recover_dxf(path: Path) -> Drawing:
    try:
        doc, _auditor = ezdxf.recover.readfile(path)
    except (OSError, ezdxf.DXFError) as exc:
        raise DxfLoadError(
            f"DXF is corrupt en kon niet worden hersteld: {path.name}: {exc}"
        ) from exc
    return doc


def _is_modelspace(layout: BaseLayout) -> bool:
    return isinstance(layout, Modelspace) or layout.name.upper() == "MODEL"


def resolve_paper_mm(layout: BaseLayout) -> tuple[float, float]:
    """Return a usable page size in millimetres.

    ``page_real_width`` / ``page_real_height`` are preferred when a converter
    wrote them. Otherwise the DXF ``paper_width`` / ``paper_height`` values are
    used. NaN, infinite, and non-positive sizes are invalid: the geometry
    bounding box is tried next, and A3 (420 x 297 mm) is the last resort.
    """
    stored = _valid_stored_paper_mm(layout)
    if stored is not None:
        return stored
    logger.warning(
        "Layout '%s' heeft geen geldige papierafmetingen (page_real_width/"
        "page_real_height of paper_width/paper_height). De bounding box wordt gebruikt.",
        layout.name,
    )
    box = _bbox_size_mm(layout)
    if box is not None:
        return box
    logger.warning(
        "Layout '%s' heeft geen bruikbare geometrie. A3 (420 x 297 mm) wordt gebruikt.",
        layout.name,
    )
    return A3_WIDTH_MM, A3_HEIGHT_MM


def _valid_stored_paper_mm(layout: BaseLayout) -> tuple[float, float] | None:
    real_width = _dxf_millimetres(layout, "page_real_width")
    real_height = _dxf_millimetres(layout, "page_real_height")
    if real_width is not None and real_height is not None:
        return real_width, real_height
    width = _dxf_millimetres(layout, "paper_width")
    height = _dxf_millimetres(layout, "paper_height")
    if width is not None and height is not None:
        return width, height
    return None


def _dxf_millimetres(layout: BaseLayout, name: str) -> float | None:
    dxf = layout.dxf
    try:
        if not dxf.hasattr(name):
            return None
        raw = dxf.get(name)
    except Exception:
        logger.exception("Attribuut '%s' van layout '%s' is onleesbaar.", name, layout.name)
        return None
    return _finite_positive(raw)


def _finite_positive(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _bbox_size_mm(layout: BaseLayout) -> tuple[float, float] | None:
    try:
        box = ezdxf_bbox.extents(layout)
    except Exception:
        logger.exception("Bounding box van layout '%s' kon niet worden berekend.", layout.name)
        return None
    if not box.has_data:
        return None
    width = _finite_positive(box.size.x)
    height = _finite_positive(box.size.y)
    if width is None or height is None:
        return None
    return width, height


def _get_layout(doc: Drawing, name: str) -> BaseLayout:
    try:
        return doc.layouts.get(name)
    except KeyError as exc:
        raise NoLayoutsError(f"Onbekende layout: {name}") from exc


def _page_for_layout(layout: BaseLayout) -> Page:
    stored = _valid_stored_paper_mm(layout)
    if stored is not None:
        try:
            page = Page.from_dxf_layout(layout)
        except Exception:
            logger.exception(
                "Paginainstellingen van layout '%s' zijn onleesbaar.", layout.name
            )
        else:
            if _page_is_usable(page):
                return page
        return Page(*stored)
    width, height = resolve_paper_mm(layout)
    return Page(width, height)


def _page_is_usable(page: Page) -> bool:
    if _finite_positive(page.width) is None or _finite_positive(page.height) is None:
        return False
    margins = page.margins
    return all(
        _finite_non_negative(value)
        for value in (margins.top, margins.right, margins.bottom, margins.left)
    )


def _finite_non_negative(value: object) -> bool:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number >= 0


def _normalize_formats(formats: Iterable[str]) -> tuple[str, ...]:
    chosen: list[str] = []
    for raw in formats:
        fmt = raw.strip().lower()
        if not fmt:
            continue
        if fmt not in SUPPORTED_FORMATS:
            allowed = ", ".join(SUPPORTED_FORMATS)
            raise ValueError(f"Onbekend formaat '{raw}'. Kies uit: {allowed}.")
        if fmt not in chosen:
            chosen.append(fmt)
    if not chosen:
        raise ValueError("Geen uitvoerformaat opgegeven. Kies pdf en/of dxf.")
    return tuple(chosen)


def _select_layouts(
    available: Sequence[LayoutInfo],
    layout_names: Iterable[str] | None,
) -> list[LayoutInfo]:
    if layout_names is None:
        return list(available)
    wanted = [name.strip() for name in layout_names if name.strip()]
    if not wanted:
        return list(available)
    by_name = {info.name: info for info in available}
    missing = [name for name in wanted if name not in by_name]
    if missing:
        known = ", ".join(info.name for info in available)
        raise NoLayoutsError(
            f"Onbekende layout(s): {', '.join(missing)}. Aanwezig: {known}."
        )
    return [by_name[name] for name in wanted]


def _safe_filename(name: str) -> str:
    cleaned = _INVALID_FILENAME.sub("_", name).strip(" .")
    return cleaned or "layout"


def _unique_filename(name: str, used: set[str]) -> str:
    base = _safe_filename(name)
    candidate = base
    number = 2
    while candidate.lower() in used:
        candidate = f"{base}_{number}"
        number += 1
    used.add(candidate.lower())
    return candidate
