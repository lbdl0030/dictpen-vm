
## 声音（2026-10-22）

两件事缺一不可，缺了 app 就只是「点了没反应」：

1. **`[audio].cards = 2`**：厂商播放链路按 card 序号打开设备。只有 `card0` 时
   SoundPlayer 报 `ALSA lib pcm_hw.c:(_snd_pcm_hw_open) Invalid value for card`，
   `openOutputDevice faild`。第二块 `usb-audio` 出现后 `open=======success`、
   `card1 ... state: RUNNING`。
2. **拉起 SoundPlayer**：x6pro 的 `rcS` 里没有它的入口，`S50vm-ui` 在
   `runSysconfigMgr` 之后补一句 `guardian_run /usr/bin/runSoundPlayer start`，
   否则 app 的 `playMedia` 发出去没人消费。

验证（用户入口）：首页 →「随身听」→ 全部 → 小红帽 → 第 1 集，
`dpctl audio-check` 之外再用宿主端点峰值量一次：实测 52.6%（手工双卡）与
59.8%（上述两处改动落包后冷启动），故事连续播放。
