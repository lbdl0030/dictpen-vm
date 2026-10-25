/* libvmsn.so —— 给 x3s342 的厂商 HAL store 模块做替身。
 *
 * 应用（libgetdevicename.so）用 yd_hal_store_module_new() 拿一个 0x78 字节的
 * 模块，vtable 里按偏移放回调：
 *   0x18 -> 读设备名（readDeviceName）
 *   0x60 -> 读 device_secret（readDeviceSecret）
 * 回调形如 int cb(char *out)：成功返回 0，内容写进 out。
 *
 * 这台 VM 没有 Rockchip vendor storage，真模块拿不到 SN/secret，应用就带着
 * 字面量 "vendor_storage" 去云端注册。这里用配置文件顶上：
 *   /userdata/cfg/vm_device.conf
 *     device_name=YDPX3s-vm
 *     device_secret=...
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MODULE_SIZE 0x78
#define OFF_NAME 0x18
#define OFF_SECRET 0x60
#define OFF_SCREEN 0x30

static char g_name[64] = "YDPX3s-vm";
static char g_secret[128] = "vmdevsecret0000000000000000000000";
static char g_screen[32] = "800*480";
static int g_loaded;

static void load_conf(void)
{
    FILE *f;
    char line[256];
    if (g_loaded) return;
    g_loaded = 1;
    f = fopen("/userdata/cfg/vm_device.conf", "r");
    if (!f) return;
    while (fgets(line, sizeof line, f)) {
        char *p = strchr(line, '=');
        char *v;
        if (!p) continue;
        *p = 0;
        v = p + 1;
        v[strcspn(v, "\r\n")] = 0;
        if (!strcmp(line, "device_name") && v[0]) snprintf(g_name, sizeof g_name, "%s", v);
        else if (!strcmp(line, "device_secret") && v[0]) snprintf(g_secret, sizeof g_secret, "%s", v);
        else if (!strcmp(line, "screen") && v[0]) snprintf(g_screen, sizeof g_screen, "%s", v);
    }
    fclose(f);
}

static int cb_name(char *out)
{
    load_conf();
    if (out) strcpy(out, g_name);
    return 0;
}

static int cb_secret(char *out)
{
    load_conf();
    if (out) strcpy(out, g_secret);
    return 0;
}

/* 其余槽（0x08 readProductAndDeviceInfo、0x68 writeDeviceSecret 等）留给真实
 * 路径去报错：返回 -1 走调用者的失败分支，比"成功但没填数据"安全 ——
 * 后者会让上层拿到未初始化的结构体。 */
static int cb_screen(char *out)
{
    load_conf();
    if (out) strcpy(out, g_screen);
    return 0;
}

static int cb_fail(void *out)
{
    (void)out;
    return -1;
}

void *yd_hal_store_module_new(void)
{
    void **m = calloc(1, MODULE_SIZE);
    int i;
    if (!m) return NULL;
    for (i = 0; i < MODULE_SIZE / 8; i++)
        m[i] = (void *)cb_fail;
    m[OFF_NAME / 8] = (void *)cb_name;
    m[OFF_SECRET / 8] = (void *)cb_secret;
    m[OFF_SCREEN / 8] = (void *)cb_screen;
    return m;
}

void yd_hal_store_module_destory(void *m)
{
    free(m);
}
