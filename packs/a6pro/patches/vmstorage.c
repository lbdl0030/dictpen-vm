/* Preload shim: make the pen's Settings storage follow the VM state disk.
 *
 * a6pro userland is ARM EABI + uClibc (ld-uClibc.so.0), so a glibc-linked
 * shim cannot load.  This file stays libc-free: statfs goes through a raw
 * syscall and allocates via libstdc++ operator new (bound by the loader), so
 * the app's string destructor frees it with the matching operator delete.
 *
 * The vendor adapter returns a fixed 4GB figure; the settings page uses
 * totalSpace (GB), user/available (MB).
 */
typedef struct {
    char *p;
    unsigned long len;
    union {
        unsigned long cap;
        char buf[16];
    } u;
} VmString;

struct kstatfs {
    unsigned long f_type, f_bsize, f_blocks, f_bfree, f_bavail;
    unsigned long f_files, f_ffree, f_fsid[2];
    unsigned long f_namelen, f_frsize, f_flags, f_spare[4];
};

extern void *_Znwj(unsigned int); /* operator new(size_t) */

static long sys_statfs(const char *path, struct kstatfs *st)
{
    register long r0 asm("r0") = (long)path;
    register long r1 asm("r1") = (long)st;
    register long r7 asm("r7") = 99; /* __NR_statfs (ARM EABI) */
    asm volatile("svc #0" : "+r"(r0) : "r"(r1), "r"(r7) : "memory");
    return r0;
}

static char *put_u(char *p, unsigned long v)
{
    char tmp[20];
    int n = 0;
    do {
        tmp[n++] = (char)('0' + (v % 10));
        v /= 10;
    } while (v);
    while (n)
        *p++ = tmp[--n];
    return p;
}

static VmString storage_json(void)
{
    struct kstatfs st;
    unsigned long total_mb = 0, free_mb = 0, used_mb = 0, total_gb = 0;
    if (sys_statfs("/userdata", &st) == 0 && st.f_bsize) {
        unsigned long long total = (unsigned long long)st.f_blocks * st.f_bsize;
        unsigned long long freeb = (unsigned long long)st.f_bavail * st.f_bsize;
        total_mb = (unsigned long)(total >> 20);
        free_mb = (unsigned long)(freeb >> 20);
        used_mb = total_mb > free_mb ? total_mb - free_mb : 0;
        total_gb = (unsigned long)((total + (1ULL << 30) - 1) >> 30);
    }
    char *buf = (char *)_Znwj(256);
    char *p = buf;
    const char *a = "{\"totalSpace\":";
    const char *b = ",\"firmware\":0,\"resource\":0,\"user\":";
    const char *c = ",\"available\":";
    while (*a) *p++ = *a++;
    p = put_u(p, total_gb);
    while (*b) *p++ = *b++;
    p = put_u(p, used_mb);
    while (*c) *p++ = *c++;
    p = put_u(p, free_mb);
    *p++ = '}';
    VmString s;
    s.p = buf;
    s.len = (unsigned long)(p - buf);
    s.u.cap = 255;
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

/* localdict is missing on the VM; keep loadNmtPatch from aborting the app. */
__attribute__((visibility("default")))
void _ZN28YAsyncLocalTranslatorPrivate12loadNmtPatchEv(void *self)
{
    (void)self;
}
