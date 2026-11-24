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
- Python 3.11
- Android SDK Platform-Tools（adb）
- Android 设备，**已 root 或可运行 app_process**
- 屏幕分辨率 **3200×2136**（本脚本按此标定，其他分辨率不能直接用）

---

## ⚠️ 必须自备的内容

仓库**故意不含**下面两样（体积大、且可自行获取）。因为脚本里的路径已经改成
**自调用**（`%~dp0` / `__file__`），所以这两样必须放在**与脚本同级的 `tools\` 目录**里，
保持下面这个结构才能直接跑：

```
PJSK-AP-Script\
├─ autoplay.bat
├─ manual.py
├─ executor.py
├─ data\
│   └─ chart_329_master.json
├─ dev\
│   ├─ src\Injector.java
│   └─ dex\classes.dex
└─ tools\                          ← 需自备，目录名必须叫 tools
    ├─ py311\
    │   └─ python.exe               ← Python 运行时
    └─ platform-tools\
        ├─ adb.exe                  ← 这三件是一套
        ├─ AdbWinApi.dll
        └─ AdbWinUsbApi.dll
```

### 1. Python 运行时

把整个 Python 3.11 便携版放到 `tools\py311\`，保证 `tools\py311\python.exe` 存在。

- 用官网 embeddable 包或任意便携版都行
- **必须装 `numpy`**（`mirror.py` 等依赖，虽然打歌链路主路径不强依赖，但缺了可能报错）
- 如果不想放 `tools\py311\`，就改 `autoplay.bat` 里那两处 `python.exe` 的相对路径

### 2. adb（Android SDK Platform-Tools）

把 `adb.exe`、`AdbWinApi.dll`、`AdbWinUsbApi.dll` **三个一起**放到
`tools\platform-tools\`（少一个 dll，adb 都起不来）。

- 下载：https://developer.android.com/tools/releases/platform-tools
- `manual.py` 会自动从这个目录找 adb，找不到才回退到系统 PATH

### 3. Android 设备条件

- 开启 **USB 调试**，连上后 `adb devices` 能看到设备且状态是 `device`
- 设备要能运行 `app_process`（多数已 root 的机器可以；未 root 的看机型）
- 首次连接时要在设备上点「允许 USB 调试」

---

## ⚠️ 新设备需要注意的点

### 1. 设备号必须改（最容易踩）

`manual.py` 第 60 行的默认设备号是开发者本人的平板：

```python
ap.add_argument('--serial', default='2b76871e')
```

**换设备后必须改这一行**，或运行时显式指定：

```
python manual.py --serial <你的设备号>
```

查看设备号：`adb devices` 第一列就是。

不改的话，注入器会去连一个不存在的设备，直接报连接失败。

### 2. 分辨率必须是 3200×2136

`executor.py` 里的坐标是按这块屏幕**硬标定**的：

```python
LANE_LEFT  = 346.0 / 3200.0     # 最左轨起点
LANE_PITCH = 210.0 / 3200.0     # 轨间距
Y_NORM     = 1594.0 / 2136.0    # 判定线 y
```

- 同为 3200×2136 → 直接可用
- 分辨率不同 → **坐标会整体错位**，不能只靠 `--screen` 参数解决，
  `executor.py` 里这三个常量要按新屏幕实测重新标定（分隔线位置、轨距、判定线高度）

### 3. `wm size` 报的是竖屏值，别被误导

部分设备 `adb shell wm size` 返回 `2136x3200`（竖屏顺序），而游戏是横屏。
**不要把这个值直接传给 `--screen`**，否则宽高颠倒、x/y 全错
（实测会得到 x=301 而不是正确的 451）。

`--screen` 请显式传 `3200x2136`。

### 4. 注入器要重新推一次

每台新设备都要把 dex 推到自己的 `/data/local/tmp/`：

```
adb push dev/dex/classes.dex /data/local/tmp/injector.dex
```

### 5. 延迟可能不同，需要重新校准

`HOLD_UP_SLACK_MS = 25` 和 flick 时序都是在这台设备上实测调出来的
（注入延迟约 21ms）。不同设备的注入延迟不同，**换机后建议先跑一次看判定**，
偏早就用 `--offset` 正数往后推，偏晚就用负数。

---

## 使用

1. 确认 `tools\` 已按上面的结构放好，`adb devices` 能看到设备。

2. 把 `dev/dex/classes.dex` 推到设备：
   ```
   adb push dev/dex/classes.dex /data/local/tmp/injector.dex
   ```

3. 改 `manual.py` 的 `--serial` 默认值为你的设备号（见上面「新设备注意点 1」）。

4. 进游戏，选好「ヤミナベ!!!!」MASTER，停在开始前，
   然后运行 `autoplay.bat`。

5. 按提示操作：
   - 脚本启动注入器、送排程
   - **在「第一颗音符落到判定线」的那一刻按回车**
   - 脚本立即开始注入

6. 对齐不准时用偏移校准：
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