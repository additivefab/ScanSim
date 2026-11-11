"""Self-contained offline playback; no internet, server, or JavaScript library required."""
import base64,json
from pathlib import Path
import numpy as np

def write_viewer(out: Path, cfg: dict, metrics: dict, trace: np.ndarray) -> None:
    def image(name): return 'data:image/png;base64,'+base64.b64encode((out/name).read_bytes()).decode()
    data=json.dumps({'config':cfg,'metrics':metrics,'trace':trace.tolist()},ensure_ascii=False,separators=(',',':'))
    html='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ZL · 底片扫描仪仿真回放</title><style>
body{font:16px/1.65 system-ui,"Noto Sans CJK SC",sans-serif;margin:0;background:#f4f6f8;color:#182638}main{max-width:1180px;margin:auto;padding:30px}h1{font-size:32px;margin:5px 0}h2{font-size:21px}.eyebrow{letter-spacing:.14em;font-size:12px}.notice{border-left:4px solid #bc7e27;background:#fff5df;padding:14px 20px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}.card{background:white;border:1px solid #dfe5eb;border-radius:10px;padding:20px;margin:18px 0}.flow{display:flex;align-items:stretch;gap:10px;flex-wrap:wrap}.flow div{border:1px solid #cbd5df;border-radius:6px;padding:8px;flex:1;min-width:110px}.flow b{display:block}.flow small{display:block;word-break:break-all}img{width:100%;image-rendering:pixelated;border:1px solid #eee}input[type=range]{width:100%}button{padding:8px 16px;cursor:pointer;margin-right:8px}table{border-collapse:collapse;width:100%;font-size:14px}th,td{padding:6px 10px;border-bottom:1px solid #e5e7eb;text-align:left}code{background:#eef1f4;padding:1px 4px}.muted{color:#596778;font-size:14px}canvas{width:100%;height:160px;border:1px solid #ddd}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}.readout{font-variant-numeric:tabular-nums;font-size:18px;font-weight:650}a{color:#245d88}@media(max-width:750px){.grid{grid-template-columns:1fr}main{padding:16px}}
</style><main><div class="eyebrow">ZL_OEM_SCANER / ENGINEERING SANDBOX / 2026-11-10</div>
<h1>从扫描光点到数字底片</h1><p>第一行逐ADC采样回放 · 完整帧预计算结果 · 数据与配置可追溯</p>
<div class="notice"><b>这是可修改的行为级仿真，不是实机性能认证。</b> 光学、PMT、模拟电路和ADC参数均为演示假设；校准使用模型已知静态参数。下方动画只回放已计算的数据，不代表已经连接开发板。</div>
<section class="card"><h2>01 / 拖动时间，查看当前采样周期</h2><p class="muted">各级同时处理不同历史输入：同一时刻的电流、电压和输出码，不应理解为同一个光子瞬间穿过所有器件。</p>
<button id="play">播放 / 暂停</button><button id="prev">上一个采样</button><button id="next">下一个采样</button><button id="start">跳到第一个像素</button>
<input id="time" type="range" min="0" step="1"><div class="readout" id="timeInfo"></div>
<div class="flow"><div><b>扫描位置</b><small id="pos"></small></div><div><b>透射光 / PMT</b><small id="pmt"></small></div><div><b>模拟前端</b><small id="ana"></small></div><div><b>ADC</b><small id="adc"></small></div><div><b>延迟后的像素标签</b><small id="tag"></small></div></div>
<h3>第一行 ADC 输入电压</h3><canvas id="wave" width="1000" height="160"></canvas><p class="muted">横轴：采样序号（含前后保护窗口）；纵轴：V。竖线对应当前采样位置。完整数据见 line_trace.csv。</p></section>
<div class="grid"><section class="card"><h2>02 / 输入：合成光密度底片</h2><img src="__TRUTH__"><p class="muted">浅色代表较高光密度；这是光密度显示，不是现场拍摄的底片照片。</p></section>
<section class="card"><h2>03 / 输出：重建光密度</h2><img src="__DENSITY__"><p class="muted">可见噪声、拖尾、畸变与行间变化。显示范围固定，真实浮点数据保存在 TIFF 中。</p></section></div>
<section class="card"><h2>04 / 完整图像如何逐行形成</h2><input id="frame" type="range" min="0" step="1"><div id="frameInfo"></div><canvas id="accum" height="260" width="1000"></canvas><p class="muted">这是预计算帧的累积回放。数字流水线最后一行完成后仍须排空；传输耗时及真实电机启动时间未包含在光栅扫描预算中。</p></section>
<section class="card"><h2>05 / 当前样本明细</h2><table id="details"></table></section>
<section class="card"><h2>06 / 预算与误差</h2><pre id="metrics"></pre><p>使用 <code>python run_sim.py --trace-all</code> 可导出所有行、所有 ADC 时刻。更细的模拟子步见 micro_trace.csv；每行关键事件见 line_events.csv。</p></section>
<p class="muted">原始码、透射率、光密度和显示灰度是不同的数据产品。不得把演示模型的RMSE解释成被调研设备的测量精度。</p></main>
<script>const data=__DATA__;const tr=data.trace,c=data.config,s=c.scan;const slider=document.getElementById('time');slider.max=tr.length-1;slider.value=s.pre_samples;
const fields=['采样时刻 / s','此样本码字就绪时刻 / s','本行采样序号','阶段：0=SOS,1=前沿,2=有效区,3=行间','名义横坐标 / µm','实际光点横坐标 / µm','实际纵坐标 / µm','到达PMT光功率 / W','滤波后阳极电流 / A','静态模拟映射输出 / V','ADC输入 / V','该时刻采得的码字','采样时有效区','当前输出时钟的有效标签','当前输出时钟的码字（-1=未就绪）','当前输出对应像素X（-1=无效）'];
const fmt=(v,n=4)=>Number(v).toPrecision(n);function update(){let i=+slider.value,r=tr[i];document.getElementById('timeInfo').textContent=`k=${i}  |  t=${(r[0]*1e6).toFixed(3)} µs  |  ${['SOS','前沿保护','有效成像','行间/消隐'][r[3]]}`;
document.getElementById('pos').textContent=`x=${r[5].toFixed(1)} µm；y=${r[6].toFixed(1)} µm`;
document.getElementById('pmt').textContent=`P=${fmt(r[7])} W；I=${fmt(r[8])} A`;
document.getElementById('ana').textContent=`Vin=${r[10].toFixed(5)} V`;
document.getElementById('adc').textContent=`本次码字=${r[11]}；延迟 ${c.adc.latency_samples} 拍后就绪`;
document.getElementById('tag').textContent=`valid=${r[13]}；x=${r[15]}；data=${r[14]}`;
document.getElementById('details').innerHTML=fields.map((f,j)=>`<tr><th>${f}</th><td>${fmt(r[j],9)}</td></tr>`).join('');
let cv=document.getElementById('wave'),cx=cv.getContext('2d'),end=Math.min(tr.length,s.pre_samples+s.nx*s.samples_per_pixel+64);cx.clearRect(0,0,cv.width,cv.height);cx.beginPath();let lo=c.adc.vmin,hi=c.adc.vmax;for(let k=0;k<end;k++){let x=k/(end-1)*cv.width,y=145-(tr[k][10]-lo)/(hi-lo)*125;if(k===0)cx.moveTo(x,y);else cx.lineTo(x,y)}cx.stroke();let x=i/(end-1)*cv.width;if(x<=cv.width){cx.beginPath();cx.moveTo(x,0);cx.lineTo(x,160);cx.stroke()}cx.fillText(lo+' V',3,155);cx.fillText(hi+' V',3,13);}
slider.oninput=update;document.getElementById('prev').onclick=()=>{slider.value=Math.max(0,+slider.value-1);update()};document.getElementById('next').onclick=()=>{slider.value=Math.min(tr.length-1,+slider.value+1);update()};document.getElementById('start').onclick=()=>{slider.value=s.pre_samples;update()};let timer=null;document.getElementById('play').onclick=()=>{if(timer){clearInterval(timer);timer=null;return}timer=setInterval(()=>{slider.value=(+slider.value+4)%Math.min(tr.length,s.pre_samples+s.nx*s.samples_per_pixel+64);update()},60)};
document.getElementById('metrics').textContent=JSON.stringify(data.metrics,null,2);let fs=document.getElementById('frame'),im=new Image();im.src='__DENSITY__';fs.max=s.ny;fs.value=s.ny;function accum(){let n=+fs.value,cv=document.getElementById('accum'),cx=cv.getContext('2d');cx.clearRect(0,0,cv.width,cv.height);cx.imageSmoothingEnabled=false;if(n>0)cx.drawImage(im,0,0,s.nx,n,0,0,cv.width,cv.height*n/s.ny);document.getElementById('frameInfo').textContent=`已写入 ${n} / ${s.ny} 行；名义行周期 ${(s.line_samples/s.fs_hz*1e6).toFixed(1)} µs`;};fs.oninput=accum;im.onload=accum;update();</script></html>'''
    html=html.replace('__DATA__',data).replace('__TRUTH__',image('preview_truth.png')).replace('__DENSITY__',image('preview_density.png'))
    (out/'viewer.html').write_text(html,encoding='utf-8')
