"""Digital-chain section of the first-level workbench (S2-T09).

Shows digital logic tracks (ADC clock, line sync, keep window), code waveforms, one cursor shared
by every track, sample lineage with its four timestamps and queues, queue occupancy and a loss
track. Everything the reader must see is rendered into the HTML by Python so it can be tested
without a browser; the script only draws the canvases and moves the single cursor. No network
request is made. A track with no recorded data says so instead of drawing a guess.
"""

from __future__ import annotations

import html
import json
from typing import Any

ORIGIN_LABEL = "仿真数据（SIMULATED）"

CSS = """
.dig{min-width:0}.dig canvas{width:100%;max-width:100%;height:90px}
.dig table{table-layout:fixed}.dig h3{font-size:13px;margin:10px 0 4px;color:#52606d}
.loss{background:#fde8e6}.dig .cursor-box{position:sticky;top:0;background:#fff;padding:4px 0}
"""

JS = """
(function(){
var el=document.getElementById('digital-data');if(!el)return;
var d=JSON.parse(el.textContent),slider=document.getElementById('dig-cursor'),
    out=document.getElementById('dig-readout');
function lineAt(i){for(var k=0;k<d.lines.length;k++){var l=d.lines[k];
  if(i>=l.start_index&&i<l.start_index+l.count)return l}return null}
function move(){
  var i=parseInt(slider.value,10),l=lineAt(i),t=d.times[i-d.first_index];
  out.textContent='样本 '+i+(t===undefined?' · 未记录':' · 采样时刻 '+t+' ps')+
    (l?' · 行 '+l.line+' 窗内':' · 窗外（数字窗口已舍弃）');
  draw(i)}
function draw(cur){
  d.tracks.forEach(function(tr){
    var c=document.getElementById('trk-'+tr.id);if(!c)return;var g=c.getContext('2d'),w=c.width,h=c.height;
    g.clearRect(0,0,w,h);if(!tr.values.length){g.fillText('未记录',6,14);return}
    var n=d.times.length,mn=Math.min.apply(null,tr.values),mx=Math.max.apply(null,tr.values),r=(mx-mn)||1;
    g.strokeStyle='#2a6fb0';g.beginPath();
    for(var k=0;k<tr.values.length;k++){var x=k/(n-1||1)*w,y=h-4-(tr.values[k]-mn)/r*(h-8);
      if(tr.kind==='logic'&&k>0){g.lineTo(x,h-4-(tr.values[k-1]-mn)/r*(h-8))}
      k?g.lineTo(x,y):g.moveTo(x,y)}
    g.stroke();var cx=(cur-d.first_index)/(n-1||1)*w;g.strokeStyle='#c0392b';
    g.beginPath();g.moveTo(cx,0);g.lineTo(cx,h);g.stroke()})}
slider.addEventListener('input',move);move();
})();
"""


def _table(head: list[str], rows: list[list[str]], cls: str = "") -> str:
    th = "".join(f"<th>{html.escape(h)}</th>" for h in head)
    body = "".join(
        f'<tr class="{cls}">' + "".join(f"<td>{html.escape(str(x))}</td>" for x in r) + "</tr>"
        for r in rows
    )
    return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"


def build_digital_section(run: dict[str, Any]) -> str:
    """``run`` keys: first_index, times, lines, tracks, lineage, queues, losses, evidence."""
    lineage_rows = [
        [s["sample_index"], s["channel"], s["sample_time"], s["adc_output_time"],
         s["pin_arrival_time"], s["capture_time"], " → ".join(s.get("queue_path", [])) or "未记录"]
        for s in run.get("lineage", [])
    ]  # fmt: skip
    lineage = (
        _table(
            ["样本", "通道", "采样时刻 ps", "ADC 输出 ps", "引脚到达 ps", "捕获 ps", "经过的队列"],
            lineage_rows,
        )  # fmt: skip
        if lineage_rows
        else '<p class="nodata">未记录样本谱系。</p>'
    )
    queue_rows = [
        [q["name"], q.get("capacity", "未知"), q["peak"], q.get("overflow_count", 0)]
        for q in run.get("queues", [])
    ]
    queues = (
        _table(["队列", "容量（B）", "峰值占用（B）", "溢出次数"], queue_rows)
        if queue_rows
        else '<p class="nodata">未记录队列占用。</p>'
    )
    loss_rows = [
        [x["layer"], x["reason"], x["first"], x["last"], x.get("time_ps", "未知")]
        for x in run.get("losses", [])
    ]
    losses = (
        _table(["层级", "原因", "首个样本", "末个样本", "发生时刻 ps"], loss_rows, "loss")
        if loss_rows
        else '<p class="nodata">丢样轨道：本运行没有记录到丢失。</p>'
    )
    tracks = "".join(
        f"<h3>{html.escape(t['name'])}</h3>"
        + (
            f'<canvas id="trk-{t["id"]}" width="1000" height="90"></canvas>'
            if t["values"]
            else '<p class="nodata">未记录</p>'
        )
        for t in run.get("tracks", [])
    )
    evidence = html.escape(str(run.get("evidence", "未标注")))
    first = int(run.get("first_index", 0))
    n = len(run.get("times", []))
    blob = json.dumps(
        {k: run.get(k, []) for k in ("times", "lines", "tracks")} | {"first_index": first},
        ensure_ascii=False,
    ).replace("<", "\\u003c")
    return (
        f'<section class="dig" id="digital"><h2>数字链</h2><p class="note">{ORIGIN_LABEL} · '
        f"证据：{evidence}</p>"
        '<div class="cursor-box cursor"><label for="dig-cursor">游标（样本序号）</label>'
        f'<input type="range" id="dig-cursor" min="{first}" max="{first + max(n - 1, 0)}" '
        f'value="{first}"{"" if n else " disabled"}><span id="dig-readout"></span></div>'
        f"{tracks}<h3>样本谱系</h3>{lineage}<h3>各级队列</h3>{queues}"
        f"<h3>丢样轨道</h3>{losses}"
        f'<script type="application/json" id="digital-data">{blob}</script>'
        f"<script>{JS}</script></section>"
    )
