"""Command-line and desktop entry point for publishing DWG/DXF layouts.

Without arguments a small window opens. With arguments the tool stays a CLI:

    python main.py scan tekening.dwg
    python main.py export tekening.dwg --format pdf --output ./out
    python main.py export tekening.dxf --format dxf --format pdf --layouts "Blad1,Blad2"
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from converter import (
    Dwg2dxfNotFoundError,
    DwgConversionError,
    DxfLoadError,
    LayoutInfo,
    NoLayoutsError,
    format_layout_line,
    list_layouts,
    load_document,
    publish,
)

USER_ERRORS = (
    Dwg2dxfNotFoundError,
    DwgConversionError,
    DxfLoadError,
    NoLayoutsError,
    ValueError,
)
logger = logging.getLogger("dwg_layout_publisher")


def configure_logging() -> None:
    """Send logs, including exception tracebacks, to the terminal."""
    root = logging.getLogger()
    if root.handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )


def log_failure(exc: BaseException) -> None:
    """Write the full traceback of a user-facing error to the log."""
    configure_logging()
    logger.error("%s", exc, exc_info=exc)


def build_parser() -> argparse.ArgumentParser:
    """Build the scan/export argument parser."""
    parser = argparse.ArgumentParser(
        prog="dwg-layout-publisher",
        description=(
            "Lees PaperSpace-layouts uit een DWG- of DXF-bestand en publiceer "
            "elke layout naar een los PDF- of DXF-bestand."
        ),
    )
    sub = parser.add_subparsers(dest="command")

    scan = sub.add_parser("scan", help="Toon de layouts in een DWG- of DXF-bestand.")
    scan.add_argument("input", type=Path, help="Pad naar een .dwg- of .dxf-bestand.")
    scan.add_argument(
        "--include-model",
        action="store_true",
        help="Neem modelspace op in de lijst.",
    )

    export = sub.add_parser("export", help="Exporteer layouts naar losse bestanden.")
    export.add_argument("input", type=Path, help="Pad naar een .dwg- of .dxf-bestand.")
    export.add_argument(
        "--format",
        action="append",
        choices=("pdf", "dxf"),
        dest="formats",
        required=True,
        help="Uitvoerformaat. Herhaal deze optie om pdf en dxf allebei te schrijven.",
    )
    export.add_argument(
        "--output",
        "-o",
        type=Path,
        required=True,
        help="Map waarin de bestanden worden geschreven.",
    )
    export.add_argument(
        "--layouts",
        default="",
        help='Kommagescheiden layoutnamen, bijvoorbeeld "Blad1,Blad2". Leeg = alles.',
    )
    export.add_argument(
        "--include-model",
        action="store_true",
        help="Exporteer modelspace ook.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI, or the window when no arguments are given."""
    configure_logging()
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list:
        return run_gui()

    parser = build_parser()
    args = parser.parse_args(args_list)
    if not args.command:
        parser.print_help()
        return 2
    try:
        if args.command == "scan":
            return cmd_scan(args.input, include_model=args.include_model)
        return cmd_export(
            args.input,
            args.output,
            args.formats,
            args.layouts,
            include_model=args.include_model,
        )
    except USER_ERRORS as exc:
        log_failure(exc)
        print(str(exc), file=sys.stderr)
        return 1


def cmd_scan(path: Path, *, include_model: bool = False) -> int:
    """Print every matching layout and return a process status code."""
    doc = load_document(path)
    layouts = list_layouts(doc, include_model=include_model)
    if not layouts:
        raise NoLayoutsError(
            "Geen PaperSpace-layouts gevonden. "
            "Gebruik --include-model om modelspace mee te nemen."
        )
    for info in layouts:
        print(format_layout_line(info))
    return 0


def cmd_export(
    path: Path,
    output: Path,
    formats: list[str],
    layouts: str,
    *,
    include_model: bool = False,
) -> int:
    """Export layouts and print each written path."""
    names = [part.strip() for part in layouts.split(",")] if layouts.strip() else None
    written = publish(
        path,
        output,
        formats,
        names,
        include_model=include_model,
    )
    for item in written:
        print(item)
    return 0


def run_gui() -> int:
    """Open the file dialog. Falls back to a CLI hint when Tkinter is missing."""
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError:
        print(
            "Tkinter ontbreekt. Gebruik: python main.py scan|export ...",
            file=sys.stderr,
        )
        return 1

    class PublisherApp(tk.Tk):
        def __init__(self) -> None:
            super().__init__()
            self.title("DWG Layout Publisher")
            self.minsize(640, 420)
            self._layouts: list[LayoutInfo] = []
            self._build(tk, filedialog, messagebox, ttk)

        def _build(self, tk, filedialog, messagebox, ttk) -> None:
            self._filedialog = filedialog
            self._messagebox = messagebox
            padding = {"padx": 8, "pady": 4}
            frame = ttk.Frame(self, padding=12)
            frame.pack(fill="both", expand=True)
            frame.columnconfigure(1, weight=1)

            self.input_var = tk.StringVar()
            self.output_var = tk.StringVar()
            self.pdf_var = tk.BooleanVar(value=True)
            self.dxf_var = tk.BooleanVar(value=False)
            self.model_var = tk.BooleanVar(value=False)
            self.status_var = tk.StringVar(value="Kies een DWG- of DXF-bestand.")

            ttk.Label(frame, text="Bestand").grid(row=0, column=0, sticky="w", **padding)
            ttk.Entry(frame, textvariable=self.input_var).grid(
                row=0, column=1, sticky="ew", **padding
            )
            ttk.Button(frame, text="Bladeren", command=self._browse_input).grid(
                row=0, column=2, **padding
            )

            ttk.Label(frame, text="Uitvoermap").grid(row=1, column=0, sticky="w", **padding)
            ttk.Entry(frame, textvariable=self.output_var).grid(
                row=1, column=1, sticky="ew", **padding
            )
            ttk.Button(frame, text="Bladeren", command=self._browse_output).grid(
                row=1, column=2, **padding
            )

            options = ttk.Frame(frame)
            options.grid(row=2, column=0, columnspan=3, sticky="w", **padding)
            ttk.Checkbutton(options, text="PDF", variable=self.pdf_var).pack(side="left")
            ttk.Checkbutton(options, text="DXF", variable=self.dxf_var).pack(
                side="left", padx=(12, 0)
            )
            ttk.Checkbutton(
                options,
                text="Inclusief modelspace",
                variable=self.model_var,
            ).pack(side="left", padx=(12, 0))

            buttons = ttk.Frame(frame)
            buttons.grid(row=3, column=0, columnspan=3, sticky="w", **padding)
            ttk.Button(buttons, text="Layouts zoeken", command=self._scan).pack(side="left")
            ttk.Button(buttons, text="Exporteren", command=self._export).pack(
                side="left", padx=(8, 0)
            )

            self.layout_list = tk.Listbox(frame, selectmode="extended", height=12)
            self.layout_list.grid(row=4, column=0, columnspan=3, sticky="nsew", **padding)
            frame.rowconfigure(4, weight=1)

            ttk.Label(frame, textvariable=self.status_var, wraplength=600).grid(
                row=5, column=0, columnspan=3, sticky="w", **padding
            )

        def _browse_input(self) -> None:
            chosen = self._filedialog.askopenfilename(
                title="DWG of DXF kiezen",
                filetypes=[
                    ("CAD-bestanden", "*.dwg *.dxf"),
                    ("DWG", "*.dwg"),
                    ("DXF", "*.dxf"),
                    ("Alle bestanden", "*.*"),
                ],
            )
            if chosen:
                self.input_var.set(chosen)
                source = Path(chosen)
                if not self.output_var.get():
                    self.output_var.set(str(source.parent / f"{source.stem}_layouts"))

        def _browse_output(self) -> None:
            chosen = self._filedialog.askdirectory(title="Uitvoermap kiezen")
            if chosen:
                self.output_var.set(chosen)

        def _scan(self) -> None:
            path = self.input_var.get().strip()
            if not path:
                self._fail("Kies eerst een bestand.")
                return
            try:
                doc = load_document(path)
                self._layouts = list_layouts(doc, include_model=self.model_var.get())
            except USER_ERRORS as exc:
                log_failure(exc)
                self._layouts = []
                self._fill_list()
                self._fail(str(exc))
                return
            if not self._layouts:
                self._fill_list()
                self._fail("Geen PaperSpace-layouts gevonden.")
                return
            self._fill_list(select_all=True)
            self.status_var.set(f"{len(self._layouts)} layout(s) gevonden.")

        def _fill_list(self, *, select_all: bool = False) -> None:
            self.layout_list.delete(0, "end")
            for info in self._layouts:
                self.layout_list.insert("end", format_layout_line(info))
            if select_all:
                self.layout_list.selection_set(0, "end")

        def _selected_names(self) -> list[str] | None:
            indices = self.layout_list.curselection()
            if not indices:
                return None
            return [self._layouts[index].name for index in indices]

        def _formats(self) -> list[str]:
            formats: list[str] = []
            if self.pdf_var.get():
                formats.append("pdf")
            if self.dxf_var.get():
                formats.append("dxf")
            return formats

        def _export(self) -> None:
            path = self.input_var.get().strip()
            output = self.output_var.get().strip()
            formats = self._formats()
            if not path:
                self._fail("Kies eerst een bestand.")
                return
            if not output:
                self._fail("Kies een uitvoermap.")
                return
            if not formats:
                self._fail("Kies minstens PDF of DXF.")
                return
            names = self._selected_names()
            include_model = self.model_var.get() or bool(names and "Model" in names)
            self.status_var.set("Bezig met exporteren...")
            self.update_idletasks()
            try:
                written = publish(
                    path,
                    output,
                    formats,
                    names,
                    include_model=include_model,
                )
            except USER_ERRORS as exc:
                log_failure(exc)
                self._fail(str(exc))
                return
            self.status_var.set(f"Klaar: {len(written)} bestand(en) in {output}.")
            self._messagebox.showinfo(
                "Export klaar",
                f"{len(written)} bestand(en) geschreven naar:\n{output}",
            )

        def _fail(self, message: str) -> None:
            self.status_var.set(message)
            self._messagebox.showerror("DWG Layout Publisher", message)

    PublisherApp().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
