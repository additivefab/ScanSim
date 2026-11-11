@echo off
cd /d "%~dp0"
iverilog -g2012 -s tb_scanner_capture -o scanner_tb scanner_capture.sv tb_scanner_capture.sv
if errorlevel 1 exit /b 1
vvp scanner_tb
