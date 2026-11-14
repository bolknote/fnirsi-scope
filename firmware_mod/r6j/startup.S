.syntax unified
.cpu cortex-m4
.thumb
.text

.global draw_startup
.thumb_func
draw_startup:
    /* Enter after the stock LCD and backlight initialization. Match the
     * registers and blanked backlight of the stock custom-logo path. */
    movs r4, #0
    ldr r5, =0x40015034
    ldr r6, =0x200000f8
    str r4, [r5]

    /* Stock cold-boot LCD blitter: (x, y, width, height, RGB565 pixels). */
    movs r0, #0
    movs r1, #0
    movw r2, #320
    movs r3, #240
    sub sp, #8
    ldr r12, =startup_pixels
    str.w r12, [sp]
    ldr r12, =0x0803a23d
    blx r12
    add sp, #8

    /* Restore the configured brightness and hold the picture with the
     * original boot delay. The builder skips the now-unused file cleanup. */
    ldr lr, =0x0802c065
    bx lr
    .ltorg

.section .rodata.startup,"a",%progbits
.balign 4
.global startup_pixels
startup_pixels:
    .incbin "startup.rgb565"
