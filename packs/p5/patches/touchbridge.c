#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <sys/ioctl.h>
#include <linux/input.h>
#include <linux/uinput.h>

/* QEMU 的 virtio-tablet 只有 BTN_LEFT + 绝对坐标；miniapp 的输入链要的是
 * 一块单点触摸屏（BTN_TOUCH + ABS_X/ABS_Y）。这里把 tablet 的事件翻译过去。
 * 用法：touchbridge [源节点] [宽] [高] */

static int emit(int fd, int type, int code, int value)
{
    struct input_event ev;
    memset(&ev, 0, sizeof ev);
    ev.type = type;
    ev.code = code;
    ev.value = value;
    return write(fd, &ev, sizeof ev) == sizeof ev ? 0 : -1;
}

static int setup_device(int w, int h)
{
    int fd = open("/dev/uinput", O_WRONLY | O_NONBLOCK);
    if (fd < 0) {
        perror("open /dev/uinput");
        return -1;
    }
    ioctl(fd, UI_SET_EVBIT, EV_SYN);
    ioctl(fd, UI_SET_EVBIT, EV_KEY);
    ioctl(fd, UI_SET_EVBIT, EV_ABS);
    ioctl(fd, UI_SET_KEYBIT, BTN_TOUCH);
    ioctl(fd, UI_SET_ABSBIT, ABS_X);
    ioctl(fd, UI_SET_ABSBIT, ABS_Y);
    ioctl(fd, UI_SET_PROPBIT, INPUT_PROP_DIRECT);

    struct uinput_abs_setup abs;
    memset(&abs, 0, sizeof abs);
    abs.absinfo.minimum = 0;
    abs.code = ABS_X;
    abs.absinfo.maximum = w;
    ioctl(fd, UI_ABS_SETUP, &abs);
    abs.code = ABS_Y;
    abs.absinfo.maximum = h;
    ioctl(fd, UI_ABS_SETUP, &abs);

    struct uinput_setup usetup;
    memset(&usetup, 0, sizeof usetup);
    usetup.id.bustype = BUS_VIRTUAL;
    usetup.id.vendor = 0x0001;
    usetup.id.product = 0x0001;
    usetup.id.version = 1;
    snprintf(usetup.name, UINPUT_MAX_NAME_SIZE, "vm-touch");
    if (ioctl(fd, UI_DEV_SETUP, &usetup) < 0) {
        perror("UI_DEV_SETUP");
        close(fd);
        return -1;
    }
    if (ioctl(fd, UI_DEV_CREATE) < 0) {
        perror("UI_DEV_CREATE");
        close(fd);
        return -1;
    }
    return fd;
}

int main(int argc, char **argv)
{
    const char *src = argc > 1 ? argv[1] : "/dev/input/event1";
    int w = argc > 2 ? atoi(argv[2]) : 800;
    int h = argc > 3 ? atoi(argv[3]) : 254;

    int ufd = setup_device(w, h);
    if (ufd < 0)
        return 1;

    int sfd = open(src, O_RDONLY);
    if (sfd < 0) {
        perror(src);
        return 1;
    }

    fprintf(stderr, "touchbridge: %s -> vm-touch %dx%d\n", src, w, h);

    struct input_event ev;
    while (read(sfd, &ev, sizeof ev) == (ssize_t)sizeof ev) {
        if (ev.type == EV_ABS) {
            if (ev.code == ABS_X) {
                emit(ufd, EV_ABS, ABS_X, ev.value * w / 32767);
            } else if (ev.code == ABS_Y) {
                emit(ufd, EV_ABS, ABS_Y, ev.value * h / 32767);
            }
        } else if (ev.type == EV_KEY && ev.code == BTN_LEFT) {
            emit(ufd, EV_KEY, BTN_TOUCH, ev.value);
        } else if (ev.type == EV_SYN && ev.code == SYN_REPORT) {
            emit(ufd, EV_SYN, SYN_REPORT, 0);
        }
    }
    perror("read");
    return 0;
}
