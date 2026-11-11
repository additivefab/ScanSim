// Functional example only. Not an AD9268 pin-level receiver, CDC FIFO or DMA engine.
// ADC data has already traversed an external LATENCY-cycle ADC behavioural pipeline.
// Delay acquisition metadata by the SAME latency; never backpressure the real ADC.
module scanner_capture #(
    parameter integer LATENCY = 12,
    parameter integer DEPTH = 32,
    parameter integer ID_W = 16
)(
    input wire clk, input wire rst,
    input wire sample_valid,
    input wire [ID_W-1:0] sample_id,
    input wire [15:0] adc_bus,
    output wire m_valid, input wire m_ready,
    output wire [15:0] m_data,
    output wire [ID_W-1:0] m_id,
    output reg overflow
);
    localparam integer AW = (DEPTH <= 2) ? 1 : $clog2(DEPTH);
    localparam integer CW = $clog2(DEPTH+1);
    reg [LATENCY-1:0] valid_pipe;
    reg [ID_W-1:0] id_pipe [0:LATENCY-1];
    reg [15:0] data_mem [0:DEPTH-1];
    reg [ID_W-1:0] id_mem [0:DEPTH-1];
    reg [AW-1:0] rd_ptr, wr_ptr;
    reg [CW-1:0] count;
    integer k;
    wire pop = m_valid && m_ready;
    wire request = valid_pipe[LATENCY-1];
    wire push = request && ((count < DEPTH) || pop);
    assign m_valid = (count != 0);
    assign m_data = data_mem[rd_ptr];
    assign m_id = id_mem[rd_ptr];
    always @(posedge clk) begin
        if (rst) begin
            valid_pipe <= 0;
            for (k=0;k<LATENCY;k=k+1) id_pipe[k] <= 0;
            rd_ptr <= 0; wr_ptr <= 0; count <= 0; overflow <= 0;
        end else begin
            valid_pipe[0] <= sample_valid;
            id_pipe[0] <= sample_id;
            for (k=1;k<LATENCY;k=k+1) begin
                valid_pipe[k] <= valid_pipe[k-1];
                id_pipe[k] <= id_pipe[k-1];
            end
            if (request && !push) overflow <= 1;
            if (push) begin
                data_mem[wr_ptr] <= adc_bus;
                id_mem[wr_ptr] <= id_pipe[LATENCY-1];
                wr_ptr <= (wr_ptr==DEPTH-1) ? 0 : wr_ptr+1'b1;
            end
            if (pop) rd_ptr <= (rd_ptr==DEPTH-1) ? 0 : rd_ptr+1'b1;
            case ({push,pop})
                2'b10: count <= count+1'b1;
                2'b01: count <= count-1'b1;
                default: count <= count;
            endcase
        end
    end
endmodule
