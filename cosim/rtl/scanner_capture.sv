// Functional ADC-domain capture. The synchronized rising edge sets counter zero.
// The sample index and line number travel atomically with the captured ADC word.
module scanner_capture #(
    parameter integer WINDOW_START = 2,
    parameter integer WINDOW_LENGTH = 5
)(
    input wire clk, input wire rst, input wire line_sync,
    input wire [15:0] code_in,
    output reg out_valid, output reg [15:0] code_out,
    output reg [63:0] sample_out, output reg [31:0] line_out,
    output reg first_out, output reg last_out,
    output reg [31:0] ignored_syncs
);
    localparam integer WINDOW_END = WINDOW_START + WINDOW_LENGTH - 1;
    reg sync_meta, sync_ff, sync_previous, busy;
    reg [63:0] sample_count;
    reg [31:0] position, next_line, active_line;
    wire rising = sync_ff && !sync_previous;
    wire keep = (busy && position >= WINDOW_START)
             || (!busy && rising && WINDOW_START == 0);

    initial begin
        if (WINDOW_START < 0 || WINDOW_LENGTH <= 0)
            $fatal(1, "invalid capture window");
    end

    always @(posedge clk) begin
        if (rst) begin
            sync_meta <= 0;
            sync_ff <= 0;
            sync_previous <= 0;
            busy <= 0;
            sample_count <= 64'd0;
            position <= 0;
            next_line <= 0;
            active_line <= 0;
            ignored_syncs <= 0;
            out_valid <= 0;
            code_out <= 0;
            sample_out <= 64'd0;
            line_out <= 0;
            first_out <= 0;
            last_out <= 0;
        end else begin
            sync_meta <= line_sync;
            sync_ff <= sync_meta;
            sync_previous <= sync_ff;
            sample_count <= sample_count + 64'd1;
            out_valid <= keep;
            if (keep) begin
                code_out <= code_in;
                sample_out <= sample_count;
                line_out <= busy ? active_line : next_line;
                first_out <= !busy || position == WINDOW_START;
                last_out <= busy ? position == WINDOW_END : WINDOW_END == 0;
            end
            if (busy) begin
                if (rising) ignored_syncs <= ignored_syncs + 1;
                if (position == WINDOW_END) busy <= 0;
                else position <= position + 1;
            end else if (rising) begin
                active_line <= next_line;
                next_line <= next_line + 1;
                position <= 1;
                busy <= WINDOW_END != 0;
            end
        end
    end
endmodule
