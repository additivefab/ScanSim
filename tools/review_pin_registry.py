"""Re-derive the SP2 connector tables from the schematic and compare them with the registry.

Reads page 10 (connector tables, by word position) and page 12 (constraints reference) of
legacy/originals/SmartZynq_SP2_Schematic_V1d3B_20260413.pdf.pdf with ``pdftotext`` and writes
docs/reports/pin_registry_review_automatic.md. The registry is never edited: disagreements are
reported. The hand-checked part (power and GND rows that text position cannot settle) is written
by a person in docs/reports/pin_registry_review.md, which includes this output's numbers.
"""

import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

from zlscan.hardware import schematic_pins as sp
from zlscan.hardware.pins import PinRegistry

ROOT = Path(__file__).resolve().parents[1]
PDF = ROOT / "legacy/originals/SmartZynq_SP2_Schematic_V1d3B_20260413.pdf.pdf"
REPORT = ROOT / "docs/reports/pin_registry_review_automatic.md"
STATUSES = ("confirmed", "mismatch", "gnd_symbol_no_text", "power_name_not_aligned", "unexplained")


def pdftotext(args: list[str], page: int, out: Path) -> str:
    cmd = ["pdftotext", *args, "-f", str(page), "-l", str(page), str(PDF), str(out)]
    subprocess.run(cmd, check=True, capture_output=True)
    return out.read_text(encoding="utf-8")


def table_section(rows: list[sp.RowResult], count: Counter) -> list[str]:
    lines = [
        "| 表 | 一致 | 不一致 | GND（符号无文字） | 电源名未与引脚对齐 | 无法解释 |",
        "|---|---|---|---|---|---|",
    ]
    for label in sp.REGIONS:
        cells = " | ".join(str(count[(label, s)]) for s in STATUSES)
        lines.append(f"| {label} | {cells} |")
    lines += ["", f"合计 {len(rows)} 行。", ""]
    for status in STATUSES[1:]:
        hit = [r for r in rows if r.status.value == status]
        if not hit:
            continue
        lines += [f"### {status}", "", "| 表 | 物理脚 | 登记表 | 页面文字 |", "|---|---|---|---|"]
        for r in hit:
            role = f"{r.registry_role} {r.registry_package_pin}".strip()
            text = r.page_text or "（无）"
            lines.append(f"| {r.revision} {r.connector} | {r.physical_pin} | {role} | {text} |")
        lines.append("")
    return lines


def main() -> int:
    registry = PinRegistry.load()
    with tempfile.TemporaryDirectory() as tmp:
        html = pdftotext(["-bbox"], 10, Path(tmp) / "p10.html")
        xdc_text = pdftotext(["-layout"], 12, Path(tmp) / "p12.txt")
    words = sp.parse_bbox_words(html)
    rows: list[sp.RowResult] = []
    for region in sp.REGIONS.values():
        reading = sp.read_table(words, region)
        rows += sp.compare_table(registry.table(region.revision, region.connector), reading)
    xdc = sp.compare_xdc(registry, sp.parse_xdc(xdc_text))
    count = Counter((f"{r.revision} {r.connector}", r.status.value) for r in rows)
    xc = Counter(r.status.value for r in xdc)

    lines = [
        "# 引脚表自动复核结果（S0-T06）",
        "",
        "对象：`src/zlscan/hardware/pin_registry.json`（与审查包的表逐字节相同，120 行）。",
        "方法：`pdftotext -bbox` 取第 10 页词坐标，按位置把引脚号与网络名配对"
        "（独立于审查包的做法）；再用第 12 页约束参考核对逻辑下标与封装引脚。",
        "登记表不被修改，不一致处只在此列出。",
        "",
        "## 第 10 页：物理引脚 → 网络名",
        "",
        *table_section(rows, count),
        "## 第 12 页：`J5[n]`、`J6[n]` → 封装引脚",
        "",
        f"检查登记表的 {len(xdc)} 个信号行：一致 {xc['confirmed']}，不一致 {xc['mismatch']}，"
        f"XDC 中找不到 {xc['not_in_xdc']}。",
        "",
    ]
    for r in xdc:
        if r.status is not sp.XdcStatus.CONFIRMED:
            lines.append(
                f"- {r.revision} {r.connector}[{r.logical_index}]: {r.status.value} {r.detail}"
            )
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("page 10:", dict(Counter(r.status.value for r in rows)), "| page 12:", dict(xc))
    print(f"wrote {REPORT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
