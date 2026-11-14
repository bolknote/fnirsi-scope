#ifndef FEATURES_R5_H
#define FEATURES_R5_H
#include "guard.h"
#define R5_MAGIC 0x35465043u
#define R5_RESERVE 4096u
#define R5_EVENT 126u
#define R5_FRAME_BYTES 0xe10u
enum {R5_IDLE,R5_ARMING,R5_ARMED,R5_SAVING,R5_READY,R5_GAP,R5_ERROR,R5_CANCELLED};
enum {R5_SINGLE,R5_AUTOSAVE,R5_SERIES};
enum {R5_OK,R5_BUSY,R5_INVALID,R5_TIMEOUT,R5_STORAGE,R5_CALIBRATION,R5_NO_MEMORY,R5_NO_FRAME,R5_UNSUPPORTED,R5_NOT_SCOPE};
struct r6_remote {u32 generation,applied_id,stage,token,queued_at,error,frame_kind,last_usb_tick,usb_seen,sample_generation_at_run;};
struct r5_context {
 u32 magic,phase,error,mode,count,gap_ms,timeout_ms,pretrigger;
 u32 filter_mask,cursor,ui_open,completed,frame_id,frame_crc,last_file;
 u32 started,next_arm,pending,pending_arg,command_id,command_done,cancel_after_save;
 u32 filter_generation,filter_crc[2],filter_scale,frame_ready;
 u8 usb_reply[64];
 u8 frame[R5_FRAME_BYTES];
 struct r6_remote remote;
 /* Appended to preserve all r6i context offsets. */
 u32 live_queued_at;
};
_Static_assert(sizeof(struct r5_context)<=R5_RESERVE,"r5 reservation overflow");
void r5_defaults(struct r5_context *a);
static u32 r5_post(u32 queue,u8 event);
struct r5_context *r5_get(void);
u32 r5_key(u32 key);
void r5_gui(void);
void r5_scope_service(void);
void r5_after_save(void);
void r5_filter_points(void);
u32 r5_usb_setup(u32 ctx,u32 req);
void r5_text(u32,u32,u32,u32,const char *,u32,u32,u32);
void r5_paint_text(u32,u32,u32,u32,const char *,u32,u32,u32);
void r5_scope_panel(void);
void r5_image(const char *,u32,u32);
u32 r5_command(u32 operation,u32 argument);
static void r6_dispatch(struct r5_context *a);
static void r6_service(struct r5_context *a);
u32 r6_key_event(u32 key);
static void r6_settings(struct r5_context *a,u8 *out);
static void r6_expire(struct r5_context *a);
static void r6_wake(struct r5_context *a);
static u32 r6_keepalive(const struct r5_context *a);
#endif
