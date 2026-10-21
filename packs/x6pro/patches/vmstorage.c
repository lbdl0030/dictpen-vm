/* Preload shim: make the pen's Settings storage follow the VM state disk.
 *
 * The vendor adapter returns a fixed hardware figure, typically
 * {"totalSpace":4,"firmware":4036,"resource":60,"user":0,"available":0}.
 * The settings page uses totalSpace (GB), user/available (MB), so the VM
 * state-disk size/free can be substituted here without touching the app.
 */
#include <stdio.h>
#include <stdlib.h>
#include <sys/statvfs.h>

typedef struct {
    char *p;
    unsigned long len;
    union {
        unsigned long cap;
        char buf[16];
    } u;
} VmString;

static VmString storage_json(void)
{
    struct statvfs v;
    unsigned long total_mb = 0, free_mb = 0, used_mb = 0, total_gb = 0;
    if (statvfs("/userdata", &v) == 0) {
        unsigned long long total = (unsigned long long)v.f_blocks * v.f_frsize;
        unsigned long long freeb = (unsigned long long)v.f_bavail * v.f_frsize;
        total_mb = (unsigned long)(total >> 20);
        free_mb = (unsigned long)(freeb >> 20);
        used_mb = total_mb > free_mb ? total_mb - free_mb : 0;
        total_gb = (unsigned long)((total + (1ULL << 30) - 1) >> 30);
    }
    char *buf = (char *)malloc(256);
    int n = snprintf(buf, 256,
                     "{\"totalSpace\":%lu,\"firmware\":0,\"resource\":0,"
                     "\"user\":%lu,\"available\":%lu}",
                     total_gb, used_mb, free_mb);
    VmString s;
    s.p = buf;
    s.len = n > 0 ? (unsigned long)n : 0;
    s.u.cap = s.len;
    return s;
}

__attribute__((visibility("default")))
VmString _ZN11YADAPTERAPI18YSystemInfoAdapter14getStorageInfoB5cxx11Ev(void *self)
{
    (void)self;
    return storage_json();
}

__attribute__((visibility("default")))
VmString _ZN11YADAPTERAPI8Internal25YSystemInfoAdapterPrivate14getStorageInfoB5cxx11Ev(void *self)
{
    (void)self;
    return storage_json();
}
