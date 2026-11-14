#include "features.h"
/* Freestanding ARM ABI helper emitted for local aggregate initialization. */
void __aeabi_memclr4(void *p,u32 n){volatile u8 *q=p;while(n--)*q++=0;}
static u32 r5_tick(void){return GV32(0x200062d4);}
static u32 r5_active(const struct r5_context *a){return a->phase==R5_ARMING || a->phase==R5_ARMED || a->phase==R5_SAVING || a->phase==R5_GAP;}
struct r5_context *r5_get(void){
 struct guard *g=guard_peek();if(!g || !g->r5_context)return 0;
 struct r5_context *a=(void *)g->r5_context;return a->magic==R5_MAGIC?a:0;
}
void r5_defaults(struct r5_context *a){
 u8 *p=(void *)a;for(u32 i=0;i<R5_RESERVE;i++)p[i]=0;
 a->magic=R5_MAGIC;a->mode=R5_AUTOSAVE;a->count=1;a->gap_ms=500;a->timeout_ms=30000;a->pretrigger=50;
}
static u32 r5_post(u32 queue,u8 event){return ((u32 (*)(u32,void *,u32))0x08041cf1)(GV32(queue),&event,0);}
static void r5_redraw(void){
 /* This is the stock task-context queue API, not xQueueSendFromISR.
  * USB IRQ publishes only the mailbox; the scope/second task wakes GUI. */
 u32 ipsr;__asm__ volatile("mrs %0,ipsr":"=r"(ipsr));
 if(!ipsr)r5_post(0x20002d6c,R5_EVENT);
}
static void r5_stop_native(void){
 GS[0x2e]=0;
 /* Match the native single-shot stop; no calibration or input-range writes. */
 GV32(0x40011014)=0x10;
 r5_post(0x20002d6c,2);
}
static void r5_error(struct r5_context *a,u32 why){r5_stop_native();a->error=why;a->phase=R5_ERROR;r5_redraw();}
static u32 r5_arm(struct r5_context *a,u32 new_series){
 struct guard *g=guard_peek();
 if(!g || g->active || GS[0x2f] || GS[0x30] || GS[0x33])return R5_CALIBRATION;
 if(g->spi_fault)return R5_CALIBRATION;
 if(GS[0xf68]!=2 && GS[0xf68]!=10)return R5_NOT_SCOPE;
 if(GS[0xf68]==10 && !a->ui_open)return R5_BUSY;
 if(!(GS[0x14]&3))return R5_INVALID;
 /* The stock completed-frame path is confirmed for timebases 0..19. */
 if(GS[0x2d]>=20)return R5_UNSUPPORTED;
 if(a->mode!=R5_SINGLE && GS[0xe1b]+(new_series?(a->mode==R5_SERIES?a->count:1):1)>254)return R5_STORAGE;
 GS[0x2e]=0;GS[0xf68]=2;a->ui_open=0;
 GS[0x17]=2;
 *(volatile short *)(GS+0x1a)=(short)((s32)a->pretrigger*3-150);
 a->frame_ready=0;a->error=0;a->cancel_after_save=0;
 if(new_series)a->completed=0;
 a->phase=R5_ARMING;a->started=r5_tick();
 /* This is the ORIGINAL PLAY/PAUSE action, called from the GUI task. It
  * resets the stock capture window and queues FPGA op 8 for SINGLE. */
 ((void (*)(void))0x0800bd61)();
 if(!GS[0x2e]){a->phase=R5_ERROR;a->error=R5_INVALID;return R5_INVALID;}
 return R5_OK;
}
static void r5_cancel(struct r5_context *a){
 if(a->phase==R5_SAVING){a->cancel_after_save=1;return;}
 r5_stop_native();a->frame_ready=0;a->phase=R5_CANCELLED;a->error=0;
}
/* Called only at the REAL stock SINGLE completion store, not on an arbitrary
 * RUN->STOP change. The snapshot is made after the enclosing native FSM returns. */
void r5_capture_event(void){struct r5_context *a=r5_get();if(a && (a->phase==R5_ARMED || a->phase==R5_ARMING))a->frame_ready=1;}
static u32 r5_crc(const volatile u8 *p,u32 n){
 u32 crc=~0u;for(u32 i=0;i<n;i++){crc^=p[i];for(u32 k=0;k<8;k++)crc=(crc>>1)^((0u-(crc&1))&0xedb88320u);}return ~crc;
}
/* Opcode 34 is a mailbox-only live snapshot. It is completed in the native
 * scope task after reconstruction, without posting KEY or changing RUN.
 * Only the RAM copy is IRQ-masked; CRC and USB transfers are outside it.
 * frame_ready gates 0x5b throughout publication and protects old readers. */
static void r6_live_frame(struct r5_context *a){
 if(a->pending!=34)return;
 struct guard *g=guard_peek();
 if(!g || g->active || g->spi_fault || GS[0x2f] || GS[0x30] || GS[0x33])return;
 if(g->spi_inflight || !GS[0x2e] || GS[0xf68]!=2 ||
    (a->remote.generation&1) || a->remote.stage ||
    g->spi_sample_reads==a->remote.sample_generation_at_run ||
    !*(volatile u16 *)(GS+0xdb6) || GS[0x34] || GS[10] || GS[11])return;
 u32 mask=irq_off();
 if(a->pending!=34 || !GS[0x2e] || GS[0xf68]!=2 || g->spi_inflight){irq_restore(mask);return;}
 a->frame_ready=2;__asm__ volatile("dmb":::"memory");
 for(u32 i=0;i<R5_FRAME_BYTES;i++)a->frame[i]=GS[i];
 irq_restore(mask);
 a->frame_crc=r5_crc(a->frame,R5_FRAME_BYTES);
 a->remote.frame_kind=2;a->phase=R5_READY;a->error=0;
 __asm__ volatile("dmb":::"memory");
 ++a->frame_id;a->frame_ready=0;
 __asm__ volatile("dmb":::"memory");a->command_done=a->command_id;
 __asm__ volatile("dmb":::"memory");a->pending=0;
}
/* A missing trigger or a stopped task must not permanently hold the mailbox.
 * Never cancel after frame_ready has gated the buffer for the copying task. */
static void r6_live_expire(struct r5_context *a){
 if(a && a->pending==34 && a->frame_ready!=2 && (u32)(r5_tick()-a->live_queued_at)>=3000){
  a->error=R5_NO_FRAME;a->command_done=a->command_id;
  __asm__ volatile("dmb":::"memory");a->pending=0;
 }
}
static void r5_poll(struct r5_context *a){
 r6_expire(a);
 u32 now=r5_tick();
 if((a->phase==R5_ARMING || a->phase==R5_ARMED) && !a->frame_ready){
  if((u32)(now-a->started)>=a->timeout_ms)r5_error(a,R5_TIMEOUT);
  else if(a->phase==R5_ARMING && (u32)(now-a->started)>=50 &&
          !((u32 (*)(u32))0x0804206d)(GV32(0x20002d78)) && (GV32(0x40010c0c)&0x40u))a->phase=R5_ARMED;
 }else if(a->phase==R5_GAP && (s32)(now-a->next_arm)>=0 && !a->pending){
  a->pending_arg=0;a->pending=15;r5_redraw();
 }
}
void r5_scope_service(void){
 struct r5_context *a=r5_get();
 ((void (*)(void))0x08020e99)();
 if(!a)return;
 if(a->pending && a->pending!=34)r5_redraw();
 /* The stock STOP path also reconstructs points inline, without calling
  * 242ec. Cover that path too; the CRC guard prevents filtering twice when
  * the normal RUN/completed-SINGLE path already passed through r5_timebase. */
 r5_filter_points();
 r6_live_frame(a);
 u32 now=r5_tick();
 if(a->phase==R5_ARMING || a->phase==R5_ARMED){
  if(a->frame_ready && !GS[0x2e] && GS[0x17]==2){
   /* No USB reader is allowed through while this buffer changes. */
   a->frame_ready=2;__asm__ volatile("dmb":::"memory");
   for(u32 i=0;i<R5_FRAME_BYTES;i++)a->frame[i]=GS[i];
   a->frame_crc=r5_crc(a->frame,R5_FRAME_BYTES);
   __asm__ volatile("dmb":::"memory");
   ++a->frame_id;++a->completed;a->frame_ready=0;a->remote.frame_kind=0;
   a->phase=a->mode==R5_SINGLE?R5_READY:R5_SAVING;
   a->last_file=0;
   r5_redraw();
  }
 }
 (void)now;r5_poll(a);r6_service(a);
}
void r5_after_save(void){
 struct r5_context *a=r5_get();if(!a || a->phase!=R5_SAVING)return;
 struct guard *g=guard_peek();
 if(GS[0xe10]!=2 || (g && g->save_stage)){
  a->error=R5_STORAGE;a->phase=R5_ERROR;
 }else{
  a->last_file=GS[0xe11];a->error=0;
  if(a->cancel_after_save)a->phase=R5_CANCELLED;
  else if(a->mode==R5_SERIES && a->completed<a->count){a->phase=R5_GAP;a->next_arm=r5_tick()+a->gap_ms;}
  else a->phase=R5_READY;
 }
 a->frame_ready=0;
}
/* One outstanding mailbox command, published last with a Cortex-M barrier.
 * USB IRQ never allocates, calls FatFs, changes acquisition, or waits for a task. */
u32 r5_command(u32 operation,u32 argument){
 struct r5_context *a=r5_get();if(!a)return R5_NO_MEMORY;
 if(!operation)return R5_OK;
 if(operation>36 || operation==15)return R5_INVALID;
 if(a->pending)return R5_BUSY;
 if(r5_active(a) && operation!=2 && operation!=26)return R5_BUSY;
 if(operation==34){
  if(argument)return R5_INVALID;
  if(!GS[0x2e] || GS[0xf68]!=2)return R5_NO_FRAME;
  a->live_queued_at=r5_tick();
 }
 a->pending_arg=argument;++a->command_id;
 __asm__ volatile("dmb":::"memory");a->pending=operation;
 r5_redraw();return R5_OK;
}
static void r5_execute(struct r5_context *a){
 u32 op=a->pending,v=a->pending_arg,rc=0;if(!op || op==34)return;
 if(op>=16){r6_dispatch(a);return;}
 /* Keep the mailbox busy throughout execution: an interrupt must not
  * enqueue another command and have its identifier acknowledged early. */
 switch(op){
 case 1:rc=r5_arm(a,1);break;
 case 2:r5_cancel(a);break;
 case 3:if(v>R5_SERIES)rc=R5_INVALID;else a->mode=v;break;
 case 4:if(!v || v>50)rc=R5_INVALID;else a->count=v;break;
 case 5:if(v>10000)rc=R5_INVALID;else a->gap_ms=v;break;
 case 6:if(!v || v>600)rc=R5_INVALID;else a->timeout_ms=v*1000;break;
 case 7:if(v>100)rc=R5_INVALID;else a->pretrigger=v;break;
 case 8:if(v>3)rc=R5_INVALID;else{a->filter_mask=v;a->filter_scale=~0u;}break;
 case 9:if(v>1 || !(GS[0x14]&(1u<<v)))rc=R5_INVALID;else GS[0x16]=(u8)v;break;
 case 10:if(v>1)rc=R5_INVALID;else GS[0x18]=(u8)v;break;
 case 11:if((short)v < -100 || (short)v > 100)rc=R5_INVALID;else *(volatile short *)(GS+0x1c)=(short)v;break;
 case 12:if(v>19)rc=R5_UNSUPPORTED;else GS[0x2d]=(u8)v;break;
 case 13:
  if(!a->frame_id || GS[0x2e])rc=R5_NO_FRAME;
  else{a->phase=R5_SAVING;a->frame_ready=0;}
  break;
 case 14:
  if(GS[0xf68]!=2 && GS[0xf68]!=9 && GS[0xf68]!=10)rc=R5_NOT_SCOPE;
  else{r5_stop_native();GS[0xf68]=10;a->ui_open=1;}
  break;
 case 15:rc=r5_arm(a,0);break;
 default:rc=R5_INVALID;
 }
  if(rc){a->error=rc;if(op==1 || op==15)a->phase=R5_ERROR;}
  else if(op>=3 && op<=12)a->error=0;
 a->command_done=a->command_id;
 __asm__ volatile("dmb":::"memory");a->pending=0;
}
static char *r5_put(char *p,const char *s){while(*s)*p++=*s++;*p=0;return p;}
static char *r5_number(char *p,u32 n){char b[10];u32 k=0;do{b[k++]=(char)('0'+n%10);n/=10;}while(n);while(k)*p++=b[--k];*p=0;return p;}
static void r5_menu_draw(struct r5_context *a){
 u32 ru=GS[0xf60]==3;
 const char *rul[]={"Режим","Число кадров","Пауза, мс","Ожидание, с","До триггера, %","CH1: ФНЧ 20 МГц","CH2: ФНЧ 20 МГц","Пуск","Отмена захвата","Альбом снимков"};
 const char *enl[]={"Mode","Frames","Gap, ms","Timeout, s","Pretrigger, %","CH1: 20 MHz DSP","CH2: 20 MHz DSP","Arm capture","Cancel capture","Photo album"};
 const char *rum[]={"Один кадр","Кадр + запись","Серия + запись"};
 const char *enm[]={"Single","Capture + save","Series + save"};
 /* The native pool also owns acquisition/FatFs buffers. Render ten 24-line
  * strips, waiting for each native DMA completion before reusing the memory. */
 u16 *fb=((void *(*)(u32))0x0803acfd)(320*24*2);if(!fb){a->error=R5_NO_MEMORY;return;}
 u8 old[12];for(u32 i=0;i<12;i++)old[i]=((volatile u8 *)0x20008350)[i];
 volatile u16 *clip=(void *)0x20008350;
 for(u32 strip=0;strip<240;strip+=24){
 clip[0]=0;clip[1]=(u16)strip;clip[2]=320;clip[3]=24;GV32(0x20008358)=(u32)fb;
 for(u32 i=0;i<320*24;i++)fb[i]=0x18c3;
 r5_paint_text(8,2,304,26,ru?"Захват / фильтры r6":"Capture / filters r6",18,0xffff,1);
 u32 first=a->cursor>6?a->cursor-6:0;
 for(u32 row=0;row<7 && first+row<10;row++){
  u32 item=first+row,y=30+25*row,color=item==a->cursor?0x07ff:0xffff;
  if(item==a->cursor)r5_box(4,y,312,24,0x31a6);
  r5_paint_text(8,y,176,24,ru?rul[item]:enl[item],14,color,0);
  char value[40]={0};
  if(item==0)r5_put(value,ru?rum[a->mode]:enm[a->mode]);
  else if(item==1)r5_number(value,a->count);
  else if(item==2)r5_number(value,a->gap_ms);
  else if(item==3)r5_number(value,a->timeout_ms/1000);
  else if(item==4)r5_number(value,a->pretrigger);
  else if(item==5 || item==6){
   u32 ch=item-5,bypass=GS[0x2d]>=6 || GS[0x34] || GS[10+ch];
   r5_put(value,a->filter_mask&(1u<<ch)?(bypass?(ru?"Вкл / обход":"ON / bypass"):(ru?"Вкл (цифр.)":"ON (DSP)")):(ru?"Выкл":"OFF"));
  }
  r5_paint_text(184,y,124,24,value,14,color,1);
 }
 r5_paint_text(4,207,312,15,ru?"↑↓ выбор; ←→ менять; ▶ пуск; MENU назад":"Up/Down select; Left/Right change; PLAY arm; MENU back",12,0xbdf7,1);
 if(a->error){
  const char *rue[]={"","Занято","Неверная настройка","Время ожидания истекло","Ошибка записи / нет места","Калибровка / сбой SPI","Недостаточно памяти","Нет готового кадра","Развёртка: не медленнее 20 мс/дел","Откройте осциллограф"};
  const char *ene[]={"","Busy","Invalid setting","Trigger timeout","Save failed / full","Calibration / SPI fault","Out of memory","No frozen frame","Use 20 ms/div or faster","Open oscilloscope"};
  r5_paint_text(4,222,312,16,a->error<10?(ru?rue[a->error]:ene[a->error]):(ru?"Ошибка":"Error"),12,0xfd20,1);
 }
 else r5_paint_text(4,222,312,16,ru?"20 МГц: данные; вход и триггер без фильтра":"20 MHz: data DSP; input and trigger unfiltered",12,0xbdf7,1);
 ((void (*)(void))0x08042ee1)();
 ((u32 (*)(u32,u32))0x080423a9)(GV32(0x20002d84),0xffffffffu);
 }
 for(u32 i=0;i<12;i++)((volatile u8 *)0x20008350)[i]=old[i];
 ((void (*)(void *))0x0803acbd)(fb);
}
void r5_gui(void){
 struct r5_context *a=r5_get();if(!a)return;
 r5_poll(a);
 r5_execute(a);
 if(a->phase==R5_ERROR && !a->ui_open){GS[0xf68]=10;a->ui_open=1;}
 if(a->phase==R5_SAVING && !a->frame_ready){
  a->frame_ready=3;
  /* Use the native SAVE preparation/progress/filename path in its own GUI
   * task. The existing SAVE adapter reports completion through r5_after_save. */
  ((void (*)(void))0x0800d7e5)();
 }
 if(a->ui_open)r5_menu_draw(a);
}
static void r5_change(struct r5_context *a,s32 direction){
 u32 item=a->cursor;
 if(item==0)a->mode=(a->mode+3+direction)%3;
 else if(item==1){s32 n=(s32)a->count+direction;if(n>=1 && n<=50)a->count=n;}
 else if(item==2){s32 n=(s32)a->gap_ms+direction*100;if(n>=0 && n<=10000)a->gap_ms=n;}
 else if(item==3){s32 n=(s32)a->timeout_ms+direction*5000;if(n>=5000 && n<=600000)a->timeout_ms=n;}
 else if(item==4){s32 n=(s32)a->pretrigger+direction*5;if(n>=0 && n<=100)a->pretrigger=n;}
 else if(item==5 || item==6){a->filter_mask^=1u<<(item-5);a->filter_scale=~0u;}
}
u32 r5_key(u32 key){
 struct r5_context *a=r5_get();if(!a)return key==24?1:0;
 struct guard *g=guard_peek();if(g && g->active)return key==24?1:0;
 if(key==24){if(!r5_active(a))r5_command(14,0);return 1;}
 if(a->ui_open){
  if(key==1){a->ui_open=0;return 0;}
  if(key==10){a->ui_open=0;GS[0xf68]=2;r5_post(0x20002d6c,2);return 1;}
  if(key==11 && a->cursor)a->cursor--;
  else if(key==12 && a->cursor<9)a->cursor++;
  else if(key==13)r5_change(a,-1);
  else if(key==14)r5_change(a,1);
  else if(key==15)r5_command(1,0);
  else if(key==5){
   if(a->cursor==7)r5_command(1,0);
   else if(a->cursor==8)r5_command(2,0);
   else if(a->cursor==9){a->ui_open=0;GS[0xf68]=2;((void (*)(void))0x0800e631)();}
   else r5_change(a,1);
  }
  r5_redraw();return 1;
 }
 if(r5_active(a)){
  if(key==1){r5_cancel(a);return 0;}
  if(key==10 || key==15){r5_command(2,0);return 1;}
  return 1;
 }
 /* Fifth channel row is an actual setting, with bounded cursor handling.
  * The native code still owns rows 0..3 and all other settings. */
 if(GS[0xf68]==9 && GS[0xf69] && (GS[0xf6a]&15)>=3 && (GS[0xf6a]&15)<=4){
  if(key==12 && GS[0xf6b]==3){GS[0xf6b]=4;r5_post(0x20002d6c,20);return 1;}
  if(GS[0xf6b]==4){
   if(key==11)GS[0xf6b]=3;
   else if(key==5 || key==13 || key==14 || key==15){a->filter_mask^=1u<<((GS[0xf6a]&15)-3);a->filter_scale=~0u;}
   else return 0;
   r5_post(0x20002d6c,20);return 1;
  }
 }
 return 0;
}
/* Display-data low-pass, not an invented analog hardware switch. The point
 * spacing is EXACTLY the native scale/25 used by the existing CSV exporter.
 * Coefficients above the displayed Nyquist rate are explicitly bypassed. */
void r5_filter_points(void){
 struct r5_context *a=r5_get();if(!a || !a->filter_mask || GS[0x2f] || GS[0x30] || GS[0x33] || GS[0x34])return;
 u32 scale=GS[0x2d];if(scale>=6)return;
 const struct r5_biquad *c=r5_lpf+scale;
 for(u32 ch=0;ch<2;ch++){
  if(!(a->filter_mask & GS[0x14] & (1u<<ch)) || GS[10+ch])continue;
  volatile u8 *p=GS+0x356+301*ch;
  u32 crc=r5_crc(p,301);
  if(a->filter_scale==scale && a->filter_crc[ch]==crc)continue;
  float x1=p[0],x2=x1,y1=x1,y2=x1;
  for(u32 i=0;i<301;i++){
   float x=p[i],y=c->b0*x+c->b1*x1+c->b2*x2-c->a1*y1-c->a2*y2;
   x2=x1;x1=x;y2=y1;y1=y;
   s32 n=(s32)(y+0.5f);p[i]=(u8)(n<28?28:n>228?228:n);
  }
  a->filter_crc[ch]=r5_crc(p,301);
 }
 a->filter_scale=scale;
}
void r5_timebase(void){((void (*)(void))0x080242ed)();r5_filter_points();}
