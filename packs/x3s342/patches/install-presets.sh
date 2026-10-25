#!/bin/sh
# 首次启动把 /etc/miniapp/resources/presetpkgs/*.amr 装进 userdisk。
# OTA 不带 userdata，装机后的应用注册表要靠这一步补；全部成功才写标记。
MARK=/userdata/.vm-presets-installed
LOG=/userdata/applog/vm-presets.log
[ -f "$MARK" ] && exit 0

# 运行时稳定：memoryUsage 连续两次成功（应用启动期会重启一次）
i=0
while [ $i -lt 120 ]; do
    if miniapp_cli memoryUsage >/dev/null 2>&1; then
        sleep 3
        miniapp_cli memoryUsage >/dev/null 2>&1 && break
    fi
    sleep 1
    i=$((i + 1))
done

n=0
ok=0
: > "$LOG"
for f in /etc/miniapp/resources/presetpkgs/*.amr; do
    [ -f "$f" ] || continue
    n=$((n + 1))
    tries=0
    while [ $tries -lt 4 ]; do
        out=$(miniapp_cli install "$f" 2>&1)
        case "$out" in
            *'"ret": 0'*) ok=$((ok + 1)); break;;
        esac
        tries=$((tries + 1))
        sleep 2
    done
    [ $tries -ge 4 ] && echo "FAIL $(basename "$f"): $out" >> "$LOG"
done
echo "installed $ok/$n" >> "$LOG"
[ "$n" -gt 0 ] && [ "$ok" -eq "$n" ] && touch "$MARK"
exit 0
