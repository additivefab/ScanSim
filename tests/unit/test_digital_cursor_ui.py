"""Actual page helpers: exact clock edges, event/sample selection and cursor race isolation."""

import json
import re
import shutil
import subprocess

import pytest

from zlscan.api.server import STATIC

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js needed for JS checks")


def run(script):
    html = (STATIC / "index.html").read_text()
    js = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    result = subprocess.run(
        [shutil.which("node")],
        input="const PAGE=" + json.dumps(js) + ";\n" + script,
        encoding="utf-8",
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_whole_script_parses_and_fractional_model_step_does_not_get_rounded_probe():
    run(r"""
const vm=require('node:vm'),assert=require('node:assert/strict');new Function(PAGE);
const helpers=PAGE.match(/function stepTick\(.*?(?=function showImages\()/s)[0];
const elements={};
for(const id of ['info','sample-info','digital-info','readout','cursor','cursor-ps'])
  elements[id]={textContent:'',value:0};
const requests=[],line={style:{}};
const context=vm.createContext({manifest:{
  fine_step_period_ps:{numerator:'1000000000000',denominator:'12000000'},
  fine_clock_phase_ps:'9007199254740993',tick_start_ps:'9007199254740993',tick_end_ps:'9007199255740993'},
  cursorTick:null,cursorGeneration:0,win:{start:0,end:12},BigInt,Number,JSON,
  document:{getElementById:id=>elements[id],querySelectorAll:()=>[line]},stale:()=>false,
  api:path=>{requests.push(path);return Promise.resolve({status:'not_recorded',reason:'test'})}});
vm.runInContext(helpers,context);
const phase=9007199254740993n;
context.setTick(String(phase+83333n));
assert.equal(requests[0],'/state?step=1&probes=power_w',
 'exact floor edge must map back to its count');
assert.equal(context.cursorTick,String(phase+83333n));
context.setTick(String(phase+83334n));
assert.equal(requests.filter(x=>x.startsWith('/state')).length,1,
 'no false physical point between fine edges');
assert.equal(requests.at(-1),'/digital-state?tick='+String(phase+83334n));
assert.equal(elements.cursor.value,1);assert.equal(context.cursorGeneration,2);
assert.equal(line.style.display,'block');assert(Number.isFinite(parseFloat(line.style.left)));
assert(elements.info.textContent.includes('细步之间'));
context.setTick('1.5');
assert.equal(context.cursorGeneration,2,'invalid cursor does not change selection');
""")


def test_late_cursor_scoped_answers_are_rejected_without_a_replacement_request():
    run(r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
const pending=[];
const context=vm.createContext({reqSeq:0,view:'research',runId:'r',viewGeneration:0,
 cursorGeneration:1,latestIssued:{},lastApplied:{},URLSearchParams,
 fetch:()=>new Promise(resolve=>pending.push(()=>resolve({json:async()=>({})})))});
vm.runInContext(PAGE.match(/function api\(.*?(?=function badge\()/s)[0],context);
(async()=>{
 const physical=context.api('/state?step=1'),digital=context.api('/digital-state?tick=1'),
       lineage=context.api('/samples/1/lineage'),trace=context.api('/traces?signal=adc.code');
 context.cursorGeneration++;
 for(const resolve of pending)resolve();
 assert(context.stale(await physical));assert(context.stale(await digital));
 assert(context.stale(await lineage));
 assert.equal(context.stale(await trace),false,'window trace survives cursor-only selection');
})().catch(error=>{console.error(error);process.exitCode=1});
""")


def test_events_use_original_ps_and_adc_click_moves_to_source_edge():
    run(r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
const selected=[],requests=[];
const context=vm.createContext({manifest:{
 exact_tick_cursor:true,frozen_commit:true,timeline_available:true,os:4},
 BigInt,Math,String,stepTick:s=>s*250000n,setTick:t=>selected.push(t),stale:()=>false,
 document:{getElementById:()=>({})},sampleText:()=>'',
 api:path=>{requests.push(path);return Promise.resolve({})}});
vm.runInContext(PAGE.match(/function selectEvent\(.*?(?=function sampleText\()/s)[0],context);
vm.runInContext(PAGE.match(/function selectSample\(.*?(?=function paint\()/s)[0],context);
context.selectEvent({step_exact:'7',sample_grid:'fine',time_ticks:'1750001'});
assert.equal(selected[0],'1750001','original ps is not rounded to a fine edge');
context.selectEvent({step_exact:'8',sample_grid:'adc_source'});assert.equal(selected[1],'2000000');
context.selectSample(11);assert.equal(selected[2],'2000000');assert.equal(requests[0],'/samples/2/lineage');
""")
