#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
xvlog --sv scanner_capture.sv tb_scanner_capture.sv
xelab tb_scanner_capture -s scanner_sim
xsim scanner_sim -tclbatch run.tcl
