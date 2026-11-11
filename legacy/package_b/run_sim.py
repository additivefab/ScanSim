#!/usr/bin/env python3
"""Run from the project root. Requires Python 3.11+ and requirements.txt."""
import argparse,json,sys
from pathlib import Path
from src.scanner import load_config,budget,run

def main() -> int:
    p=argparse.ArgumentParser(description='Film -> PMT -> analog -> ADC -> density image')
    p.add_argument('--config',default='configs/demo_log.json')
    p.add_argument('--output',default='examples/demo_log')
    p.add_argument('--density-file',type=Path,help='Float TIFF or NPY: true optical density, shape ny by nx')
    p.add_argument('--estimate-only',action='store_true')
    p.add_argument('--trace-all',action='store_true',help='Large CSV: every ADC tick of every line')
    args=p.parse_args()
    try:
        c=load_config(args.config)
        m=budget(c) if args.estimate_only else run(c,Path(args.output),args.density_file,args.trace_all)
        print(json.dumps(m,ensure_ascii=False,indent=2))
        return 0
    except (OSError,ValueError,KeyError) as exc:
        print(f'Configuration/input error: {exc}',file=sys.stderr); return 2
if __name__=='__main__': raise SystemExit(main())
