#!/usr/bin/perl
# Generic "physical key" stand-in for the vendor HAL. Configured by env:
#   VM_KEY_DEVICE  uinput device name (HAL may look it up by name)
#   VM_KEY_BYPATH  by-path link name (HAL may open /dev/input/by-path/<it>)
#   VM_KEY_CODES   comma separated input codes to support
# The init script (S21vm-keys) exports these from /etc/vm-keys.env.
#
# uinput parses struct input_event by the **writer's** word size (6.1 compat
# path): 64-bit Perl = 24 bytes, 32-bit Perl = 16 bytes. s6/s7/x5/a6/a7 all
# ship a 32-bit Perl; writing 24 there is a short write with an empty $!.
use strict;
use warnings;
use Fcntl;
use Config;
use POSIX qw(mkfifo);

use constant UI_SET_EVBIT => 0x40045564;
use constant UI_SET_KEYBIT => 0x40045565;
use constant UI_DEV_CREATE => 0x5501;
use constant UINPUT => "/dev/uinput";
use constant FIFO => "/tmp/fake-keys.fifo";
use constant EV_KEY => 1;
use constant EV_SYN => 0;
use constant SYN_REPORT => 0;

my $DEV = $ENV{VM_KEY_DEVICE} || "gpio-keys";
my $BYPATH = $ENV{VM_KEY_BYPATH} || "fake-keys";
my @KEYS = map { 0 + $_ } grep { /^\d+$/ } split /,/, ($ENV{VM_KEY_CODES} || "102");
@KEYS or die "vm-keys: VM_KEY_CODES empty\n";

my $EVSIZE = $Config{ptrsize} == 8 ? 24 : 16;
my $EVFMT  = $Config{ptrsize} == 8 ? "q<q<S<S<l<" : "l<l<S<S<l<";

sysopen(my $ui, UINPUT, O_WRONLY | O_NONBLOCK) or die "open " . UINPUT . ": $!\n";
my $ioctl = sub {
    my ($req, $arg) = @_;
    ioctl($ui, $req, $arg) or warn "vm-keys: ioctl $req($arg): $!\n";
};
$ioctl->(UI_SET_EVBIT, EV_SYN);
$ioctl->(UI_SET_EVBIT, EV_KEY);
$ioctl->(UI_SET_KEYBIT, $_) for @KEYS;   # 按值传，不能传字符串的地址

my $udev = pack("a80", $DEV)
         . pack("S4", 0x19, 0x0001, 0x0001, 0x0100)
         . pack("l", 0)
         . pack("l*", (0) x 64)
         . pack("l*", (0) x 64)
         . pack("l*", (0) x 64)
         . pack("l*", (0) x 64);
syswrite($ui, $udev) == length($udev) or die "write uinput_user_dev: $!\n";
$ioctl->(UI_DEV_CREATE, 0);

sub emit {
    my ($code) = @_;
    for my $e ([EV_KEY, $code, 1], [EV_SYN, SYN_REPORT, 0],
               [EV_KEY, $code, 0], [EV_SYN, SYN_REPORT, 0]) {
        syswrite($ui, pack($EVFMT, 0, 0, $e->[0], $e->[1], $e->[2])) == $EVSIZE
            or warn "vm-keys: write event $code (size $EVSIZE): $!\n";
    }
}

my $node = "";
for (1 .. 50) {
    for my $e (glob("/sys/class/input/event*")) {
        open(my $fh, "<", "$e/device/name") or next;
        my $name = <$fh>;
        close($fh);
        if (defined $name && $name =~ /^\Q$DEV\E/) {
            ($node = $e) =~ s{/sys/class/input/}{/dev/input/};
            last;
        }
    }
    last if $node;
    select(undef, undef, undef, 0.1);
}
$node or die "vm-keys: uinput device '$DEV' never appeared\n";

my %links = map { $_ => 1 } ("/dev/input/by-path/$BYPATH", "/dev/input/by-path/fake-keys");
for my $link (sort keys %links) {
    unlink($link);
    symlink($node, $link) or warn "vm-keys: symlink $link: $!\n";
}

unlink(FIFO);
mkfifo(FIFO, 0666) or die "mkfifo " . FIFO . ": $!\n";
sysopen(my $fifo, FIFO, O_RDONLY | O_NONBLOCK) or die "open fifo: $!\n";
print STDERR "vm-keys: $node dev=$DEV codes=@KEYS -> " . join(",", sort keys %links)
    . " size=$EVSIZE\n";

my $buf = "";
while (1) {
    my $n = sysread($fifo, $buf, 64);
    if (!$n) { select(undef, undef, undef, 0.2); next }
    emit($_) for ($buf =~ /(\d+)/g);
    $buf = "";
}
