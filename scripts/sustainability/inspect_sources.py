"""Inspect acquired references without importing models or production modules."""

from collections import Counter
import csv
import hashlib
from html import unescape
import json
import math
import os
from pathlib import Path
import re
import resource
import time
import xml.etree.ElementTree as ET
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "reference-downloads-2026-11-03"
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def xlsx_rows(path, sheet_name):
    with zipfile.ZipFile(path) as archive:
        if sum(m.file_size for m in archive.infolist()) > 256 * 1024 ** 2:
            raise ValueError("Uncompressed XLSX budget exceeded")
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            with archive.open("xl/sharedStrings.xml") as stream:
                for _, element in ET.iterparse(stream, events=("end",)):
                    if element.tag == "{" + NS["s"] + "}si":
                        shared.append("".join(element.itertext()))
                        element.clear()
        targets = {r.attrib["Id"]: r.attrib["Target"] for r in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))}
        sheets = ET.fromstring(archive.read("xl/workbook.xml")).findall("s:sheets/s:sheet", NS)
        sheet = next(s for s in sheets if s.get("name") == sheet_name)
        target = targets[sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]]
        member = target.lstrip("/") if target.startswith("/") else "xl/" + target
        with archive.open(member) as stream:
            for _, row in ET.iterparse(stream, events=("end",)):
                if row.tag != "{" + NS["s"] + "}row":
                    continue
                values = {}
                for cell in row.findall("s:c", NS):
                    value = cell.findtext("s:v", default="", namespaces=NS)
                    if cell.get("t") == "s" and value:
                        value = shared[int(value)]
                    elif cell.get("t") == "inlineStr":
                        value = "".join(cell.find("s:is", NS).itertext())
                    values[re.sub(r"\d+", "", cell.get("r"))] = value
                yield int(row.get("r")), values
                row.clear()


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def main():
    start = time.monotonic()
    os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])
    resource.setrlimit(resource.RLIMIT_CPU, (900, 900))
    output = ROOT / "results" / "source-inspection.json"
    if output.exists():
        raise FileExistsError(output)
    result = {}
    with (DATA / "agribalyse-summary.csv").open(encoding="utf-8-sig", newline="") as stream:
        sample = stream.read(16384)
        stream.seek(0)
        reader = csv.DictReader(stream, dialect=csv.Sniffer().sniff(sample, delimiters=",;\t"))
        rows = list(reader)
        result["agribalyse_csv"] = {"rows": len(rows), "distinct_agb": len({r["Code AGB"] for r in rows}),
            "distinct_ciqual": len({r["Code CIQUAL"] for r in rows}), "columns": reader.fieldnames,
            "preparation": dict(Counter(r["Préparation"] for r in rows)),
            "examples": [{k: r[k] for k in ("Code AGB", "Code CIQUAL", "LCI Name", "Préparation", "Changement climatique")} for r in rows[:5]]}
        del rows
    rows = [r for _, r in xlsx_rows(DATA / "agribalyse-3.2-food-products.xlsx", "Synthese")
            if number(r.get("A")) is not None and number(r.get("N")) is not None]
    agb_codes = {r["A"] for r in rows}
    result["agribalyse_workbook"] = {"product_rows": len(rows), "distinct_agb": len(agb_codes),
        "distinct_ciqual": len({r.get("B") for r in rows}),
        "missing_english_names": sum(not r.get("F") for r in rows),
        "preparation": dict(Counter(r.get("K") for r in rows)),
        "climate_min_max": [min(float(r["N"]) for r in rows), max(float(r["N"]) for r in rows)],
        "dqr_min_max": [min(float(r["L"]) for r in rows), max(float(r["L"]) for r in rows)]}
    for sheet in ("SEL CF for users", "SEL WF for users"):
        rows = [r for _, r in xlsx_rows(DATA / "su-eatable-life-v2.xlsx", sheet)
                if r.get("B") and number(r.get("C")) is not None]
        result[sheet] = {"item_rows_with_numeric_value": len(rows), "distinct_items": len({r["B"] for r in rows}),
            "uncertainty": dict(Counter(r.get("D") for r in rows)),
            "recommended_level": dict(Counter(r.get("E") for r in rows)),
            "unit_warning": "Workbook uses per kg OR litre; no automatic conversion to per kg performed.",
            "examples": [{k: r.get(k) for k in "BCDEF"} for r in rows[:4]]}
    for label, name in (("foodex2_agb", "Matching_FoodEx2v15.2_Agribalyse_v3.2.xlsx"),
                        ("foodex2_pn", "Matching_FoodEx2v15.2_PN2018.xlsx"),
                        ("foodex2_ecoinvent", "Matching_FoodEx2v15.2_ecoinvent3.10.xlsx")):
        raw = list(xlsx_rows(DATA / name, "Matching"))
        headers = raw[0][1]
        rows = [{headers[k]: value for k, value in row.items() if k in headers} for _, row in raw[1:] if row.get("C")]
        info = {"rows": len(rows), "distinct_foodex_codes": len({r.get("L7_FoodEx2_code") for r in rows}),
            "columns": list(headers.values()),
            "match_found": dict(Counter(r.get("match_found") for r in rows)),
            "proxy": dict(Counter(r.get("proxy") for r in rows))}
        if label == "foodex2_agb":
            codes = {r["agb_code"] for r in rows if r.get("agb_code")}
            info.update(distinct_target_codes=len(codes), target_codes_in_downloaded_workbook=len(codes & agb_codes),
                        sample_target_codes_not_in_workbook=sorted(codes-agb_codes)[:12])
        result[label] = info
    html = (DATA / "concito_page.html").read_text()
    assert "https://creativecommons.org/licenses/by/4.0/" in html
    records = []
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", html, re.S):
        cells = [unescape(re.sub(r"<[^>]+>", " ", value)).strip()
                 for value in re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S)]
        if not cells:
            continue
        assert len(cells) == 11, "CONCITO table schema changed"
        assert all(number(v) is not None for v in cells[3:10]), "Non-numeric CONCITO factors"
        records.append(cells)
    assert records, "No CONCITO data found"
    derived = ROOT / "data" / "concito-v1.2-public-table.csv"
    with derived.open("x", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["product", "market", "category", "total_kg_co2e_per_kg", "agriculture", "iluc",
                         "processing", "packaging", "transport", "retail", "source_id"])
        writer.writerows(records)
    result["concito"] = {"rows": len(records), "distinct_product_codes": len({r[-1].rsplit("-", 1)[0] for r in records}),
        "markets": dict(Counter(r[1] for r in records)), "license_link_on_source": "CC-BY-4.0",
        "derived_path": str(derived.relative_to(ROOT)), "sha256": hashlib.sha256(derived.read_bytes()).hexdigest(),
        "source": "data/reference-downloads-2026-11-03/concito_page.html",
        "note": "Derived public HTML table, not an official workbook; three-decimal displayed factors retained."}
    result["checks"] = {"elapsed_seconds": round(time.monotonic()-start, 3),
                        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({key: {k: v for k, v in value.items() if k in {
        "rows", "product_rows", "distinct_agb", "item_rows_with_numeric_value", "distinct_product_codes",
        "markets", "distinct_target_codes", "target_codes_in_downloaded_workbook", "elapsed_seconds", "peak_rss_kib"}}
        for key, value in result.items()}, indent=2))


if __name__ == "__main__":
    main()
