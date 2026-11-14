#include "guard.h"
#include "features.h"
/* V1.2.0 CSV export. Runs only in the existing screenshot-save action.
 * The device's own filesystem remains the only storage implementation.
 * Screen CSV uses the SAME scale-string lookup as the live UI.
 * Acquisition banks are preserved verbatim, with no guessed sample rate.
 */
typedef unsigned char u8;
typedef signed char s8;
typedef unsigned short u16;
typedef unsigned int u32;
typedef signed int s32;
#define STATE ((volatile u8 *)0x200000f8)
#define SCRATCH ((char *)0x20008360)
#define U16(p) ((u32)(p)[0] | (u32)(p)[1]<<8)

static void *(*const alloc)(u32)=(void *)0x0803acfd;
static void (*const release)(void *)=(void *)0x0803acbd;
static u32 (*const open_file)(void *,const char *,u32)=(void *)0x080348b9;
static u32 (*const write_file)(void *,const void *,u32,u32 *)=(void *)0x08035531;
static void (*const close_file)(void *)=(void *)0x08034015;
static void (*const unlink_scratch)(void)=(void *)0x0803512d;
static u32 (*const stock_bmp)(void)=(void *)0x0803d085;
static u32 (*const stock_bin)(void)=(void *)0x0803ced5;
static void (*const format)(char *,const char *,u32)=(void *)0x080073b5;

struct export {
    u8 state[0xe10];
    u8 file[0x102c];
    char data[512];
    char paths[3][64];
    u32 used, failed, created;
};

static void flush(struct export *e) {
    if (!e->used || e->failed) return;
    u32 written=0;
    u32 status=write_file(e->file,e->data,e->used,&written);
    if (status || written!=e->used) {e->failed=1;guard_save_error(7,status?status:0x100,1);}
    e->used=0;
}
static void character(struct export *e,char c) {
    if (e->failed) return;
    e->data[e->used++]=c;
    if (e->used==sizeof(e->data)) flush(e);
}
static void string(struct export *e,const char *s) {
    while (*s && !e->failed) character(e,*s++);
}
static void number(struct export *e,u32 n) {
    char temp[10];u32 length=0;
    do {temp[length++]=(char)('0'+n%10);n/=10;} while(n);
    while(length) character(e,temp[--length]);
}
static void signed_number(struct export *e,s32 n) {
    if(n<0) {character(e,'-'); number(e,0u-(u32)n);} else number(e,(u32)n);
}
static void decimal_micro(struct export *e,s32 n) {
    if(n<0) {character(e,'-');n=-n;}
    number(e,(u32)n/1000000);character(e,'.');
    u32 v=(u32)n%1000000;
    for(u32 div=100000;div;div/=10) character(e,(char)('0'+v/div%10));
}
static void scientific(struct export *e,u32 value,s32 exponent) {
    number(e,value);character(e,'e');signed_number(e,exponent);
}
static void end(struct export *e) {string(e,"\r\n");}
static void key_number(struct export *e,const char *key,s32 value) {
    string(e,key);character(e,',');signed_number(e,value);end(e);
}
static void key_string(struct export *e,const char *key,const char *value) {
    string(e,key);character(e,',');string(e,value);end(e);
}

static const char *scale(const u8 *s,u32 channel) {
    u32 index=s[2+channel]+3u*s[6+channel];
    if(index>12) return "";
    return ((const char *const *)0x0804c954)[index];
}
static const char *timebase(const u8 *s) {
    if(s[0x2d]>28) return "";
    return ((const char *const *)0x0804c988)[s[0x2d]];
}
static u32 prefix(const char **p) {
    u32 n=0;
    while(**p>='0' && **p<='9') {n=n*10+(u32)(**p-'0');++*p;}
    return n;
}
static u32 millivolts(const char *label) {
    u32 n=prefix(&label);
    if(label[0]=='m' && label[1]=='V') return n;
    if(label[0]=='V') return n*1000;
    return 0;
}
static void screen_data(struct export *e) {
    const u8 *s=e->state;
    const char *t=timebase(s);u32 amount=prefix(&t);s32 exponent=0;
    if(*t=='n') exponent=-9;else if(*t=='u') exponent=-6;else if(*t=='m') exponent=-3;
    else if(*t!='S') amount=0;
    u32 mv[2]={millivolts(scale(s,0)),millivolts(scale(s,1))};
    u32 flags=s[0x34]==1?4:s[0x14];
    string(e,"index,time_s,ch1_v,ch2_v,ch1_screen_code,ch2_screen_code\r\n");
    for(u32 i=0;i<301 && !e->failed;++i) {
        number(e,i);character(e,',');
        /* i * (time/div) / 25 = i * amount * 4 * 10^(exponent-2). */
        if(amount) scientific(e,i*amount*4,exponent-2);
        for(u32 ch=0;ch<2;++ch) {
            character(e,',');
            if((flags&(1u<<ch)) && mv[ch]) {
                s32 code=s[0x356+ch*301+i];
                s32 zero=128+(s8)s[4+ch];
                decimal_micro(e,(code-zero)*(s32)mv[ch]*40);
            }
        }
        for(u32 ch=0;ch<2;++ch) {
            character(e,',');
            if(flags&(1u<<ch)) number(e,s[0x356+ch*301+i]);
        }
        end(e);
    }
}
static void bank_data(struct export *e) {
    string(e,"bank_index,bank0_code,bank1_code\r\n");
    for(u32 i=0;i<1024 && !e->failed;++i) {
        number(e,i);character(e,',');number(e,e->state[0x5b0+i]);
        character(e,',');number(e,e->state[0x9b0+i]);end(e);
    }
}
static void metadata(struct export *e) {
    const u8 *s=e->state;
    string(e,"key,value\r\n");
    key_string(e,"format","fnirsi-2c53t-csv-v1");
    key_string(e,"firmware","1.2.0-RU-REMOTE-r6");
    key_number(e,"screen_points",301);
    struct guard *g=guard_peek();
    key_number(e,"calibration_required",g?g->needs_cal:0);
    key_number(e,"last_calibration_error",g?g->reason:0);
    for(u32 i=0;i<120;i++){
        string(e,"calibration_word_");number(e,i);character(e,',');
        number(e,U16(s+0x260+2*i));end(e);
    }
    key_string(e,"screen_time_origin","left_edge");
    key_string(e,"screen_code_unit","1/25_division");
    key_number(e,"channel_flags",s[0x34]==1?4:s[0x14]);
    key_number(e,"display_mode",s[0x34]);
    key_string(e,"timebase_label",timebase(s));
    key_string(e,"ch1_scale_label",scale(s,0));
    key_string(e,"ch2_scale_label",scale(s,1));
    key_number(e,"ch1_zero_screen_code",128+(s8)s[4]);
    key_number(e,"ch2_zero_screen_code",128+(s8)s[5]);
    key_number(e,"ch1_probe_exponent",s[6]);
    key_number(e,"ch2_probe_exponent",s[7]);
    key_number(e,"timebase_index",s[0x2d]);
    key_number(e,"trigger_mode",s[0x17]);
    key_number(e,"trigger_edge",s[0x18]);
    key_number(e,"trigger_channel",s[0x16]);
    key_number(e,"trigger_position_code",(short)U16(s+0x1a));
    key_number(e,"trigger_level_code",(short)U16(s+0x1c));
    key_number(e,"acquisition_mode",s[0xdbe]);
    key_number(e,"acquisition_channel_config",s[0xdbf]);
    key_number(e,"acquisition_timebase_index",s[0xdc0]);
    key_number(e,"acquisition_ch1_range_index",s[0xdc1]);
    key_number(e,"acquisition_ch2_range_index",s[0xdc2]);
    key_number(e,"acquisition_ch1_offset",(s8)s[0xdc3]);
    key_number(e,"acquisition_ch2_offset",(s8)s[0xdc4]);
    key_number(e,"firmware_window_start",U16(s+0xdb4));
    key_number(e,"firmware_window_count",U16(s+0xdb6));
    key_number(e,"raw_bank_points",1024);
    key_string(e,"raw_layout","firmware_acquisition_banks");
    key_string(e,"raw_values","firmware_processed_uint8");
    key_string(e,"raw_bank_validity","not_inferred");
    key_number(e,"raw_timing_verified",0);
}
static void path(char *out,u32 n,const char *suffix) {
    const char *p="2:/Waveform CSV/";
    while(*p) *out++=*p++;
    if(n>=100) *out++=(char)('0'+n/100);
    if(n>=10) *out++=(char)('0'+n/10%10);
    *out++=(char)('0'+n%10);
    while((*out++=*suffix++));
}
static void remove_path(const char *p) {
    char *out=SCRATCH;
    while((*out++=*p++));
    unlink_scratch();
}

u32 export_csv(u32 n) {
    struct export *e=alloc(sizeof(*e));
    if(!e) {
        guard_save_error(4,0,1);
        char temporary[64];
        path(temporary,n,".csv");remove_path(temporary);
        path(temporary,n,"-raw.csv");remove_path(temporary);
        path(temporary,n,"-meta.csv");remove_path(temporary);
        return 1;
    }
    /* Native f_mkdir returns void; f_open below supplies a checked error.
     * Calling it on an existing folder is harmless. No disk formatting. */
    ((void (*)(const char *))0x08034535)("2:/Waveform CSV");
    u32 saved;
    __asm__ volatile("mrs %0, primask\ncpsid i":"=r"(saved)::"memory");
    for(u32 i=0;i<sizeof(e->state);++i) e->state[i]=STATE[i];
    __asm__ volatile("msr primask, %0"::"r"(saved):"memory");
    path(e->paths[0],n,".csv");
    path(e->paths[1],n,"-raw.csv");
    path(e->paths[2],n,"-meta.csv");
    e->failed=0;e->created=0;
    for(u32 k=0;k<3 && !e->failed;++k) {
        e->used=0;
        u32 code=open_file(e->file,e->paths[k],10);
        if(code) {e->failed=1;guard_save_error(6,code,1);break;}
        ++e->created;
        if(k==0) screen_data(e);else if(k==1) bank_data(e);else metadata(e);
        flush(e);
        /* Stock close synchronizes its file/directory caches and returns void. */
        close_file(e->file);
    }
    u32 failed=e->failed;
    /* Also remove an older companion file if this number was overwritten. */
    if(failed) for(u32 k=0;k<3;++k) remove_path(e->paths[k]);
    release(e);
    return failed;
}

/* Replace the stock SAVE callback, retaining its BMP/BIN and list semantics.
 * CSV failure is reported, but successful BMP/BIN files are kept in the album.
 */
void save_extension(void) {
    guard_save_clear();
    u32 n=STATE[0xe11];
    if(n==255 || !n || STATE[0xe1b]>=254) {guard_save_error(1,n,0);goto error;}
    format(SCRATCH,(const char *)0x080bcad2,n);
    u32 rc=stock_bmp();
    if(rc) {guard_save_error(2,rc,0);unlink_scratch();goto error;}
    rc=stock_bin();
    if(rc) {
        guard_save_error(3,rc,0);
        format(SCRATCH,(const char *)0x080bcad2,n);
        unlink_scratch();goto error;
    }
    u32 failed=export_csv(n);
    u32 count=STATE[0xe1b];
    STATE[0xe25+count]=(u8)n;
    STATE[0xe1b]=(u8)(count+1);
    STATE[0xe10]=failed?3:2;
    goto done;
error:
    STATE[0xe10]=3;
done:
    STATE[0xf68]=2;
    r5_after_save();
}
