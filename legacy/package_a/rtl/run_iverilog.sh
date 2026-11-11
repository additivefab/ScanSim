#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
iverilog -g2012 -s tb_scanner_capture -o scanner_tb scanner_capture.sv tb_scanner_capture.sv
vvp scanner_tb
