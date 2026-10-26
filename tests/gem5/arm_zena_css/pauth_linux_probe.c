/* SPDX-License-Identifier: BSD-3-Clause */
/* Linux userspace PAC checks with real kernel-installed keys and controls. */
#include <asm/hwcap.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/auxv.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>

extern int pauth_branch_recovery(uintptr_t, uintptr_t, unsigned *);

static void
target(void)
{
    __asm__ volatile("" ::: "memory");
}

static uintptr_t
sign_pointer(uintptr_t pointer, uintptr_t modifier)
{
    __asm__ volatile("pacia %0, %1" : "+r"(pointer) : "r"(modifier));
    return pointer;
}

static uintptr_t
authenticate(uintptr_t pointer, uintptr_t modifier)
{
    __asm__ volatile("autia %0, %1" : "+r"(pointer) : "r"(modifier));
    return pointer;
}

int
main(void)
{
    const uintptr_t pointer = (uintptr_t)&target;
    uintptr_t modifier = 1, signed_pointer;
    unsigned flag = 1;
    int status;
    struct rlimit limit = {0, 0};
    if (!(getauxval(AT_HWCAP) & HWCAP_PACA)) {
        puts("PAUTH_LINUX status=SKIP reason=no_HWCAP_PACA");
        return 77;
    }
    /* A particular signature can be zero; try distinct modifiers. */
    do {
        signed_pointer = sign_pointer(pointer, modifier);
    } while (signed_pointer == pointer && ++modifier < 256);
    if (signed_pointer == pointer) {
        puts("PAUTH_LINUX status=SKIP reason=signing_not_observed");
        return 77;
    }
    if (authenticate(signed_pointer, modifier) != pointer) {
        return 1;
    }
    uintptr_t wrong_modifier = modifier + 1;
    uintptr_t invalid = authenticate(signed_pointer, wrong_modifier);
    while (invalid == pointer && ++wrong_modifier < modifier + 256) {
        invalid = authenticate(signed_pointer, wrong_modifier);
    }
    if (invalid == pointer || invalid == signed_pointer) {
        return 2;
    }
    if (pauth_branch_recovery(signed_pointer, modifier, &flag)) {
        return 3;
    }
    if (setrlimit(RLIMIT_CORE, &limit)) {
        return 4;
    }
    pid_t child = fork();
    if (child < 0) {
        return 5;
    }
    if (child == 0) {
        __asm__ volatile("blr %0" ::"r"(invalid) : "x30", "memory");
        _exit(1);
    }
    if (waitpid(child, &status, 0) != child || !WIFSIGNALED(status) ||
        WTERMSIG(status) != SIGSEGV) {
        return 6;
    }
    puts("PAUTH_LINUX status=PASS signed=1 auth=1 reject=1 recovery=1 "
         "fault=SIGSEGV");
    return 0;
}
