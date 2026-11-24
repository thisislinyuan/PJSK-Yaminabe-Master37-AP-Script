"""手动对齐播放器：由你控制 t0。

## 为什么这样最可靠

「点播放」到「第一颗音符落地」的延迟受加载、动画、设备状态影响，
自动测量需要投屏识别，但投屏只有约 6fps（166ms/帧），精度不够。
你在现场看着屏幕按回车，反应误差 150~250ms，可以用 --lead 抵消。

## 三种用法

**1. 你已经在演奏中，看到某颗音符快落地时按回车**
       python manual.py --lead 200
   脚本认为「按键时刻往前 200ms」就是谱面某处。配合 --mark 指定是哪一颗：
       python manual.py --lead 200 --mark 0
   表示「按键那一刻，谱面第 0.000s 那颗音符正要落地」。

**2. 你按回车那一刻，谱面正好走到第 N 秒**
       python manual.py --at 3000

**3. 先跑一遍看偏差，再校准**
       python manual.py --lead 200 --dry   # 只看排程，不注入

## 流程

   运行脚本 → 脚本启动注入器、送排程、打印提示 → 你按回车 → 立即开始注入
"""
import argparse
import json
import subprocess
import os
import sys
import time

sys.path.insert(0, '.')
from executor import build_events

ADB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 'tools', 'platform-tools', 'adb.exe')
DEX = '/data/local/tmp/injector.dex'
CLS = 'Injector'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--offset', type=int, default=0,
                    help='全局时间偏移 ms：正=整体推迟（音符打早了就用这个）')
    ap.add_argument('--cal', default='data/cal.json',
                    help='保存/读取校准偏移的文件')
    ap.add_argument('--chart', default='data/chart_329_master.json')
    ap.add_argument('--lead', type=int, default=0,
                    help='按键时刻 = 谱面第 0ms 往后推多少 ms（0=按键即第0颗）')
    ap.add_argument('--at', type=int, default=None,
                    help='按键时刻对应谱面第几毫秒（覆盖 --lead）')
    ap.add_argument('--mark', type=float, default=None,
                    help='按键时刻对应哪一颗音符的时刻（秒）')
    ap.add_argument('--only-first', type=float, default=None)
    ap.add_argument('--y', type=float, default=1594)
    ap.add_argument('--screen', default='3200x2136')
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--serial', default='2b76871e')
    a = ap.parse_args()

    chart = json.load(open(a.chart, encoding='utf-8'))
    ev = build_events(chart)

    # 读校准文件里的偏移（若命令行没显式给）
    import os
    cal_off = 0
    if os.path.exists(a.cal):
        try:
            cal_off = int(json.load(open(a.cal)).get('offset_ms', 0))
        except Exception:
            cal_off = 0
    total_off = a.offset if a.offset else cal_off
    if total_off:
        print('应用时间偏移 %+dms（正=整体推迟）' % total_off)
        ev = [(t + total_off * 1000, act, x, pid, y) for t, act, x, pid, y in ev]

    if a.only_first:
        ev = [e for e in ev if e[0] <= int(a.only_first * 1e6)]

    w, _ = (int(x) for x in a.screen.split('x'))
    print('谱面 %d 音符 / %d 事件 / 共 %.1f 秒'
          % (len(chart['notes']), len(ev),
             chart['notes'][-1]['t']))

    # 对齐点：按键时刻对应谱面的哪个时刻
    # lead>0 表示「你按键时，谱面已经走过 lead 毫秒」（按晚了，摸不到第0颗）
    # lead<0 表示「你按键时，谱面还没到第0颗」（按早了，需等 |lead| 毫秒）
    if a.at is not None:
        align_ms = a.at
    elif a.mark is not None:
        align_ms = int(a.mark * 1000) + a.lead
    else:
        align_ms = a.lead
    print('对齐点：按键那一刻 = 谱面 %d ms' % align_ms)
    if align_ms > 0:
        print('  （前 %d ms 的音符会跳过，因为已经过了）' % align_ms)
    elif align_ms < 0:
        print('  （会等 %d ms 才开始第一个音符）' % (-align_ms))

    # 事件是 5 元组 (t_us, action, x_norm, pid, y_norm_override)
    # y_norm_override 非 None 表示 flick 滑动中的中间点（纵向已上移）
    from executor import Y_NORM
    lines = []
    for t, act, xn, pid, yov in ev:
        yy = (yov * 2136.0) if yov is not None else a.y
        lines.append('E %d %d %.1f %.1f %d' % (t, act, xn * w, yy, pid))

    if a.dry:
        print('排程 %d 条，前 4 条:' % len(lines))
        for l in lines[:4]:
            print('  ', l)
        return 0

    # 启动注入器
    cmd = [ADB, '-s', a.serial, 'shell',
           'app_process -Djava.class.path=%s /system/bin %s' % (DEX, CLS)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    ok = False
    for _ in range(20):
        line = proc.stdout.readline()
        if not line:
            break
        if line.startswith('READY'):
            print('注入器就绪')
            ok = True
            break
    if not ok:
        print('注入器启动失败')
        proc.kill()
        return 1

    # 先送排程（不 GO），这样按键后只需发一行，往返最短
    proc.stdin.write('\n'.join(lines) + '\n')
    proc.stdin.flush()
    print('排程已送达（%d 条），等待你按键' % len(lines))

    print()
    print('=' * 56)
    if a.at is not None:
        print(' 请在「谱面第 %d ms」的那一刻按回车' % a.at)
        print(' 按回车后立即开始注入，不再等待')
    elif a.mark is not None:
        print(' 请在「第 %.2fs 那颗音符」落地的那一刻按回车' % a.mark)
        print(' 按回车后立即开始注入')
    else:
        print(' >>> 请在「第一颗音符落到判定线」的那一刻按回车 <<<')
        print(' 按回车后立即开始注入（第一颗音符会马上被打）')
    print('=' * 56)
    input()

    # 按键 → 只发一行，最小延迟
    # T0D 负值 = 已经走到对齐点
    shift = -align_ms
    proc.stdin.write('T0D %d\nGO\n' % shift)
    proc.stdin.flush()
    print('GO 已发（相对对齐点 %+dms）' % shift)

    for line in proc.stdout:
        line = line.strip()
        if line:
            print('  ', line)
        if line.startswith('DONE'):
            break
    try:
        proc.stdin.write('BYE\n')
        proc.stdin.flush()
    except Exception:
        pass
    time.sleep(0.5)
    print('完成')
    return 0


if __name__ == '__main__':
    sys.exit(main())