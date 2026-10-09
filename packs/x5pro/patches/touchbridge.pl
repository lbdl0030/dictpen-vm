#!/usr/bin/perl
# touchbridge.pl - virtual touchscreen for the dictpen VM.
#
# The QEMU window's pointer arrives on the virtio-tablet as EV_ABS/ABS_X,ABS_Y
# plus EV_KEY/BTN_LEFT.  The tablet advertises BTN_TOUCH but never emits it,
# and the pen UI is written for the AXS single-touch panel (axs_ts.ko), so it
# waits for BTN_TOUCH and ignores the mouse entirely - clicking in the window
# does nothing.
#
# This bridge reads the tablet and republishes every event through a uinput
# device that looks like that panel (name "axs_ts_pen", ABS_X/ABS_Y in panel
# pixels and BTN_TOUCH), rewriting BTN_LEFT into BTN_TOUCH and rescaling the
# tablet's 0..32767 coordinates to 0..559 / 0..169.  The event node it creates
# is linked to /dev/input/by-path/axs_ts, which is what cfg.json's "tp" points
# at, so the UI sees an ordinary touchscreen.
#
# Mouse -> touch semantics:
#   * hover motion is not reported (a touchscreen has no hover),
#   * a click is one press + release at the same spot: while pressed, moves
#     below CLICK_SLOP panel pixels are ignored so the button's own wobble does
#     not turn the click into a drag,
#   * beyond that the pointer is followed = drag,
#   * a wheel notch becomes a swipe (the pen's lists scroll by finger).
#
# Usage: touchbridge.pl [source] [link-name]
#   source    device node, or the name of an input device to look up in sysfs
#             (default "QEMU Virtio Tablet": /dev/input/by-path does not exist
#             yet when this bridge starts, so the name is what we can rely on)
#   link-name defaults to /dev/input/by-path/axs_ts
# Set TOUCHBRIDGE_DEBUG=1 to log every forwarded event.
use strict;
use warnings;
use Config;
use Fcntl qw(O_WRONLY O_NONBLOCK);

use constant {
    EV_SYN => 0x00,
    EV_KEY => 0x01,
    EV_REL => 0x02,
    EV_ABS => 0x03,
    SYN_REPORT => 0x00,
    ABS_X => 0x00,
    ABS_Y => 0x01,
    REL_WHEEL => 0x08,
    BTN_LEFT => 0x110,      # 272: what the tablet sends
    BTN_TOUCH => 0x14a,     # 330: what the pen UI listens for
    BTN_TOOL_FINGER => 0x145,
    # 本机（x5pro，app 3.4.6）的触摸读取器按 MT 协议 B 解析：只发单点帧
    # （ABS_X/ABS_Y + BTN_TOUCH）时它一条都不派发（dispatchTouchEvent 0 条），
    # 补上 MT-B 帧后才有反应 —— 已在同版本 app 上实测确认，所以这里比 s6pro
    # 多出一组 MT-B 帧（单点帧同时保留，tslib/老读法也满足）。
    ABS_MT_SLOT => 0x2f,
    ABS_MT_POSITION_X => 0x35,
    ABS_MT_POSITION_Y => 0x36,
    ABS_MT_TRACKING_ID => 0x39,
    # The pen's own touchscreen reports panel pixels (cfg.json carries only
    # tp_xoffset/tp_yoffset, no scaling), so the bridge must publish exactly the
    # range the UI is using - otherwise every touch lands offset.
    #
    # The size is therefore *read*, not hardcoded: this script is shared by every
    # pack, and a per-machine constant here is silently wrong everywhere else
    # (560x170 was baked in and misplaced every touch on the 640x172 a7pro).
    # Priority: 3rd argument WxH > $TOUCHBRIDGE_PANEL > the pen's own cfg.json.
    CFG_JSON => '/etc/miniapp/resources/cfg.json',
    # Only used if cfg.json cannot be read at all; logged loudly when it happens.
    PANEL_GUESS_X => 560,
    PANEL_GUESS_Y => 170,
    # A click wobbles the pointer by a pixel or two; below this the touch stays
    # a click instead of turning into a drag (panel pixels).
    CLICK_SLOP => 4,
    # Wheel -> swipe: steps and delay shape how fast the list scrolls.
    SWIPE_STEPS => 8,
    SWIPE_MARGIN => 18,
    SWIPE_STEP_DELAY => 0.012,
};

use constant {
    UI_DEV_CREATE => 0x00005501,
    UI_SET_EVBIT => 0x40045564,
    UI_SET_KEYBIT => 0x40045565,
    UI_SET_ABSBIT => 0x40045567,
};

use constant DEV_NAME => 'axs_ts_pen';
use constant UINPUT => '/dev/uinput';

# Mirror the tablet's input_id (BUS_VIRTUAL / QEMU) - the UI already accepts it.
use constant { BUSTYPE => 0x06, VENDOR => 0x0627, PRODUCT => 0x0003, VERSION => 0x0001 };

my $SRC = shift @ARGV // 'QEMU Virtio Tablet';
my $LINK = shift @ARGV // '/dev/input/by-path/axs_ts';
my $PANEL_ARG = shift @ARGV;                    # optional "WxH", overrides cfg.json
my $DEBUG = $ENV{TOUCHBRIDGE_DEBUG} ? 1 : 0;

# --------------------------------------------------------------------------- #
# Panel size - the coordinate space the pen UI works in.
# Reading the first "width"/"height" out of cfg.json is enough: they are the
# "screen" ones, which is exactly the space the UI maps touches into.
sub panel_from_cfg {
    my ($path) = @_;
    open(my $fh, '<', $path) or return undef;
    local $/;
    my $t = <$fh>;
    close($fh);
    return undef unless defined $t;
    my ($x) = $t =~ /"width"\s*:\s*(\d+)/;
    my ($y) = $t =~ /"height"\s*:\s*(\d+)/;
    return undef unless $x && $y && $x > 1 && $y > 1;
    return ($x, $y);
}

my ($panel_x, $panel_y, $panel_src);
if (defined $PANEL_ARG && $PANEL_ARG =~ /^(\d+)x(\d+)$/) {
    ($panel_x, $panel_y, $panel_src) = ($1, $2, 'argument');
} elsif (defined $ENV{TOUCHBRIDGE_PANEL}
         && $ENV{TOUCHBRIDGE_PANEL} =~ /^(\d+)x(\d+)$/) {
    ($panel_x, $panel_y, $panel_src) = ($1, $2, 'TOUCHBRIDGE_PANEL');
} elsif (my @p = panel_from_cfg(CFG_JSON)) {
    ($panel_x, $panel_y, $panel_src) = (@p, CFG_JSON);
} else {
    ($panel_x, $panel_y, $panel_src) = (PANEL_GUESS_X, PANEL_GUESS_Y, 'guess');
    warn "touchbridge: cannot read a panel size from " . CFG_JSON
       . "; guessing ${panel_x}x${panel_y} - touches will be offset unless the"
       . " panel really is that size (set TOUCHBRIDGE_PANEL or pass WxH)\n";
}
logmsg("panel ${panel_x}x${panel_y} (from $panel_src)");

# struct input_event is { struct timeval; __u16 type, code; __s32 value; }.
# The size that matters is the *kernel's* view of this process: a 32-bit
# userspace on a 64-bit kernel gets the compat (4-byte timeval) layout, so the
# size follows the POINTER size, not ivsize -- Debian's -64int armhf builds
# have a 64-bit IV on 32-bit pointers (that combination made every write
# "fail": the kernel consumed the first 16 bytes as a compat event and left
# the real event behind, returning 16 for a 24-byte syswrite).
my $EVSIZE = 2 * $Config{ptrsize} + 8;
my $EVFMT = ($Config{ptrsize} == 8 ? 'q q' : 'L L') . ' S S l';

# Perl's ioctl returns undef on error and the (possibly 0) result on success.
# A numeric SCALAR is passed to the driver as the argument itself, which is what
# uinput's UI_SET_*BIT want even though they are declared _IOW.
sub ioctl_ok {
    my ($fh, $req, $val) = @_;
    my $r = ioctl($fh, $req, $val);
    die sprintf("ioctl 0x%08x(%d) failed: %s\n", $req, $val, $!) unless defined $r;
}

sub logmsg { print STDERR "touchbridge: @_\n"; }

# Name of the input device behind /sys/class/input/eventN, or undef.
sub event_name {
    my ($d) = @_;
    open(my $fh, '<', "$d/device/name") or return undef;
    my $name = <$fh>;
    close($fh);
    return undef unless defined $name;
    chomp $name;
    return $name;
}

sub event_node {
    my ($d) = @_;
    return '/dev/input/' . substr($d, rindex($d, '/') + 1);
}

# Find the node of the input device called $name, waiting up to $timeout for it.
sub wait_for_device {
    my ($name, $timeout) = @_;
    my $deadline = time() + $timeout;
    while (1) {
        for my $d (glob('/sys/class/input/event*')) {
            my $fn = event_name($d);
            return event_node($d) if defined $fn && $fn eq $name;
        }
        return undef if time() >= $deadline;
        select(undef, undef, undef, 0.2);
    }
}

sub absinfo_value {
    my ($fh, $abs) = @_;
    my $buf = "\0" x 24;                      # struct input_absinfo
    my $r = ioctl($fh, 0x80184540 + $abs, $buf);   # EVIOCGABS(abs)
    return undef unless defined $r;
    my ($value, $min, $max) = unpack('l3', $buf);
    return ($value, $min, $max);
}

# Tablet range -> panel range, filled in from the source device below.
my @scale;                                    # [min, span] per axis

# --------------------------------------------------------------------------- #
# $SRC is either a node or an input device name; a name has to be resolved
# through sysfs because /dev/input/by-path is still being populated.
my $srcnode = $SRC;
if ($SRC !~ m{/}) {
    $srcnode = wait_for_device($SRC, 15)
        or die "touchbridge: no input device named '$SRC' found\n";
}
open(my $ev, '<', $srcnode) or die "touchbridge: open $srcnode: $!\n";
logmsg("reading $srcnode (event size $EVSIZE)");

# Learn the tablet's axis range and keep the pointer position in *its* units;
# every forwarded coordinate is rescaled into panel pixels.
my @pos;                                       # last ABS_X/ABS_Y seen
for my $axis (ABS_X, ABS_Y) {
    my @ai = absinfo_value($ev, $axis);
    @ai = (0, 0, 32767) unless @ai == 3;
    $pos[$axis] = $ai[0];
    $scale[$axis] = [$ai[1], ($ai[2] - $ai[1]) || 1];
}
my @panel = ($panel_x - 1, $panel_y - 1);      # last valid panel pixel
sub to_panel {
    my ($axis, $v) = @_;
    my $n = int(($v - $scale[$axis][0]) * $panel[$axis] / $scale[$axis][1] + 0.5);
    return $n < 0 ? 0 : $n > $panel[$axis] ? $panel[$axis] : $n;
}
logmsg(sprintf('source x %d..%d y %d..%d -> panel %d x %d',
               $scale[ABS_X][0], $scale[ABS_X][0] + $scale[ABS_X][1],
               $scale[ABS_Y][0], $scale[ABS_Y][0] + $scale[ABS_Y][1],
               $panel[0], $panel[1]));

sysopen(my $ui, UINPUT, O_WRONLY | O_NONBLOCK)
    or die "touchbridge: open " . UINPUT . ": $! (is uinput loaded?)\n";
for my $b ([UI_SET_EVBIT, EV_SYN], [UI_SET_EVBIT, EV_KEY], [UI_SET_EVBIT, EV_ABS],
           [UI_SET_KEYBIT, BTN_TOUCH], [UI_SET_KEYBIT, BTN_TOOL_FINGER],
           [UI_SET_ABSBIT, ABS_X], [UI_SET_ABSBIT, ABS_Y],
           [UI_SET_ABSBIT, ABS_MT_SLOT], [UI_SET_ABSBIT, ABS_MT_POSITION_X],
           [UI_SET_ABSBIT, ABS_MT_POSITION_Y], [UI_SET_ABSBIT, ABS_MT_TRACKING_ID]) {
    ioctl_ok($ui, $b->[0], $b->[1]);
}

# Legacy uinput_user_dev: name + input_id + ff_effects_max + 4 abs arrays.
# 所有轴都用 **UI 坐标量程**（800x254）：桥上报的就是 UI 像素（见 touch_press）。
my @absmax = (0) x 64;
$absmax[ABS_X] = $panel_x - 1;
$absmax[ABS_Y] = $panel_y - 1;
$absmax[ABS_MT_SLOT] = 9;
$absmax[ABS_MT_POSITION_X] = $panel_x - 1;
$absmax[ABS_MT_POSITION_Y] = $panel_y - 1;
$absmax[ABS_MT_TRACKING_ID] = 0xffff;
my $udev = pack('a80', DEV_NAME)
         . pack('S4', BUSTYPE, VENDOR, PRODUCT, VERSION)
         . pack('l', 0)
         . pack('l*', @absmax)
         . pack('l*', (0) x 64)                # absmin
         . pack('l*', (0) x 64)                # absfuzz
         . pack('l*', (0) x 64);               # absflat
syswrite($ui, $udev) == length($udev) or die "touchbridge: uinput_user_dev: $!\n";

ioctl_ok($ui, UI_DEV_CREATE, 0);
logmsg('uinput device "' . DEV_NAME . '" created');

# The new node is the one whose /sys/class/input/eventN/device/name matches.
my $node = wait_for_device(DEV_NAME, 5)
    or die "touchbridge: created device not found in /sys/class/input\n";
(my $dir = $LINK) =~ s{/[^/]+$}{};
-d $dir or mkdir($dir, 0755) or die "touchbridge: mkdir $dir: $!\n";
unlink($LINK);
symlink($node, $LINK) or die "touchbridge: symlink $LINK -> $node: $!\n";
logmsg("$LINK -> $node");

sub write_ev {
    my ($type, $code, $value) = @_;
    my $n = syswrite($ui, pack($EVFMT, 0, 0, $type, $code, $value));
    warn "touchbridge: uinput write failed: $!\n" unless $n && $n == $EVSIZE;
    logmsg(sprintf('-> type=%d code=%d value=%d', $type, $code, $value)) if $DEBUG;
}

# --- single-touch emulation ------------------------------------------------- #
# A touchscreen only reports coordinates while it is being touched, and a click
# is a press+release at the same spot.  A mouse is not like that: it reports
# motion at any time and its button usually wobbles the pointer, which the pen
# UI would read as a drag (and never as a click).  So:
#   * hover motion is swallowed,
#   * while pressed, moves smaller than CLICK_SLOP are swallowed too,
#   * anything beyond that is forwarded - that is a real drag.
my $down = 0;                       # is the guest touch currently pressed?
my $dragging = 0;                   # did this touch exceed the click slop?
my ($slop_x, $slop_y);              # position where the touch went down
my $abs_seen = 0;                   # a frame carried new coordinates
my $mt_id = 0;                      # MT-B tracking id, bumped on every press

# MT-B 帧（见常量区说明）：SLOT 固定 0（单指），每次按下换一个 TRACKING_ID，
# 抬起时 TRACKING_ID = -1。
sub mt_down {
    my ($x, $y) = @_;
    write_ev(EV_ABS, ABS_MT_SLOT, 0);
    write_ev(EV_ABS, ABS_MT_TRACKING_ID, $mt_id);
    write_ev(EV_ABS, ABS_MT_POSITION_X, $x);
    write_ev(EV_ABS, ABS_MT_POSITION_Y, $y);
    write_ev(EV_KEY, BTN_TOOL_FINGER, 1);
    $mt_id = ($mt_id + 1) & 0x7fff;
}

sub mt_up {
    write_ev(EV_ABS, ABS_MT_TRACKING_ID, -1);
    write_ev(EV_KEY, BTN_TOOL_FINGER, 0);
}

sub touch_press {
    my ($x, $y) = (to_panel(ABS_X, $pos[ABS_X]), to_panel(ABS_Y, $pos[ABS_Y]));
    # 单点帧 + MT-B 帧合在同一次 SYN 里发出（与真机 AXS 面板一样）：
    # 分两次 SYN 发会多派发一条坐标过期的 DOWN。
    write_ev(EV_ABS, ABS_X, $x);
    write_ev(EV_ABS, ABS_Y, $y);
    mt_down($x, $y);
    write_ev(EV_KEY, BTN_TOUCH, 1);
    write_ev(EV_SYN, SYN_REPORT, 0);
    ($slop_x, $slop_y) = ($x, $y);
    $down = 1;
    $dragging = 0;
    $abs_seen = 0;
}

sub touch_release {
    mt_up();
    write_ev(EV_KEY, BTN_TOUCH, 0);
    write_ev(EV_SYN, SYN_REPORT, 0);
    $down = 0;
    $dragging = 0;
    $abs_seen = 0;
}

# The pen's list scrolling is a finger swipe (it has no wheel), so turn a wheel
# notch into a swipe over the content area.
sub swipe {
    my ($v, $x) = @_;
    return if $down;                       # ignore a wheel during a real drag
    my $top = SWIPE_MARGIN;
    my $bottom = $panel_y - SWIPE_MARGIN;
    my ($y0, $y1) = $v > 0 ? ($top, $bottom) : ($bottom, $top);
    $x = $panel_x / 2 if $x < 60 || $x > $panel_x - 60;
    write_ev(EV_ABS, ABS_X, int($x));
    write_ev(EV_ABS, ABS_Y, $y0);
    write_ev(EV_KEY, BTN_TOUCH, 1);
    write_ev(EV_SYN, SYN_REPORT, 0);
    for my $i (1 .. SWIPE_STEPS) {
        my $y = int($y0 + ($y1 - $y0) * $i / SWIPE_STEPS + 0.5);
        write_ev(EV_ABS, ABS_X, int($x));
        write_ev(EV_ABS, ABS_Y, $y);
        write_ev(EV_SYN, SYN_REPORT, 0);
        select(undef, undef, undef, SWIPE_STEP_DELAY);
    }
    write_ev(EV_KEY, BTN_TOUCH, 0);
    write_ev(EV_SYN, SYN_REPORT, 0);
}

my $buf;
while (1) {
    my $n = sysread($ev, $buf, $EVSIZE * 16);
    last if defined $n && $n == 0;             # device removed
    if (!defined $n) {
        next if $!{EINTR};
        warn "touchbridge: read $srcnode: $!\n";
        last;
    }
    for (my $o = 0; $o + $EVSIZE <= $n; $o += $EVSIZE) {
        my (undef, undef, $type, $code, $value) = unpack($EVFMT, substr($buf, $o, $EVSIZE));
        if ($type == EV_ABS) {
            next unless $code == ABS_X || $code == ABS_Y;
            $pos[$code] = $value;              # keep the source value for rescaling
            $abs_seen = 1;
        } elsif ($type == EV_KEY) {
            next unless $code == BTN_LEFT;
            $value ? touch_press() : touch_release();
        } elsif ($type == EV_REL) {
            swipe($value, to_panel(ABS_X, $pos[ABS_X])) if $code == REL_WHEEL && $value;
            next;
        } elsif ($type == EV_SYN) {
            if ($abs_seen && $down) {
                my ($x, $y) = (to_panel(ABS_X, $pos[ABS_X]), to_panel(ABS_Y, $pos[ABS_Y]));
                if ($dragging || abs($x - $slop_x) >= CLICK_SLOP
                              || abs($y - $slop_y) >= CLICK_SLOP) {
                    $dragging = 1;             # a real drag: follow the pointer
                    write_ev(EV_ABS, ABS_X, $x);
                    write_ev(EV_ABS, ABS_Y, $y);
                    write_ev(EV_ABS, ABS_MT_POSITION_X, $x);
                    write_ev(EV_ABS, ABS_MT_POSITION_Y, $y);
                    write_ev(EV_SYN, SYN_REPORT, 0);
                }
            }
            $abs_seen = 0;
        }
    }
}

logmsg("$srcnode closed, exiting");