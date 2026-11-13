"""Exercise the actual page request helpers with deliberately reordered network promises."""

import re
import shutil
import subprocess

import pytest

from zlscan.api.server import STATIC


@pytest.mark.skipif(
    shutil.which("node") is None, reason="Node.js is needed for page JS race checks"
)
def test_independent_panels_and_newest_cursor_queries_do_not_overwrite_each_other():
    html = (STATIC / "index.html").read_text()
    helpers = re.search(r"function api\(.*?(?=function badge\()", html, re.S).group()
    script = r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
const pending=[];
const context=vm.createContext({reqSeq:0,view:'research',runId:'demo',
  latestIssued:{},lastApplied:{},viewGeneration:0,cursorGeneration:0,URLSearchParams,
  fetch:()=>new Promise(resolve=>pending.push(()=>resolve({json:async()=>({})}))) });
vm.runInContext(HELPERS,context);
(async()=>{
  const trace=context.api('/traces?signal=adc.code&start=0&end=8');
  const events=context.api('/events?start=0&end=8');
  pending[1]();assert.equal(context.stale(await events),false);
  pending[0]();assert.equal(context.stale(await trace),false,'independent trace must survive');
  const oldState=context.api('/state?step=1');
  const newState=context.api('/state?step=2');
  pending[2]();assert.equal(context.stale(await oldState),true,'newest requested cursor wins');
  pending[3]();assert.equal(context.stale(await newState),false);
  const oldResearch=context.api('/state?step=3');
  context.view='instrument';context.viewGeneration++;
  context.view='research';context.viewGeneration++;
  pending[4]();assert.equal(context.stale(await oldResearch),true,'old view epoch');
  const oldTile=context.api('/images/density/tiles/0/0/0');
  const newTile=context.api('/images/density/tiles/0/1/0');
  pending[5]();assert.equal(context.stale(await oldTile),true,'new tile replaces previous tile');
  pending[6]();assert.equal(context.stale(await newTile),false);
  const primary=context.api('/images/density/manifest');
  const compare=context.api('/images/density/manifest',null,'other');
  pending[8]();assert.equal(context.stale(await compare),false);
  pending[7]();assert.equal(context.stale(await primary),false,'comparison has a separate slot');
  const oldRun=context.api('/state?step=4');
  context.runId='other';context.viewGeneration++;
  pending[9]();assert.equal(context.stale(await oldRun),true,'run selection rejects old replies');
  const oldSample=context.api('/samples/2551/lineage');
  const newSample=context.api('/samples/2560/lineage');
  pending[11]();assert.equal(context.stale(await newSample),false);
  pending[10]();assert.equal(context.stale(await oldSample),true,'latest selected sample wins');

})().catch(error=>{console.error(error);process.exitCode=1});
"""
    import json

    result = subprocess.run(
        [shutil.which("node")],
        input="const HELPERS=" + json.dumps(helpers) + ";\n" + script,
        encoding="utf-8",
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js needed for page helper checks")
def test_image_pixel_jumps_to_source_sample_with_exact_large_origin():
    import json

    html = (STATIC / "index.html").read_text()
    helper = re.search(
        r"function selectPixel\(.*?(?=function loadRunChoices\()", html, re.S
    ).group()
    script = r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
const elements={cursor:{value:0},'pixel-info':{textContent:''}};
const origin=2n**53n+17n;
const reply={samples:[{sample_index:String(origin+21n),normalized_weight:1}]};
let drawCount=0,request='';
const context=vm.createContext({document:{getElementById:id=>elements[id]},
  runId:'capture',imageGeneration:2,selectedPixel:null,
  manifest:{timeline_available:true,sample_origin:String(origin),steps:100},
  api:path=>{request=path;return Promise.resolve(reply)},stale:()=>false,
  paintImage:()=>{},draw:()=>drawCount++,win:{start:0,end:100},BigInt,Number,JSON});
vm.runInContext(HELPER,context);
(async()=>{
  const tile={x:0,y:0,flags:[[0,0],[0,0]],density:[[1,1],[1,1]]};
  context.selectPixel('capture',1,1,{reasons:{}},tile,{},2);
  await new Promise(setImmediate);
  assert.equal(request,'/images/density/pixels/1/1/lineage');
  assert.equal(elements.cursor.value,21);assert.equal(drawCount,1);
  assert.equal(context.win.start,5);assert.equal(context.win.end,85);
  context.selectPixel('comparison',1,1,{reasons:{}},tile,{},2);
  await new Promise(setImmediate);
  assert.equal(drawCount,1,'comparison must not move primary cursor using its own clock');
  context.selectPixel('capture',1,1,{reasons:{}},tile,{},1);
  await new Promise(setImmediate);
  assert.equal(drawCount,1,'old image generation must not move cursor');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run(
        [shutil.which("node")],
        input="const HELPER=" + json.dumps(helper) + ";\n" + script,
        encoding="utf-8",
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js needed for page helper checks")
def test_raw_trace_painter_breaks_at_missing_samples():
    import json

    html = (STATIC / "index.html").read_text()
    helper = re.search(r"function paint\(.*?(?=function moveCursor\()", html, re.S).group()
    script = r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
const moves=[],lines=[];
const graphics={clearRect:()=>{},beginPath:()=>{},stroke:()=>{},fillRect:()=>{},
  moveTo:(x,y)=>moves.push([x,y]),lineTo:(x,y)=>lines.push([x,y])};
const context=vm.createContext({win:{start:0,end:3},Math,Number});
vm.runInContext(HELPER,context);
context.paint({width:30,height:90,getContext:()=>graphics},
  {level:'raw',values:[1,null,3],first_step:0,block_steps:1});
assert.equal(moves.length,2,'gap must break the held path');
assert.equal(moves[1][0],20);
assert.equal(lines[0][0],10,'previous value is held only up to start of missing sample');
assert(moves.concat(lines).flat().every(Number.isFinite));
"""
    result = subprocess.run(
        [shutil.which("node")],
        input="const HELPER=" + json.dumps(helper) + ";\n" + script,
        encoding="utf-8",
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js needed for page helper checks")
def test_view_switch_before_manifest_arrives_reloads_manifest():
    import json

    html = (STATIC / "index.html").read_text()
    helper = re.search(
        r"document.querySelectorAll\('input\[name=view\]'\).*?(?=function zoom\()", html, re.S
    ).group()
    script = r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
let change,loads=0,signals=0,images=0;
const input={value:'instrument',addEventListener:(_,fn)=>change=fn};
const context=vm.createContext({document:{querySelectorAll:()=>[input],getElementById:()=>({})},
  manifest:null,view:'research',viewGeneration:0,load:()=>loads++,
  listSignals:()=>signals++,showImages:()=>images++});
vm.runInContext(HELPER,context);change();
assert.equal(loads,1,'view change must restart invalidated initial manifest request');
assert.equal(signals,0);assert.equal(context.viewGeneration,1);
context.manifest={};change();assert.equal(signals,1);assert.equal(images,1);
"""
    result = subprocess.run(
        [shutil.which("node")],
        input="const HELPER=" + json.dumps(helper) + ";\n" + script,
        encoding="utf-8",
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
