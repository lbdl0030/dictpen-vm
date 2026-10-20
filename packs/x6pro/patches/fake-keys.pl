#!/usr/bin/perl
# fake-keys：给厂商 HAL 造"实体按键"设备（x6pro/adc-keys 那套的 VM 替身）。
#
# 依据（都在台账）：`/usr/configs/hal_key_config.json` 的 RK3562_Melon_V0 节按
#   **by-path 名**找设备：home/vol± 都是 `platform-adc-keys-event`；
#   码来自厂商 DTB：home=102 / volume-up=115 / volume-down=114 / play-pause=164
# （f11=87 是 gpio-keys 上的调试键，控制台不暴露它 —— F11 已经被窗口拿去做全屏了）
#
# 命令从 FIFO 读：`echo 102 > /tmp/fake-keys.fifo` = 按一次 Home。
use strict;
use warnings;
use Fcntl;
use Config;
use POSIX qw(mkfifo);

use constant EV_SYN => 0;
use constant EV_KEY => 1;
use constant SYN_REPORT => 0;
use constant UI_SET_EVBIT => 0x40045564;
use constant UI_SET_KEYBIT => 0x40045565;
use constant UI_DEV_CREATE => 0x5501;
use constant UINPUT => "/dev/uinput";
use constant LINK_PLATFORM => "/dev/input/by-path/platform-adc-keys-event";   # HAL 按这个 by-path 名打开
use constant LINK_FAKE => "/dev/input/by-path/fake-keys";                    # 保留调试用的别名
use constant FIFO => "/tmp/fake-keys.fifo";
use constant DEV_NAME => "adc-keys";   # HAL 按名字/ id 认设备；先试真机那个名字
use constant NAME_MATCH => "adc-keys";
# uinput 的 struct input_event 按**写入进程**的位宽解析（6.1 内核的 compat 路径）：
# 64 位 Perl = 24 字节，32 位 Perl = 16 字节。s6pro 的 /usr/bin/perl 是 32 位，
# 用 24 字节写会只收 16（短写、$! 为空），键就永远发不出去。别写死。
my $EVSIZE = $Config{ptrsize} == 8 ? 24 : 16;
my $EVFMT  = $Config{ptrsize} == 8 ? "q<q<S<S<l<" : "l<l<S<S<l<";
use constant KEYS => (102, 115, 114, 164);

sysopen(my $ui, UINPUT, O_WRONLY | O_NONBLOCK) or die "open " . UINPUT . ": $!\n";
my $ioctl = sub {
    my ($req, $arg) = @_;
    ioctl($ui, $req, $arg) or warn "fake-keys: ioctl $req($arg): $!\n";
};
$ioctl->(UI_SET_EVBIT, EV_SYN);
$ioctl->(UI_SET_EVBIT, EV_KEY);
$ioctl->(UI_SET_KEYBIT, $_) for KEYS;

my $udev = pack("a80", DEV_NAME)
         . pack("S4", 0x19, 0x0001, 0x0001, 0x0100)   # BUS_HOST / 1 / 1 / 0x100：rockchip adc-keys 的常见 id     # BUS_USB / vendor / product / version
         . pack("l", 0)
         . pack("l*", (0) x 64)                    # absmax
         . pack("l*", (0) x 64)                    # absmin
         . pack("l*", (0) x 64)                    # absfuzz
         . pack("l*", (0) x 64);                   # absflat
syswrite($ui, $udev) == length($udev) or die "write uinput_user_dev: $!\n";
$ioctl->(UI_DEV_CREATE, 0);

sub emit {
    my ($code) = @_;
    for my $e ([EV_KEY, $code, 1], [EV_SYN, SYN_REPORT, 0], [EV_KEY, $code, 0], [EV_SYN, SYN_REPORT, 0]) {
        syswrite($ui, pack($EVFMT, 0, 0, $e->[0], $e->[1], $e->[2])) == $EVSIZE
            or warn "fake-keys: write event: $!\n";
    }
}

# 内核/udev 把 eventN 挂出来之后，把 by-path 指过去（HAL 认的是这条**路径**）
my $node = "";
for (1 .. 50) {
    for my $e (glob("/sys/class/input/event*")) {
        open(my $fh, "<", "$e/device/name") or next;
        my $name = <$fh>;
        close($fh);
        if (defined $name && $name =~ /^\Q@{[NAME_MATCH]}\E/) {
            ($node = $e) =~ s{/sys/class/input/}{/dev/input/};
            last;
        }
    }
    last if $node;
    select(undef, undef, undef, 0.1);
}
$node or die "fake-keys: uinput device never appeared\n";
for my $link (LINK_PLATFORM, LINK_FAKE) {
    unlink($link);
    symlink($node, $link) or die "symlink $link: $!\n";
}

unlink(FIFO);
mkfifo(FIFO, 0666) or die "mkfifo " . FIFO . ": $!\n";
sysopen(my $fifo, FIFO, O_RDONLY | O_NONBLOCK) or die "open fifo: $!\n";
print STDERR "fake-keys: $node -> " . LINK_PLATFORM . ", " . LINK_FAKE . "\n";

my $buf = "";
while (1) {
    my $n = sysread($fifo, $buf, 64);
    if (!$n) { select(undef, undef, undef, 0.2); next }
    emit($_) for ($buf =~ /(\d+)/g);
    $buf = "";
}
