/* Mid-function hooks preserve caller-saved GPRs, VFP registers and FPSCR.
 * Instructions needed on return are read from the local stock image. */
#include "build/stock_replay.h"
#define NAKED __attribute__((naked,used))
#define SAVE "push {r0-r3,r12,lr}\n sub sp,#8\n vmrs r0,fpscr\n str r0,[sp]\n mrs r0,apsr\n str r0,[sp,#4]\n vpush {d0-d7}\n"
#define RESTORE "vpop {d0-d7}\n ldr r0,[sp]\n vmsr fpscr,r0\n ldr r0,[sp,#4]\n msr APSR_nzcvq,r0\n add sp,#8\n pop {r0-r3,r12,lr}\n"
NAKED void native_pool_init(unsigned op){__asm__ volatile(STOCK_POOL_INIT "ldr pc,=0x0803ab17\n .ltorg\n");}
/* The actual cold boot clears the allocator map INLINE and sets its ready
 * flag, so it never enters native_pool_init. Reserve our blocks immediately
 * after that store, before later boot/RTOS users can allocate. No VFP at this
 * early point: only integer state is touched by the reservation routine. */
NAKED void hook_boot_pool(void){__asm__ volatile(
 "push {r0-r3,r12,lr}\n mrs r0,apsr\n push {r0,r1}\n"
 "bl guard_reserve_context\n pop {r0,r1}\n msr APSR_nzcvq,r0\n"
 "pop {r0-r3,r12,lr}\n ldr pc,=0x08035f49\n .ltorg\n");}
NAKED void native_settings_save(unsigned mode){__asm__ volatile(STOCK_SETTINGS_SAVE "ldr pc,=0x080293c3\n .ltorg\n");}
NAKED void native_second(void){__asm__ volatile(STOCK_SECOND_PUSH "ldr r12,=0x08009c79\n blx r12\n ldr pc,=0x080406cf\n .ltorg\n");}
NAKED void hook_step(void){__asm__ volatile(SAVE
 "bl guard_before_step\n cmp r0,#0\n beq 1f\n" RESTORE
 "ldr r6,=0x200000f8\n mov r10,r6\n ldr pc,=0x080240a5\n"
 "1:\n" RESTORE STOCK_CAL_STEP "ldr pc,=0x08022aeb\n .ltorg\n");}
NAKED void hook_after_step(void){__asm__ volatile(SAVE "bl guard_after_step\n" RESTORE
 STOCK_CAL_AFTER_STEP "ldr pc,=0x080240ab\n .ltorg\n");}
NAKED void hook_key(void){__asm__ volatile(SAVE
 "ldrb.w r0,[sp,#103]\n bl r5_key\n cmp r0,#0\n bne 3f\n"
 "ldrb.w r0,[sp,#103]\n bl guard_key\n cmp r0,#2\n beq 2f\n cmp r0,#0\n beq 1f\n" RESTORE "ldr pc,=0x08040039\n"
 "1:\n" RESTORE STOCK_KEY "ldr pc,=0x0804007d\n"
 "2:\n" RESTORE "ldr pc,=0x0804008d\n"
 "3:\n" RESTORE "ldr pc,=0x08040039\n .ltorg\n");}
/* Consume remote tokens before SAVE/calibration gates and the physical-key
 * callback table. Ordinary keys replay the original eight-byte prefix. */
NAKED void hook_remote_key(void){__asm__ volatile(SAVE
 "ldrb.w r0,[sp,#103]\n bl r6_key_event\n cmp r0,#0\n beq 1f\n" RESTORE
 "ldr pc,=0x08040045\n1:\n" RESTORE
 STOCK_REMOTE_KEY "ldr pc,=0x08040061\n .ltorg\n");}
NAKED void hook_boot(void){__asm__ volatile(SAVE "bl guard_boot_validate\n cmp r0,#0\n beq 1f\n" RESTORE
 "b 2f\n1:\n" RESTORE "movs r0,#0\n2:\n" STOCK_BOOT "ldr pc,=0x0802d1b1\n .ltorg\n");}
NAKED void hook_spi_begin(void){__asm__ volatile(SAVE "ldrb.w r0,[sp,#150]\n bl guard_spi_begin\n cmp r0,#1\n beq 2f\n cmp r0,#2\n beq 3f\n" RESTORE
 STOCK_SPI_BEGIN "ldr pc,=0x0803e4cb\n"
 "2:\n" RESTORE "ldr pc,=0x0803e49b\n"
 "3:\n" RESTORE "mov.w r2,#0\n" STOCK_SPI_BEGIN_OFFSET "ldr pc,=0x0803e4cb\n .ltorg\n");}
/* Centering must not learn multiplicative gain from an unknown input.
 * Preserve the native centering path with nominal/bypassed gain instead. */
NAKED void hook_gain_auto(void){__asm__ volatile(SAVE "bl guard_gain_invalid\n cmp r0,#0\n bne 1f\n" RESTORE
 STOCK_GAIN_AUTO "ldr pc,=0x0800d979\n"
 "1:\n" RESTORE STOCK_GAIN_AUTO_R5_LOW STOCK_GAIN_AUTO_R5_HIGH
 STOCK_GAIN_AUTO_R8_LOW STOCK_GAIN_AUTO_R8_HIGH "ldr pc,=0x0800da3b\n .ltorg\n");}
/* These are the three stock post-acquisition gain gates. A bad gain takes
 * the already existing zero-sentinel path. No extra code runs per SPI byte.
 * Replay flags/registers exactly on valid gain values. */
NAKED void hook_gain_rolling(void){__asm__ volatile(SAVE "bl guard_gain_bypass\n cmp r0,#0\n bne 1f\n" RESTORE
 STOCK_GAIN_ROLLING "ldr pc,=0x0803eb3d\n"
 "1:\n" RESTORE "mov.w r3,#0\n" STOCK_GAIN_ROLLING_STORE "ldr pc,=0x0803eb3d\n .ltorg\n");}
NAKED void hook_gain_ch1(void){__asm__ volatile(SAVE "bl guard_gain_bypass\n cmp r0,#0\n bne 1f\n" RESTORE
 STOCK_GAIN_CH1 "ldr pc,=0x0803edc9\n"
 "1:\n" RESTORE "mov.w r3,#0\n" STOCK_GAIN_CH1_FINISH "ldr pc,=0x0803edc9\n .ltorg\n");}
NAKED void hook_gain_ch2(void){__asm__ volatile(SAVE "bl guard_gain_bypass\n cmp r0,#0\n bne 1f\n" RESTORE
 STOCK_GAIN_CH2 "ldr pc,=0x0803f099\n"
 "1:\n" RESTORE "mov.w r0,#0\n" STOCK_GAIN_CH2_FINISH "ldr pc,=0x0803f099\n .ltorg\n");}
/* Release CS before collecting telemetry. Register and flags restoration is
 * exact; the native queue pointer and next receive remain unchanged. */
NAKED void hook_spi_end(void){__asm__ volatile(
 STOCK_SPI_END_MASK STOCK_SPI_END_RELEASE SAVE "bl guard_spi_end\n" RESTORE
 STOCK_SPI_END_QUEUE "ldr pc,=0x0803e4ab\n .ltorg\n");}
NAKED void hook_nominal(void){__asm__ volatile(SAVE "bl guard_nominal_loaded\n" RESTORE
 STOCK_NOMINAL "ldr pc,=0x0802d1c7\n .ltorg\n");}

/* Preserve stock mass-storage behavior for LUN0 and standard setup requests. */
NAKED unsigned native_scsi(unsigned ctx){__asm__ volatile(STOCK_SCSI "ldr pc,=0x08030213\n .ltorg\n");}
NAKED unsigned native_usb_setup(unsigned ctx,unsigned request){__asm__ volatile(STOCK_USB_SETUP "ldr pc,=0x0803107f\n .ltorg\n");}

NAKED void native_text(u32 x,u32 y,u32 w,u32 h,const char *s,u32 font,u32 color,u32 flags){__asm__ volatile(STOCK_TEXT "ldr pc,=0x0800f15b\n .ltorg\n");}
NAKED void native_scope_panel(void){__asm__ volatile(STOCK_SCOPE_PANEL "ldr pc,=0x0801333f\n .ltorg\n");}
NAKED void native_image(const char *p,u32 x,u32 y){__asm__ volatile(STOCK_IMAGE "ldr pc,=0x08039f73\n .ltorg\n");}
NAKED void hook_capture_complete(void){__asm__ volatile(STOCK_CAPTURE_COMPLETE SAVE "bl r5_capture_event\n" RESTORE "ldr pc,=0x08021623\n .ltorg\n");}
void r5_gui_dispatch(u32 event){
 if(event==R5_EVENT)r5_gui();
 else ((void (*const *)(void))0x0804be74)[event]();
}
