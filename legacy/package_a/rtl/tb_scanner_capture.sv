`timescale 1ns/1ps
module tb_scanner_capture;
    localparam integer L=12, N=256;
    reg clk=0; always #25 clk=~clk; // 20 MSPS example
    reg rst=1, sample_valid=0, ready=0, check_enable=1;
    reg [15:0] sample_id=0, source_code=0;
    reg [15:0] vectors[0:N-1];
    reg [15:0] adc_pipe[0:L-1];
    wire [15:0] adc_bus=adc_pipe[L-1];
    wire valid,overflow; wire [15:0] data,id;
    integer p,j,expected=0,ticks=0,cycles;
    scanner_capture #(.LATENCY(L),.DEPTH(32)) dut(
        .clk(clk),.rst(rst),.sample_valid(sample_valid),.sample_id(sample_id),
        .adc_bus(adc_bus),.m_valid(valid),.m_ready(ready),.m_data(data),.m_id(id),.overflow(overflow));
    // Independent ADC pipeline, no vendor electrical timing represented.
    always @(posedge clk) begin
        if(rst) for(p=0;p<L;p=p+1) adc_pipe[p]<=0;
        else begin
            adc_pipe[0]<=source_code;
            for(p=1;p<L;p=p+1) adc_pipe[p]<=adc_pipe[p-1];
        end
    end
    always @(posedge clk) begin
        if (rst) expected=0;
        else if(check_enable && valid && ready) begin
            if(id !== expected[15:0]) $fatal(1,"ID mismatch expected %0d got %0d",expected,id);
            if(data !== vectors[expected]) $fatal(1,"Code mismatch at %0d",expected);
            expected=expected+1;
        end
    end
    initial begin
        $readmemh("vectors.hex",vectors);
        $dumpfile("scanner.vcd");$dumpvars(0,tb_scanner_capture);
        repeat(4) @(negedge clk);rst=0;
        for(j=0;j<N;j=j+1) begin
            @(negedge clk);
            sample_valid=1;sample_id=j;source_code=vectors[j];
            // Bounded downstream stalls, FIFO must retain order and stable data.
            ready=(j%12<10);
            if(j%64==63) begin
                @(negedge clk);sample_valid=0;ready=1;
                repeat(10) @(negedge clk);
            end
        end
        @(negedge clk);sample_valid=0;ready=1;
        cycles=0;
        while(expected<N && cycles<2000) begin @(negedge clk);cycles=cycles+1;end
        if(expected!=N) $fatal(1,"Missing output samples: %0d",expected);
        if(overflow) $fatal(1,"Unexpected overflow in bounded stall case");
        $display("PASS: 256 ADC values and delayed tags match; bounded stalls preserve order.");
        // Deliberately block output longer than capacity. ADC does not pause.
        @(negedge clk);rst=1;check_enable=0;
        repeat(3) @(negedge clk);rst=0;ready=0;
        for(j=0;j<80;j=j+1) begin
            @(negedge clk);sample_valid=1;sample_id=j;source_code=j;
        end
        @(negedge clk);sample_valid=0;
        repeat(20) @(negedge clk);
        if(!overflow) $fatal(1,"Overflow was not reported");
        $display("PASS: deliberate overload sets sticky overflow; frame must be rejected.");
        $finish;
    end
    initial begin #500000; $fatal(1,"Timeout"); end
endmodule
