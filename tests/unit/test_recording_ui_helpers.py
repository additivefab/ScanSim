"""Exercise actual recording page helpers, without claiming browser visual acceptance."""

import json
import re
import shutil
import subprocess

import pytest

from zlscan.api.server import STATIC


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js required for actual page helpers")
def test_missing_events_are_not_no_events_and_sample_provenance_stays_explicit():
    html = (STATIC / "index.html").read_text()
    script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    events = re.search(r"function eventText\(.*?(?=function sampleText\()", html, re.S).group()
    samples = re.search(r"function sampleText\(.*?(?=function selectSample\()", html, re.S).group()
    test = r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
new vm.Script(PAGE);
const context=vm.createContext({String});vm.runInContext(EVENTS+SAMPLES,context);
const missing=context.eventText({status:'not_recorded',events:[],reason:'analogue index missing'});
assert(missing.includes('未完整记录')&&missing.includes('analogue index missing'));
assert(!missing.includes('无已登记事件'));
const partial=context.eventText({status:'not_recorded',events:[{kind:'epoch_observed',step:9}]});
assert(partial.includes('部分记录')&&partial.includes('epoch_observed@9'));
const quality=context.eventText({status:'exact_record',
 events:[{kind:'front_end_clipped',step_exact:'7',sample_grid:'fine'}]});
assert(quality.includes('模型记录[细步网格]')&&quality.includes('@7'));
const adc=context.eventText({status:'exact_record',
 events:[{kind:'adc_saturated',step_exact:'8',sample_grid:'adc'}]});
assert(adc.includes('ADC源网格'));

const sample=context.sampleText({status:'exact_record',lineage:{sample_index:'9223372036854775825',
 epoch:0,line_number:'0',value:0,sample_time:'123',host_arrival_time:null},
 model_lineage:{adc_output_time:'124',pin_arrival_time:'125',capture_time:'126'},
 transport_diagnostic:{last_stage_completion:'130'}});
assert(sample.includes('9223372036854775825')&&sample.includes('码 0'));
assert(sample.includes('时钟定义推导')&&sample.includes('假设参数推导'));
assert(sample.includes('主机到达：未记录')&&sample.includes('不是主机收件时刻'));
"""
    result = subprocess.run(
        [shutil.which("node")],
        input="const PAGE="
        + json.dumps(script)
        + "; const EVENTS="
        + json.dumps(events)
        + "; const SAMPLES="
        + json.dumps(samples)
        + ";"
        + test,
        encoding="utf-8",
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
