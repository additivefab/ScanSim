`timescale 1ps/1ps

module tb_scanner_capture #(
    parameter [63:0] SAMPLE_START = 64'd0
);
    reg clk = 0, rst = 1, sync = 0;
    reg [15:0] codes [0:63];
    integer count = 0;
    wire valid, first, last;
    wire [15:0] code;
    wire [63:0] sample;
    wire [31:0] line, ignored;
    always #500 clk = ~clk;
    scanner_capture dut(clk, rst, sync, codes[count], valid, code, sample, line, first, last, ignored);
    initial begin
        $readmemh("codes.hex", codes);
        #1000 rst = 0;
        // Test-only initialization avoids billions of cycles to reach a counter boundary.
        #1 dut.sample_count = SAMPLE_START;
        #24 sync = 1; #1000 sync = 0;
        #525 sync = 1; #1000 sync = 0;
        #12450 sync = 1; #1000 sync = 0;
        #10030 sync = 1; #1000 sync = 0;
    end
    always @(posedge clk) begin
        if (!rst) begin
            #1;
            if (valid) $display("CAPTURE %0d %0d %0d %0d %0d", sample, line, code, first, last);
            count = count + 1;
            if (count == 64) begin
                $display("IGNORED %0d", ignored);
                $finish;
            end
        end
    end
endmodule
