#!/usr/bin/perl
# QEMU virtio-tablet (mouse) -> single-touch touchscreen at /dev/input/by-path/axs_ts.
# Profile matters more than anything else here:
#   * ABS_X/ABS_Y must declare real ranges, otherwise every touch reads (0,0);
#   * NO MT slots / BTN_TOOL_FINGER - those make libinput classify the device as a
#     touchpad, and a touch UI then never receives touch events;
#   * INPUT_PROP_DIRECT marks it as a direct-input touchscreen.
#   * BTN_LEFT held + ABS changes must be forwarded as motion, and both the press
#     and the lift must be emitted even when their packet carries no ABS: QEMU
#     never re-sends an axis that did not change, so position has to be kept as
#     state.  Gating on "an ABS arrived in this frame" makes taps do nothing and
#     leaves swipes as a press that never ends.
use strict;
use warnings;
use Fcntl;

$| = 1;
my ($W, $H) = @ARGV;
$W = 960 unless defined $W;
$H = 266 unless defined $H;

my $UI_SET_EVBIT   = 0x40045564;
my $UI_SET_KEYBIT  = 0x40045565;
my $UI_SET_ABSBIT  = 0x40045567;
my $UI_DEV_SETUP   = 0x405c5503;
my $UI_ABS_SETUP   = 0x401c5504;
my $UI_DEV_CREATE  = 0x00005501;
my $UI_SET_PHYS    = 0x4008556c;
my $UI_SET_PROPBIT = 0x4004556e;
my $INPUT_PROP_DIRECT = 1;
my $EV_SYN = 0; my $EV_KEY = 1; my $EV_ABS = 3;
my $SYN_REPORT = 0;
my $ABS_X = 0; my $ABS_Y = 1;
my $BTN_TOUCH = 0x14a; my $BTN_LEFT = 0x110;
my $LINK = '/dev/input/by-path/axs_ts';

sub find_tablet {
    open(my $fh, '<', '/proc/bus/input/devices') or return undef;
    my $name;
    while (my $line = <$fh>) {
        $name = $1 if $line =~ /^N: Name="(.*)"/;
        if ($line =~ /^H: Handlers=.*\b(event\d+)\b/) {
            my $node = $1;
            if (defined $name && $name =~ /Tablet/i) {
                close($fh);
                return '/dev/input/' . $node;
            }
        }
    }
    close($fh);
    return undef;
}

sub events_now { my %h; $h{$_} = 1 for glob('/dev/input/event*'); return %h; }

my $src = find_tablet();
die "no virtio tablet found\n" unless $src;
print "touchbridge: source $src, canvas ${W}x${H}\n";

my %before = events_now();
my $u;
sysopen($u, '/dev/uinput', O_WRONLY | O_NONBLOCK) or die "open /dev/uinput: $!\n";
my $name = 'axs_ts';
my $setup = pack('S<S<S<S<', 0x03, 0x1d6b, 0x0103, 1) . $name . ("\0" x (80 - length($name))) . pack('L<', 0);
ioctl($u, $UI_DEV_SETUP, $setup) or die "UI_DEV_SETUP: $!\n";
my $phys = 'axs_ts';
ioctl($u, $UI_SET_PHYS, $phys) or warn "UI_SET_PHYS failed: $!\n";
setbit($UI_SET_PROPBIT, $INPUT_PROP_DIRECT, 'UI_SET_PROPBIT');

sub abs_setup {
    my ($code, $max) = @_;
    my $info = pack('l<l<l<l<l<l<', 0, 0, $max, 0, 0, 0);
    my $buf = pack('S<S<', $code, 0) . $info;
    my $rc = ioctl($u, $UI_ABS_SETUP, $buf);
    warn sprintf("UI_ABS_SETUP(%d) failed: %s\n", $code, $!) unless $rc;
}
abs_setup($ABS_X, $W - 1);
abs_setup($ABS_Y, $H - 1);

# UI_SET_*BIT 的位号是「按值」传参，不是指针：给 pack('i',..) 会让内核把
# 缓冲区地址当成位号，一律 EINVAL（设备只剩 EV_SYN，触摸事件全被丢弃）。
sub setbit {
    my ($req, $val, $what) = @_;
    ioctl($u, $req, $val) or warn sprintf("%s(%d) failed: %s\n", $what, $val, $!);
}
setbit($UI_SET_EVBIT, $EV_KEY, 'UI_SET_EVBIT');
setbit($UI_SET_EVBIT, $EV_ABS, 'UI_SET_EVBIT');
setbit($UI_SET_EVBIT, $EV_SYN, 'UI_SET_EVBIT');
setbit($UI_SET_KEYBIT, $BTN_TOUCH, 'UI_SET_KEYBIT');
setbit($UI_SET_ABSBIT, $ABS_X, 'UI_SET_ABSBIT');
setbit($UI_SET_ABSBIT, $ABS_Y, 'UI_SET_ABSBIT');
ioctl($u, $UI_DEV_CREATE, 0) or die "UI_DEV_CREATE: $!\n";
print "touchbridge: uinput device created\n";

my $new;
for my $i (1 .. 40) {
    if (-e $LINK) { $new = readlink($LINK); last; }
    for my $e (glob('/dev/input/event*')) { next if $before{$e}; $new = $e; }
    last if $new;
    select(undef, undef, undef, 0.25);
}
if (!$new) { print "touchbridge: no new event node\n"; }
elsif (!-e $LINK) {
    mkdir '/dev/input/by-path' unless -d '/dev/input/by-path';
    symlink($new, $LINK) and print "touchbridge: linked $LINK -> $new\n";
}

my $ev;
sysopen($ev, $src, O_RDONLY | O_NONBLOCK) or die "open $src: $!\n";
my ($rx, $ry, $btn, $seen, $down, $lx, $ly) = (16383, 16383, 0, 0, 0, -1, -1);
sub emit {
    my $buf = '';
    foreach my $it (@_) { $buf .= pack('q<q<S<S<l<', 0, 0, $it->[0], $it->[1], $it->[2]); }
    $buf .= pack('q<q<S<S<l<', 0, 0, $EV_SYN, $SYN_REPORT, 0);
    syswrite($u, $buf);
}
sub scaled { my ($v, $max, $out) = @_; return int($v * ($out - 1) / $max + 0.5); }

print "touchbridge: ready\n";
while (1) {
    my $n = sysread($ev, my $data, 24);
    if (!defined $n || $n < 24) { select(undef, undef, undef, 0.02); next; }
    my ($s, $us, $type, $code, $value) = unpack('q<q<S<S<l<', $data);
    if ($type == $EV_ABS) {
        if ($code == $ABS_X) { $rx = $value; $seen = 1; }
        if ($code == $ABS_Y) { $ry = $value; $seen = 1; }
    } elsif ($type == $EV_KEY && $code == $BTN_LEFT) {
        $btn = $value;
    } elsif ($type == $EV_SYN && $code == $SYN_REPORT) {
        # Position is *state*, not an edge.  QEMU drops an ABS whose value did not
        # change, so the frame that carries BTN_LEFT often carries no coordinate
        # at all -- a "wait for a position in this frame" gate then loses the
        # press (tap does nothing) or the lift (swipe = stuck finger).
        my ($px, $py) = (scaled($rx, 32767, $W), scaled($ry, 32767, $H));
        if (!$btn && $down) {
            $down = 0;
            emit([$EV_KEY, $BTN_TOUCH, 0]);
            print "touchbridge: up\n";
        } elsif ($btn && !$down) {
            # Contact starts where the pointer was pressed.
            $down = 1;
            ($lx, $ly) = ($px, $py);
            emit([$EV_KEY, $BTN_TOUCH, 1], [$EV_ABS, $ABS_X, $px], [$EV_ABS, $ABS_Y, $py]);
            print "touchbridge: down $px,$py\n";
        } elsif ($btn && $down && $seen && ($px != $lx || $py != $ly)) {
            # Motion: BTN_TOUCH stays 1, only ABS changes.  Without this the
            # device can never swipe (the UI only ever saw press+release).
            ($lx, $ly) = ($px, $py);
            emit([$EV_ABS, $ABS_X, $px], [$EV_ABS, $ABS_Y, $py]);
            print "touchbridge: move $px,$py\n";
        }
        $seen = 0;
    }
}