#include "guard.h"
#include "features.h"
/* The main image pool is allocated downward in 32-byte blocks. We reserve its
 * first 1024 bytes after the native initializer returns. The RTOS heap is NOT
 * reduced. This address and all entry points are tied to the stock SHA256. */
extern void native_pool_init(u32);
extern void native_second(void);
extern void native_settings_save(u32);
static void *(*const ga)(u32)=(void *)0x0803acfd;
static void (*const gf)(void *)=(void *)0x0803acbd;
static void (*const range1)(u32)=(void *)0x080088a5;
static void (*const range2)(u32)=(void *)0x08008a59;
static u32 irq_off(void) {u32 x;__asm__ volatile("mrs %0, primask\ncpsid i":"=r"(x)::"memory");return x;}
static void irq_restore(u32 x) {__asm__ volatile("msr primask, %0"::"r"(x):"memory");}
struct guard *guard_peek(void) {
 struct guard *g=(void *)GUARD_ADDRESS;
 return *(volatile u8 *)0x20001080 && g->magic==GUARD_MAGIC?g:0;
}
void guard_reserve_context(void) {
 u32 mask=irq_off();
 if(*(volatile u8 *)0x20001080) {
  struct guard *g=ga(1024);
  if((u32)g==GUARD_ADDRESS) {
   volatile u8 *p=(void *)g;for(u32 i=0;i<1024;i++)p[i]=0;
   g->magic=GUARD_MAGIC;
   struct r5_context *a=ga(R5_RESERVE);
   if(a){r5_defaults(a);g->r5_context=(u32)a;}
  }
  /* Never write a fixed address if allocation did not return that address. */
 }
 irq_restore(mask);
}
void guard_pool_init(u32 op) {
 u32 mask=irq_off();
 native_pool_init(op);
 if(op==0)guard_reserve_context();
 irq_restore(mask);
}
struct guard *guard_get(void) {
 if(!*(volatile u8 *)0x20001080) {void *p=ga(32);if(p)gf(p);}
 return guard_peek();
}
static u32 upper(u32 ch) {
 if(!ch)return 4095;
 u32 arr=GV32(0x40001c2c);
 return arr>0 && arr<65535?arr:4095;
}
/* The stock SPI task divides by (gain_byte - 28) / 150. Its existing
 * 0/255 sentinel checks miss 28, so a saved value of 28 saturates otherwise
 * valid samples. Use the native bypass for every invalid sentinel without
 * changing the saved calibration bytes or the original valid-gain math. */
u32 guard_gain_invalid(void) {
 u32 a=GS[0x352],b=GS[0x353];
 return a<=28 || a==255 || b<=28 || b==255;
}
/* Baseline calibration targets raw codes 28/228. Applying an unverified
 * multiplicative correction amplifies the feedback error, although these
 * endpoint codes are fixed points of the original correction formula. */
u32 guard_gain_bypass(void) {
 struct guard *g=guard_peek();
 return GS[0x30] || guard_gain_invalid() || (g && g->gain_unverified);
}
static u32 valid_table(u32 pairs) {
 for(u32 i=0;i<120;i++) {
  u32 v=COEF[i];
  if(!v || v>upper(i/60)) {
   struct guard *g=guard_peek();if(g){g->bad_index=i;g->bad_value=v;}
   return 0;
  }
 }
 if(pairs)for(u32 ch=0;ch<2;ch++)for(u32 i=0;i<30;i++)
  if(COEF[ch*60+i]>=COEF[ch*60+i+30])return 0;
 return 1;
}
static u32 signature(void) {return GS[0x30]|GS[0x14]<<8|GS[4]<<16|GS[5]<<24;}
static void refresh(u32 op) {
 /* Existing task queues, non-blocking. A full queue already contains work. */
 u8 b=(u8)op;
 ((u32 (*)(u32,void *,u32))0x08041cf1)(GV32(0x20002d6c),&b,0);
}
static void acquisition(u32 op) {
 u8 b=(u8)op;
 ((u32 (*)(u32,void *,u32))0x08041cf1)(GV32(0x20002d78),&b,0);
}
static void restore_data(struct guard *g) {
 for(u32 i=0;i<120;i++)COEF[i]=g->backup[i];
 for(u32 i=0;i<4;i++)GS[0x350+i]=g->alignment[i];
 for(u32 i=0;i<0x30;i++)GS[i]=g->settings[i];
 *(volatile u16 *)(GS+0xdb4)=g->window_start;
 *(volatile u16 *)(GS+0xdb6)=g->window_count;
 GS[0x30]=0;GS[0x2f]=0;GS[0x33]=0;GS[0x2e]=0;GS[0x32]=0;
 g->active=0;g->in_step=0;g->cancel=0;
}
static void restore(struct guard *g) {
 u32 mask=irq_off();restore_data(g);irq_restore(mask);
 /* Do not restore acquisition heap pointers: the initializer can free them. */
 GV32(GS[0]?0x40011410:0x40011414)=0x1000;
 GV32(GS[1]?0x40011410:0x40011414)=0x2000;
 range1(GS[2]);range2(GS[3]);
 if(!g->spi_fault){acquisition(2);acquisition(1);acquisition(8);}
 refresh(4);refresh(5);refresh(2);refresh(6);
}
static void notice(struct guard *g,u32 why) {
 g->reason=why;g->notice=1;GS[0xe10]=3;GS[0xe24]=0;GS[0xf68]=2;
}
void guard_abort(u32 why) {
 struct guard *g=guard_peek();if(!g)return;
 if(g->active)restore(g);
 notice(g,why);
}
void guard_start(void) {
 struct guard *g=guard_get();
 if(!g){GS[0xe10]=3;return;}
 if(g->active || GS[0xf68]!=2)return;
 if(g->spi_fault){notice(g,6);return;}
 if(!valid_table(1)){g->needs_cal=1;notice(g,7);return;}
 u32 mask=irq_off();
 for(u32 i=0;i<120;i++)g->backup[i]=COEF[i];
 for(u32 i=0;i<4;i++)g->alignment[i]=GS[0x350+i];
 for(u32 i=0;i<0x30;i++)g->settings[i]=GS[i];
 g->window_start=*(volatile u16 *)(GS+0xdb4);
 g->window_count=*(volatile u16 *)(GS+0xdb6);
 g->boot_warned=1;
 g->active=1;g->in_step=0;g->cancel=0;g->reason=0;g->notice=0;
 g->seconds=0;g->frames=0;g->steps=0;g->last_frame=0;g->last_progress=0;
 GS[0xe10]=0;
 irq_restore(mask);
 ((void (*)(void))0x0800df59)();
 g->signature=signature();
}
/* Native F32->F64/comparison helpers preserve the exact original asymmetric
 * acceptance window. C calculations are float32, without FMA contraction. */
static u32 acceptable(float error) {
 union {float f;u32 u;} x={error};
 unsigned long long d=((unsigned long long (*)(u32))0x08045d71)(x.u);
 return !((u32 (*)(unsigned long long,unsigned long long))0x08045df1)(d,0x3fd3333333333333ULL)
     && !((u32 (*)(unsigned long long,unsigned long long))0x08045e0d)(d,0xbfb999999999999aULL);
}
static u32 predict(struct guard *g,u32 ch,u32 bank,u32 group,u32 range) {
 u32 sum=0;for(u32 i=0;i<500;i++)sum+=GS[0x6aa+1024*bank+i];
 g->mean_milli[ch]=sum*2;
 float err=(float)sum/500.0f-(GS[4+ch]==(u8)-100?28.0f:228.0f);
 if(acceptable(err))return 1;
 u32 i=ch*60+group*10+range+(GS[4+ch]==100?30:0);
 s32 k=COEF[i];
 if(err>0.0f && err<1.0f)--k;
 else if(err<0.0f && err>-1.0f)++k;
 else if(err>=-1.5f && err<=1.5f)k=(s32)((float)k-err);
 else k=(s32)((float)k-8.0f*err);
 if(k<=0 || (u32)k>upper(ch)){g->bad_index=i;g->bad_value=(u32)k;return 0;}
 return 1;
}
u32 guard_before_step(void) {
 if(!GS[0x30])return 0;
 struct guard *g=guard_peek();
 /* Only calibration started through the guarded callback is allowed. */
 u32 locked=irq_off();
 if(!g || !g->active){GS[0x30]=0;GS[0x2e]=0;irq_restore(locked);return 1;}
 g->in_step=1;
 irq_restore(locked);
 if(g->cancel){guard_abort(g->cancel);return 1;}
 g->last_frame=g->seconds;g->frames++;
 if(GS[0x32]<40)return 0;
 u32 phase=GS[0x30], p=phase&0x3f, range=p&15;
 if(p==1){g->in_step=1;return 0;}
 u32 group=(p>>4)-1;
 if(group>2 || range>9 || !valid_table(0)){guard_abort(7);return 1;}
 u32 okay=1;
 if(group<2) {
  okay=predict(g,0,0,group,range);
  if(okay && !(phase&0x40))okay=predict(g,1,1,group,range);
 } else {
  if(GS[0x14]==1)okay=predict(g,0,0,2,range);
  else if(GS[0x14]==2)okay=predict(g,1,1,2,range);
  else okay=0;
 }
 if(!okay){guard_abort(2);return 1;}
 g->in_step=1;g->steps++;return 0;
}
void guard_after_step(void) {
 struct guard *g=guard_peek();if(!g || !g->active)return;
 g->in_step=0;
 if(g->cancel){guard_abort(g->cancel);return;}
 u32 s=signature();if(s!=g->signature){g->signature=s;g->last_progress=g->seconds;}
}
void guard_finish(void) {
 struct guard *g=guard_peek();
 if(g && g->active) {
  if(g->cancel || !valid_table(1)){guard_abort(g->cancel?g->cancel:7);return;}
  g->active=0;g->in_step=0;g->baseline_completed=1;
  g->needs_cal=g->gain_unverified || guard_gain_invalid();g->reason=0;
  if(g->needs_cal)notice(g,10);
 }
 ((void (*)(void))0x08025fc1)();
}
u32 guard_boot_validate(void) {
 struct guard *g=guard_get();u32 ok=valid_table(1);
 if(g)g->baseline_completed=ok;
 if(g && guard_gain_invalid())g->gain_unverified=1;
 if(g && (!ok || g->gain_unverified))g->needs_cal=1;
 return ok;
}
void guard_second(void) {
 struct guard *g=guard_peek();u32 was_active=g && g->active;
 struct r5_context *running=r5_get();
 u32 suppress_idle=was_active || (running && (running->phase==R5_ARMING || running->phase==R5_ARMED || running->phase==R5_SAVING || running->phase==R5_GAP));
 if(running && r6_keepalive(running))suppress_idle=1;
 u8 previous=GS[0xf65];
 if(suppress_idle)GS[0xf65]=0;
 /* Battery/critical-power handling is retained. Its settings writer is guarded. */
 native_second();
 if(suppress_idle)GS[0xf65]=previous;
 if(!g)return;
 if(g->active) {
  ++g->seconds;
  if(g->seconds>=600)g->cancel=5;
  else if(g->seconds-g->last_frame>=10)g->cancel=3;
  else if(g->seconds-g->last_progress>=60)g->cancel=4;
  if(g->cancel && !g->in_step)guard_abort(g->cancel);
 }
 if(g->needs_cal && !g->boot_warned && !g->active && GS[0xf68]==2 && !GS[0xe10]) {
  g->boot_warned=1;notice(g,8);
 }
 if(g->notice)GS[0xe24]=0;
 struct r5_context *a=r5_get();
 if(a)r6_wake(a);
 if(a && ((a->pending && a->pending!=34) || a->ui_open || a->phase==R5_ARMING || a->phase==R5_ARMED || a->phase==R5_SAVING || a->phase==R5_GAP))
  r5_post(0x20002d6c,R5_EVENT);
}
u32 guard_key(u32 key) {
 struct guard *g=guard_peek();if(!g)return 0;
 if(g->active && key==1)return 2; /* Bypass gate; guarded native shutdown restores backup atomically. */
 if(g->active && key==10) {
  if(g->in_step)g->cancel=1;else guard_abort(1);
  return 1;
 }
 g->notice=0;return 0;
}
void guard_before_settings_save(u32 mode) {
 struct guard *g=guard_peek();
 if(g && g->active) {
  /* The stock shutdown writer disables IRQs before flash and never returns.
   * Extend that critical section through backup restoration and serialization;
   * no RTOS blocking operation occurs in this prefix. */
  irq_off();restore_data(g);
 }
 /* A missing gain already persists its own invalid sentinel. Do not erase
  * the valid DAC table merely to record that separate gain problem. For a
  * nominal table fallback, retain the existing 'not calibrated' marker. */
 if(g && g->gain_unverified){GS[0x352]=0;GS[0x353]=0;}
 if(g && g->needs_cal && !g->baseline_completed)COEF[119]=0;
 native_settings_save(mode); /* Stock writer shuts down; it does not return. */
}
void guard_save_clear(void) {
 struct guard *g=guard_get();if(g){g->save_stage=0;g->save_code=0;g->partial_save=0;g->notice=0;}
}
void guard_save_error(u32 stage,u32 code,u32 partial) {
 struct guard *g=guard_peek();if(g){g->save_stage=stage;g->save_code=code;g->partial_save=partial;}
}
/* Bounded SPI3 waits; a timeout leaves chip-select high and STOP active.
 * We don't invent a peripheral reset sequence: restart is required afterwards. */
u32 guard_spi_begin(u32 command) {
 struct guard *g=guard_peek();
 if(g){g->spi_start=GV32(0x200062d4);g->spi_budget=g->active;
  ++g->spi_commands;g->spi_last_command=command;
  g->spi_last_start_tick=g->spi_start;g->spi_inflight=1;}
 if(g && g->spi_fault)return 1;
 if(guard_gain_invalid()){if(g){g->needs_cal=1;g->gain_unverified=1;}return 2;}
 if(g && g->gain_unverified)return 2;
 if(GS[0x30])return 2;
 return 0;
}
void guard_spi_end(void) {
 struct guard *g=guard_peek();if(!g)return;
 u32 command=g->spi_last_command;
 g->spi_last_end_tick=GV32(0x200062d4);g->spi_completed_command=command;
 /* Native commands 3/4/5 update rolling points or acquisition banks. Count
  * only after chip select has been released at the original common tail. */
 if(command>=3 && command<=5 && !g->spi_fault){
  u32 lo=255,hi=0;
  for(u32 i=0;i<16;i++){u8 b=GS[0x5b0+i];g->spi_prefix[i]=b;if(b<lo)lo=b;if(b>hi)hi=b;}
  g->spi_raw_minmax=lo|(hi<<8);__asm__ volatile("dmb":::"memory");
  ++g->spi_sample_reads;
 }
 g->spi_inflight=0;
}
u32 guard_spi_wait(u32 bit) {
 u32 start=GV32(0x200062d4), n=0;
 struct guard *g=guard_peek();
 while(!(GV32(0x40003c08)&(1u<<bit))) {
  if((u32)(GV32(0x200062d4)-start)>=100 || ++n>=1000000)goto fault;
  if(g && g->spi_budget && (u32)(GV32(0x200062d4)-g->spi_start)>=1000)goto fault;
 }
 if(g && g->spi_budget && (u32)(GV32(0x200062d4)-g->spi_start)>=1000)goto fault;
 return 1;
fault:
 if(g){g->spi_fault=1;if(g->active){g->cancel=6;}else notice(g,6);}
 GS[0x2e]=0;return 0;
}
static char *put(char *p,const char *s) {while(*s)*p++=*s++;*p=0;return p;}
static char *digits(char *p,u32 v) {char b[10];u32 n=0;do{b[n++]=(char)('0'+v%10);v/=10;}while(v);while(n)*p++=b[--n];*p=0;return p;}
static char *mean(char *p,u32 v) {p=digits(p,v/1000);*p++='.';*p++=(char)('0'+v/100%10);*p++=(char)('0'+v/10%10);*p=0;return p;}
static void lines(struct guard *g,u32 x,u32 y,u32 w,u32 color) {
 for(u32 i=0;i<3;i++)((void (*)(u32,u32,u32,u32,const char *,u32,u32,u32))0x0800f155)
  (x,y+3+i*23,w,23,g->lines[i],0x20001114,color,1);
}
void guard_progress_text(u32 x,u32 y,u32 w,u32 h,const char *s,u32 font,u32 color,u32 flags) {
 struct guard *g=guard_peek();
 if(!g || !g->active){((void (*)(u32,u32,u32,u32,const char *,u32,u32,u32))0x0800f155)(x,y,w,h,s,font,color,flags);return;}
 u32 ru=GS[0xf60]==3;u32 p=GS[0x30]&0x3f,stage=p>>4;if(!stage){stage=1;p=0;}
 char *a=put(g->lines[0],ru?"Калибр. ":"Cal. ");a=digits(a,stage);a=put(a,"/3 #");digits(a,(p&15)+1);
 a=mean(g->lines[1],g->mean_milli[0]);a=put(a," / ");mean(a,g->mean_milli[1]);
 a=digits(g->lines[2],g->seconds);put(a,ru?"с MENU: стоп":"s MENU: stop");
 lines(g,x,y,w,color);
}
void guard_notice_text(u32 x,u32 y,u32 w,u32 h,const char *s,u32 font,u32 color,u32 flags) {
 struct guard *g=guard_peek();u32 ru=GS[0xf60]==3;
 if(!g || (!g->notice && !g->save_stage)) {
  ((void (*)(u32,u32,u32,u32,const char *,u32,u32,u32))0x0800f155)(x,y,w,h,s,font,color,flags);return;
 }
 if(g->notice) {
  put(g->lines[0],g->reason==10?(ru?"Нуль откалиброван":"Baseline calibrated"):g->reason==8?(ru?"Нужна калибровка":"Calibration needed"):(ru?"Калибровка: стоп":"Calibration stopped"));
  const char *why;
  switch(g->reason) {
   case 1:why=ru?"Отменено MENU":"Cancelled by MENU";break;
   case 2:why=ru?"Предел поправки":"Coefficient limit";break;
   case 3:why=ru?"Нет отсчётов":"No fresh samples";break;
   case 4:why=ru?"Нет продвижения":"No progress";break;
   case 5:why=ru?"Истекло 10 минут":"10 minute limit";break;
   case 6:why=ru?"SPI: перезапуск":"SPI: restart needed";break;
   case 8:why=g->gain_unverified || guard_gain_invalid()?(ru?"Усиление: номинал":"Gain: nominal"):(ru?"Загружен шаблон":"Nominal table loaded");break;
   case 10:why=ru?"Усиление: номинал":"Gain: nominal";break;
   default:why=ru?"Ошибка таблицы":"Invalid calibration";break;
  }
  put(g->lines[1],why);
  put(g->lines[2],g->reason==10?(ru?"Точность не проверена":"Accuracy unverified"):g->reason==8?(ru?"Удерж. AUTO: нуль":"Hold AUTO: baseline"):g->reason==7?(ru?"Нужен перезапуск":"Restart the device"):(ru?"Откат. СТОП":"Restored; STOP mode"));
 } else {
  put(g->lines[0],g->partial_save?(ru?"BMP/BIN: готовы":"BMP/BIN saved"):(ru?"Ошибка записи":"SAVE failed"));
  const char *kind;
  switch(g->save_stage){case 1:kind=ru?"Номер":"INDEX";break;case 2:kind="BMP";break;case 3:kind="BIN";break;
   case 4:kind=ru?"CSV: память":"CSV RAM";break;case 5:kind=ru?"CSV: папка":"CSV DIR";break;case 6:kind=ru?"CSV: открыть":"CSV OPEN";break;default:kind=ru?"CSV: запись":"CSV WRITE";break;}
  char *p=put(g->lines[1],kind);p=put(p,ru?" код ":" code ");digits(p,g->save_code);
  put(g->lines[2],ru?"Проверьте память":"Check storage");
 }
 lines(g,x,y,w,color);
}
/* The original scanner accepts every extension. R2 CSV companions therefore
 * produce duplicate album entries. Ignore old CSVs in-place, without deleting
 * user files. New exports live in a separate root folder. */
u32 guard_scan_album(const char *folder) {
 u8 *dir=ga(0x30),*info=ga(0x118);u32 n=0;u8 seen[32];
 for(u32 i=0;i<32;i++)seen[i]=0;
 if(!dir || !info){if(dir)gf(dir);if(info)gf(info);return 255;}
 u32 rc=((u32 (*)(void *,const char *))0x08034c41)(dir,folder);
 if(!rc) {
  while(!((u32 (*)(void *,void *))0x08034f21)(dir,info) && info[0x16]) {
   const char *s=(const char *)info+0x16;u32 id=0,valid=1;
   if(*s<'0' || *s>'9')continue;
   while(*s>='0' && *s<='9'){id=id*10+*s++-'0';if(id>254){valid=0;break;}}
   if(!valid || !id || s[0]!='.' || (s[1]|32)!='b' || (s[2]|32)!='i' || (s[3]|32)!='n' || s[4])continue;
   if(seen[id/8]&(1u<<(id%8)))continue;
   ((void (*)(char *,const char *,u32))0x080073b5)((void *)0x20008360,(const char *)0x080bcad2,id);
   if(((u32 (*)(void))0x08035a09)())continue;
   seen[id/8]|=(1u<<(id%8));GS[0xe25+n++]=(u8)id;
  }
  ((void (*)(void *))0x080341c5)(dir);
 }
 gf(dir);gf(info);
 if(rc)return 255;
 for(u32 i=1;i<n;i++){u8 v=GS[0xe25+i];u32 j=i;while(j && GS[0xe24+j]>v){GS[0xe25+j]=GS[0xe24+j];--j;}GS[0xe25+j]=v;}
 GS[0xe11]=n?(u8)(GS[0xe24+n]+1):1;
 return n;
}
/* Invalid settings headers enter the nominal block before the last-word
 * validator. Mark this path too, rather than silently calling it calibrated. */
void guard_nominal_loaded(void) {struct guard *g=guard_get();if(g){g->needs_cal=1;g->baseline_completed=0;g->gain_unverified=1;}}
