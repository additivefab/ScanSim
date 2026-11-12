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
  latestIssued:{},lastApplied:{},viewGeneration:0,URLSearchParams,
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
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    import json

    result = subprocess.run(
        [shutil.which("node"), "-e", "const HELPERS=" + json.dumps(helpers) + ";\n" + script],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
