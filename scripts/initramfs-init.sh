#!/bin/busybox sh
# dictpen-vm initramfs init: load virtio/erofs/ext4, mount the Youdao rootfs,
# build a writable (optionally persistent) overlay, prepare dropbear keys, then
# switch_root into the device system (DICTPEN_INIT) or drop to a shell.
#
# Cmdline knobs:
#   DICTPEN_INIT=/sbin/init    target init
#   DICTPEN_OVERLAY=1          enable the writable overlay
#   DICTPEN_STATE=/dev/vdb     block device used for persistence
#   DICTPEN_DEVMODE=1          keep the miniapp launcher (S50launcher) enabled
#   DICTPEN_ROOTFSTYPE=erofs   filesystem type of the read-only root image
/bin/busybox mkdir -p /proc /sys /dev /tmp /mnt /run /ro /upper /state /data /userdata /userdisk /misc
/bin/busybox --install -s /bin
mount -t proc none /proc
mount -t sysfs none /sys
mount -t devtmpfs none /dev 2>/dev/null || true
# devpts has to be mounted by us: the firmware's own userspace never allocates a
# pseudo-terminal, so nothing in its boot mounts it, and dropbear answers every
# login that *asks* for a tty with "PTY allocation request failed on channel 0"
# (exit 255).  Commands run over ssh - "dpctl sh", our hooks - need no pty, so
# only an interactive client (double-clicked ssh-vm.cmd, VSCode, Termius) shows
# the failure, which is why it looked like the ssh setup was broken.
# switch_root below uses `mount --move /dev`, which takes submounts with it, so
# this survives into the real root.
mkdir -p /dev/pts
mount -t devpts devpts /dev/pts 2>/dev/null || true

echo "### dictpen-vm initramfs (kernel $(uname -r)) ###"

MODDIR=/lib/modules/$(uname -r)
for m in virtio_ring virtio virtio_pci_modern_dev virtio_pci_legacy_dev virtio_pci virtio_blk failover net_failover virtio_net evdev uinput drm drm_kms_helper drm_shmem_helper virtio_input ttm drm_ttm_helper drm_vram_helper bochs crc32c_generic libcrc32c erofs overlay crc16 mbcache jbd2 ext4 fat nls_cp437 nls_iso8859-1 vfat virtio-rng rfkill cfg80211 libarc4 mac80211 mac80211_hwsim ecc ecdh_generic bluetooth btintel btbcm btrtl btqca hci_uart x_tables ip_tables nf_defrag_ipv4 nf_defrag_ipv6 nf_conntrack nf_nat iptable_filter iptable_nat xt_MASQUERADE; do
    ko=$(find $MODDIR -name "$m.ko" 2>/dev/null | head -n1)
    if [ -n "$ko" ]; then
        # Module params on the kernel cmdline only work via userspace modprobe;
        # we load with busybox insmod, so pass params explicitly.
        P=""
        case "$m" in
            drm|drm_kms_helper) P="edid_firmware=Virtual-1:edid/pen.bin" ;;
            # two radios: wlan0 for the pen's station stack, wlan1 to act as the
            # AP it can scan and associate with (there is no Wi-Fi hardware here)
            mac80211_hwsim) P="radios=2" ;;
        esac
        if insmod "$ko" $P 2>/tmp/insmod-err; then
            echo "mod: $m ok"
        else
            echo "mod: $m FAILED: $(cat /tmp/insmod-err)"
        fi
    fi
done

sleep 2
echo "block devices: $(ls /dev/vd* /dev/sd* 2>/dev/null)"

ROOTDEV="${DICTPEN_ROOTDEV:-/dev/vda}"
# rootfs 镜像的文件系统类型也是平台属性（rv1106 族是 erofs，cv18xx 族是 ext4），
# 由 runtime 通过内核参数给出；这里不含任何机型信息。
ROOTFSTYPE="${DICTPEN_ROOTFSTYPE:-erofs}"
if mount -t "$ROOTFSTYPE" -o ro "$ROOTDEV" /ro 2>/tmp/mnt-err; then
    echo "### $ROOTFSTYPE root mounted: $ROOTDEV -> /ro ###"
else
    echo "### MOUNT FAILED for $ROOTDEV ($ROOTFSTYPE): $(cat /tmp/mnt-err) ###"
fi

INIT=$(cat /proc/cmdline | tr ' ' '\n' | grep '^DICTPEN_INIT=' | cut -d= -f2-)
OVERLAY=$(cat /proc/cmdline | tr ' ' '\n' | grep '^DICTPEN_OVERLAY=' | cut -d= -f2-)
DEVMODE=$(cat /proc/cmdline | tr ' ' '\n' | grep '^DICTPEN_DEVMODE=' | cut -d= -f2-)
AP=$(cat /proc/cmdline | tr ' ' '\n' | grep '^DICTPEN_AP=' | cut -d= -f2-)

# 固件包声明的东西（dpctl 把选中包的 patch 与 pack.env 并进了这个 initrd）。
# 没有包时全部按"关"处理 —— 下面的每处适配都只看这里和 /patches 下的文件，
# 框架自己不认识任何机型。
if [ -f /pack/pack.env ]; then
    . /pack/pack.env
    echo "### pack: ${PACK:-?} runtime=${RUNTIME:-?} panel=${PANEL:-?} ###"
else
    echo "### pack: 无（通用 initramfs，不套用任何机型适配）###"
fi

# A tmpfs-backed overlay is enough to get a writable root and gives us a
# chroot-able tree; it is replaced below if a persistent disk is available.
TARGET=/ro
if [ "$OVERLAY" = "1" ]; then
    mount -t tmpfs -o size=512m tmpfs /upper
    mkdir -p /upper/up /upper/work
    if mount -t overlay overlay -o lowerdir=/ro,upperdir=/upper/up,workdir=/upper/work /mnt 2>/tmp/ovl-err; then
        echo "### overlay ready (lower=/ro upper=tmpfs) ###"
        TARGET=/mnt
    else
        echo "### OVERLAY FAILED: $(cat /tmp/ovl-err) - continuing read-only ###"
    fi
fi

# --- persistent state disk -------------------------------------------------
# /dev/vdb keeps the overlay's upper layer plus the device data dirs alive
# across reboots.  On a blank image the ext4 filesystem is created with the
# guest's own e2fsprogs (run under chroot); any failure degrades gracefully to
# the tmpfs behaviour so the VM always boots.
STATE_DEV="${DICTPEN_STATE:-/dev/vdb}"
STATE_OK=0
if [ "$OVERLAY" = "1" ] && [ -b "$STATE_DEV" ]; then
    mkdir -p /state
    if mount -t ext4 -o rw "$STATE_DEV" /state 2>/tmp/st-err; then
        STATE_OK=1
        echo "### state: ext4 mounted ($STATE_DEV) ###"
    else
        mount --bind /dev "$TARGET/dev" 2>/dev/null
        for mke in /sbin/mke2fs /usr/sbin/mke2fs /sbin/mkfs.ext4 /usr/sbin/mkfs.ext4 /bin/mke2fs /usr/bin/mke2fs; do
            [ -x "$TARGET$mke" ] || continue
            echo "state: creating ext4 via $mke ..."
            if chroot "$TARGET" "$mke" -t ext4 -F -q -m 0 "$STATE_DEV" >/tmp/mkfs.log 2>&1 \
               && mount -t ext4 -o rw "$STATE_DEV" /state 2>>/tmp/st-err; then
                STATE_OK=1
                echo "### state: ext4 created and mounted ###"
                break
            fi
        done
        [ "$STATE_OK" = 1 ] || echo "### state: unavailable ($(head -n1 /tmp/st-err 2>/dev/null)) - using tmpfs ###"
    fi
fi

# Reclaim the state disk before it becomes the overlay upper (below).
# /state/upper and /state/work are created on $STATE_DEV itself, so a disk that
# is already full makes the overlay mount read-only:
#     overlayfs: failed to create directory /state/work/work (errno: 28)
# and every write to /mnt (including installing the S00 hooks) then fails with
# "Read-only file system".  The guest-side janitor (S00corelog) cannot help: it is
# installed *after* the overlay, and its script only exists under $TARGET, which is
# still read-only here.  So reclaim while /state is mounted directly.
#
# Two measured offenders on the 512 MiB disk (a7pro, RV1106):
#   userdisk/swap/swap.file   257 MiB  vendor swapfile; the VM has 2 GiB RAM and
#                                       used 0 bytes of it, so it is pure waste.
#   */applog, userdisk/corefile  log piles written by respawn loops.
# userdisk/secondary is deliberately left alone: it is the directory miniapp
# actively unpacks its app payload into (userdisk and userdata are separate
# inodes, not a bind pair), so deleting it just wastes bandwidth on the next run.
#   DICTPEN_STATE_TRIM=0        disable the reclaim
#   DICTPEN_STATE_SWAPFILE=1    keep the vendor swapfile (default: remove)
if [ "$STATE_OK" = 1 ] && [ "${DICTPEN_STATE_TRIM:-1}" != 0 ]; then
    echo "### state: reclaim (disk may be full before the overlay mounts) ###"

    # Cap crash/log piles by count.  Busybox ls/tail, no rm on directories.
    KEEP=${DICTPEN_COREFILE_KEEP:-500}
    for d in /state/userdisk/corefile /state/userdisk/applog \
             /state/data/applog /state/userdata/applog; do
        [ -d "$d" ] || continue
        n=$(ls -1 "$d" 2>/dev/null | wc -l)
        if [ "$n" -gt "$KEEP" ]; then
            ls -1t "$d" 2>/dev/null | tail -n +$((KEEP + 1)) \
                | while IFS= read -r f; do rm -f "$d/$f" 2>/dev/null; done
            echo "state: reclaimed $d ($n -> $(ls -1 "$d" 2>/dev/null | wc -l), keep $KEEP)"
        fi
    done

    # Vendor swapfile: the vendor mkswap's /userdisk/swap/swap.file, which grows to
    # 257 MiB and stays there.  It cannot be deleted while active, but it is only
    # activated by the vendor's init *after* /init has switched root, so here it is
    # still an inactive file and safe to drop.
    if [ "${DICTPEN_STATE_SWAPFILE:-0}" = 1 ]; then
        :
    else
        for f in /state/userdisk/swap/swap.file /state/userdata/swap/swap.file; do
            [ -f "$f" ] || continue
            rm -f "$f" 2>/dev/null && echo "state: removed vendor swapfile $f"
        done
    fi
fi

# Prefer a state-backed overlay upper so runtime system tweaks also survive.
if [ "$STATE_OK" = 1 ]; then
    mkdir -p /state/upper /state/work
    # the chroot bind (if the disk was just formatted) would keep /mnt busy
    umount "$TARGET/dev" 2>/dev/null
    if umount /mnt 2>/dev/null \
       && mount -t overlay overlay -o lowerdir=/ro,upperdir=/state/upper,workdir=/state/work /mnt 2>/tmp/ovl2-err; then
        echo "### overlay: persistent upper (/state/upper) ###"
    else
        echo "### overlay: persistent remount failed, back to tmpfs ($(cat /tmp/ovl2-err 2>/dev/null)) ###"
        mount -t overlay overlay -o lowerdir=/ro,upperdir=/upper/up,workdir=/upper/work /mnt 2>/dev/null
        STATE_OK=0
    fi
fi

# login -> root shell, keep / read-write
if [ "$OVERLAY" = "1" ] && [ -f "$TARGET/etc/inittab" ]; then
    sed -e 's|::respawn:-/bin/login|::respawn:-/bin/sh|' \
        -e 's|/bin/mount -o remount,ro /|/bin/true|' \
        "$TARGET/etc/inittab" > "$TARGET/etc/inittab.new" \
        && mv "$TARGET/etc/inittab.new" "$TARGET/etc/inittab" \
        && echo "inittab: login -> root shell, keep / rw"
fi

# device data partitions: persisted on the state disk when available.
# /data and /misc are symlinks (to /userdata and a block device), so only real
# directories are mounted - the symlinked names resolve to those targets.
for d in data userdata userdisk misc; do
    p="$TARGET/$d"
    [ -d "$p" ] || continue
    if [ -L "$p" ]; then
        echo "data mount: /$d is a symlink, covered via its target"
        continue
    fi
    if [ "$STATE_OK" = 1 ]; then
        mkdir -p "/state/$d"
        mount --bind "/state/$d" "$p" && echo "data mount: /$d (state)"
    else
        mount -t tmpfs tmpfs "$p" && echo "data mount: /$d (tmpfs)"
    fi
done
mkdir -p $TARGET/data/applog $TARGET/userdata/applog $TARGET/userdata/cfg \
         $TARGET/userdata/DictPenData $TARGET/userdisk/corefile 2>/dev/null

# --- activation license ------------------------------------------------------
# The pen activates by fetching a license bound to its device code and stores it
# in /userdata/cfg/license (174 bytes).  Two hooks cover the two cases:
#   S00license - runs before the app: the VM has no device code, so the server
#                round-trip cannot work and a development license is written
#                instead.  On a6pro (4.7.4) the app only tests that the file
#                exists - licenseVerified() never inspects its contents - which
#                is enough to skip the activation screen.  **a7pro (4.13.0) is
#                not like that**: YStartupModulePrivate::initSecondaryStartupSignal
#                really verifies it, and with no device code in system_info.bin
#                it logs "license verify failed" and parks the onboarding app
#                (验证引导, 8080282888534774) in front, which draws nothing.  The
#                only way past that on a7pro is a provisioned system_info.bin
#                (see packs/a7pro/README.md).
#   S00sysinfo - if DICTPEN_SN / DICTPEN_UUID are on the cmdline (or a
#                credentials file lives at /patches/sysinfo.creds), write them
#                into the 400-byte system_info.bin so S00license can defer to
#                S99license and fetch a real license.
#   S99license - runs after the network is up: if a device code is present
#                (a real device's system_info.bin was dropped in, see README),
#                fetch the real license with scripts/restore_license.sh.
# DICTPEN_LICENSE=0 (dpctl up --no-license) disables all three.
if [ -f /patches/restore_license.sh ] && [ -d "$TARGET/root" ]; then
    mkdir -p $TARGET/root/.vm
    cp -f /patches/restore_license.sh $TARGET/root/.vm/restore_license.sh \
        && echo "license: restore_license.sh installed"
    # The SN/UUID writer needs to run before S00license so that
    # S00license can see the device code and defer to S99license.
    if [ -f /patches/write_system_info.sh ]; then
        cp -f /patches/write_system_info.sh $TARGET/root/.vm/write_system_info.sh
        echo "license: write_system_info.sh installed"
        cat > $TARGET/etc/init.d/S00sysinfo <<'SYSOF'
#!/bin/sh
# Write DICTPEN_SN / DICTPEN_UUID / DICTPEN_MODEL / DICTPEN_SKU into
# /userdata/cfg/system_info.bin so that S00license can defer to S99license.
grep -q 'DICTPEN_LICENSE=0' /proc/cmdline 2>/dev/null && exit 0
SN=$(tr ' ' '\n' </proc/cmdline | grep '^DICTPEN_SN=' | cut -d= -f2-)
UUID=$(tr ' ' '\n' </proc/cmdline | grep '^DICTPEN_UUID=' | cut -d= -f2-)
MODEL=$(tr ' ' '\n' </proc/cmdline | grep '^DICTPEN_MODEL=' | cut -d= -f2-)
SKU=$(tr ' ' '\n' </proc/cmdline | grep '^DICTPEN_SKU=' | cut -d= -f2-)
# Fallback: credentials file bundled with the pack
[ -f /patches/sysinfo.creds ] && . /patches/sysinfo.creds
[ -n "$SN" ] && [ -n "$UUID" ] || exit 0
echo "S00sysinfo: writing SN=$SN" >/dev/kmsg
sh /root/.vm/write_system_info.sh -s "$SN" -u "$UUID" ${MODEL:+-m "$MODEL"} ${SKU:+-k "$SKU"} \
    >/tmp/sysinfo.log 2>&1
rc=$?
echo "S00sysinfo: rc=$rc $(tail -n 1 /tmp/sysinfo.log 2>/dev/null)" >/dev/kmsg
SYSOF
        chmod +x $TARGET/etc/init.d/S00sysinfo
        echo "S00sysinfo hook installed"
    fi
    cat > $TARGET/etc/init.d/S99license <<'LICEOF'
#!/bin/sh
# Ask the server for an activation license.  Only possible when the pen has a
# device code; the VM's unprovisioned system_info.bin has none, and S00license
# has already written the development license by then.
grep -q 'DICTPEN_LICENSE=0' /proc/cmdline 2>/dev/null && exit 0
CFG=/userdata/cfg/system_info.bin
[ -f "$CFG" ] || exit 0
[ -f /userdata/cfg/license ] && exit 0
grep -aqE '^ME[0-9]+' "$CFG" 2>/dev/null || exit 0
echo "S99license: restoring activation license ..." >/dev/kmsg
sh /root/.vm/restore_license.sh >/tmp/license.log 2>&1
echo "S99license: rc=$? $(tail -n 1 /tmp/license.log 2>/dev/null)" >/dev/kmsg
LICEOF
    chmod +x $TARGET/etc/init.d/S99license
    echo "S99license hook installed"

    # The development license must exist before S01systeminit starts the app:
    # the activation app reads /userdata/cfg/license once, when its page is
    # first shown, and a file that appears later is not picked up.  Writing it
    # here also means no app restart is needed on a fresh state disk.
    cat > $TARGET/etc/init.d/S00license <<'DEVLICEOF'
#!/bin/sh
# Development activation license for the VM (see initramfs-init.sh).  A license
# bound to a device code cannot be fetched without one, and on a6pro the app
# only tests that the file exists, so a fixed 174-byte placeholder is enough to
# skip the activation screen entirely.  On a7pro (4.13.0) the placeholder is
# really verified and fails - the device code must come from a provisioned
# system_info.bin, which this hook detects below and defers to S99license.
grep -q 'DICTPEN_LICENSE=0' /proc/cmdline 2>/dev/null && exit 0
CFG=/userdata/cfg/system_info.bin
LIC=/userdata/cfg/license
[ -f "$LIC" ] && exit 0
# provisioned device: leave the license to S99license's server round-trip
if [ -f "$CFG" ] && grep -aqE '^ME[0-9]+' "$CFG" 2>/dev/null; then
    echo "S00license: device code present - deferring to S99license" >/dev/kmsg
    exit 0
fi
printf '%s' '1:GJpNLNyW5U176Um66/kd6c28LrCxShU9Q092JVcGJZ4Ymk0s3JblTXvpSbrr+R3pzbwusLFKFT1DT3YlVwYlnhiaTSzcluVNe+lJuuv5HenNvC6wsUoVPUNPdiVXBiWeGJpNLNyW5U176Um66/kd6c28LrCxShU9Q092JVcGJZ4Y' > "$LIC"
chown root:root "$LIC" 2>/dev/null
chmod 640 "$LIC"
echo "S00license: dev license written ($(wc -c < "$LIC") bytes)" >/dev/kmsg
DEVLICEOF
    chmod +x $TARGET/etc/init.d/S00license
    echo "S00license (dev license) hook installed"
fi

# Windows QEMU's virtio-gpu lacks virgl/blob, so falcon's DRM path cannot
# complete; stop the app launcher from crash-looping on the console.  With
# DICTPEN_DEVMODE=1 the launcher is left alone for app-level debugging.
if [ "$DEVMODE" = "1" ]; then
    echo "devmode: miniapp launcher left enabled"
elif [ -f "$TARGET/etc/init.d/S50launcher" ]; then
    mv $TARGET/etc/init.d/S50launcher $TARGET/etc/init.d/S50launcher.disabled         && echo "miniapp launcher disabled (virtio-gpu DRM incomplete on Windows)"
fi

# Normalise the app's panel config for the virtual display, but **only the keys
# the pack lists** (/patches/cfg.json.overrides, one "key=value" per line) - the
# framework has no opinion about any panel and never rewrites the rest of the
# file.  A value starting with a digit is written as a JSON number, one starting
# with "/" as a JSON string, and true/false as a JSON boolean.
#
# Why a pack overrides these (see packs/<name>/patches/cfg.json.overrides):
#   direction  the real panel may be driven rotated (falcon then writes rows at a
#              stride for a rotated target); the VM's display has no plane
#              rotation, so the UI must be written 1:1 or the picture is sheared;
#   bpp        the panel may ask for 16bpp while the guest's planes only take
#              32-bit formats;
#   dbuffer    the app may page-flip between two buffers (yres_virtual = 2*yres).
#              A VM display that cannot grow its virtual size rejects
#              FBIOPUT_VSCREENINFO, and the flipped-to page is then outside the
#              scanout: the framebuffer has content while the screen stays black
#              (measured on s7pro). Turning it off makes the app draw directly;
#   tp         the real touch panel is a kernel module's device; in the VM that
#              device is republished by the touch bridge.
CFG=$TARGET/etc/miniapp/resources/cfg.json
if [ -f /patches/cfg.json.overrides ] && [ -f "$CFG" ]; then
    n=0
    while IFS='=' read -r k v; do
        case "$k" in ''|\#*) continue ;; esac
        case "$v" in
            [0-9]*) sed -i "s/\"$k\": *[0-9]*/\"$k\": $v/" "$CFG" ;;
            /*)     sed -i "s#\"$k\": *\"[^\"]*\"#\"$k\": \"$v\"#" "$CFG" ;;
            true|false) sed -i "s/\"$k\": *[a-z]*/\"$k\": $v/" "$CFG" ;;
            *)      continue ;;
        esac
        n=$((n + 1))
    done < /patches/cfg.json.overrides
    echo "cfg.json: applied $n override(s) from the pack"
fi

# The QEMU window's pointer arrives as BTN_LEFT on the virtio tablet, but the
# pen UI only reacts to BTN_TOUCH (it drives the device's own AXS touchscreen),
# so a small Perl bridge republishes the tablet through uinput as an "axs_ts"
# touchscreen.  It must be up before S01systeminit launches the app.
if [ -f /patches/touchbridge.pl ] && [ -d "$TARGET/root" ]; then
    mkdir -p $TARGET/root/.vm
    cp -f /patches/touchbridge.pl $TARGET/root/.vm/touchbridge.pl \
        && echo "touchbridge: script installed"
    cat > $TARGET/etc/init.d/S00touchbridge <<'TBEOF'
#!/bin/sh
# Start the uinput touch bridge, then wait for the device link the app opens
# (cfg.json's tp path), so it exists before S01systeminit starts the UI.
if [ -e /dev/uinput ] && [ -f /root/.vm/touchbridge.pl ] \
   && [ ! -e /dev/input/by-path/axs_ts ]; then
    setsid /usr/bin/perl /root/.vm/touchbridge.pl >/tmp/touchbridge.log 2>&1 </dev/null &
    i=0
    while [ $i -lt 50 ]; do
        [ -e /dev/input/by-path/axs_ts ] && break
        sleep 0.1
        i=$((i+1))
    done
fi
if [ -e /dev/input/by-path/axs_ts ]; then
    echo "S00touchbridge: touch device ready" >/dev/kmsg
else
    # bridge unavailable: keep the raw tablet so the app still gets a tp node
    ln -sf /dev/input/by-path/QEMU-Virtio-Tablet /dev/input/by-path/axs_ts
    echo "S00touchbridge: bridge failed, using the raw tablet" >/dev/kmsg
fi
TBEOF
    chmod +x $TARGET/etc/init.d/S00touchbridge
    echo "S00touchbridge hook installed"
fi

# --- battery: publish a healthy pack (only if the pack asks for it) ----------
# Battery state comes from /sys/class/power_supply/{battery,ac,usb,charger}:
# hal-battery reads it through libydhal_devices.so and republishes it over ubus.
# The VM has no PMIC, so the class directory is empty, every read fails and the
# app sees 0% - it then announces "low battery" and powers the pen off about a
# minute after the desktop appears.  Overlay the class directory with a tmpfs
# holding a healthy pack so the reading is plausible instead.
# A pack opts in with `[adapt] battery = true` (ADAPT_BATTERY=1 in pack.env):
# machines whose power stack reads something else must not get a fake pack.
if [ "$ADAPT_BATTERY" = "1" ]; then
cat > $TARGET/etc/init.d/S00battery <<'BATEOF'
#!/bin/sh
# Fake a healthy battery pack (the VM has no PMIC to talk to).
[ -d /sys/class/power_supply ] || exit 0
mount -t tmpfs none /sys/class/power_supply 2>/dev/null || {
    echo "S00battery: tmpfs mount failed" >/dev/kmsg; exit 0; }
mkdir -p /sys/class/power_supply/battery /sys/class/power_supply/ac \
         /sys/class/power_supply/usb /sys/class/power_supply/charger
echo 80          > /sys/class/power_supply/battery/capacity
echo 4200000     > /sys/class/power_supply/battery/voltage_now
echo 500000      > /sys/class/power_supply/battery/current_now
echo Discharging > /sys/class/power_supply/battery/status
echo Good        > /sys/class/power_supply/battery/health
echo 0           > /sys/class/power_supply/ac/online
echo 0           > /sys/class/power_supply/usb/online
echo 0           > /sys/class/power_supply/charger/online
echo Discharging > /sys/class/power_supply/charger/status
echo "S00battery: fake pack published (capacity 80%)" >/dev/kmsg
BATEOF
chmod +x $TARGET/etc/init.d/S00battery
echo "battery hook installed"
fi

# --- keep the crash-log pile from filling the state disk ---------------------
# CapFrame and run_input_event_monitor exit 0xFF00 and get respawned about once
# a second; every round drops a ~352 byte log into /userdisk/corefile (observed:
# 27877 files / 111MB after ~35h).  That is enough to fill the 512MB state disk,
# and then the guest's own services start failing with ENOSPC - including the
# input subsystem, which the user sees as "taps do nothing".  Trim at boot and
# then every 10 minutes while the guest runs.
#   DICTPEN_COREFILE=0                 turn the hook off
#   DICTPEN_COREFILE_KEEP=<n>          newest logs to keep (default 500)
#   DICTPEN_COREFILE_INTERVAL=<secs>   janitor period (default 600)
cat > $TARGET/usr/bin/vm-corelog-trim <<'TREOF'
#!/bin/sh
# Keep only the newest DICTPEN_COREFILE_KEEP entries of /userdisk/corefile.
# Count-based rather than age-based on purpose: the guest clock starts at 1970
# and only gets set once ntpd syncs, so an age rule would delete nothing (or
# everything) depending on when it happens to run.
D=/userdisk/corefile
KEEP=${DICTPEN_COREFILE_KEEP:-500}
cd "$D" 2>/dev/null || exit 0
# ls prints bare names, so do the deleting from inside $D: passing those names
# to rm from another cwd would target the caller's directory instead.
before=$(ls 2>/dev/null | wc -l)
[ "$before" -le "$KEEP" ] && exit 0
ls -t 2>/dev/null | tail -n +$((KEEP + 1)) | xargs rm -f 2>/dev/null
after=$(ls 2>/dev/null | wc -l)
echo "corelog: $D $before -> $after files (keep $KEEP)" >/dev/kmsg
TREOF
chmod +x $TARGET/usr/bin/vm-corelog-trim

cat > $TARGET/usr/bin/vm-corelog-janitor <<'JANEOF'
#!/bin/sh
# Trim the crash-log pile every DICTPEN_COREFILE_INTERVAL seconds.
while :; do
    sleep "${DICTPEN_COREFILE_INTERVAL:-600}"
    /usr/bin/vm-corelog-trim
done
JANEOF
chmod +x $TARGET/usr/bin/vm-corelog-janitor

cat > $TARGET/etc/init.d/S00corelog <<'CLHOOK'
#!/bin/sh
# Keep /userdisk/corefile from filling the state disk: trim now, and keep a
# small janitor loop running afterwards.  The vendor's crond is deliberately not
# used - /var/spool/cron/crontabs does not exist yet when the S00 hooks run, and
# a self-contained loop has no such timing dependency.
grep -q 'DICTPEN_COREFILE=0' /proc/cmdline 2>/dev/null && exit 0
/usr/bin/vm-corelog-trim
if ! ps w 2>/dev/null | grep -q '[v]m-corelog-janitor'; then
    setsid /usr/bin/vm-corelog-janitor >/dev/null 2>&1 </dev/null &
fi
echo "S00corelog: corefile trim active (boot + every ${DICTPEN_COREFILE_INTERVAL:-600}s)" >/dev/kmsg
CLHOOK
chmod +x $TARGET/etc/init.d/S00corelog
echo "corelog hook installed"

# --- virtual access point (wlan1) -------------------------------------------
# The pen's own Wi-Fi chip does AP mode in firmware and the rootfs ships no
# hostapd, so the guest is given a matching aarch64 build (its kernel is
# aarch64, so 64-bit binaries run as-is).  With it wlan1 broadcasts an SSID that
# the pen's own wlan0 stack - wpa_supplicant -Dnl80211, driven by wifi-manager -
# can scan and join, which is what makes the app's network page usable.
# DICTPEN_AP=0 或包里没开 [adapt].ap 都关掉。
if [ -d /patches/ap ] && [ "$AP" != "0" ] && [ "$ADAPT_AP" = "1" ] && [ -d "$TARGET/root" ]; then
    mkdir -p $TARGET/root/.vm/ap/lib $TARGET/root/.vm/ap/xtables
    cp -f /patches/ap/hostapd /patches/ap/ld-linux-aarch64.so.1 \
        $TARGET/root/.vm/ap/ 2>/tmp/ap-err
    cp -f /patches/ap/iptables-legacy $TARGET/root/.vm/ap/ 2>>/tmp/ap-err
    cp -f /patches/ap/lib/* $TARGET/root/.vm/ap/lib/ 2>>/tmp/ap-err
    cp -f /patches/ap/xtables/* $TARGET/root/.vm/ap/xtables/ 2>>/tmp/ap-err
    chmod 755 $TARGET/root/.vm/ap/hostapd \
        $TARGET/root/.vm/ap/iptables-legacy \
        $TARGET/root/.vm/ap/ld-linux-aarch64.so.1 2>/dev/null
    cat > $TARGET/root/.vm/ap/hostapd.conf <<'APCONF'
# Virtual access point published by dictpen-vm (see README).
# Open network on purpose: typing a passphrase on the pen's tiny touch UI
# through the mouse bridge is impractical.  Add wpa=2 + wpa_passphrase here for
# a WPA2 network.
interface=wlan1
driver=nl80211
ssid=Youdao-VM
hw_mode=g
channel=1
auth_algs=1
ignore_broadcast_ssid=0
APCONF
    cat > $TARGET/etc/init.d/S00wifi-ap <<'APHOOK'
#!/bin/sh
# Bring up the virtual access point before the pen's Wi-Fi stack starts.
# The AP address matches the firmware's own softap (dnsmasq's dhcp-range), so
# /etc/dnsmasq.conf starts serving leases to whoever joins.
AP=/root/.vm/ap
[ -x "$AP/hostapd" ] || exit 0
[ -e /sys/class/net/wlan1 ] || exit 0

/sbin/ifconfig wlan1 down 2>/dev/null
/usr/sbin/iw dev wlan1 set type __ap 2>/dev/null
/sbin/ifconfig wlan1 10.201.126.1 netmask 255.255.255.0 up 2>/dev/null

# dnsmasq (started later by S80) refuses to run without its lease directory
mkdir -p /var/lib/misc

# hostapd is exec'd through its own loader, so it shows up as ld-linux-aarch64
/usr/bin/killall -9 ld-linux-aarch64 2>/dev/null
/usr/bin/killall -9 hostapd 2>/dev/null
setsid "$AP/ld-linux-aarch64.so.1" --library-path "$AP/lib" \
    "$AP/hostapd" "$AP/hostapd.conf" >/tmp/hostapd.log 2>&1 </dev/null &
sleep 2
if grep -q "AP-ENABLED" /tmp/hostapd.log 2>/dev/null; then
    echo "S00wifi-ap: AP 'Youdao-VM' up on wlan1" >/dev/kmsg
else
    echo "S00wifi-ap: hostapd did not start: $(tail -n 2 /tmp/hostapd.log 2>/dev/null | tr '\n' ' ')" >/dev/kmsg
fi

# --- uplink: NAT the AP's clients out through eth0 --------------------------
# The AP lives inside this guest, so a client's packets really do arrive on
# wlan1 - forwarding them out eth0 and masquerading makes them leave through
# QEMU's user-mode NAT, and the replies come back the same way.
if [ -x "$AP/iptables-legacy" ]; then
    echo 1 > /proc/sys/net/ipv4/ip_forward
    IPT() {
        XTABLES_LOCKFILE=/tmp/xtables.lock XTABLES_LIBDIR="$AP/xtables" \
        "$AP/ld-linux-aarch64.so.1" --library-path "$AP/lib" \
            "$AP/iptables-legacy" "$@"
    }
    IPT -t nat -A POSTROUTING -o eth0 -s 10.201.126.0/24 -j MASQUERADE
    IPT -A FORWARD -i wlan1 -o eth0 -j ACCEPT
    IPT -A FORWARD -i eth0 -o wlan1 -j ACCEPT
    if IPT -t nat -L POSTROUTING -n 2>/dev/null | grep -q MASQUERADE; then
        echo "S00wifi-ap: uplink NAT ready (10.201.126.0/24 -> eth0)" >/dev/kmsg
    else
        echo "S00wifi-ap: uplink NAT FAILED" >/dev/kmsg
    fi
fi
APHOOK
    chmod +x $TARGET/etc/init.d/S00wifi-ap
    echo "S00wifi-ap hook installed (virtual AP on wlan1)"
fi

# --- virtual Bluetooth controller (hci0) ------------------------------------
# QEMU does not emulate Bluetooth and the kernel has hci_uart but no hci_vhci,
# so the HCI transport is the second serial port, whose other end is
# scripts/bt-hci-emu.py on the host.  btattach publishes it as hci0.
if [ -e "$TARGET/usr/bin/btattach" ] && [ -d "$TARGET/root" ]; then
    cat > $TARGET/etc/init.d/S00bluetooth <<'BTHOOK'
#!/bin/sh
# Attach the guest's hci_uart to the virtual controller.  The pen's firmware
# would hciattach a chip on ttyS1; in the VM the controller is emulated on the
# host and wired to /dev/ttyAMA1 by QEMU.
[ -e /dev/ttyAMA1 ] || exit 0
[ -e /sys/class/bluetooth/hci0 ] && exit 0

# bluetoothd is D-Bus based and this firmware only brings up the session bus;
# without the system bus it exits with "Unable to get on D-Bus".
if [ ! -e /var/run/dbus/system_bus_socket ] && [ -x /usr/bin/dbus-daemon ]; then
    mkdir -p /var/run/dbus
    /usr/bin/dbus-daemon --system --fork 2>/dev/null
fi

/usr/bin/killall -9 btattach 2>/dev/null
setsid /usr/bin/btattach -B /dev/ttyAMA1 -S 115200 -P h4 \
    >/tmp/btattach.log 2>&1 </dev/null &
i=0
while [ $i -lt 50 ]; do
    [ -e /sys/class/bluetooth/hci0 ] && break
    sleep 0.2
    i=$((i + 1))
done
if [ -e /sys/class/bluetooth/hci0 ]; then
    echo "S00bluetooth: hci0 attached" >/dev/kmsg
else
    echo "S00bluetooth: no hci0: $(head -n 1 /tmp/btattach.log 2>/dev/null)" >/dev/kmsg
fi
BTHOOK
    chmod +x $TARGET/etc/init.d/S00bluetooth

    # The rootfs ships no hciconfig, but bt_init.sh / runBluez poll it to decide
    # whether a controller is attached - without it the pen's BT start-up hangs.
    cat > $TARGET/usr/bin/hciconfig <<'HCISHIM'
#!/bin/sh
# Minimal hciconfig for dictpen-vm: report the kernel's HCI devices.
for d in /sys/class/bluetooth/hci*; do
    [ -e "$d" ] || continue
    echo "$(basename $d):	Type: Primary  Bus: UART"
done
HCISHIM
    chmod +x $TARGET/usr/bin/hciconfig
    echo "S00bluetooth hook + hciconfig shim installed"
fi

# quiet the hardware-absent lens script loop
if [ -f "$TARGET/usr/bin/len_onoff.sh" ]; then
    printf '#!/bin/sh\nexit 0\n' > $TARGET/usr/bin/len_onoff.sh
fi

# --- 包的"覆盖 guest 文件"（[adapt].replace）---------------------------------
# 包可以把任意文件覆盖到 guest 根的任意位置（例如替换某个 HAL 库）。覆盖谁、盖到哪
# 完全由包决定（列表 /patches/replace.list 由 dpctl 从 pack.toml 生成），框架不解释
# 这些文件是什么，因此也不认识任何机型。列表和文件要在 switch_root 后还能用，所以
# 先把它们搬进可写的 overlay（/root），再由一个早期 rcS 钩子在 app 起来之前覆盖。
if [ -f /patches/replace.list ] && [ -d "$TARGET/root" ]; then
    mkdir -p $TARGET/root/.vm/replace
    cp -f /patches/replace.list $TARGET/root/.vm/replace/list
    n=0
    while read -r src dst; do
        [ -n "$src" ] || continue
        [ -f "/patches/$src" ] || continue
        cp -f "/patches/$src" "$TARGET/root/.vm/replace/$src" && n=$((n + 1))
    done < /patches/replace.list
    cat > $TARGET/etc/init.d/S00replace <<'REPEOF'
#!/bin/sh
# 把包声明的文件覆盖到 guest 根（映射表来自 /root/.vm/replace/list）。
D=/root/.vm/replace
[ -f "$D/list" ] || exit 0
while read -r src dst; do
    [ -n "$src" ] || continue
    [ -f "$D/$src" ] || continue
    [ -e "$dst" ] || continue
    cp -f "$D/$src" "$dst" && chmod 755 "$dst"
    echo "S00replace: $src -> $dst rc=$?" >/dev/kmsg
done < "$D/list"
REPEOF
    chmod +x $TARGET/etc/init.d/S00replace
    echo "S00replace hook installed ($n file(s))"
fi

# boot logo: a pack-declared splash written straight to the framebuffer, both
# early (as soon as the DRM fb exists) and late (after rcS console traffic).
if [ -f /patches/logo.fb.gz ] && [ -d "$TARGET/root" ]; then
    mkdir -p $TARGET/root/.vm
    cp -f /patches/logo.fb.gz $TARGET/root/.vm/logo.fb.gz 2>/dev/null
    cat > $TARGET/etc/init.d/S99logo <<'LOGOEOF'
#!/bin/sh
if [ -f /root/.vm/logo.fb.gz ] && [ -e /dev/fb0 ]; then
    sleep 2
    gunzip -c /root/.vm/logo.fb.gz > /dev/fb0 2>/dev/null
    echo "S99logo: boot logo painted" >/dev/kmsg
fi
LOGOEOF
    chmod +x $TARGET/etc/init.d/S99logo
    echo "S99logo hook installed"
fi

# dropbear dev access: /etc/dropbear in the device rootfs is a SYMLINK to
# /var/run/dropbear (tmpfs on real hardware), so keep keys under the writable
# overlay (/root/.ssh) and let the device inittab start dropbear with -r.
if [ -d "$TARGET/root" ]; then
    mkdir -p $TARGET/root/.ssh && chmod 700 $TARGET/root/.ssh
    if [ ! -f $TARGET/root/.ssh/dropbear_rsa_host_key ]; then
        chroot $TARGET /usr/bin/dropbearkey -t rsa -s 2048 \
            -f /root/.ssh/dropbear_rsa_host_key >/dev/null 2>&1 \
            && echo "dropbear host key generated in /root/.ssh"
    fi
    grep -q dictpen-devvm $TARGET/root/.ssh/authorized_keys 2>/dev/null || \
        echo '__DICTPEN_PUBKEY__' >> $TARGET/root/.ssh/authorized_keys
    chmod 600 $TARGET/root/.ssh/authorized_keys
    grep -q dropbear $TARGET/etc/inittab 2>/dev/null || \
        sed -i 's|^::sysinit:/etc/init.d/rcS|::sysinit:/usr/sbin/dropbear -p 22 -r /root/.ssh/dropbear_rsa_host_key\n::sysinit:/etc/init.d/rcS|' $TARGET/etc/inittab
fi

# 包自带的钩子（可选）：包里声明 [adapt].hooks = ["patches/xxx.sh"] 后，dpctl 会把
# 它们并到 /patches/hooks/ ，这里按文件名顺序在 switch_root 之前逐个 source。
# 钩子在当前 shell 里执行，可以用 $TARGET 直接改 guest 根（框架不解释它们做什么）。
if [ -d /patches/hooks ]; then
    for h in $(ls /patches/hooks 2>/dev/null | sort); do
        echo "pack hook: $h"
        . "/patches/hooks/$h" || echo "pack hook $h 失败(rc=$?)"
    done
fi

if [ -n "$INIT" ] && [ -x "$TARGET$INIT" ]; then
    # paint the Youdao boot logo onto the display once the DRM fb exists
if [ -f /patches/logo.fb.gz ] && [ -e /dev/fb0 ]; then
    gunzip -c /patches/logo.fb.gz > /dev/fb0 2>/dev/null && echo "boot logo painted to fb0"
fi

echo "### switch_root -> $TARGET, init $INIT ###"
    mount --move /dev $TARGET/dev 2>/dev/null || true
    mount --move /proc $TARGET/proc 2>/dev/null || true
    mount --move /sys $TARGET/sys 2>/dev/null || true
    # /state lives in the initramfs root; move it into the new root or it would
    # be torn down with the initramfs.
    mkdir -p "$TARGET/state" 2>/dev/null
    mount --move /state "$TARGET/state" 2>/dev/null && echo "state: moved to /state (persistent)"
    exec switch_root "$TARGET" "$INIT"
fi

echo "###BOOT-OK### interactive shell (exit to respawn)"
while :; do
    setsid cttyhack sh
    echo "shell exited, respawning in 1s ..."
    sleep 1
done