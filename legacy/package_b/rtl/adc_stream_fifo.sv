// Synchronous FIFO for a non-backpressure-able ADC source.
// data/last/sof are transported atomically. Overflow MUST invalidate the frame.
// A real design also needs ADC physical capture, asynchronous FIFO when crossing
// clocks, AXI packetization, reset-domain checks, DMA and board timing constraints.
module adc_stream_fifo #(
    parameter integer DATA_W=16,
    parameter integer DEPTH=32,
    parameter integer PTR_W=$clog2(DEPTH)
)(
    input wire clk, input wire rst,
    input wire in_valid, input wire [DATA_W-1:0] in_data,
    input wire in_last, input wire in_sof,
    output wire out_valid, input wire out_ready,
    output wire [DATA_W-1:0] out_data, output wire out_last, output wire out_sof,
    output reg overflow_sticky, output reg [31:0] dropped_samples
);
    reg [DATA_W+1:0] mem[0:DEPTH-1];
    reg [PTR_W-1:0] wp,rp;
    reg [PTR_W:0] count;
    wire pop=out_valid && out_ready;
    // Permit write when a simultaneous read releases the full FIFO's oldest slot.
    wire push=in_valid && ((count < DEPTH) || pop);
    initial if (DEPTH < 2 || (1<<PTR_W)!=DEPTH)
        $fatal(1,"DEPTH must be a power of two >= 2");
    assign out_valid=(count!=0);
    assign {out_sof,out_last,out_data}=mem[rp];
    always @(posedge clk) begin
        if (rst) begin
            wp<=0; rp<=0; count<=0;
            overflow_sticky<=0; dropped_samples<=0;
        end else begin
            if(push) begin mem[wp]<={in_sof,in_last,in_data}; wp<=wp+1'b1; end
            if(pop) rp<=rp+1'b1;
            case ({push,pop})
                2'b10: count<=count+1'b1;
                2'b01: count<=count-1'b1;
                default: count<=count;
            endcase
            if(in_valid && !push) begin
                overflow_sticky<=1;
                if(dropped_samples!=32'hffffffff) dropped_samples<=dropped_samples+1'b1;
            end
        end
    end
endmodule
