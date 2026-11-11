# REFERENCE ONLY. Not loaded by the functional simulator. NOT a complete pin file.
# User schematic p12: M19 = PL 50 MHz, Bank34 LVCMOS33.
# The top module in this package uses clk, not a board-specific wrapper.
# Uncomment ONLY in a separately verified hardware top with matching port name.
# create_clock -period 20.000 -name clk_50 [get_ports clk_50]
# set_property -dict {PACKAGE_PIN M19 IOSTANDARD LVCMOS33} [get_ports clk_50]
# ADC_DCO / ADC_DATA / SOS / ENCODER pins deliberately omitted.
# Check board revision, ADC module schematic, actual VCCO, logic levels and timing.
