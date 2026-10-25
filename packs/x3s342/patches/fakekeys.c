#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <linux/input.h>
#include <linux/uinput.h>

/* 厂商按键 HAL 的实体键替身（x3s342 用）。
 *
 * 这一台客机里没有 perl，同族那份 vm-keys.pl 跑不了，所以按 touchbridge 的路子
 * 编一个静态二进制。要对齐的是 /usr/configs/hal_key_config.json 的 Cherry-3566_V0 节：
 *   home_key_id = platform-adc-keys-event, home_key_code = 115
 * HAL 按 by-path 名字找设备，所以建完 uinput 设备后要补上那条符号链接。
 *
 * 用法：fakekeys <设备名> <by-path 链接名> <码>[,<码>...]
 * 触发：echo 115 > /tmp/fake-keys.fifo   （一次按下 + 抬起）
 */

#define FIFO_PATH "/tmp/fake-keys.fifo"
#define BYPATH_DIR "/dev/input/by-path"

static int emit(int fd, int type, int code, int value)
{
    struct input_event ev;
    memset(&ev, 0, sizeof ev);
    ev.type = type;
    ev.code = code;
    ev.value = value;
    return write(fd, &ev, sizeof ev) == (ssize_t)sizeof ev ? 0 : -1;
}

static int hold_us = 120000;

static int press(int fd, int code)
{
    emit(fd, EV_KEY, code, 1);
    emit(fd, EV_SYN, SYN_REPORT, 0);
    /* 按下与抬起之间要留时间：应用的按键分类（短按/长按）看的就是这个间隔，
     * 0 间隔的 down+up 只会走到 onHomeButtonPress，不会当成一次点击。 */
    usleep(hold_us);
    emit(fd, EV_KEY, code, 0);
    emit(fd, EV_SYN, SYN_REPORT, 0);
    return 0;
}

static int setup_device(const char *name, int *codes, int ncode)
{
    int fd = open("/dev/uinput", O_WRONLY | O_NONBLOCK);
    if (fd < 0) {
        perror("open /dev/uinput");
        return -1;
    }
    ioctl(fd, UI_SET_EVBIT, EV_KEY);
    for (int i = 0; i < ncode; i++)
        ioctl(fd, UI_SET_KEYBIT, codes[i]);

    struct uinput_setup usetup;
    memset(&usetup, 0, sizeof usetup);
    usetup.id.bustype = BUS_VIRTUAL;
    usetup.id.vendor = 0x0001;
    usetup.id.product = 0x0001;
    usetup.id.version = 1;
    snprintf(usetup.name, UINPUT_MAX_NAME_SIZE, "%s", name);
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

/* uinput 建好后内核才分配 event 节点，按设备名找回它，再补 by-path 链接。 */
static int link_bypath(const char *name, const char *link)
{
    for (int try = 0; try < 50; try++) {
        for (int i = 0; i < 32; i++) {
            char path[64];
            snprintf(path, sizeof path, "/dev/input/event%d", i);
            int fd = open(path, O_RDONLY);
            if (fd < 0)
                continue;
            char devname[UINPUT_MAX_NAME_SIZE];
            int got = ioctl(fd, EVIOCGNAME(sizeof devname), devname) > 0;
            close(fd);
            if (!got || strcmp(devname, name) != 0)
                continue;
            char target[64];
            snprintf(target, sizeof target, "../event%d", i);
            char linkpath[256];
            snprintf(linkpath, sizeof linkpath, "%s/%s", BYPATH_DIR, link);
            unlink(linkpath);
            if (symlink(target, linkpath) < 0) {
                perror(linkpath);
                return -1;
            }
            fprintf(stderr, "fakekeys: %s -> %s\n", linkpath, target);
            return 0;
        }
        usleep(100000);
    }
    fprintf(stderr, "fakekeys: no event node named %s\n", name);
    return -1;
}

static int open_fifo(void)
{
    if (access(FIFO_PATH, F_OK) != 0 && mkfifo(FIFO_PATH, 0666) < 0) {
        perror(FIFO_PATH);
        return -1;
    }
    /* O_RDWR：没有写端时读也不会拿到 EOF，读端就一直挂着等下一条。 */
    int fd = open(FIFO_PATH, O_RDWR | O_NONBLOCK);
    if (fd < 0)
        perror(FIFO_PATH);
    return fd;
}

int main(int argc, char **argv)
{
    if (argc < 4) {
        fprintf(stderr, "usage: fakekeys <name> <by-path> <code>[,<code>...]\n");
        return 2;
    }
    const char *name = argv[1];
    const char *link = argv[2];

    int codes[16];
    int ncode = 0;
    char buf[256];
    snprintf(buf, sizeof buf, "%s", argv[3]);
    for (char *tok = strtok(buf, ","); tok && ncode < 16; tok = strtok(NULL, ",")) {
        while (isspace((unsigned char)*tok))
            tok++;
        if (*tok)
            codes[ncode++] = atoi(tok);
    }
    if (!ncode) {
        fprintf(stderr, "fakekeys: no key code given\n");
        return 2;
    }

    const char *hold = getenv("VM_KEY_HOLD_US");
    if (hold && *hold)
        hold_us = atoi(hold);

    int ufd = setup_device(name, codes, ncode);
    if (ufd < 0)
        return 1;
    if (link_bypath(name, link) < 0)
        return 1;

    int rfd = open_fifo();
    if (rfd < 0)
        return 1;

    fprintf(stderr, "fakekeys: %s (%s) ready, %d codes, fifo %s\n",
            name, link, ncode, FIFO_PATH);

    struct pollfd pfd = { .fd = rfd, .events = POLLIN };
    char line[64];
    size_t llen = 0;
    for (;;) {
        if (poll(&pfd, 1, 1000) <= 0)
            continue;
        char chunk[64];
        ssize_t n = read(rfd, chunk, sizeof chunk);
        if (n <= 0) {
            if (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK)
                break;
            continue;
        }
        for (ssize_t i = 0; i < n; i++) {
            char c = chunk[i];
            if (c == '\n' || c == '\r') {
                line[llen] = 0;
                if (llen) {
                    int code = atoi(line);
                    if (code > 0)
                        press(ufd, code);
                }
                llen = 0;
            } else if (llen < sizeof line - 1) {
                line[llen++] = c;
            }
        }
    }
    perror("fakekeys: fifo closed");
    return 0;
}
