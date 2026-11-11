#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
command -v iverilog >/dev/null || { echo 'Install Icarus Verilog first.' >&2; exit 2; }
iverilog -g2012 -s tb_scanner -o sim.out tag_delay.sv adc_stream_fifo.sv tb_scanner.sv
vvp sim.out | tee simulation.log
