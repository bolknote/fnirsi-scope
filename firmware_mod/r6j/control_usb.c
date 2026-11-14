#include "features.h"
/* Vendor IN only. No OUT payload callback, USB stack replacement, MSC detach,
 * synchronous task wait, filesystem call, or arbitrary memory access. */
static u32 r5_u16(const u8 *p){return p[0]|(u32)p[1]<<8;}
static void r5_le32(u8 *p,u32 v){for(u32 i=0;i<4;i++)p[i]=(u8)(v>>(8*i));}
static void r5_status(struct r5_context *a,u8 *p,u32 command_rc){
 r6_live_expire(a);
 for(u32 i=0;i<64;i++)p[i]=0;
 r5_le32(p,R5_MAGIC);r5_le32(p+4,0x00050001);
 r5_le32(p+8,a?a->phase:R5_ERROR);
 r5_le32(p+12,(a?a->error:R5_NO_MEMORY)|(command_rc<<16));
 if(!a)return;
 r5_le32(p+16,a->frame_id);r5_le32(p+20,a->frame_crc);
 r5_le32(p+24,a->completed);r5_le32(p+28,a->mode==R5_SERIES?a->count:1);
 r5_le32(p+32,a->command_id);r5_le32(p+36,a->command_done);
 r5_le32(p+40,a->mode);r5_le32(p+44,a->last_file);
 r5_le32(p+48,R5_FRAME_BYTES);r5_le32(p+52,GV32(0x200062d4));
 r5_le32(p+56,a->filter_mask);r5_le32(p+60,a->pretrigger);
}
/* Fixed read-only register pages; never read SPI DATA (which consumes a byte),
 * never accept a host MMIO address. These registers have no clear-on-read
 * semantics on the version-locked AT32 peripheral map used by stock firmware. */
static u32 r6_probe(struct r5_context *a,u8 *out,u32 page){
 static const u32 regs[2][12]={
  {0x40003c00,0x40003c04,0x40003c08,0x40003c1c,
   0x40021018,0x4002101c,0x40021004,0x40010004,
   0x40003800,0x40003804,0x40003808,0x40021000},
  {0x40010c00,0x40010c04,0x40010c08,0x40010c0c,
   0x40011000,0x40011004,0x40011008,0x4001100c,
   0x40007400,0x40007408,0x4000742c,0x40001c2c}
 };
 if(page>2)return 0;
 for(u32 i=0;i<64;i++)out[i]=0;
 r5_le32(out,0x36425046);r5_le32(out+4,0x00060002);
 r5_le32(out+8,page);r5_le32(out+12,r5_tick());
 if(page<2)for(u32 i=0;i<12;i++)r5_le32(out+16+4*i,GV32(regs[page][i]));
 else{
  struct guard *g=guard_peek();if(!g)return 0;
  const u32 *p=&g->spi_commands;for(u32 i=0;i<8;i++)r5_le32(out+16+4*i,p[i]);
  for(u32 i=0;i<16;i++)out[48+i]=g->spi_prefix[i];
 }
 (void)a;return 64;
}
/* Versioned, read-only calibration state; never expose coefficients or writes. */
static void r6_calibration_status(u8 *out){
 struct guard *g=guard_peek();for(u32 i=0;i<64;i++)out[i]=0;
 r5_le32(out,0x364c4143);r5_le32(out+4,1);
 if(!g)return;
 r5_le32(out+8,g->active);r5_le32(out+12,g->seconds);
 r5_le32(out+16,g->frames);r5_le32(out+20,g->steps);
 r5_le32(out+24,g->reason);r5_le32(out+28,g->needs_cal);
 r5_le32(out+32,g->baseline_completed);r5_le32(out+36,g->gain_unverified);
 r5_le32(out+40,g->in_step);r5_le32(out+44,g->cancel);
 r5_le32(out+48,g->spi_fault);
}
u32 r5_usb_setup(u32 ctx,u32 request){
 const u8 *r=(const void *)request;
 /* Bootstrap diagnostics must work before the optional pool context exists.
  * These are READS of the same fixed MCUFLASH/RAM ranges already exposed by
  * the diagnostic disk. No arbitrary peripheral pointer or write is accepted.
  * The original EP0 sender copies directly from these persistent regions;
  * no stack or MSC bulk reply buffer is used for a deferred USB transfer. */
 if(r[0]==0xc0 && (r[1]==0x5d || r[1]==0x5e)){
  u32 offset=r5_u16(r+2)|(r5_u16(r+4)<<16),n=r5_u16(r+6);
  u32 size=r[1]==0x5d?0x100000u:0x38000u,base=r[1]==0x5d?0x08000000u:0x20000000u;
  if(!n || n>64 || offset>size || n>size-offset)return 0;
  struct r5_context *diag=r5_get();
  if(diag){diag->remote.last_usb_tick=r5_tick();diag->remote.usb_seen=1;}
  *(volatile u16 *)(ctx+0x26)=(u16)n;*(volatile u16 *)(ctx+0x28)=(u16)n;
  *(volatile u8 *)(ctx+0x220)=2;
  ((void (*)(u32,u32,void *,u32))0x08040eb5)(ctx,0,(void *)(base+offset),n);
  return 1;
 }
 if(r[0]!=0xc0 || (r[1]!=0x5a && r[1]!=0x5b && r[1]!=0x5c && r[1]!=0x5f && r[1]!=0x60))return 0;
 struct r5_context *a=r5_get();
 /* A no-context request is refused by the original stack; never transmit
  * a pointer into unreserved RAM. The first UI/image-pool initialization
  * reserves the context before normal USB Sharing is available. */
 if(!a)return 0;
 a->remote.last_usb_tick=r5_tick();a->remote.usb_seen=1;
 u8 *out=a->usb_reply;u32 n=r5_u16(r+6);if(n>64)n=64;
 if(r[1]==0x60){r6_calibration_status(out);if(r5_u16(r+2) || r5_u16(r+4))n=0;}
 else if(r[1]==0x5f){if(r5_u16(r+4) || !r6_probe(a,out,r5_u16(r+2)))n=0;}
 else if(r[1]==0x5c){r6_settings(a,out);if(r5_u16(r+2) || r5_u16(r+4))n=0;}
 else if(r[1]==0x5a){
  u32 rc=r5_command(r5_u16(r+2),r5_u16(r+4));r5_status(a,out,rc);
  if(a->phase==R5_ARMING || a->phase==R5_ARMED || a->phase==R5_SAVING || a->phase==R5_GAP)r5_redraw();
 }
 else{
  u32 offset=r5_u16(r+2),id=r5_u16(r+4);
  if(!a->frame_id || a->frame_ready==2 || id!=(a->frame_id&65535) || offset>R5_FRAME_BYTES || n>R5_FRAME_BYTES-offset)n=0;
  else for(u32 i=0;i<n;i++)out[i]=a->frame[offset+i];
 }
 /* Same EP0 fields/state as the native GET_MAX_LUN data response. */
 *(volatile u16 *)(ctx+0x26)=(u16)n;*(volatile u16 *)(ctx+0x28)=(u16)n;
 *(volatile u8 *)(ctx+0x220)=2;
 ((void (*)(u32,u32,void *,u32))0x08040eb5)(ctx,0,out,n);
 return 1;
}
