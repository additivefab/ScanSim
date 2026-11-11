"""A self-contained offline viewer of ACTUALLY GENERATED data, no CDN or server."""
from __future__ import annotations
import base64, io, json
from pathlib import Path
import numpy as np
from PIL import Image

def make_viewer(r:dict,path:Path)->None:
    c=r['config'];tr=r['trace'][0]
    # All ADC ticks in the first acquisition window are retained, no time downsampling.
    keys=['sample_time_s','source_sample_time_s','x_sample_equiv_px','p_detector_w','pmt_anode_a','v_log_v','v_adc_diff_v','adc_sample_code','adc_bus_code','bus_valid','bus_pixel_col']
    dat={k:np.asarray(tr[k]).tolist() for k in keys}
    im=Image.fromarray(np.uint8(np.rint(np.clip(r['density']/c['density_max'],0,1)*255)))
    b=io.BytesIO();im.save(b,format='PNG');img=base64.b64encode(b.getvalue()).decode()
    html='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>底片扫描仪逐采样时刻观察器</title>
<style>body{font-family:system-ui,sans-serif;margin:0;background:#f4f6f8;color:#172b3a}main{max-width:1100px;margin:auto;padding:28px}h1{font-size:27px}.note{border-left:4px solid #bf8400;padding:12px;background:#fff8e7}section{background:white;padding:20px;margin:18px 0;border-radius:8px}canvas{width:100%;height:220px;border:1px solid #ddd}input[type=range]{width:100%}button{padding:8px 18px;margin-right:8px}table{border-collapse:collapse;width:100%}td,th{text-align:left;border-bottom:1px solid #ddd;padding:8px}code{background:#eee;padding:2px 4px}.flow{line-height:2.2}img{image-rendering:pixelated;max-width:100%;width:768px}small{color:#556}</style>
<main><h1>从一束光到一个像素：逐采样时刻观察器</h1><div class="note">数据来自本资料包的 Python 行为仿真，不是实机测量。下方只逐时刻显示第 0 行采集窗口；空白行周期、预热及标定见 frame_events.csv。光路为功能示意，不代表已复原透镜和反射镜的机械坐标。</div>
<section><h2>1. 信号链与时间</h2><div class="flow">激光 → 准直/整形（待确认） → 多面转镜 → 扫描聚焦系统 → 底片透射 → 集光系统（待确认结构） → PMT → 对数模拟前端 → 滤波/差分驱动 → ADC → FPGA 延迟标签/FIFO → 图像</div><p>蓝本采用单波长、单路灰度采集；PMT 高压模块是供电支路，不在光路中。</p><button id="play">播放</button><button id="prev">前一采样</button><button id="next">后一采样</button><input id="step" type="range" min="0" value="0"><p id="time"></p><canvas id="wave" width="1000" height="220"></canvas></section>
<section><h2>2. 当前采样与已到达数字总线的样本</h2><table><thead><tr><th>量</th><th>值</th><th>说明</th></tr></thead><tbody id="values"></tbody></table><p><strong>注意：</strong>adc_sample_code 是此刻模拟输入对应的量化值；adc_bus_code 属于 12 个采样时钟以前。Vlog 与 ADC 输入可不同，因为后面还有滤波。标称群延迟补偿不能消除边沿拖尾。</p></section>
<section><h2>3. 整帧重建结果</h2><img src="data:image/png;base64,__IMG__"><p>这里只显示完整结果供对照，动画并不声称实时完成了 DMA。灰度显示值=密度/显示上限，非 DICOM，也非经认证检测图像。原始数据、浮点密度与无效掩膜均另行保存。</p></section>
<section><h2>接下来怎样实验</h2><p>修改 <code>config/demo.json</code> 后运行 <code>python run_demo.py</code>，再打开新生成的 <code>results/default/viewer.html</code>。将 log_bw_current_dependent 置为 true 可研究低光照时的响应变慢，但该规律只是可替换假设，不是实际放大板的已知特性。</p></section></main>
<script>const d=__DATA__, cfg=__CFG__; const n=d.sample_time_s.length;const s=document.getElementById('step');s.max=n-1;let timer=null;
function fmt(v,p=6){return Number(v).toPrecision(p)}
function draw(){const k=+s.value;document.getElementById('time').textContent=`采样索引 ${k}/${n-1}；采样时刻 ${(d.sample_time_s[k]*1e6).toFixed(3)} µs；当前总线数据来源时刻 ${(d.source_sample_time_s[k]*1e6).toFixed(3)} µs`;
const rows=[['等效采样横坐标',fmt(d.x_sample_equiv_px[k])+' pixel','含标称模拟群延迟补偿'],['PMT 接收光功率',fmt(d.p_detector_w[k])+' W','采样时刻的光学输入记录'],['PMT 阳极电流',fmt(d.pmt_anode_a[k])+' A','已含 PMT 响应及噪声'],['对数前端电压',fmt(d.v_log_v[k])+' V','示例传递函数输出'],['ADC 差分输入电压',fmt(d.v_adc_diff_v[k])+' V','不等于模块 SMA 单端输入的已知电压'],['本次量化值',d.adc_sample_code[k],'采样事件'],['当前 ADC 总线码',d.adc_bus_code[k],'12 周期前的样本；功能模型未画出 ns 级建立过程'],['总线 valid',d.bus_valid[k],'与采样标签一起延迟'],['总线样本对应列',d.bus_pixel_col[k],'两 ADC 样本/输出像素；-1 表示无效']];document.getElementById('values').innerHTML=rows.map(r=>'<tr>'+r.map(v=>'<td>'+v+'</td>').join('')+'</tr>').join('');
const cv=document.getElementById('wave'),q=cv.getContext('2d');q.clearRect(0,0,1000,220);q.strokeStyle='#ccc';q.beginPath();q.moveTo(0,110);q.lineTo(1000,110);q.stroke();q.strokeStyle='#1f77b4';q.beginPath();for(let j=0;j<n;j++){let x=j/(n-1)*1000,y=200-(d.v_adc_diff_v[j]+1)/2*180;j?q.lineTo(x,y):q.moveTo(x,y)}q.stroke();q.strokeStyle='#c33';q.beginPath();q.moveTo(k/(n-1)*1000,0);q.lineTo(k/(n-1)*1000,220);q.stroke();q.fillStyle='#172b3a';q.fillText('ADC 差分输入波形，纵轴示意 -1 V 至 +1 V',12,16);}
s.oninput=draw;document.getElementById('next').onclick=()=>{s.value=Math.min(n-1,+s.value+1);draw()};document.getElementById('prev').onclick=()=>{s.value=Math.max(0,+s.value-1);draw()};document.getElementById('play').onclick=()=>{if(timer){clearInterval(timer);timer=null;return}timer=setInterval(()=>{s.value=(+s.value+2)%n;draw()},35)};draw();</script></html>'''
    html=html.replace('__IMG__',img).replace('__DATA__',json.dumps(dat)).replace('__CFG__',json.dumps(c,ensure_ascii=False))
    path.write_text(html,encoding='utf-8')
