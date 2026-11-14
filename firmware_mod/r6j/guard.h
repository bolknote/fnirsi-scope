/* Version-locked experimental guard. No unallocated global RAM is used. */
#ifndef GUARD_R3_H
#define GUARD_R3_H
typedef unsigned char u8;
typedef signed char s8;
typedef unsigned short u16;
typedef unsigned int u32;
typedef signed int s32;
#define GS ((volatile u8 *)0x200000f8)
#define COEF ((volatile u16 *)(GS+0x260))
#define GV32(a) (*(volatile u32 *)(a))
#define GUARD_ADDRESS 0x20033bc0u
#define GUARD_MAGIC 0x33474e46u
/* Bounds: CH1 is a 12-bit DAC; CH2 uses the native PWM ARR when configured,
 * with the stock 12-bit nominal domain as a conservative boot fallback. */
struct guard {
 u32 magic, active, in_step, cancel, reason, needs_cal, spi_fault;
 u32 seconds, last_frame, last_progress, signature, frames, steps;
 u32 spi_start, spi_budget;
 u32 saved_auto, notice, save_stage, save_code, csv_kind, partial_save;
 u32 bad_index, bad_value, mean_milli[2];
 u16 backup[120];
 u8 alignment[4], settings[0x30];
 u16 window_start, window_count;
 char lines[3][80];
 u32 boot_warned;
 u32 diag_key,diag_asc,diag_ascq,diag_reads,diag_errors,diag_rejected_writes,diag_last_lba,diag_last_opcode;
 u32 r5_context;
 /* Read-only acquisition telemetry. Appended so existing diagnostics retain
  * their offsets. Written only by the native FPGA task, never by USB. */
 u32 spi_commands,spi_sample_reads,spi_last_command,spi_last_start_tick;
 u32 spi_last_end_tick,spi_completed_command,spi_inflight,spi_raw_minmax;
 u8 spi_prefix[16];
 u32 baseline_completed,gain_unverified;
};
_Static_assert(sizeof(struct guard)<=1024,"reserved context overflow");
struct guard *guard_get(void);
struct guard *guard_peek(void);
void guard_reserve_context(void);
void guard_save_error(u32 stage,u32 code,u32 partial);
void guard_save_clear(void);
void guard_abort(u32 reason);
void guard_spi_end(void);
#endif
