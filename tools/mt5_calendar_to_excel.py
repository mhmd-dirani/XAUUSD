#!/usr/bin/env python3
"""Convert MT5 Common/Files/gold_calendar.csv into an Excel .xlsx workbook.

Works on Windows with the user's existing MetaTrader5 library.
The XLSX writer uses Python's standard library only (no openpyxl/pandas).
No broker orders, network calendar calls, or Telegram messages.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path
import re
import zipfile
import time
from xml.sax.saxutils import escape

HEADERS = [
    "ValueID", "EventID", "Event", "Currency", "Importance", "EventType", "ServerTime",
    "UTC_Estimate", "UTC_Status", "OffsetSeconds", "Actual", "Forecast",
    "Previous", "Unit", "TimeMode", "SourceURL", "ExportedUTC",
]
NUMBER_COLUMNS = {"OffsetSeconds", "Actual", "Forecast", "Previous"}


def excel_col(i: int) -> str:
    name = ""
    while i:
        i, d = divmod(i - 1, 26)
        name = chr(65 + d) + name
    return name


def safe_xml(s: str) -> str:
    # XML 1.0 does not permit these control characters.
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    return escape(s)


def cell(col: int, row: int, value: str, header: bool = False) -> str:
    ref = f"{excel_col(col)}{row}"
    style = ' s="1"' if header else ""
    text = str(value or "")
    if not header and HEADERS[col - 1] in NUMBER_COLUMNS and text.strip():
        try:
            x = float(text)
            if x == x and abs(x) != float("inf"):
                return f'<c r="{ref}"{style}><v>{x:.12g}</v></c>'
        except ValueError:
            pass
    return f'<c r="{ref}" t="inlineStr"{style}><is><t xml:space="preserve">{safe_xml(text)}</t></is></c>'


def write_xlsx(destination: Path, rows: list[dict[str, str]]) -> None:
    data = [dict(zip(HEADERS, HEADERS))] + rows
    lines = []
    for n, row in enumerate(data, 1):
        cells = "".join(cell(i, n, row.get(key, ""), n == 1)
                        for i, key in enumerate(HEADERS, 1))
        lines.append(f'<row r="{n}">{cells}</row>')
    widths = [16, 16, 45, 12, 14, 26, 23, 23, 23, 17, 16, 16, 16, 24, 28, 42, 23]
    cols = "".join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>'
                   for i, w in enumerate(widths, 1))
    last_col = excel_col(len(HEADERS))
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        f'<cols>{cols}</cols>'
        f'<sheetData>{"".join(lines)}</sheetData>'
        f'<autoFilter ref="A1:{last_col}{len(data)}"/>'
        '</worksheet>'
    )
    types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '</Types>'
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    wb = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="USD Calendar" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>'
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Aptos"/></font>'
        '<font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Aptos"/></font></fonts>'
        '<fills count="3"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF14324A"/><bgColor indexed="64"/></patternFill></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>'
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for part, content in {
            "[Content_Types].xml": types,
            "_rels/.rels": relationships,
            "xl/workbook.xml": wb,
            "xl/_rels/workbook.xml.rels": wb_rels,
            "xl/styles.xml": styles,
            "xl/worksheets/sheet1.xml": sheet,
        }.items():
            z.writestr(part, content.encode("utf-8"))


def main() -> None:
    p = argparse.ArgumentParser(description="MT5 economic calendar CSV → Excel")
    p.add_argument("--csv", type=Path, help="Optional direct path to exported CSV")
    p.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "data" / "gold_calendar.xlsx")
    p.add_argument("--mt5-path", default=r"C:\Program Files\MetaTrader 5\terminal64.exe")
    p.add_argument("--watch", action="store_true", help="Automatically update Excel whenever MT5 publishes new events")
    p.add_argument("--interval", type=int, default=30, help="Seconds between CSV checks in --watch mode")
    args = p.parse_args()
    import sys
    for stream in (sys.stdout, sys.stderr):      # Windows cp1252 console: never crash on symbols
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if args.interval < 5:
        p.error("--interval must be at least 5 seconds")

    csv_path = args.csv
    if csv_path is None:
        import MetaTrader5 as mt5
        if not mt5.initialize(path=args.mt5_path, timeout=60000):
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        try:
            terminal = mt5.terminal_info()
            if terminal is None:
                raise RuntimeError(f"MT5 terminal_info failed: {mt5.last_error()}")
            csv_path = Path(terminal.commondata_path) / "Files" / "gold_calendar.csv"
        finally:
            mt5.shutdown()
    def update_excel():
        if not csv_path.exists():
            raise FileNotFoundError(f"Calendar CSV not found: {csv_path}\nAttach GoldCalendarBridge.mq5 to an MT5 chart first.")
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter=";")
            missing = [key for key in HEADERS if key != "EventType" and key not in (reader.fieldnames or [])]
            if missing:
                raise ValueError(f"Unexpected CSV format; missing columns: {missing}")
            rows = list(reader)
        if not rows:
            raise ValueError("Calendar contains no events; refusing to overwrite Excel")
        temporary = args.out.with_name(args.out.stem + ".pending.xlsx")
        write_xlsx(temporary, rows)
        temporary.replace(args.out)  # atomic replacement once workbook is complete
        print(f"SUCCESS: {len(rows)} USD events → {args.out}", flush=True)

    if not args.watch:
        update_excel()
    else:
        print(f"Watching {csv_path}; press Ctrl+C to stop", flush=True)
        previous = None
        try:
            while True:
                try:
                    fingerprint = (csv_path.stat().st_mtime_ns, csv_path.stat().st_size)
                    if fingerprint != previous:
                        update_excel()
                        previous = fingerprint
                except (OSError, ValueError) as exc:
                    print(f"Calendar Excel update postponed: {exc}", flush=True)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("Stopped Excel calendar watcher")
    print("NOTE: UTC_Estimate is preliminary; the Gold Monitor validates the UTC offset separately.")

if __name__ == "__main__":
    main()
