# From Vivado Tcl Console: cd <project>/rtl ; source run_xsim.tcl
# Behavioral simulation only; does not create a hardware bitstream.
set script_dir [file dirname [file normalize [info script]]]
create_project -in_memory scanner_behavioral
add_files [list [file join $script_dir tag_delay.sv] [file join $script_dir adc_stream_fifo.sv]]
add_files -fileset sim_1 [file join $script_dir tb_scanner.sv]
set_property top tb_scanner [get_filesets sim_1]
set_property simulator_language Mixed [current_project]
launch_simulation -mode behavioral
run all
