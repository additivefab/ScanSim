// Metadata delay. clk is the ADC sample clock; advance once per physical sample.
// LATENCY must be the measured sample-to-data latency expressed in this clock.
// This is NOT a source-synchronous ADC receiver or an asynchronous FIFO.
module tag_delay #(parameter integer LATENCY=12, parameter integer TAG_W=32)(
    input wire clk, input wire rst,
    input wire valid_in, input wire [TAG_W-1:0] tag_in,
    output wire valid_out, output wire [TAG_W-1:0] tag_out
);
    reg [LATENCY-1:0] vp;
    reg [TAG_W-1:0] tags [0:LATENCY-1];
    integer k;
    initial if (LATENCY < 1) $fatal(1,"LATENCY must be >= 1");
    always @(posedge clk) begin
        if (rst) begin
            vp <= {LATENCY{1'b0}};
            for(k=0;k<LATENCY;k=k+1) tags[k] <= 0;
        end else begin
            vp[0] <= valid_in;
            tags[0] <= tag_in;
            for(k=1;k<LATENCY;k=k+1) begin
                vp[k] <= vp[k-1]; tags[k] <= tags[k-1];
            end
        end
    end
    assign valid_out=vp[LATENCY-1];
    assign tag_out=tags[LATENCY-1];
endmodule
