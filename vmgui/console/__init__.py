"""宿主侧可视化操作控制台（CLI → 可视化操作）。

`python -m vmgui --console [--pack <名>]` 进来。三条纪律（`vm/ui-council/`）：

* 控制台**独占 QMP 4446**，起机用原样的 `dpctl up --display none`（决策 #14 / #24）。
* 设备视图尺寸**从帧读**、**按面板声明裁剪**（决策 #8 / #27）。
* 有自己的 pid 记账 `vm/console.pid`，`dpctl down` 一并收（决策 #24 / Q15 ④）。
"""
