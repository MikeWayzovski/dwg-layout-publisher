"""Scan and export tests that use a DXF built in memory. No dwg2dxf required."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import ezdxf
import pytest

from converter import (
    Dwg2dxfNotFoundError,
    DwgConversionError,
    DxfLoadError,
    NoLayoutsError,
    find_dwg2dxf,
    format_layout_line,
    list_layouts,
    load_document,
    publish,
)


def _write_sample(path: Path) -> None:
    doc = ezdxf.new()
    doc.modelspace().add_line((0, 0), (100, 0))

    # ezdxf always keeps at least one paperspace, so create the sheets first.
    blad1 = doc.layouts.new("Blad1")
    blad2 = doc.layouts.new("Blad2")
    doc.layouts.delete("Layout1")

    # page_setup resets viewports, so draw after the sheet size is set.
    blad1.page_setup(size=(420, 297), margins=(10, 10, 10, 10), units="mm")
    blad1.add_line((20, 20), (200, 80))
    blad1.add_viewport(
        center=(210, 150),
        size=(120, 80),
        view_center_point=(50, 0),
        view_height=120,
    )

    blad2.page_setup(size=(297, 210), margins=(5, 5, 5, 5), units="mm")
    blad2.add_circle((100, 100), radius=30)
    doc.saveas(path)


def test_list_layouts_skips_model(tmp_path: Path) -> None:
    source = tmp_path / "tekening.dxf"
    _write_sample(source)
    doc = load_document(source)

    layouts = list_layouts(doc)
    assert [item.name for item in layouts] == ["Blad1", "Blad2"]
    assert all(not item.is_model for item in layouts)
    assert layouts[0].paper_width_mm == 420
    assert layouts[0].paper_height_mm == 297
    assert layouts[0].entity_count >= 1
    assert "paperspace" in format_layout_line(layouts[0])


def test_list_layouts_can_include_model(tmp_path: Path) -> None:
    source = tmp_path / "tekening.dxf"
    _write_sample(source)
    names = [item.name for item in list_layouts(load_document(source), include_model=True)]
    assert names[0] == "Model"
    assert "Blad1" in names and "Blad2" in names


def test_publish_pdf_and_dxf(tmp_path: Path) -> None:
    source = tmp_path / "tekening.dxf"
    _write_sample(source)
    output = tmp_path / "out"

    written = publish(source, output, ["pdf", "dxf"])
    names = sorted(path.name for path in written)
    assert names == [
        "tekening_Blad1.dxf",
        "tekening_Blad1.pdf",
        "tekening_Blad2.dxf",
        "tekening_Blad2.pdf",
    ]
    for path in written:
        assert path.stat().st_size > 0
    pdf = output / "tekening_Blad1.pdf"
    assert pdf.read_bytes().startswith(b"%PDF")
    exported = ezdxf.readfile(output / "tekening_Blad1.dxf")
    assert sum(1 for _ in exported.modelspace()) > 0


def test_publish_selected_layout(tmp_path: Path) -> None:
    source = tmp_path / "tekening.dxf"
    _write_sample(source)
    written = publish(source, tmp_path / "out", ["pdf"], ["Blad2"])
    assert [path.name for path in written] == ["tekening_Blad2.pdf"]


def test_unknown_layout(tmp_path: Path) -> None:
    source = tmp_path / "tekening.dxf"
    _write_sample(source)
    with pytest.raises(NoLayoutsError, match="Onbekende layout"):
        publish(source, tmp_path / "out", ["pdf"], ["BestaatNiet"])


def test_no_paperspace_layouts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A valid DXF always contains at least one paperspace. The empty result is
    # the branch publish uses when filtering leaves nothing to export.
    source = tmp_path / "leeg.dxf"
    _write_sample(source)
    monkeypatch.setattr("converter.list_layouts", lambda *_args, **_kwargs: [])
    with pytest.raises(NoLayoutsError, match="PaperSpace"):
        publish(source, tmp_path / "out", ["pdf"])


def test_missing_and_invalid_files(tmp_path: Path) -> None:
    with pytest.raises(DxfLoadError, match="niet gevonden"):
        load_document(tmp_path / "afwezig.dxf")
    bad = tmp_path / "kapot.dxf"
    bad.write_text("dit is geen dxf", encoding="utf-8")
    with pytest.raises(DxfLoadError):
        load_document(bad)
    other = tmp_path / "plaatje.png"
    other.write_bytes(b"png")
    with pytest.raises(DxfLoadError, match="bestandstype"):
        load_document(other)


def test_find_dwg2dxf_uses_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tool = tmp_path / "dwg2dxf"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("DWG2DXF", str(tool))
    assert find_dwg2dxf() == tool


def test_find_dwg2dxf_missing_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DWG2DXF", str(tmp_path / "geen-dwg2dxf"))
    with pytest.raises(Dwg2dxfNotFoundError, match="DWG2DXF"):
        find_dwg2dxf()


@pytest.mark.skipif(os.name != "posix", reason="stub dwg2dxf is a shell script")
def test_dwg_conversion_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = tmp_path / "dwg2dxf"
    stub.write_text("#!/bin/sh\necho 'corrupt section' >&2\nexit 1\n", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("DWG2DXF", str(stub))
    dwg = tmp_path / "kapot.dwg"
    dwg.write_bytes(b"AC1027")
    with pytest.raises(DwgConversionError, match="corrupt"):
        load_document(dwg)


@pytest.mark.skipif(os.name != "posix", reason="stub dwg2dxf is a shell script")
def test_dwg_conversion_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sample = tmp_path / "bron.dxf"
    _write_sample(sample)
    stub = tmp_path / "dwg2dxf"
    stub.write_text(
        "#!/bin/sh\n"
        "out=''\n"
        "while [ $# -gt 0 ]; do\n"
        "  if [ \"$1\" = \"-o\" ]; then out=\"$2\"; shift 2; else shift; fi\n"
        "done\n"
        f"cp {sample} \"$out\"\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("DWG2DXF", str(stub))
    dwg = tmp_path / "tekening.dwg"
    dwg.write_bytes(b"AC1027")

    layouts = list_layouts(load_document(dwg))
    assert [item.name for item in layouts] == ["Blad1", "Blad2"]
