"""First-level workbench: one self-contained offline HTML page (docs/dev/08_workbench_spec.md).

This is the skeleton. It shows the component tree, a probe panel per component with each
parameter's value, unit and evidence, the five separate kinds of "pass", the recorded events and
a single cursor. It draws no physics: there is no trace data yet, and the waveform area says so.

Everything the reader must see (unknown values, evidence labels, the "simulated truth" label) is
rendered into the HTML by Python, so it can be tested without a browser; the script only toggles
panels and moves the cursor. The page makes no network request and reads no clock.
"""

from __future__ import annotations

import html
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zlscan.contracts.events import Event, Visibility
from zlscan.contracts.param import Evidence, Param
from zlscan.evidence.table import load_names, load_parameters

UNKNOWN = "未知"
EVIDENCE_LABEL = {
    Evidence.PROJECT_TEXT: "项目文本",
    Evidence.PHOTO: "图片观察",
    Evidence.SCHEMATIC: "原理图标称",
    Evidence.DATASHEET: "手册",
    Evidence.DERIVED: "推导",
    Evidence.ASSUMED: "假设",
    Evidence.MEASURED: "实测",
}
PASS_KINDS = ("文档一致性", "行为模型", "RTL 功能协同", "综合与时序", "实机验证")
ORACLE_LABEL = "仿真真值，实机不可见"


@dataclass(frozen=True)
class Component:
    id: str
    name: str
    group: str
    prefixes: tuple[str, ...]


COMPONENTS: tuple[Component, ...] = (
    Component("laser", "激光源", "光学链", ("laser.",)),
    Component("scan", "转镜与扫描光学", "光学链", ("scan.", "feed.")),
    Component("optics", "光斑与景深", "光学链", ("optics.",)),
    Component("film", "底片与产品要求", "光学链", ("product.", "calibration.")),
    Component("collect", "集光", "光学链", ("collect.",)),
    Component("pmt", "PMT", "探测与模拟链", ("pmt.",)),
    Component("afe", "模拟前端", "探测与模拟链", ("afe.",)),
    Component("adc", "ADC", "探测与模拟链", ("adc.",)),
    Component("board", "开发板", "数字链与硬件", ("board.",)),
    Component("transport", "网口与传输", "数字链与硬件", ("transport.",)),
    Component("control", "控制与可观测性", "数字链与硬件", ("control.", "hardware.")),
    Component("observation", "示波器与测点", "观测", ("observation.",)),
    Component("benchmark", "性能测试算例", "观测", ("benchmark.",)),
    Component("image", "重建图像", "结果", ()),
    Component("events", "事件", "结果", ()),
)

CSS = """
*{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui,sans-serif;color:#1d2733;background:#f4f6f8}
header{padding:10px 14px;background:#fff;border-bottom:1px solid #d5dbe1}
h1{font-size:16px;margin:0 0 4px}.meta span{display:inline-block;margin:0 12px 2px 0}
.statuses span{display:inline-block;margin:2px 8px 2px 0;padding:1px 8px;border:1px solid #b9c2cc;border-radius:10px}
main{display:grid;grid-template-columns:200px minmax(0,1fr) minmax(0,320px);gap:10px;padding:10px}
@media (max-width:900px){main{grid-template-columns:minmax(0,1fr)}}
nav,section,aside{background:#fff;border:1px solid #d5dbe1;border-radius:6px;padding:10px;min-width:0}
nav h2,aside h2,section h2{font-size:13px;margin:0 0 6px;color:#52606d}
nav button{display:block;width:100%;text-align:left;margin:2px 0;padding:6px 8px;border:1px solid transparent;
 background:none;border-radius:4px;font:inherit;cursor:pointer}
nav button[aria-pressed=true]{background:#e6f0fb;border-color:#9ec1ea}
nav .group{margin:8px 0 2px;font-size:12px;color:#7b8794}
table{width:100%;border-collapse:collapse;table-layout:fixed}
td,th{border-bottom:1px solid #e4e8ec;padding:4px 6px;text-align:left;vertical-align:top;overflow-wrap:anywhere}
td.unknown{color:#8a4b08;font-weight:600}.oracle{color:#a12622;font-weight:600}
.probe{display:none}.probe.on{display:block}canvas{width:100%;height:120px;background:#fafbfc;border:1px solid #d5dbe1}
.nodata{color:#7b8794;margin:4px 0 10px}.cursor{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.cursor input{flex:1 1 200px;min-width:0}.note{color:#52606d;font-size:12px}
"""

JS = """
(function(){
var data=JSON.parse(document.getElementById('run-data').textContent);
var buttons=document.querySelectorAll('nav button[data-component]');
function show(id){
  document.querySelectorAll('.probe').forEach(function(p){p.classList.toggle('on',p.id==='probe-'+id)});
  buttons.forEach(function(b){b.setAttribute('aria-pressed',b.dataset.component===id?'true':'false')});
}
buttons.forEach(function(b){b.addEventListener('click',function(){show(b.dataset.component)})});
show(buttons.length?buttons[0].dataset.component:'');
var slider=document.getElementById('time-cursor'),readout=document.getElementById('cursor-readout');
function move(){
  if(!data.events.length){readout.textContent='无数据';return}
  var e=data.events[parseInt(slider.value,10)];
  readout.textContent='事件 '+(parseInt(slider.value,10)+1)+'/'+data.events.length+' · '+e.kind+' · tick '+e.tick+' ps · 波形未记录';
  document.querySelectorAll('#events-table tbody tr').forEach(function(r,i){r.style.background=i==slider.value?'#fff6d6':''});
}
slider.addEventListener('input',move);move();
var c=document.getElementById('wave').getContext('2d');c.strokeStyle='#c4ccd4';c.beginPath();
c.moveTo(0,60);c.lineTo(1000,60);c.stroke();
})();
"""


def _evidence(param: Param | None) -> str:
    return EVIDENCE_LABEL[param.evidence] if param and param.evidence else "—"


def _value_cell(param: Param | None) -> str:
    if param is None or param.value is None:
        return f'<td class="unknown">{UNKNOWN}</td>'
    return f"<td>{html.escape(str(param.value))}</td>"


def _probe_panel(comp: Component, params: Mapping[str, Param], names: Mapping[str, str]) -> str:
    rows = []
    for key in sorted(params):
        if not key.startswith(comp.prefixes) if comp.prefixes else True:
            continue
        p = params[key]
        source = html.escape(" ".join(x for x in (p.source_id, p.locator) if x) or "—")
        rows.append(
            f"<tr><td>{html.escape(names.get(key, key))}</td>{_value_cell(p)}"
            f"<td>{html.escape(p.unit or '—')}</td><td>{_evidence(p)}</td><td>{source}</td></tr>"
        )
    if comp.id == "image":
        body = '<p class="nodata">未记录：本阶段没有重建图像。</p>'
    elif comp.id == "events":
        body = '<p class="note">事件列表见页面下方。</p>'
    elif rows:
        body = (
            "<table><thead><tr><th>参数</th><th>值</th><th>单位</th><th>证据</th><th>来源</th></tr>"
            f"</thead><tbody>{''.join(rows)}</tbody></table>"
        )
    else:
        body = '<p class="nodata">该组件没有登记的参数。</p>'
    return f'<div class="probe" id="probe-{comp.id}"><h2>{html.escape(comp.name)}</h2>{body}</div>'


def _events_table(events: Sequence[Event]) -> str:
    if not events:
        return '<p class="nodata">没有事件。</p>'
    rows = []
    for e in events:
        label = (
            f' <span class="oracle">{ORACLE_LABEL}</span>'
            if e.visibility is Visibility.ORACLE_ONLY
            else ""
        )
        rows.append(
            f"<tr><td>{html.escape(e.kind)}{label}</td><td>{e.origin.value}</td>"
            f"<td>{e.visibility.value}</td><td>{e.derivation.value}</td>"
            f"<td>{e.tick}</td><td>{e.session_id}/{e.epoch}</td></tr>"
        )
    return (
        '<table id="events-table"><thead><tr><th>类型</th><th>来源</th><th>可见性</th><th>得出方式</th>'
        f"<th>tick（ps）</th><th>会话/时期</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _summary(params: Mapping[str, Param]) -> str:
    count = Counter(_evidence(p) if p.value is not None else UNKNOWN for p in params.values())
    order = [UNKNOWN, *EVIDENCE_LABEL.values()]
    return " · ".join(f"{k} {count[k]}" for k in order if count[k])


def build_viewer(
    params: Mapping[str, Param],
    events: Sequence[Event],
    meta: Mapping[str, Any],
    digital: Mapping[str, Any] | None = None,
) -> str:
    from zlscan.report.digital_view import CSS as DIG_CSS
    from zlscan.report.digital_view import build_digital_section

    digital_html = build_digital_section(dict(digital)) if digital else ""
    css = CSS + (DIG_CSS if digital else "")
    names = load_names()
    statuses = meta.get("statuses", {})
    status_html = "".join(
        f"<span>{kind}：{html.escape(str(statuses.get(kind, '未执行')))}</span>"
        for kind in PASS_KINDS
    )
    groups: dict[str, list[Component]] = {}
    for comp in COMPONENTS:
        groups.setdefault(comp.group, []).append(comp)
    nav = "".join(
        f'<div class="group">{html.escape(group)}</div>'
        + "".join(
            f'<button type="button" data-component="{c.id}" aria-pressed="false">{html.escape(c.name)}</button>'
            for c in comps
        )
        for group, comps in groups.items()
    )
    panels = "".join(_probe_panel(c, params, names) for c in COMPONENTS)
    data = {
        "meta": {k: v for k, v in meta.items() if k != "statuses"},
        "events": [
            {"kind": e.kind, "origin": e.origin.value, "visibility": e.visibility.value,
             "derivation": e.derivation.value, "tick": str(e.tick), "epoch": e.epoch}
            for e in events
        ],
    }  # fmt: skip
    blob = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    top = (
        f"<span>模式：{html.escape(str(meta.get('mode', '—')))}</span>"
        f"<span>预设：{html.escape(str(meta.get('preset', '—')))}</span>"
        f"<span>运行：{html.escape(str(meta.get('run_id', '—')))}</span>"
        f"<span>会话/时期：{html.escape(str(meta.get('session_id', '—')))}/{html.escape(str(meta.get('epoch', '—')))}</span>"
        f"<span>参数证据：{_summary(params)}</span>"
    )
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>仿真工作台（第一级骨架）</title><style>{css}</style></head><body>"
        f'<header><h1>仿真工作台（第一级骨架）</h1><div class="meta">{top}</div>'
        f'<div class="statuses">{status_html}</div>'
        '<div class="note">没有任何一种"通过"能代替另一种；这里没有物理波形，只有登记的参数与事件。</div></header>'
        f'<main><nav aria-label="组件树"><h2>组件树</h2>{nav}</nav>'
        '<section><h2>波形</h2><canvas id="wave" width="1000" height="160"></canvas>'
        '<p class="nodata">未记录：本阶段还没有迹线数据，这里不画任何波形。</p>'
        '<div class="cursor"><label for="time-cursor">游标</label>'
        f'<input type="range" id="time-cursor" min="0" max="{max(len(events) - 1, 0)}" value="0" '
        f'step="1"{"" if events else " disabled"}><span id="cursor-readout"></span></div>'
        f"<h2>事件</h2>{_events_table(events)}</section>"
        f"<aside><h2>探针</h2>{panels}</aside></main>{digital_html}"
        f'<script type="application/json" id="run-data">{blob}</script><script>{JS}</script></body></html>'
    )


def build_viewer_from_run_dir(run_dir: Path, out: Path) -> Path:
    """Build the page from ``run.json`` and optional ``events.jsonl`` in a run directory."""
    run_dir = Path(run_dir)
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    events_path = run_dir / "events.jsonl"
    events = (
        [
            Event.model_validate_json(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        if events_path.exists()
        else []
    )
    out = Path(out)
    digital_path = run_dir / "digital.json"
    digital = (
        json.loads(digital_path.read_text(encoding="utf-8")) if digital_path.exists() else None
    )
    out.write_text(build_viewer(load_parameters(), events, meta, digital), encoding="utf-8")
    return out
