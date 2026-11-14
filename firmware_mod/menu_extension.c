/* FNIRSI 2C53T V1.2.0: add the existing Russian language as a third choice.
 * No hardware access. Original first/second rows and text engine are retained.
 * Addresses are specific to SHA256 a17c5c35...eec0c760, load base 08007000.
 */
typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;

#define V8(a) (*(volatile u8 *)(a))
#define V16(a) (*(volatile u16 *)(a))
#define V32(a) (*(volatile u32 *)(a))
#define LANGUAGE V8(0x20001058)
#define FOCUS V8(0x20001061)
#define CATEGORY V8(0x20001062)
#define CURSOR V8(0x20001063)

static const char russian[] = "Русский";
static void (*const image)(u32, u32, u32) = (void *)0x08039f6d;
static void (*const text)(u32, u32, u32, u32, const char *, u32, u32, u32)
    = (void *)0x0800f155;

/* Respect the stock clipping rectangle, including partial screen refreshes. */
static void fill(u32 x, u32 y, u32 w, u32 h, u16 color) {
    u32 cx = V16(0x20008350), cy = V16(0x20008352);
    u32 cw = V16(0x20008354), ch = V16(0x20008356);
    u32 xe = x + w, ye = y + h;
    if (x < cx) x = cx;
    if (y < cy) y = cy;
    if (xe > cx + cw) xe = cx + cw;
    if (ye > cy + ch) ye = cy + ch;
    for (; y < ye; ++y) {
        volatile u16 *row = (volatile u16 *)V32(0x20008358) + (y-cy)*cw;
        for (u32 xx=x; xx<xe; ++xx) row[xx-cx] = color;
    }
}

void settings_extension(void) {
    ((void (*)(void))0x0801cf51)();
    if ((CATEGORY & 15) != 0) return;
    u32 selected = FOCUS != 0 && CURSOR == 2;
    u32 checked = LANGUAGE == 3;
    if (selected) {
        image(0x080bc499, 0x79, 0xc1);
        image(checked ? 0x080bc54a : 0x080bc5fc,
              checked ? 0xa6 : 0xa5, 0xc9);
    } else {
        fill(0x7a, 0xc2, 182, 32, 0x18c3);
        image(checked ? 0x080bc4f2 : 0x080bc5cf, 0xa6, 0xcb);
    }
    text(0xb6, 0xc1, 0x5c, 0x22, russian, 0x20001114,
         V16(0x0804c180 + (selected ? 4 : 2)), 1);
}

void popup_extension(void) {
    ((void (*)(void))0x0801f325)();
    u32 selected = LANGUAGE == 3;
    if (selected) image(0x080bc482, 0x44, 0xae);
    else fill(0x45, 0xaf, 182, 24, 0x18c3);
    text(0x44, 0xae, 0xb8, 0x1a, russian, 0x20001144,
         V16(0x0804c180 + (selected ? 4 : 2)), 1);
}
