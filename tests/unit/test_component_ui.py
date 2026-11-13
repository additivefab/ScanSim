"""Actual component helpers preserve original values, stale isolation and off-film x."""

import shutil

import pytest
from tests.unit.test_digital_cursor_ui import run

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js needed for JS checks")


def test_optical_geometry_fits_outside_beam_without_clipping_physical_position():
    run(r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
const helpers=PAGE.match(/function componentGeometry\(.*?(?=function showImages\()/s)[0];
const context=vm.createContext({Math,Number,BigInt,JSON});vm.runInContext(helpers,context);
const a=context.componentGeometry({x_m:-.012,y_m:.002,film_width_m:.08,film_height_m:.01});
assert(a.beamX<a.filmX,'off-film point is left of the film, not clamped onto it');
assert.equal(a.x,-.012);assert(a.beamX>=0&&a.beamX<=640);
assert.equal(context.componentGeometry({x_m:0,y_m:0,film_width_m:null,film_height_m:null}),null);
""")


def test_components_are_cursor_scoped_and_no_grid_frame_is_fabricated_between_steps():
    run(r"""
const vm=require('node:vm'),assert=require('node:assert/strict'),pending=[];
const context=vm.createContext({reqSeq:0,view:'research',runId:'r',viewGeneration:0,
 cursorGeneration:0,latestIssued:{},lastApplied:{},URLSearchParams,
 fetch:()=>new Promise(resolve=>pending.push(()=>resolve({json:async()=>({})})))});
vm.runInContext(PAGE.match(/function api\(.*?(?=function badge\()/s)[0],context);
(async()=>{
 const old=context.api('/components?step=7');context.cursorGeneration++;
 pending[0]();assert(context.stale(await old));
})().catch(error=>{console.error(error);process.exitCode=1});
""")


def test_actual_svg_and_cards_clear_old_values_and_select_the_real_registered_probe():
    run(r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
class Element{
 constructor(){this.children=[];this.attrs={};this.style={};this._text=''}
 set textContent(value){this._text=String(value);this.children=[]}
 get textContent(){return this._text}
 appendChild(element){this.children.push(element);return element}
 setAttribute(key,value){this.attrs[key]=String(value)}
}
const ids={};for(const id of ['component-status','optical-scene','beam-state',
 'component-nodes','component-detail'])ids[id]=new Element();
const frame={status:'replayed',tick_ps:'100',epoch:1,visibility:'ORACLE_ONLY',
 parameter_evidence:{all:'ASSUMED'},nodes:[
 {id:'laser',title:'光源',status:'replayed',value:0,unit:'W',signal_id:'source.power_w'},
 {id:'adc',title:'ADC',status:'replayed',value:20,unit:'code',sample_index:'0',
 sample_tick_ps:'0',epoch:0,held:true,signal_id:'adc.code'}],
 geometry:{x_m:-.012,y_m:.002,film_width_m:.08,film_height_m:.01,
 facet:1,line:'0',scan_active:false,laser_gate:false}};
let selected=0;
const context=vm.createContext({Math,Number,BigInt,JSON,String,view:'research',sel:{},
 componentTick:'100',physicalFrame:frame,digitalFrame:null,selectedComponent:'laser',
 document:{getElementById:id=>ids[id],createElement:()=>new Element(),
 createElementNS:()=>new Element()},listSignals:()=>selected++});
vm.runInContext(PAGE.match(/function componentGeometry\(.*?(?=function showImages\()/s)[0],context);
context.renderComponents();
const svg=ids['optical-scene'],rect=svg.children[0],dot=svg.children[2];
assert(Number(dot.attrs.cx)<Number(rect.attrs.x));
assert.equal(svg.children[1].attrs.stroke,'#a5adb5','gate is from original model');
assert(ids['component-nodes'].children[1].textContent.includes('时期 0 / 0 ps（保持）'));
ids['component-nodes'].children[0].onclick();
assert.equal(context.sel['source.power_w'],true);assert.equal(selected,1);
context.physicalFrame=Object.assign({},frame,{nodes:[{id:'film',title:'透射率',
 status:'not_computed',value:null,signal_id:'film.t_eff'}]});
context.renderComponents();ids['component-nodes'].children[0].onclick();
assert.equal(selected,1,'uncomputed placeholder must not turn into a numeric trace');
assert.equal(context.sel['film.t_eff'],undefined);
context.clearComponents('101','细步之间');
assert.equal(svg.children.length,0);assert.equal(context.physicalFrame,null);
context.physicalFrame=frame;context.renderComponents();
assert.equal(svg.children.length,0,'a previous tick cannot reappear');
assert(ids['beam-state'].textContent.includes('不显示旧位置'));
""")
