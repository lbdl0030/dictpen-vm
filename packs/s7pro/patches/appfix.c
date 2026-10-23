/* s7pro app-side bypasses, one preload library.
 *
 * Both symbols are STB_GLOBAL in .dynsym and the owning libraries are not built
 * with -Bsymbolic, so LD_PRELOAD interposes them (verified: the parentcontrol
 * stub logs its `this` pointer on every timer tick).
 *
 * 1) ParentControl: its 10 s timer calls connectServer() on the asio thread
 *    named asio_for_timer.  With no cloud in the VM the websocketpp send throws
 *    and nothing on that thread catches it, so miniapp aborts every 16 s.
 * 2) License: the pen re-verifies its license about five minutes after boot.
 *    The VM has no yd_misc_info partition ("[ERROR] yd_misc_info open fail"),
 *    so verification fails and the launcher parks the user on 开始激活 with no
 *    way back.  Treat the installed dev license as valid, same as the x7 shim.
 */

#define _GNU_SOURCE
#include <fcntl.h>
#include <stdio.h>
#include <time.h>
#include <unistd.h>

__attribute__((visibility("default")))
void _ZN7YDALAPI21YParentControlPrivate13connectServerEv(void *self)
{
    static int n;
    if (n++ < 4) {
        int fd = open("/userdata/applog/nopc.log", O_WRONLY | O_CREAT | O_APPEND, 0644);
        if (fd >= 0) {
            struct timespec t;
            clock_gettime(CLOCK_REALTIME, &t);
            dprintf(fd, "stub t=%ld self=%p" "\x0a", (long)t.tv_sec, self);
            close(fd);
        }
    }
}

__attribute__((visibility("default")))
unsigned char _ZN11YADAPTERAPI18YSystemInfoAdapter13verifyLicenseERKNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEEE(void *self, const void *license)
{
    (void)self;
    (void)license;
    return 1;
}

__attribute__((visibility("default")))
unsigned char _ZN11YADAPTERAPI8Internal25YSystemInfoAdapterPrivate13verifyLicenseERKNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEEE(void *self, const void *license)
{
    (void)self;
    (void)license;
    return 1;
}
