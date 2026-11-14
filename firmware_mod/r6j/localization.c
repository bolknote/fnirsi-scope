#include "features.h"
extern void native_text(u32,u32,u32,u32,const char *,u32,u32,u32);
extern void native_scope_panel(void);
extern void native_image(const char *,u32,u32);
/* resources.c is generated from a separately licensed, complete Noto subset. */
static u32 r5_same(const char *a,const char *b){while(*a && *a==*b){a++;b++;}return *a==*b;}
static u32 r5_utf8(const char **s){
 const u8 *p=(const void *)*s;u32 c=*p++;
 if(c>=0xc0 && c<0xe0){if(!*p)return 0;c=(c&31)<<6|(*p++&63);}
 else if(c>=0xe0 && c<0xf0){if(!p[0] || !p[1])return 0;c=(c&15)<<12|(p[0]&63)<<6|(p[1]&63);p+=2;}
 *s=(const char *)p;return c;
}
static const struct r5_glyph *r5_find(u32 code,u32 size){
 const struct r5_glyph *g=size==18?r5_glyph_18:size==16?r5_glyph_16:size==14?r5_glyph_14:r5_glyph_12;
 for(u32 i=0;i<sizeof(r5_glyph_18)/sizeof(r5_glyph_18[0]);i++)if(g[i].code==code)return g+i;
 return g+('?'-32);
}
static u32 r5_width(const char *s,const char *end,u32 size){u32 n=0;while(s<end && *s){u32 c=r5_utf8(&s);n+=r5_find(c,size)->advance;}return n;}
/* Returns the next line; a UTF-8 character is never split. */
static const char *r5_line(const char *s,u32 width,u32 size,const char **end){
 const char *p=s,*space=0;u32 n=0;
 while(*p && *p!='\n'){
  const char *before=p;u32 c=r5_utf8(&p),a=r5_find(c,size)->advance;
  if(c==' ')space=before;
  if(n+a>width && before!=s){
   if(space && space>s){*end=space;return space+1;}
   *end=before;return before;
  }
  n+=a;
 }
 *end=p;return *p=='\n'?p+1:p;
}
static void r5_pixel(s32 x,s32 y,u32 color,u32 alpha){
 const volatile u16 *v=(const void *)0x20008350;
 if(x<(s32)v[0] || y<(s32)v[1] || x>=(s32)(v[0]+v[2]) || y>=(s32)(v[1]+v[3]))return;
 volatile u16 *p=(void *)GV32(0x20008358);if(!p)return;
 p+=(y-v[1])*v[2]+x-v[0];
 if(alpha==15){*p=(u16)color;return;}
 u32 old=*p,r=(((color>>11)&31)*alpha+((old>>11)&31)*(15-alpha)+7)/15;
 u32 g=(((color>>5)&63)*alpha+((old>>5)&63)*(15-alpha)+7)/15;
 u32 b=((color&31)*alpha+(old&31)*(15-alpha)+7)/15;
 *p=(u16)(r<<11|g<<5|b);
}
static void r5_box(u32 x,u32 y,u32 w,u32 h,u32 color){for(u32 j=0;j<h;j++)for(u32 i=0;i<w;i++)r5_pixel(x+i,y+j,color,15);}
void r5_paint_text(u32 x,u32 y,u32 w,u32 h,const char *s,u32 preferred,u32 color,u32 flags){
 if(!s || !w || !h)return;
 const volatile u16 *clip=(const void *)0x20008350;
 if(x+w<=clip[0] || y+h<=clip[1] || x>=clip[0]+clip[2] || y>=clip[1]+clip[3])return;
 u32 size=preferred>=18?18:preferred>=16?16:preferred>=14?14:12;
 u32 lines=1;
 for(;;){
  const char *p=s,*end;lines=0;
  while(*p && lines<64){p=r5_line(p,w,size,&end);lines++;}
  if(lines*(size+3)<=h || size==12)break;
  size=size==18?16:size==16?14:12;
 }
 u32 height=size+3,top=y+(h>lines*height?(h-lines*height)/2:0);
 const u8 *bitmap=size==18?r5_font_18:size==16?r5_font_16:size==14?r5_font_14:r5_font_12;
 while(*s && top+size<=y+h){
  const char *end,*next=r5_line(s,w,size,&end);u32 width=r5_width(s,end,size);
  s32 pen=x+((flags&1) && w>width?(w-width)/2:0),baseline=top+size;
  while(s<end){
   u32 c=r5_utf8(&s);const struct r5_glyph *g=r5_find(c,size);
   for(u32 j=0;j<g->h;j++)for(u32 i=0;i<g->w;i++){
    s32 px=pen+g->left+i,py=baseline+g->top+j;u32 at=j*g->w+i;
    if(px<(s32)x || py<(s32)y || px>=(s32)(x+w) || py>=(s32)(y+h))continue;
    u32 a=(bitmap[g->offset+at/2]>>((at&1)?0:4))&15;
    if(a)r5_pixel(px,py,color,a);
   }
   pen+=g->advance;
  }
  s=next;top+=height;
 }
}
const char *r5_translate(const char *s){
 if(!s)return s;
 for(u32 i=0;i<sizeof(r5_translations)/sizeof(r5_translations[0]);i++)if(r5_same(s,r5_translations[i].old))return r5_translations[i].new;
 return s;
}
void r5_text(u32 x,u32 y,u32 w,u32 h,const char *s,u32 font,u32 color,u32 flags){
 if(GS[0xf60]!=3 || !s){native_text(x,y,w,h,s,font,color,flags);return;}
 const char *replacement=r5_translate(s);u32 russian=replacement!=s;
 for(const char *p=replacement;*p && !russian;){u32 c=r5_utf8(&p);if(c>=0x400 && c<=0x4ff)russian=1;}
 if(!russian){native_text(x,y,w,h,s,font,color,flags);return;}
 r5_paint_text(x,y,w,h,replacement,font==0x20001144?18:16,color,flags);
}
/* Five channel settings, including the completed DSP bandwidth control. */
void r5_scope_panel(void){
 u32 category=GS[0xf6a]&15;
 if(category!=3 && category!=4){native_scope_panel();return;}
 u32 ch=category-3;struct r5_context *a=r5_get();u32 ru=GS[0xf60]==3;
 const char *labels_ru[]={"Канал","Щуп","Связь","БПФ","ФНЧ 20 МГц"};
 const char *labels_en[]={"Channel","Probe","Coupling","FFT","20 MHz DSP"};
 r5_box(121,60,184,180,0x18c3);
 for(u32 row=0;row<5;row++){
  u32 selected=GS[0xf69] && GS[0xf6b]==row;
  u32 color=selected?0x055f:0xffff,y=61+row*31;
  if(selected)r5_box(122,y,182,30,0x31a6);
  r5_paint_text(126,y,106,30,ru?labels_ru[row]:labels_en[row],14,color,0);
  const char *value;
  if(row==0)value=(GS[0x14]&(1u<<ch))?(ru?"Вкл":"ON"):(ru?"Выкл":"OFF");
  else if(row==1)value=GS[6+ch]==0?"X1":GS[6+ch]==1?"X10":"X100";
  else if(row==2)value=GS[ch]?"DC":"AC";
  else if(row==3)value=GS[10+ch]?(ru?"Вкл":"ON"):(ru?"Выкл":"OFF");
  else value=a && (a->filter_mask&(1u<<ch))?((GS[0x2d]>=6 || GS[0x34] || GS[10+ch])?(ru?"Обход":"Bypass"):(ru?"Вкл":"ON")):(ru?"Выкл":"OFF");
  r5_paint_text(232,y,66,30,value,14,color,1);
 }
 r5_paint_text(124,219,178,18,ru?"Цифровой фильтр данных":"Digital data filter",12,0xbdf7,1);
}
/* Stock asset audit: only RUN/STOP and REF contain English operational text.
 * Brand artwork, electrical units and standard waveform symbols are retained. */
void r5_image(const char *path,u32 x,u32 y){
 native_image(path,x,y);
 if(GS[0xf60]!=3 || !path)return;
 const char *p=path,*name=path;while(*p){if(*p=='/')name=p+1;p++;}
 if(r5_same(name,"2.jpg") || r5_same(name,"4.jpg")){
  u32 run=name[0]=='2';r5_box(x+89,y+1,42,18,run?0x03a0:0x7800);
  r5_paint_text(x+83,y,54,20,run?"ПУСК":"СТОП",14,0xffff,1);
 }else if(r5_same(name,"118.jpg") || r5_same(name,"119.jpg")){
  r5_box(x+9,y+1,36,18,0x0802);r5_paint_text(x+1,y,50,20,"ОПОР",12,0xffff,1);
 }
}
