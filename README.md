# PJSKヤミナベ!!!![Master37] AP脚本

> プロジェクトセカイ「ヤミナベ!!!!」MASTER（Lv.37）全连（AP）自动化脚本
>
> 谱面解析 + ADB 多点触摸注入，在平板上实现 AP

---

## 原理

不靠图像识别，直接解析官方 SUS 谱面，算出每个音符的**绝对时刻**和**屏幕坐标**，
再通过 ADB 把触摸事件序列（DOWN / MOVE / UP）注入到设备。

```
chart_329_master.json         谱面数据（1464 notes）
        │
        ▼
   executor.py                谱面 → 触摸事件
        │                     （长条单指固定 / flick 快滑 / tap 点击）
        ▼
   manual.py                  对齐 + 送排程给注入器
        │
        ▼
   classes.dex (Injector)     Android 端多点触摸注入
        │
        ▼
     平板 / 手机
```

## 目录

| 文件 | 说明 |
|---|---|
| `autoplay.bat` | Windows 一键启动入口 |
| `manual.py` | 主控：手动对齐 t0、生成排程、调用注入器 |
| `executor.py` | 核心：谱面 → 触摸事件（18:48 AP 成功版） |
| `data/chart_329_master.json` | 谱面数据 |
| `dev/src/Injector.java` | 注入器源码（app_process 方案） |
| `dev/dex/classes.dex` | 编译好的注入器，可直接用 |

## 环境要求

- Windows（`autoplay.bat` 用）
- Python 3.11（`tools\py311\python.exe`）
- Android SDK Platform-Tools（adb）
- Android 设备，**已 root 或可运行 app_process**
- 屏幕分辨率 3200×2136（本脚本按此标定，其他分辨率需改 `--screen`）

> 仓库不含 `tools/`（adb 与 Python 运行时），请自行准备。
> `autoplay.bat` 与 `manual.py` 里的路径需改成你自己的实际路径。

## 使用

1. 把 `dev/dex/classes.dex` 推到设备：
   ```
   adb push dev/dex/classes.dex /data/local/tmp/injector.dex
   ```

2. 修改 `manual.py` 里的 `ADB` 路径和 `--serial`（设备号），
   以及 `autoplay.bat` 里的工作目录。

3. 进游戏，选好「ヤミナベ!!!!」MASTER，停在开始前，
   然后运行 `autoplay.bat`。

4. 按提示操作：
   - 脚本启动注入器、送排程
   - **在「第一颗音符落到判定线」的那一刻按回车**
   - 脚本立即开始注入

5. 对齐不准时用偏移校准：
   ```
   python manual.py --offset +30    # 整体推迟 30ms（打早了用正数）
   python manual.py --offset -30    # 整体提前
   python manual.py --lead 200      # 按回车时谱面已走过 200ms
   ```

## 关键设计说明

脚本里几个不那么直观、但实测必要的处理：

**长条（hold）**
- 纯直条：单指按住起始 note 中心不动，到尾判松手
- 需要横向移动的（异形/S形）：手指跟随轨迹，或按定点的双指/三指覆盖
- 长条期间手指独占，不被其他音符抢占（否则一条长条会被拆成多个 pid）

**flick**
- 一次快速向上滑动（0.11 归一化 ≈ 235px，30ms），速度约 7800px/s
- 与长条同刻时，位置必须错开，不能落在同一像素
  （两条轨迹同坐标会被设备判成同一根手指，滑动被吞 → 判 Late）

**尾判余量**
- `HOLD_UP_SLACK_MS = 25`：覆盖实测约 21ms 的注入延迟

**事件层改动的铁律**
- 只删 DOWN/MOVE，**保留 UP**
  因为 UP 是 pid 释放标志，改了会让后续全谱 pid 分配连锁偏移
- 不要在事后重建长条手指（新增 DOWN/UP 同样会连锁）

## 已验证

- 谱面：ヤミナベ!!!! MASTER Lv.37
- 结果：**AP（All Perfect）**
- 事件数 5862，峰值并发 10 指，重复 DOWN 0

## 说明

本项目仅供学习研究（音游自动化 / 触摸注入技术），请勿用于影响他人的场合。