"""Record the RTL testbenches and SPICE netlists the legacy packages ship (task S0-T04).

While the simulators are not installed this writes one 'not_executed' evidence record per case.
When the tools are present it stops instead of guessing: the run path has to be written and
verified against the real tools first (see docs/reports/simulator_install_attempt.md).

The working tree must be clean so that the recorded commit describes the code that ran.
"""

import sys
from datetime import UTC, datetime
from pathlib import Path

from zlscan.evidence import sim_tools as st
from zlscan.evidence.records import environment_info, git_state

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    code = git_state(ROOT)
    if code["dirty"] is not False:
        print("refusing to record: the working tree is not clean. Commit first.", file=sys.stderr)
        return 2
    found = {tool: st.find_tool(tool) for tool in sorted({t for c in st.CASES for t in c.tools})}
    now = datetime.now(UTC)
    try:
        paths = st.write_not_executed_records(
            runs_dir=ROOT / "evidence_runs",
            date=now.strftime("%Y-%m-%d"),
            repo_root=ROOT,
            found=found,
            code=code,
            environment=environment_info(),
            now=now,
        )
    except RuntimeError as exc:
        print(f"stopping: {exc}", file=sys.stderr)
        return 3
    for path in paths:
        print(f"not_executed: {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
