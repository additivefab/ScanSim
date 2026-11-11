# REFERENCE ONLY. No ADC pin assignment is supplied because its part/interface
# and adapter pinout are unknown. DO NOT generate a bitstream from this file alone.
# Uploaded schematic pages 7,11,12: 50-MHz PL clock is M19, bank34, 3.3 V.
# Uncomment only after the actual top-level port clk_50 exists and board is checked.
# create_clock -period 20.000 -name clk_50 [get_ports clk_50]
# set_property -dict {PACKAGE_PIN M19 IOSTANDARD LVCMOS33} [get_ports clk_50]
# Real ADC capture also needs set_input_delay -min/-max relative to ADC DCO,
# clock uncertainty, correct VCCO/IOSTANDARD, CDC analysis, reset analysis and
# board-level timing. Never fix unknown timings by broad false-path constraints.
