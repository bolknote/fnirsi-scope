#include "features.h"
/* Configuration executes in the ORIGINAL keyboard task. Native handlers may
 * block posting GUI work, so never invoke them from GUI itself or a USB IRQ. */
enum {R6_IDLE,R6_KEY_QUEUED,R6_EXECUTING,R6_FREEZING};
static u32 r6_scope(struct r5_context *a){
 if(GS[0xf68]==10 && a->ui_open){a->ui_open=0;GS[0xf68]=2;}
 /* 7 is the actual native USB Sharing screen (confirmed on the device).
  * Leave that display for remote acquisition; keep the USB connection and
  * system settings intact. Do not call the physical MENU disconnect path. */
 if(GS[0xf68]!=2 && GS[0xf68]!=9 && GS[0xf68]!=7)return R5_NOT_SCOPE;
 a->ui_open=0;GS[0xf68]=2;GS[0x355]=0;return 0;
}
static u32 r6_busy(void){
 struct guard *g=guard_peek();
 return !g || g->active || g->spi_fault || GS[0x2f] || GS[0x30] || GS[0x33];
}
static void r6_done(struct r5_context *a,u32 error){
 a->error=error;a->remote.error=error;a->remote.applied_id=a->command_id;
 if(a->remote.generation&1)++a->remote.generation;
 a->remote.stage=R6_IDLE;a->command_done=a->command_id;
 __asm__ volatile("dmb":::"memory");a->pending=0;
}
static void r6_dispatch(struct r5_context *a){
 if(a->remote.stage!=R6_IDLE)return;
 a->remote.token=0x80u|(a->command_id&0x7f);a->remote.queued_at=r5_tick();
 a->remote.stage=R6_KEY_QUEUED;
 if(!r5_post(0x20002d70,(u8)a->remote.token))r6_done(a,R5_BUSY);
}
static void r6_expire(struct r5_context *a){
 if(a->remote.stage==R6_KEY_QUEUED && (u32)(r5_tick()-a->remote.queued_at)>2000)r6_done(a,R5_TIMEOUT);
}
static void r6_wake(struct r5_context *a){
 /* Native RUN/STOP may leave the scope task asleep. Its semaphore, also
  * used by native position keys, requests reconstruction in that task. */
 if(a->remote.stage==R6_FREEZING)r5_post(0x20002d80,0);
}
static u32 r6_keepalive(const struct r5_context *a){
 return a->remote.usb_seen && (u32)(r5_tick()-a->remote.last_usb_tick)<10000;
}
static void r6_menu(u32 category,u32 row){
 u8 saved[5];for(u32 i=0;i<4;i++)saved[i]=GS[0xf68+i];saved[4]=GS[0x355];
 GS[0xf68]=9;GS[0xf69]=1;GS[0xf6a]=(u8)category;GS[0xf6b]=(u8)row;
 ((void (*)(void))0x0800bd61)();
 for(u32 i=0;i<4;i++)GS[0xf68+i]=saved[i];GS[0x355]=saved[4];
}
static u32 r6_menu_value(u32 category,u32 row,u32 offset,u32 target,u32 attempts){
 for(u32 n=0;GS[offset]!=target && n<attempts;n++)r6_menu(category,row);
 return GS[offset]==target?R5_OK:R5_INVALID;
}
static u32 r6_step(u32 channel,u32 which,u32 offset,s32 target,u32 horizontal){
 u8 saved=GS[0x354];GS[0x354]=(u8)((channel<<7)|which);
 for(u32 n=0;n<201;n++){
  s32 value=(which || offset==0x1c)?(offset==0x1c?(short)*(volatile u16 *)(GS+offset):(s8)GS[offset]):GS[offset];
  if(value==target){GS[0x354]=saved;return 0;}
  u32 fn=horizontal?(value<target?0x0800b1f9:0x0800b7cd):(value<target?0x0800a149:0x0800a901);
  ((void (*)(void))fn)();
  s32 after=(which || offset==0x1c)?(offset==0x1c?(short)*(volatile u16 *)(GS+offset):(s8)GS[offset]):GS[offset];
  if(after==value){GS[0x354]=saved;return R5_INVALID;}
  if((u32)(r5_tick()-r5_get()->remote.queued_at)>20000){GS[0x354]=saved;return R5_TIMEOUT;}
 }
 GS[0x354]=saved;return R5_INVALID;
}
u32 r6_key_event(u32 key){
 if(key<0x80)return 0;
 struct r5_context *a=r5_get();if(!a)return 1;
 if(a->remote.stage!=R6_KEY_QUEUED || key!=a->remote.token || a->pending<16)return 1;
 if((u32)(r5_tick()-a->remote.queued_at)>2000){r6_done(a,R5_TIMEOUT);return 1;}
 u32 op=a->pending,v=a->pending_arg,ch=0;
 if(op!=36 && r6_busy()){r6_done(a,R5_CALIBRATION);return 1;}
 u32 rc=op==36?R5_OK:r6_scope(a);if(rc){r6_done(a,rc);return 1;}
 if(op!=25 && op!=26 && op!=27 && op!=36 && GS[0x2e]){r6_done(a,R5_BUSY);return 1;}
 ++a->remote.generation;a->remote.stage=R6_EXECUTING;
 switch(op){
 case 16:
  if(!v || v>3){rc=R5_INVALID;break;}
  /* Enable before disabling so a two-to-one-channel change never passes
   * through the native forbidden zero-channel configuration. */
  for(ch=0;ch<2;ch++)if((v&(1u<<ch)) && !(GS[0x14]&(1u<<ch)))r6_menu(3+ch,0);
  for(ch=0;ch<2;ch++)if(!(v&(1u<<ch)) && (GS[0x14]&(1u<<ch)))r6_menu(3+ch,0);
  if(GS[0x14]!=v)rc=R5_INVALID;
  else if(!(v&(1u<<GS[0x16]))){GS[0x16]=v==2?1:0;r5_post(0x20002d78,8);}
  break;
 case 17:case 18:
  ch=op-17;if(v>9)rc=R5_INVALID;else rc=r6_step(ch,0,2+ch,(s32)v,0);break;
 case 19:case 20:
  ch=op-19;if(v>1)rc=R5_INVALID;else rc=r6_menu_value(3+ch,1,6+ch,v,1);break;
 case 21:case 22:
  ch=op-21;if(v>1)rc=R5_INVALID;else rc=r6_menu_value(3+ch,2,ch,v,1);break;
 case 23:case 24:
  ch=op-23;if((short)v < -100 || (short)v > 100)rc=R5_INVALID;else rc=r6_step(ch,1,4+ch,(short)v,0);break;
 case 25:
  if(v>1){rc=R5_INVALID;break;}
  if(GS[0x2e])((void (*)(void))0x0800bd61)();
  a->remote.sample_generation_at_run=guard_peek()->spi_sample_reads;
  GS[0x17]=(u8)v;((void (*)(void))0x0800bd61)();
  a->phase=R5_IDLE;a->error=0;a->frame_ready=0;
  if(!GS[0x2e])rc=R5_INVALID;
  break;
 case 26:case 27:
  if(v){rc=R5_INVALID;break;}
  if(r5_active(a))r5_cancel(a);
  if(GS[0x2e])((void (*)(void))0x0800bd61)();
  if(GS[0x2e]){rc=R5_INVALID;break;}
  a->phase=R5_IDLE;a->frame_ready=0;
  if(op==27){a->phase=R5_IDLE;a->remote.stage=R6_FREEZING;a->remote.queued_at=r5_tick();r6_wake(a);}
  break;
 case 28:
  if(v>19)rc=R5_UNSUPPORTED;else rc=r6_step(0,0,0x2d,(s32)v,1);break;
 case 29:
  if(v>2)rc=R5_INVALID;
  else {
   rc=r6_menu_value(5,0,0x17,v,3);
   /* Stock mode selection also starts acquisition. Configuration is STOP-
    * only, so restore STOP using the native action before acknowledging. */
   if(GS[0x2e])((void (*)(void))0x0800bd61)();
  }
  break;
 case 30:
  if(v>1 || !(GS[0x14]&(1u<<v)))rc=R5_INVALID;else rc=r6_menu_value(5,2,0x16,v,1);break;
 case 31:
  if(v>1)rc=R5_INVALID;else rc=r6_menu_value(5,1,0x18,v,1);break;
 case 32:
  if((short)v < -100 || (short)v > 100)rc=R5_INVALID;else rc=r6_step(0,2,0x1c,(short)v,0);break;
 case 33:
  if(v)rc=R5_INVALID;
  else if(a->mode!=R5_SINGLE)rc=R5_INVALID;
  else rc=r5_arm(a,1);
  break;
 case 35:
  if(v)rc=R5_INVALID;
  else {guard_start();if(!guard_peek() || !guard_peek()->active)rc=R5_CALIBRATION;}
  break;
 case 36: {
  struct guard *g=guard_peek();
  if(v)rc=R5_INVALID;
  else if(g && g->active){if(g->in_step)g->cancel=1;else guard_abort(1);}
  break;
 }
 default:rc=R5_INVALID;
 }
 /* This is the same full-redraw scheduler used by physical keys. It runs
  * here in KEY task, never recursively inside the GUI task. */
 ((void (*)(void))0x08012909)();
 if(rc || a->remote.stage!=R6_FREEZING)r6_done(a,rc);
 return 1;
}
static void r6_service(struct r5_context *a){
 r6_expire(a);
 if(a->remote.stage!=R6_FREEZING)return;
 u32 elapsed=r5_tick()-a->remote.queued_at;
 if(elapsed>3000){r6_done(a,R5_TIMEOUT);return;}
 if(elapsed<50 || GS[0x2e] || GS[0xf68]!=2 || r6_busy())return;
 if(((u32 (*)(u32))0x0804206d)(GV32(0x20002d78)) || !(GV32(0x40010c0c)&0x40u))return;
 if(guard_peek()->spi_inflight)return;
 if(guard_peek()->spi_sample_reads==a->remote.sample_generation_at_run){r6_done(a,R5_NO_FRAME);return;}
 if(!*(volatile u16 *)(GS+0xdb6)){r6_done(a,R5_NO_FRAME);return;}
 a->frame_ready=2;__asm__ volatile("dmb":::"memory");
 for(u32 i=0;i<R5_FRAME_BYTES;i++)a->frame[i]=GS[i];
 a->frame_crc=r5_crc(a->frame,R5_FRAME_BYTES);__asm__ volatile("dmb":::"memory");
 ++a->frame_id;a->frame_ready=0;a->last_file=0;a->phase=R5_READY;a->remote.frame_kind=1;
 r6_done(a,0);
}
static void r6_settings(struct r5_context *a,u8 *out){
 for(u32 i=0;i<64;i++)out[i]=0;
 r5_le32(out,0x364c5443);r5_le32(out+4,0x00060001);
 r5_le32(out+8,a->remote.generation);r5_le32(out+12,a->remote.applied_id);
 r5_le32(out+16,a->remote.stage);r5_le32(out+20,a->remote.error);
 for(u32 i=0;i<32;i++)out[24+i]=GS[i];
 out[56]=GS[0x2d];out[57]=GS[0x2e];out[58]=GS[0xf68];out[59]=GS[0xf69];
 out[60]=GS[0xf6a];out[61]=GS[0xf6b];
 struct guard *g=guard_peek();
 out[62]=(u8)((g&&g->active?1:0)|(g&&g->spi_fault?2:0)|(g&&g->needs_cal?4:0)|(GS[0x34]?8:0));
 /* Bit 2 advertises opcode 34; old clients mask it away. */
 out[63]=(u8)(a->filter_mask|4u|(a->remote.frame_kind<<4));
}
