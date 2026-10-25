/* Preload shim: make the pen's Settings storage follow the VM state disk.
 *
 * 厂商 YSystemInfoAdapter::getStorageInfo 返回的是固件写死的硬件容量
 * （x6pro 上实测是 {"totalSpace":4,"firmware":4036,"resource":60,"user":0,"available":0}），
 * 设置页把 totalSpace 当 GB、user/available 当 MB 用，于是 VM 里永远显示 4GB 且接近占满。
 * 这里用 statvfs("/userdata")（就是状态盘）生成同一份 JSON，不改应用。
 *
 * p5 与 x3s342/x6pro 是同一个适配层：两个 mangled 符号名一致（已在
 * /oem/YoudaoDictPen/output/libs/libyddal_adapter.so 里核对过），AArch64 的
 * std::string 走 x8 返回，所以结构体与返回方式照抄。
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
