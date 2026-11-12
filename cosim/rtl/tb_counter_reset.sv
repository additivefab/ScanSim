`timescale 1ps/1ps
module tb_counter_reset;
    reg clk = 0, rst = 1, sync = 0, started = 0;
    wire valid, first, last;
    wire [15:0] code;
    wire [63:0] sample;
    wire [31:0] line, ignored;
    reg [63:0] expected_count = 0;
    integer cycle = 0, epoch = 0, captured = 0;
    scanner_capture dut(clk, rst, sync, 16'h5a5a, valid, code, sample, line, first, last, ignored);
    always #500 clk = ~clk;
    initial begin
        #1000 rst = 0;
        #1;
        dut.sample_count = 64'd4294967280;
        expected_count = 64'd4294967280;
        started = 1;
    end
    always @(negedge clk) begin
        if (started) begin
            cycle = cycle + 1;
            sync = (cycle % 10 == 2);
            if (cycle == 38) begin
                if (!dut.busy) $fatal(1, "reset must interrupt an active window");
                $display("RESET_DURING_BUSY");
                rst = 1; sync = 0;
            end
            if (cycle == 39) begin
                rst = 0; epoch = 1;
            end
            if (cycle == 70) begin
                if (captured < 20) $fatal(1, "insufficient reset boundary captures");
                $display("RESET_OK");
                $finish;
            end
        end
    end
    always @(posedge clk) begin
        if (rst) begin
            expected_count = 0;
            #1;
            if (valid) $fatal(1, "reset must clear output valid");
        end else if (started) begin
            #1;
            if (valid) begin
                if (sample !== expected_count) $fatal(1, "counter identity changed");
                if (code !== 16'h5a5a) $fatal(1, "word and source identity separated");
                $display("RESET_CAPTURE %0d %0d %0d %0d %0d %0d", epoch, sample, line, code, first, last);
                captured = captured + 1;
            end
            expected_count = expected_count + 64'd1;
        end
    end
endmodule
