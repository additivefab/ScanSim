import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from zlscan.engine.saved_service import saved_service
from zlscan.evidence.records import environment_info, git_state
from zlscan.io.manifest import sha256_file

out=Path('evidence_runs/2026-11-13_probe_recording/output')
report=dict(started_at=datetime.now(timezone.utc).isoformat(), code=git_state(Path.cwd()), background='after task-owned wheel audit completed; OS caches and background load uncontrolled', scope='same-host research probes, caches uncontrolled, no browser/drawing/network/hardware timing', environment=environment_info(), policies={})
for policy in ['raw','summary']:
    source=out/('formal_'+policy)/'source'
    before={p.relative_to(source).as_posix():sha256_file(p) for p in source.rglob('*') if p.is_file()}
    start=time.perf_counter()
    with saved_service(source) as service:
        registered_ms=1000*(time.perf_counter()-start)
        measurements={}
        for name,lo,hi,width in [('overview',0,11000,20),('fine',10200,11000,800)]:
            values=[]
            for _ in range(5):
                start=time.perf_counter()
                response=service.traces(service.runs['recording-c0'],'pmt.power_w',lo,hi,width,'research')
                values.append(1000*(time.perf_counter()-start))
            measurements[name]=dict(milliseconds=values, median_ms=statistics.median(values), returned_values=len(response['values']), status=response['status'], derivation=response['derivation'], response_bytes=len(json.dumps(response).encode()))
    after={p.relative_to(source).as_posix():sha256_file(p) for p in source.rglob('*') if p.is_file()}
    assert before==after
    report['policies'][policy]=dict(registration_ms=registered_ms, queries=measurements, source_unchanged=True)
report['finished_at']=datetime.now(timezone.utc).isoformat()
(out/'query_measurements.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
