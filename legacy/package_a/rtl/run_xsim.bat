@echo off
cd /d "%~dp0"
call xvlog --sv scanner_capture.sv tb_scanner_capture.sv
if errorlevel 1 exit /b 1
call xelab tb_scanner_capture -s scanner_sim
if errorlevel 1 exit /b 1
call xsim scanner_sim -tclbatch run.tcl
