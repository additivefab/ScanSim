`timescale 1ns/1ps
module tb_scanner;
    localparam integer L=12,NX=8,NY=4;
    reg clk=0; always #50 clk=~clk; // 10 MHz demonstration only
    reg rst=1;
    reg sample_valid=0; reg [15:0] sample_data=0;
    reg [31:0] sample_tag=0;
    wire delayed_valid; wire [31:0] delayed_tag;
    reg [15:0] adc_pipe[0:L-1];
    integer k,cyc,received,expected;
    wire [15:0] adc_data=adc_pipe[L-1];
    tag_delay #(.LATENCY(L),.TAG_W(32)) tags (
        .clk(clk),.rst(rst),.valid_in(sample_valid),.tag_in(sample_tag),
        .valid_out(delayed_valid),.tag_out(delayed_tag));
    always @(posedge clk) begin
        if(rst) for(k=0;k<L;k=k+1) adc_pipe[k]<=0;
        else begin
            adc_pipe[0]<=sample_data;
            for(k=1;k<L;k=k+1) adc_pipe[k]<=adc_pipe[k-1];
        end
    end
    wire ov,os,ol,sticky; wire [15:0] od; wire [31:0] dropped;
    reg ready=0,check_outputs=1;
    adc_stream_fifo #(.DEPTH(32)) fifo (
        .clk(clk),.rst(rst),.in_valid(delayed_valid),.in_data(adc_data),
        .in_last(delayed_tag[16]),.in_sof(delayed_tag[17]),
        .out_valid(ov),.out_ready(ready),.out_data(od),.out_last(ol),.out_sof(os),
        .overflow_sticky(sticky),.dropped_samples(dropped));
    // At each receiving edge, tags and ADC data must refer to the same sample.
    always @(posedge clk) if(!rst && delayed_valid && check_outputs) begin
        if(adc_data!==delayed_tag[15:0]) $fatal(1,"ADC/tag mismatch");
    end
    always @(posedge clk) if(!rst && ov && ready && check_outputs) begin
        if(od !== received[15:0]) $fatal(1,"Data order mismatch got %d expected %d",od,received);
        if(ol !== (received%NX==NX-1)) $fatal(1,"Line-last mismatch");
        if(os !== (received==0)) $fatal(1,"SOF mismatch");
        received=received+1;
    end
    initial begin
        $dumpfile("scanner.vcd"); $dumpvars(0,tb_scanner);
        received=0;expected=0;
        repeat(4) @(negedge clk); rst=0;
        // Four rows, two blank cycles after each row; downstream periodically stalls.
        for(cyc=0;cyc<NY*(NX+2);cyc=cyc+1) begin
            @(negedge clk);
            ready=(cyc%7!=1 && cyc%7!=2);
            sample_valid=(cyc%(NX+2)<NX);
            sample_data=expected;
            sample_tag={14'd0,(expected==0),(expected%NX==NX-1),sample_data};
            if(sample_valid) expected=expected+1;
        end
        @(negedge clk);sample_valid=0;ready=1;
        repeat(80) @(negedge clk);
        if(received!=NX*NY || sticky) $fatal(1,"Missing samples or unexpected overflow");
        // Explicit overload test: acquisition continues while consumer stalls.
        check_outputs=0;rst=1;repeat(3) @(negedge clk);rst=0;ready=0;
        repeat(50) begin @(negedge clk); sample_valid=1; sample_data=16'h1234; end
        @(negedge clk);sample_valid=0;
        repeat(L+3) @(negedge clk);
        if(!sticky || dropped==0) $fatal(1,"Overflow was not detected");
        $display("PASS: tag alignment, backpressure ordering, line/sof flags and overflow");
        $finish;
    end
    initial begin #1000000; $fatal(1,"Watchdog timeout"); end
endmodule
