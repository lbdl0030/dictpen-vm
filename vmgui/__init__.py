"""vmgui - QEMU 虚拟机客户端 GUI（显示 + 输入，适配不同固件/虚拟硬件）。

设计见 .trae/documents/vmgui-design.md。

层次：
    config       配置 schema / 合并 / 优先级 / 保存 / 热重载
    backends     协议适配层（qmp 优先，vnc 可选）
    core         连接生命周期、事件总线、日志
    view         Qt 窗口 / 画布 / 缩放 / 设置面板
    input        坐标逆变换、键映射、鼠标捕获
"""
__version__ = "0.1.0"