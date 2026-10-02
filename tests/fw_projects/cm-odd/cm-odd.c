// Firmware that does unusual things, for the code map emulator tests (tests/test_codemap.py): polls a peripheral flag that never comes, busy waits, halts in "while (1)" after answering, sleeps waiting for an interrupt, traps, calls a null pointer, and sums its input.
#include "hal.h"
#include <stdint.h>
#include <stdlib.h>
#include "simpleserial.h"

#if defined(__arm__)
#define PERIPH ((volatile uint32_t *)0x40021000u)
#elif defined(__riscv)
#define PERIPH ((volatile uint32_t *)0xFFFFFF00u)
#else
#define PERIPH ((volatile uint8_t *)0x0640u)
#endif

volatile uint32_t sink;

uint8_t poll_flag(uint8_t *x, uint8_t len)
{
    trigger_high();
    while (!(*PERIPH & 0x2)) ;   // a ready flag that never comes in emulation
    trigger_low();
    simpleserial_put('r', 1, x);
    return 0;
}

uint8_t delay(uint8_t *x, uint8_t len)
{
    trigger_high();
    for (volatile uint32_t i = 0; i < 20000u * x[0]; i++) ;
    trigger_low();
    simpleserial_put('r', 1, x);
    return 0;
}

uint8_t halt_after(uint8_t *x, uint8_t len)
{
    trigger_high();
    simpleserial_put('r', 1, x);
    while (1) ;
    return 0;
}

uint8_t wait_irq(uint8_t *x, uint8_t len)
{
#if defined(__arm__)
    __asm__ volatile ("cpsie i\n wfi");
#elif defined(__riscv)
    __asm__ volatile ("wfi");
#else
    __asm__ volatile ("sei\n sleep");
#endif
    simpleserial_put('r', 1, x);
    return 0;
}

uint8_t trap(uint8_t *x, uint8_t len)
{
#if defined(__arm__)
    __asm__ volatile ("svc 0");
#elif defined(__riscv)
    __asm__ volatile ("ecall");
#else
    __asm__ volatile ("break");
#endif
    simpleserial_put('r', 1, x);
    return 0;
}

uint8_t null_call(uint8_t *x, uint8_t len)
{
    void (*volatile f)(void) = 0;
    f();
    simpleserial_put('r', 1, x);
    return 0;
}

uint8_t echo_sum(uint8_t *x, uint8_t len)
{
    uint8_t s = 0;
    trigger_high();
    for (int i = 0; i < len; i++) s += x[i];
    trigger_low();
    simpleserial_put('r', 1, &s);
    return 0;
}

int main(void)
{
    platform_init();
    init_uart();
    trigger_setup();
    simpleserial_init();
    simpleserial_addcmd('a', 1, poll_flag);
    simpleserial_addcmd('b', 1, delay);
    simpleserial_addcmd('c', 1, halt_after);
    simpleserial_addcmd('d', 1, wait_irq);
    simpleserial_addcmd('e', 1, trap);
    simpleserial_addcmd('f', 1, null_call);
    simpleserial_addcmd('s', 4, echo_sum);
    while (1)
        simpleserial_get();
}
