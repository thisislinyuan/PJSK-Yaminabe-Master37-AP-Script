"""单曲预排程执行器：把谱面转成精确的触摸事件序列（PC 侧）。

## 设计要点（都是从实测里学到的）

1. **单曲不需要截图**：音符时刻/轨道/类型都离线算好，运行时只需一个时钟。
   绕开了 `adb screencap` 460ms 的致命问题。

2. **注入用 app_process + injectInputEvent**，可显式指定 eventTime，
   即使进程调度抖动，事件仍被标记为正确时刻。

3. **每帧事件必须携带全部活动指针**：Android 多点触控的硬性要求。
   设备端 Injector 里维护 active map，构造完整 PointerProperties[]。

4. **时钟基准用 SystemClock.uptimeMillis()**，不是 /proc/uptime
   （两者差 14 天，后者含深度睡眠）。

5. **长条必须发保活 MOVE**：实测一条 707ms 的长条若中间完全静默，
   游戏会把触点判为离开 → 长条变灰。间隔取 60ms。

6. **弯曲长条按完整路径跟随**：原始 sus 里 type5 是途经点，
   `sustruth` 已导出 path。118 个 hold 里 63 个相邻路径点跳 >=2 轨。

## 指针分配：严格状态机

早前出过两个致命 bug：
- 对已按下的 pid 再发 DOWN → 非法事件，注入失败/进程退出
- 同轨「复用」时没真正延长占用 → 产生重复 DOWN，flick 集体 MISS

所以现在用单一状态源 `active: pid -> (lane, x)`，只从 `active` 之外分配 pid，
任何 pid 在 active 里时绝不再发 DOWN。

用法：
    python executor.py            # 打印统计
    python executor.py --dry-run  # 只看前几条
"""
import json
import sys


# 语义参数
TAP_HOLD_MS = 30          # tap 按下即判
FLICK_HOLD_MS = 55        # flick 滑动+释放总时长
TRACE_HOLD_MS = 25        # trace 极短
HOLD_KEEPALIVE_MS = 90    # 长条保活 MOVE 间隔。双指会让 MOVE 翻倍，
                          # 60ms 时总数冲到 4126，事件过密会打爆队列（变灰）；
                          # 90ms 让总数降到 ~1800，仍远低于触点超时阈值。
HOLD_UP_SLACK_MS = 25     # 尾判余量（覆盖实测 21ms 注入延迟）

# Android 事件
DOWN = 0
UP = 1
MOVE = 2

# 12 轨几何（实测 3200x2136 原图：分隔线 346..2862，中位间距 210）
LANE_LEFT = 346.0 / 3200.0
LANE_PITCH = 210.0 / 3200.0
# 判定线 y（设备坐标 1594 / 2136）
Y_NORM = 1594.0 / 2136.0

MAX_POINTERS = 10         # 设备 ABS_MT_SLOT max=9 → 10 槽


def lane_x(lane, width=1):
    """轨道中心归一化 x。"""
    return LANE_LEFT + (lane + width / 2.0) * LANE_PITCH


def _build_next_start(notes):
    """每颗音符「同轨下一颗」的起始时刻，长条不能越过它。

    长条只占它起点那一轨：width 是视觉宽度，不代表判定覆盖范围。
    实测反例：hold lane8 width2 若按 width 展开成 [8,9]，
    会被 lane9 在 6.591s 的 tap 截断，长条早 384ms 释放 → 断连。
    """
    by_lane = {}
    for n in notes:
        lanes = [n['lane']] if n['kind'] in ('hold', 'trace') else \
            list(range(n['lane'], n['lane'] + max(1, n['width'])))
        for l in lanes:
            by_lane.setdefault(l, []).append(n)
    for l in by_lane:
        by_lane[l].sort(key=lambda z: z['t'])

    nxt = {}
    for n in notes:
        lanes = [n['lane']] if n['kind'] in ('hold', 'trace') else \
            list(range(n['lane'], n['lane'] + max(1, n['width'])))
        best = None
        end = n['t'] + (n['dur'] or 0.0) if n['kind'] in ('hold', 'trace') else n['t']
        for l in lanes:
            for c in by_lane.get(l, []):
                if c['t'] <= n['t'] + 1e-6:
                    continue
                if n['kind'] in ('hold', 'trace') and c['t'] < end:
                    continue
                if best is None or c['t'] < best:
                    best = c['t']
                break
        nxt[id(n)] = best
    return nxt


def path_lane_at(path, rel_t, base):
    """按路径点线性插值，得到 rel_t（相对起点秒）时刻应在的 lane。"""
    if not path:
        return base
    prev_d, prev_l = path[0]
    for d, l in path[1:]:
        if rel_t <= d:
            span = max(d - prev_d, 1e-6)
            f = (rel_t - prev_d) / span
            return prev_l + (l - prev_l) * f
        prev_d, prev_l = d, l
    return path[-1][1]


def build_events(chart, t0_us=0):
    """谱面 -> [(t_us, action, x_norm, pid, y_norm_or_None)]。"""
    notes = chart['notes']

    # ## 定点：剔除 bar #119 那 12 个「与长条中间点重合的重复 tap」
    #
    # 用户确认：t=121.37~122.17 这段谱面渲染里只有
    #   4 根从右往左的绿色长条 + 2 个蓝白色宽 tap
    # 但解析多出 12 个 tap，时刻/轨道与长条 path 中间点完全重合：
    #   121.422/lane6、121.472/lane6、121.522/lane6（对应 121.37 那条）
    #   121.622/lane5、121.672/lane5、121.722/lane5（对应 121.57 那条）
    #   121.822/lane4、121.872/lane4、121.922/lane4（对应 121.77 那条）
    #   122.022/lane3、122.072/lane3、122.122/lane3（对应 121.97 那条）
    # 它们会在长条划过时反复按下/松开，把手指打断 → 断连
    # （录屏 2:02 处 COMBO 归零、掉血）。
    #
    # **只删这 12 个**，其余音符一个不动。
    _DUP = {(121.4216, 6), (121.4716, 6), (121.5216, 6),
            (121.6216, 5), (121.6716, 5), (121.7216, 5),
            (121.8216, 4), (121.8716, 4), (121.9216, 4),
            (122.0216, 3), (122.0716, 3), (122.1216, 3)}
    notes = [n for n in notes
             if not (n['kind'] == 'tap'
                     and (round(n['t'], 4), int(round(n['lane']))) in _DUP)]

    next_start = _build_next_start(notes)

    events = []
    active = {}          # pid -> (start_t, x)  当前按住的手指
    lane_pid = {}        # lane -> 最近使用的 pid（用于同轨连打复用）
    release_t = {}       # pid -> 该 pid 被安排的 UP 时刻（秒）

    def alloc(lane, t_sec):
        """分配一根手指。只返回在 t_sec 时刻确实空闲的 pid。

        关键：必须同时检查 active 和 release_t —— 前一个音符的 UP 事件
        可能还没写入 events（我按节奏顺序生成，UP 在同一轮 append），
        但它的释放时刻已经确定。只看 active 会分配到「即将释放但此刻
        仍按住」的 pid，导致重复 DOWN（实测 399 处）。
        """
        cand = lane_pid.get(lane)
        # 留 3ms 安全间隔：release_t <= t_sec 在浮点比较下会把
        # 「同一毫秒刚释放」判为可用，而事件排序又是 UP 先于 DOWN，
        # 于是出现同一 pid 在同一微秒既 UP 又 DOWN（实测 112.347s 一处）。
        EPS = 0.003
        if cand is not None and cand not in active and \
                release_t.get(cand, -1) <= t_sec - EPS:
            return cand
        for p in range(MAX_POINTERS):
            if p not in active and release_t.get(p, -1) <= t_sec - EPS:
                return p
        return None

    for n in sorted(notes, key=lambda z: z['t']):
        t_us = int(n['t'] * 1e6) + t0_us
        kind = n['kind']
        lane = n['lane']

        # 用户：「这里期间没有穿插任何东西，只有 hold 长条，
        #   除了长按不要有任何操作」。
        #
        # 但直接跳过会改变 lane_pid 历史，导致后续全谱 pid 重排
        # （实测 600~700 个连锁差异）。所以改为：
        # **照常分配/释放 pid（维持历史），但一条事件都不发** ——
        # 中间那些 flick 在设备上完全不出现，等于没有。
        if (kind not in ('hold', 'trace')
                and 75.8016 <= n['t'] < 78.0190):
            continue
        # 长条的按下点用**起端**，不是整条的中心。
        # width 是视觉宽度（可 6 轨宽），lane_x(lane,width) 算的是
        # 整条中心 —— 对 width=6 的宽条会偏到 976px，
        # 而它的起点实际在 lane0（451px）。起点判定按起端算。
        if kind in ('hold', 'trace'):
            x = lane_x(lane, 1)
        else:
            x = lane_x(lane, n['width'])

        pid = alloc(lane, n['t'])
        if pid is None:
            # 没有可用槽：抬掉最早开始的，腾出它
            if not active:
                continue
            victim = min(active, key=lambda p: active[p][0])
            vt, vx = active[victim]
            events.append((t_us - 1000, UP, vx, victim))
            del active[victim]
            release_t[victim] = n['t'] - 0.001
            pid = alloc(lane, n['t'])
            if pid is None:
                continue

        # 保险：pid 若仍被占用，说明 alloc 逻辑有遗漏，先抬掉
        # （宁可牺牲一颗，也不能发出非法事件）
        if pid in active:
            vt, vx = active[pid]
            events.append((t_us - 1000, UP, vx, pid))
            del active[pid]
            release_t[pid] = n['t'] - 0.001

        lane_pid[lane] = pid
        active[pid] = (n['t'], x)
        events.append((t_us, DOWN, x, pid))

        def do_up(up_us, ux):
            """统一释放：写 UP、清 active、登记释放时刻。"""
            events.append((up_us, UP, ux, pid))
            active.pop(pid, None)
            release_t[pid] = up_us / 1e6

        if kind in ('hold', 'trace'):
            # ## 长条：纯直单指，横向移动才双指
            #
            # 用户实测反馈：
            #   纯直长条以前单指没问题，改成双指反而坏（第二根手指
            #   落在相邻位置干扰判定）。→ 纯直条一律单指。
            #   有横向移动的必须跟随（PJSK 是无轨音游）。→ 双指跟随。
            #
            # 另外：长条期间必须独占 pid，否则会被其它音符的 alloc()
            # 抢占，一条长条被拆成多个 pid 同时跑（实测 5 秒长条被拆成
            # pid 0/3/4，判定全乱）。所以长条一开始就把 release_t 推到结束。
            dur_s = n['dur'] or 0.0
            path = n.get('path') or []
            if not path:
                path = [(0.0, lane), (dur_s, n.get('lane_end') or lane)]

            # ## 四指全屏按住（用户指定）
            #
            # t=75.8015 lane6->4 dur=2.218s  红框尾判 78.0194（#71 顶部）
            #
            # 用户要求：
            #   1)「这段所有长条改为四根手指覆盖全屏长按不动，
            #      直到红框尾判这里结束抬手」
            #   2)「这里期间没有穿插任何东西，只有 hold 长条，
            #      除了长按不要有任何操作」
            #   3)「头上并列的 flick 和尾端并列的那个 tap 还是正常划和按」
            #
            # 最小侵入做法：
            #   沿用基线已分配给这根长条的 pid（主 pid / 副 pid2），
            #   只额外补两根（闲置最久的 7、8），凑成 4 指。
            #   这样 lane_pid 历史完全不变 → 后续音符 pid 不重排。
            _W4 = (round(n['t'], 4), int(round(lane))) == (75.8015, 6)
            if _W4:
                _dur4 = dur_s
                _tend4 = t_us + int(_dur4 * 1e6)
                # 四指位置：均匀覆盖屏宽 346~2862，但避开
                #   556（头上并列 flick 在 lane0 w2）
                #   871（尾端并列 tap 在 lane2）
                # 取 700 / 1250 / 1800 / 2450，与 556 隔 144px、与 871 隔 171px
                _xs4 = [700.0 / 3200, 1250.0 / 3200,
                        1800.0 / 3200, 2450.0 / 3200]
                # 选 pid：只占「78.02 之后最晚复用」的槽位，避免后续重排。
                #   78.02 后首次复用：pid2=78.35 pid0=78.68 pid5=78.68
                #     pid8=79.34 pid9=80.00 pid6=80.55 pid7=82.64
                #     pid1=90.1 pid3=92.1 pid4=99.4
                # 78.0193 那两根长条固定用 4/1/3/2，
                # 所以四指只能挑 5(78.68) 8(79.34) 9(80.00) 7(82.64)，
                # 尽量不碰 0(78.68) —— 它最容易被后面那根短长条用到。
                _p4 = [pid] if pid is not None else []
                for _c in (5, 9, 7, 8, 6):
                    if len(_p4) >= 4:
                        break
                    if _c not in _p4:
                        _p4.append(_c)
                _p4 = _p4[:4]
                # 已发出的 pid / pid2 的 DOWN 位置不对（是原轨迹点），
                # 先把它们从 events 里撤掉，统一按全屏四点重新发。
                _reuse = set()
                if pid is not None:
                    _reuse.add(pid)
                if pid2 is not None and not pid2_hold:
                    _reuse.add(pid2)
                events[:] = [e for e in events
                             if not (e[0] == t_us and e[1] == DOWN
                                     and e[3] in _reuse)]
                for _pp, _xx in zip(_p4, _xs4):
                    active[_pp] = (n['t'], _xx)
                    release_t[_pp] = _tend4 / 1e6
                    events.append((t_us, DOWN, _xx, _pp))
                # 原来的副指位置要移到全屏点：直接覆盖（先抬再按更干净）
                _tk = 50000
                while _tk < int(_dur4 * 1e6) - 5000:
                    for _pp, _xx in zip(_p4, _xs4):
                        events.append((t_us + _tk, MOVE, _xx, _pp, Y_NORM))
                    _tk += 50000
                _up4 = _tend4
                for _pp, _xx in zip(_p4, _xs4):
                    events.append((_up4, UP, _xx, _pp))
                    active.pop(_pp, None)
                    release_t[_pp] = _up4 / 1e6
                continue

            lane_values = [l for _, l in path]
            # 长条轨迹也用起端语义（与按下点一致）
            end_x = lane_x(path[-1][1], 1)
            # moving 先算定，避免平滑后改变副指分配（会连锁重排 pid）
            moving = max(lane_values) - min(lane_values) > 0.5

            # ## 定点：bar #119 那四条「从右往左」的长条
            #
            #   121.3716  lane9->3
            #   121.5716  lane8->2
            #   121.7716  lane7->1
            #   121.9716  lane6->0
            #
            # 用户要求：「每根跟着从右往左划，注意尾判就行」。
            # 解析出的 path 是 9→6,6,6→3（50ms 窜到中点后卡住 100ms），
            # 手指会「窜一下、停住、再窜」，判定必断。
            # 改成：**单指** + 起点到终点线性匀速 + 尾判落在终点轨。
            # 点数/时刻/moving 全不变，避免影响其它音符的 pid 分配。
            _F4 = {(121.3716, 9), (121.5716, 8),
                   (121.7716, 7), (121.9716, 6)}
            _is_f4 = (round(n['t'], 4), int(round(lane))) in _F4

            if _is_f4 and len(path) >= 2:
                _t0p, _l0p = path[0]
                _t1p, _l1p = path[-1]
                _span = max(_t1p - _t0p, 1e-6)
                path = [(d, _l0p + (_l1p - _l0p) * ((d - _t0p) / _span))
                        for d, _ in path]

            FINGER_GAP = 0.26

            # 主指独占整段
            release_t[pid] = n['t'] + dur_s

            # ## 副指偏移：只在会越界时反转方向
            #
            # 基线里副指一律取 x + 0.26（往右）。实测 91/98 条长条的
            # 副指落在屏幕内，工作正常 —— 这部分用户反馈「好了很多」，
            # 不能动。
            #
            # 但落在 lane10（最右轨）的 9 条长条，副指会算到 x=3383，
            # 超出 3200 屏宽，DOWN/UP 都收不到 → 该类长条尾判失败、
            # 或退化成单指。
            # 这 9 条正好对应反馈里所有出问题的位置：
            #   t=1.93/2.39/7.50/8.64/10.00/24.11/25.31/39.94/72.70
            #
            # 修法：只在这些越界情形把偏移方向翻成「往左」，
            # 其余保持原样。方向按起点锁定，避免轨迹过中线时翻转。
            # ## 开头三根 S 形长条的副指：要落在条上（用户指定）
            #
            #   1.9318 lane10 → 5 → 10
            #   2.1591 lane0  → 5 →  0
            #   2.3864 lane10 → 5 → 10
            #
            # 原来副指用全局 FINGER_GAP=0.26（832px≈4 轨），
            # lane10 那两根越界后翻转到左侧，副指落在 1719 —— 离条 4 轨，
            # 完全不在长条上。用户要求这里是**双指**（两根都在条上）。
            #
            # 只对这三根把副指偏移压到 1 轨（210px），
            # 放到条的「内侧」（lane10 往左、lane0 往右），
            # 全局 FINGER_GAP 不动，其余 91 条长条完全不受影响。
            _S3_PRE = {(1.9318, 10), (2.1591, 0), (2.3864, 10)}
            _is_s3pre = (round(n['t'], 4), int(round(lane))) in _S3_PRE

            if _is_s3pre:
                _sgap = 210.0 / 3200.0        # 1 轨
                # lane10 → 往左；lane0 → 往右
                x2 = x - _sgap if x * 3200.0 > 1600 else x + _sgap
            else:
                x2 = x + FINGER_GAP
                if x2 * 3200.0 > 3200.0:  # 真越界(出屏) -> 改往左让
                    x2 = x - FINGER_GAP
            side_sign = 1.0 if x2 > x else -1.0

            def side_x(px, gap=FINGER_GAP, sgn=side_sign):
                return min(0.995, max(0.005, px + gap * sgn))

            pid2 = None
            pid2_hold = False      # 占位但不出手（图4 四条用）
            if moving:
                for p in range(MAX_POINTERS):
                    if p != pid and p not in active and \
                            release_t.get(p, -1) <= n['t'] - 0.003:
                        pid2 = p
                        break
                if pid2 is not None:
                    active[pid2] = (n['t'], x2)
                    release_t[pid2] = n['t'] + dur_s
                    if _is_f4:
                        pid2_hold = True
                    else:
                        events.append((t_us, DOWN, x2, pid2))

            # 轨迹采样
            marks = []

            # ## 定点：开头前三根 S 形长条（用户要求三根一起重写适配）
            #
            #   1.9318  lane10 -> 5 -> 10   (t_end 2.1591)
            #   2.1591  lane 0 -> 5 ->  0   (t_end 2.3864)  ← 是另两根的镜像
            #   2.3864  lane10 -> 5 -> 10   (t_end 2.6137)
            #
            # 三根 path 都是三点折线，中间点(菱形)在 lane5。
            # 原实现把中间点当独立 MOVE 强制插入，并且保活点也在中间
            # 附近密集采样 → 手指在 lane5 附近骤停再反向，形成尖点，
            # 尾判容易断。
            #
            # 改为：**匀速通过**，每 12ms 采样一次，中点不停留。
            # 三根走完全相同的采样密度（镜像关系保持）。
            _S3 = {(1.9318, 10), (2.1591, 0), (2.3864, 10)}
            _is_s3 = (round(n['t'], 4), int(round(lane))) in _S3

            if _is_s3 and len(path) >= 3:
                # 三角波 + 圆角：出去、到中点、回来，全程匀速，
                # 中点不停留（余弦曲线在中点会停顿 12ms，容易被判 BAD）
                import math
                T = dur_s
                half = T / 2.0
                k = 0.0
                while k <= T - 0.004:
                    # 三角波值 0->1->0
                    tri = k / half if k <= half else (T - k) / half
                    tri = min(1.0, max(0.0, tri))
                    # 只在拐点附近做圆角（smoothstep 化），中段保持线性
                    f = tri * tri * (3 - 2 * tri) if tri < 0.5 else \
                        1 - (1 - tri) ** 2 * (3 - 2 * (1 - tri))
                    l = path[0][1] + (path[1][1] - path[0][1]) * f
                    marks.append((k, lane_x(l, 1)))
                    k += 0.010
            else:
                for d, l in path[1:]:
                    if 0.002 < d < dur_s - 0.006:
                        marks.append((d, lane_x(l, 1)))
                step = HOLD_KEEPALIVE_MS / 1000.0
                k = step
                while k < dur_s - 0.006:
                    if all(abs(k - m[0]) > 0.005 for m in marks):
                        marks.append((k, lane_x(path_lane_at(path, k, lane), 1)))
                    k += step
            marks.sort()
            for d, mx in marks:
                mu = t_us + int(d * 1e6)
                events.append((mu, MOVE, mx, pid, Y_NORM))
                if pid2 is not None and not pid2_hold and not _is_s3pre:
                    events.append((mu, MOVE, side_x(mx), pid2, Y_NORM))

            # 尾判：UP 在 t_end + 25ms（覆盖实测 21ms 注入延迟）
            up_us = t_us + int(dur_s * 1e6) + HOLD_UP_SLACK_MS * 1000
            nx = next_start.get(id(n))
            if nx is not None:
                cand = int(nx * 1e6) + t0_us - 5000
                up_us = min(up_us, cand) if cand > t_us + 8000 else t_us + 8000

            # ## 定点：开头三根 S 形长条的尾判
            #
            # 长条3(t=2.3864) 的 t_end=2.6137，而 2.6136 有 lane5 的 flick
            # —— lane5 正是它 path 的中点轨。原 UP 在 t_end+25ms=2.6387，
            # 比 flick 的 DOWN 晚 25ms，尾判被 flick 抢走。
            # 长条1(t_end=2.1591)、长条2(t_end=2.3864) 紧随其后的
            # 是另一根长条的起点（不同轨），理论上不冲突，但用户实测
            # 三根尾判都会断，所以三根统一把 UP 压到 t_end 本身，
            # 不留 25ms 余量 —— 保证「先松手、后按下一个音符」。
            if (round(n['t'], 4), int(round(lane))) in {
                    (1.9318, 10), (2.1591, 0), (2.3864, 10)}:
                up_us = min(up_us, t_us + int(dur_s * 1e6))

            # 三根 S 形长条的副指：固定在起点 x2 不动，只发保活
            if _is_s3pre and pid2 is not None and not pid2_hold:
                _k2 = 0.020
                while _k2 < dur_s - 0.006:
                    events.append((t_us + int(_k2 * 1e6), MOVE, x2, pid2,
                                   Y_NORM))
                    _k2 += 0.020

            do_up(up_us, end_x)
            if pid2 is not None:
                if not pid2_hold:
                    events.append((up_us, UP,
                                   x2 if _is_s3pre else side_x(end_x),
                                   pid2))
                active.pop(pid2, None)
                release_t[pid2] = up_us / 1e6

        elif kind == 'flick':
            # ## flick：一次快速滑动到位
            #
            # 早前用两段 MOVE（0.070 再 0.042），第二段位移只有 90px，
            # 速度分布不满足 flick 判定，偶发 MISS（用户反馈开头掉一个）。
            # flick 判定看的是「手指有无足够快速的向上位移」，
            # 一次到位的滑动更接近真人操作。
            #
            # 位移取 0.11 归一化（约 235px，约 1/9 屏高），
            # 时间 30ms → 速度约 7800px/s，明显超过 flick 阈值，
            # 又不会滑出判定区（判定区纵向余量约 300px）。
            sweep = 0.11
            # ## #66（85.9314）三个 flick 判定偏 Late（用户两次录屏一致）
            #
            # 排查结论：这三个 flick 的时序与全谱其他 flick 完全相同
            #   （DOWN→MOVE 30.2ms，DOWN→UP 55ms，sweep 0.11 也一致），
            # 唯一特殊之处是**三指在同一微秒齐发**
            #   （1081 / 1921 / 451 三根手指同时 DOWN）。
            # 设备端在多点同时注入时会排队，导致实际滑动被推后
            #   → 命中判定窗口下沿，判 Late。
            #
            # 修法：只对这三个把滑动提前（30.2ms → 12ms），
            #   抬手仍保持 55ms 不变（保证手指停留时间够）。
            #   其他所有 flick 一律不动。
            _LATE3 = {(451, 85.9314), (1081, 85.9314), (1921, 85.9314)}
            _is_late3 = (round(x * 3200), round(n['t'], 4)) in _LATE3
            _mv_delay = int(12000) if _is_late3 else int(FLICK_HOLD_MS * 550)
            events.append((t_us + _mv_delay, MOVE, x, pid,
                           Y_NORM - sweep))
            do_up(t_us + FLICK_HOLD_MS * 1000, x)

        else:
            do_up(t_us + TAP_HOLD_MS * 1000, x)

    # 收尾：任何残留的按住都抬掉
    for p, (_, px) in active.items():
        events.append((events[-1][0] + 20000, UP, px, p))

    # 补齐 5 元组并按 (时间, UP先) 排序
    norm = []
    for e in events:
        norm.append((e[0], e[1], e[2], e[3], e[4] if len(e) > 4 else None))
    norm.sort(key=lambda e: (e[0], e[1]))
    # =========================================================
    # ## bar #89~#92 整段重写（用户指定段落）
    #
    # 用户从第一次就说明：这一段是
    #   **1 根绿色长条 + 后面 9 根黄色异形 hold**
    # 并且唯一的打法要求：
    #   「长条单指按起始 note 的中间不动，到尾判松手；tap/flick 正常打」
    #
    # 谱面实况（bar89~92 → 97.97~100.37s，整段 97.36~100.44）正好 1+9：
    #   1) 97.3600→99.1182  lane3 w3  1.758s  ← 绿色长条
    #   2) 99.1182→99.2647  lane0 w2  0.146s ┐
    #   3) 99.2648→99.4113  lane4 w2  0.146s │
    #   4) 99.4113→99.5578  lane9 w1  0.146s │
    #   5) 99.5578→99.7043  lane1 w1  0.146s ├ 9 根黄色异形
    #   6) 99.7043→99.8508  lane6 w1  0.146s │
    #   7) 99.8508→99.9973  lane9 w1  0.146s │
    #   8) 99.9974→100.2172 lane1 w1  0.220s │
    #   9) 100.2171→100.4369 lane4 w2 0.220s │
    #   10)100.2171→100.4369 lane9 w1 0.220s ┘
    #
    # 做法（纯事件层后处理，不动 lane_pid/release_t）：
    #   把这 10 根长条**整段拆掉重建**：
    #     每根 = 1 根独占手指，固定在其**起始 note 几何中心**，
    #     只发保活 MOVE（位置恒定），到尾判 t_end 才 UP。
    #   tap/flick 完全不动（30 个全部原样保留）。
    # =========================================================
    # 先把本段所有 tap/flick 的原始事件完整备份，
    # 最后回填 —— 保证一个不漏（长条与其共用 pid 时极易误删）。
    _tf_t = set()
    for _m in notes:
        if _m['kind'] in ('tap', 'flick') and 97.3 <= _m['t'] <= 100.5:
            _tf_t.add(_m['t'])
    _TF_SAVE = {}
    for _t in _tf_t:
        _tu = int(_t * 1e6)
        _win = [x for x in norm
                if _tu - 3000 <= x[0] <= _tu + 60000]
        _TF_SAVE[_t] = _win

    _BD = [(97.3600, 99.1182, 3, 3),
           (99.1182, 99.2647, 0, 2), (99.2648, 99.4113, 4, 2),
           (99.4113, 99.5578, 9, 1), (99.5578, 99.7043, 1, 1),
           (99.7043, 99.8508, 6, 1), (99.8508, 99.9973, 9, 1),
           (99.9974, 100.2172, 1, 1), (100.2171, 100.4369, 4, 2),
           (100.2171, 100.4369, 9, 1)]

    _bi = []
    for _a_t, _b_t, _ln, _wd in _BD:
        _au, _bu = int(_a_t * 1e6), int(_b_t * 1e6)
        _hx0 = lane_x(_ln, _wd)
        # 起点附近该侧的所有 DOWN（基线是多指，x 与目标中心不同）
        _all_dn = [x for x in norm
                   if _au - 3000 <= x[0] <= _au + 5000 and x[1] == DOWN]
        _bi.append((_au, _bu, _all_dn, _hx0))

    if any(d for _, _, d, _ in _bi):
        # 1) 把这些长条的旧手指事件全部清掉
        #    判定：起点时刻的所有 DOWN（整段统一认领）
        _old_pids = set()
        for _au, _bu, _dns, _hx0 in _bi:
            for _d in _dns:
                _old_pids.add((_d[3], _d[2]))
        _del = set()
        for _au, _bu, _dns, _hx0 in _bi:
            for _d in _dns:
                _pid, _dx = _d[3], _d[2]
                for _e in norm:
                    if _e[3] != _pid:
                        continue
                    if not (_au - 2000 <= _e[0] <= _bu + 6000):
                        continue
                    # 严格按「该手指自己的起点 x」匹配（±0.06 ≈ 1 轨）。
                    # 不能放宽：同一 pid 会被 tap 复用，且 x 相差很远
                    # （长条 pid1@2446 vs tap pid1@556，差 1890px），
                    # 放宽会把 tap 一起删掉（实测误删 13 个）。
                    # 容差收到 0.025（约 80px，不到半轨）：
                    # 长条 pid1@2446 与 tap pid1@2341 只差 105px，
                    # 0.06 会把该 tap 误删（实测 2 个）。
                    if abs(_e[2] - _dx) < 0.025:
                        _del.add((_e[0], _e[1], _e[2], _e[3]))
        norm = [e for e in norm
                if (e[0], e[1], e[2], e[3]) not in _del]

        # 2) 逐根重建：独占 pid + 固定起始 note 中心
        _used = set()
        _seq = {}
        for _au, _bu, _, _hx0 in _bi:
            # 该 pid 在 [起, 尾判] 内必须完全空闲（连 MOVE/UP 都不能有）
            _cnt = {}
            for _e in norm:
                if _au - 3000 <= _e[0] <= _bu + 3000:
                    _cnt[_e[3]] = _cnt.get(_e[3], 0) + 1
            _free = [c for c in range(MAX_POINTERS)
                     if _cnt.get(c, 0) == 0 and c not in _used]
            if not _free:
                # 退一步：只要该区间内没有 DOWN 的也算可用
                _dn_pids = set(_e[3] for _e in norm
                               if _au - 3000 <= _e[0] <= _bu + 3000
                               and _e[1] == DOWN)
                _free = [c for c in range(MAX_POINTERS)
                         if c not in _dn_pids and c not in _used]
            if not _free:
                # 全被占了：复用「已分配但与本段区间不重叠」的 pid
                _free = [c for c in range(MAX_POINTERS)
                         if c not in _cnt]
            if not _free:
                for c in range(MAX_POINTERS):
                    _free.append(c)
                    break
            if not _free:
                continue
            _pidh = _free[0]
            _used.add(_pidh)
            # 起点比原时刻晚 2ms（让旧 UP 先发生）；
            # 若同一时刻有多根长条（如 100.2171 的两根），
            # 依次各再让 2ms，避免互相抢位置。
            _dn = _au + 2000 + 2000 * _seq.get(_au, 0)
            _seq[_au] = _seq.get(_au, 0) + 1
            # 尾判要留余量：基线普通长条用 HOLD_UP_SLACK_MS(25ms)，
            # 原先这里直接 UP 在 t_end、一点余量都没有 →
            # 松手偏早、判定不稳（用户实测反馈）。
            _end = _bu + HOLD_UP_SLACK_MS * 1000
            norm.append((_dn, DOWN, _hx0, _pidh, None))
            _dur = _end - _dn
            if _dur <= 0:
                norm.append((_end, UP, _hx0, _pidh, None))
                continue
            _step = max(12000, _dur // 5)
            _tt = _step
            while _tt < _dur - 2000:
                norm.append((_dn + _tt, MOVE, _hx0, _pidh, Y_NORM))
                _tt += _step
            norm.append((_end, UP, _hx0, _pidh, None))
        norm.sort(key=lambda e: (e[0], e[1]))

    # =========================================================
    # 收尾兜底（只作用于本段 97.3~100.5，不碰别处）
    # =========================================================
    _L, _H = int(97.3 * 1e6), int(100.5 * 1e6)

    # a) 剔本段内的游离 MOVE：x 与同 pid 当前按住位置不符
    _hold_x = {}
    _a = []
    for _e in sorted(norm, key=lambda z: (z[0], z[1])):
        _in = _L <= _e[0] <= _H
        if _e[1] == DOWN:
            _hold_x[_e[3]] = _e[2]
            _a.append(_e)
        elif _e[1] == UP:
            _hold_x.pop(_e[3], None)
            _a.append(_e)
        else:
            if not _in:
                _a.append(_e)          # 段外 MOVE 一律保留
                continue
            _hx = _hold_x.get(_e[3])
            if _hx is not None and abs(_e[2] - _hx) < 0.02:
                _a.append(_e)

    # b) 只在本段内消除「同 pid 按住期间又 DOWN」
    _b = []
    _held = {}
    for _e in sorted(_a, key=lambda z: (z[0], z[1])):
        _in = _L <= _e[0] <= _H
        if _e[1] == DOWN:
            _p = _e[3]
            if _in and _p in _held:
                _b.append((_e[0] - 1000, UP, _held[_p], _p, None))
            _held[_p] = _e[2]
        elif _e[1] == UP:
            _held.pop(_e[3], None)
        _b.append(_e)
    norm = sorted(_b, key=lambda e: (e[0], e[1]))

    # =========================================================
    # ## #107 两段 hold 中间：删掉全部假 tap / flick
    #    （用户指定：「这两段 hold 中间除了按住长条的手指继续按住
    #      直到尾判以外，不要有任何操作」）
    #
    # 谱面实况（bar107 = 112.37s），两段 hold：
    #   ① 112.1716→112.3216  lane8 w2
    #      path: 8→8→8→4→8→8→8  （t+0.075 处有菱形凸起到 lane4）
    #   ② 112.3216→112.4716  lane0 w2
    #      path: 0→0→0→4→0→0→0  （同样有菱形凸起）
    #
    # 解析器把「菱形凸起」和「绿条内部的装饰」都当成了 note：
    #   ① 内部 4 个 lane8 w2 tap（x=2236）：
    #      112.1966 / 112.2216 / 112.2716 / 112.2966
    #      + 1 个菱形 flick lane4 w2（x=1396）：112.2466
    #   ② 内部 4 个 lane0 w2 tap（x=556）：
    #      112.3466 / 112.3716 / 112.4216 / 112.4466
    #      + 1 个菱形 flick lane4 w2（x=1396）：112.3966
    #
    # 删法：按（时刻 + x）精确定位那一个 DOWN，再把它连同该 pid
    #   到下一个 UP 之间的 MOVE/UP 一起删 —— 保证成对删干净，
    #   不会留下孤儿 UP 或误删长条自己的手指。
    #
    # 长条手指（2131/2963 与 451/1283 及其滑动路径）完全不动。
    # =========================================================
    _FAKE = [(112.1966, 2236), (112.2216, 2236),
             (112.2716, 2236), (112.2966, 2236),
             (112.2466, 1396), (112.3966, 1396),
             (112.3466, 556), (112.3716, 556),
             (112.4216, 556), (112.4466, 556)]

    _del = set()
    for _ft, _fxi in _FAKE:
        _fu = int(_ft * 1e6)
        _fxn = _fxi / 3200.0
        _dn = None
        for _e in norm:
            if (abs(_e[0] - _fu) < 2000 and _e[1] == DOWN
                    and abs(_e[2] - _fxn) < 0.006):
                _dn = _e
                break
        if _dn is None:
            continue
        _p = _dn[3]
        _del.add((_dn[0], _dn[1], _dn[2], _dn[3]))
        # 该 pid 接着的 MOVE / UP，删到第一个 UP 为止
        for _e in sorted(norm, key=lambda z: z[0]):
            if _e[3] != _p or _e[0] <= _dn[0]:
                continue
            if _e[0] > _fu + 150000:
                break
            _del.add((_e[0], _e[1], _e[2], _e[3]))
            if _e[1] == UP:
                break
    norm = [e for e in norm
            if (e[0], e[1], e[2], e[3]) not in _del]

    # =========================================================
    # ## #107 两段 hold 中间：删掉全部假 tap/flick
    #    （用户指定：「这两段 hold 中间除了按住长条的手指继续按住
    #      直到尾判以外不要有任何操作」）
    #
    # 谱面实况（bar107 = 112.37s）：
    #   112.1716→112.3216  lane8 w2  hold
    #     path = 8→8→8→4→8→8→8   （t+0.075 处有一个菱形凸起到 lane4）
    #   112.3216→112.4716  lane0 w2  hold
    #     path = 0→0→0→4→0→0→0   （同样有菱形凸起）
    #
    # 解析器把菱形凸起当成了 note，产生：
    #   · 4 个 tap   @ lane0 中心 x=556  （112.3466/3716/4216/4466）
    #   · 1 个 flick @ lane4 中心 x=1396 （112.3966，t+0.075）
    #
    # 长条自己的手指在 2131/2963（第一段）与 451/1283（第二段），
    # 与上述位置都不同，不会被误删。
    #
    # 纯事件层过滤 → 其他部分零影响。
    # =========================================================
    _FAKE_X = (lane_x(0, 2), lane_x(4, 2))     # 556, 1396
    _FA0, _FA1 = int(112.1700 * 1e6), int(112.4800 * 1e6)
    norm = [e for e in norm
            if not (_FA0 <= e[0] <= _FA1
                    and any(abs(e[2] - fx) < 0.006 for fx in _FAKE_X))]

    # 回填 tap/flick：本段内缺失的补回来（按原事件时间窗）
    _have = {}
    for _e in norm:
        _have[(_e[0], _e[1], _e[3])] = True
    # tap 该有的 x（谱面给出的 lane/width 中心）
    _tf_x = {}
    for _m in notes:
        if _m['kind'] in ('tap', 'flick'):
            _tf_x[_m['t']] = lane_x(_m['lane'], _m['width'])

    _add = []
    for _t, _evs in _TF_SAVE.items():
        _tu = int(_t * 1e6)
        _wx = _tf_x.get(_t)
        if _wx is None:
            continue
        # 该 tap 是否仍有「位置对得上」的 DOWN（只看时刻会误判：
        # 同一刻长条也会补 DOWN，x 却完全不同）
        if any(_tu - 3000 <= _e[0] <= _tu + 3000 and _e[1] == DOWN
               and abs(_e[2] - _wx) < 0.06 for _e in norm):
            continue
        # 只补该 tap 自己位置的事件（避免带回长条的旧手指）
        for _e in _evs:
            if _e[1] == DOWN and abs(_e[2] - _wx) >= 0.06:
                continue
            if _e[1] == UP and abs(_e[2] - _wx) >= 0.06:
                continue
            _add.append(_e)
    if _add:
        norm.extend(_add)
        norm.sort(key=lambda e: (e[0], e[1]))

    # =========================================================
    # ## #2 三根 S 形 hold：三指覆盖半边屏幕按住不动（用户指定）
    #
    # 用户要求：
    #   「tap 和 flick 正常打，hold 直接三指按住不动，覆盖半个屏幕，
    #     直到尾判松手。
    #     比如第一根长条在右半边就三指覆盖右半边屏幕按住不动，
    #     第二根长条在左半边就三指按住左半边屏幕」
    #
    # 三根长条（bar#2~#3 开头，S 形从 lane10/0 出发到中点 lane5 再回）：
    #   1.9318→2.1591  lane10 → 5 → 10   ← 右半边
    #   2.1591→2.3864  lane0  → 5 →  0   ← 左半边
    #   2.3864→2.6137  lane10 → 5 → 10   ← 右半边
    #
    # 「覆盖半屏」的取法：
    #   右半边 = 屏幕中线 0.5 到右缘，三指均匀铺在 0.5 / 0.75 / 0.95
    #   左半边 = 左缘到中线 0.5，三指均匀铺在 0.05 / 0.25 / 0.5
    #   这样三指把半边屏幕横向铺满，长条（S 形扫过的区域）必定落在其中。
    #
    # 做法（纯事件层）：
    #   删掉这三根原有的全部事件（多指滑动的那一套），
    #   换成 3 根独占手指，各自固定在一个 x 上，只发保活 MOVE，
    #   到尾判 t_end 一起松手。tap/flick 不动。
    # =========================================================
    _S2 = [(1.9318, 2.1591, 'R'),      # 右半边
           (2.1591, 2.3864, 'L'),      # 左半边
           (2.3864, 2.6137, 'R')]      # 右半边

    # 半屏三指位置
    _POS = {'R': [0.50, 0.75, 0.95],
            'L': [0.05, 0.25, 0.50]}

    # ## 先删掉「S 形折返点」的假 flick（用户指出图里没有这些）
    #
    # 三根 S 形 hold 各有一个折返点（lane5 的菱形装饰），时刻正好是
    #   hold 起点 + 0.1136：
    #     2.0455 / 2.2727 / 2.5000   ← 三个都是假的
    # 解析器把它们当成了 flick。真正的音符只有：
    #     1.9318 lane0 tap
    #     2.6136 lane5 flick
    #
    # 只删「时刻落在折返点上、且 x 在 lane5 中心」的事件。
    # 按用户截图为准：#2 里只有 1 个 tap + 2 个 flick
    #   · 顶部蓝框 flick  = 2.0455（hold1 折返点附近）
    #   · 底部蓝框 flick  = 2.6136（#2 末尾）
    #   · 中间两个折返点 2.2727 / 2.5000 图上不存在 → 删
    _MIDX = lane_x(5, 1)
    for _mt in (2.2727, 2.5000):
        _mu = int(_mt * 1e6)
        _pids = set(x[3] for x in norm
                    if abs(x[0] - _mu) < 1500 and x[1] == DOWN
                    and abs(x[2] - _MIDX) < 0.01)
        if not _pids:
            continue
        _d = set()
        for _e in norm:
            if _e[3] in _pids and _mu - 1000 <= _e[0] <= _mu + 70000:
                _d.add((_e[0], _e[1], _e[2], _e[3]))
        norm = [e for e in norm
                if (e[0], e[1], e[2], e[3]) not in _d]

    for _a_t, _b_t, _sd in _S2:
        _au = int(_a_t * 1e6)
        _bu = int(_b_t * 1e6)
        # 该长条原有手指：起点时刻附近的 DOWN，
        # 但要排除「与其他音符位置重合」的那些 —— 例如 1.9318 的
        # lane0 tap 在 x=451，它和长条同一时刻开始，若一并认领会误删。
        # 判定：长条自己的手指 x 都在这根长条扫过的横向范围内
        #       （起点 lane 到中点 lane 之间，左右各留 1 轨余量）；
        #       落在该范围外的是别的音符。
        _m = None
        for _mm in notes:
            if (_mm['kind'] in ('hold', 'trace')
                    and abs(_mm['t'] - _a_t) < 0.001):
                _m = _mm
                break
        if _m is None:
            continue
        _pl = [b for _, b in (_m.get('path') or [])]
        _lo_l = min(_pl) - 1.0
        _hi_l = max(_pl) + _m['width'] + 1.0
        _old = [x for x in norm
                if _au - 4000 <= x[0] <= _au + 6000 and x[1] == DOWN
                and _lo_l * 210.0 + 346.0 - 60 <= x[2] * 3200
                <= _hi_l * 210.0 + 346.0 + 60]
        _pids = set(x[3] for x in _old)
        if not _pids:
            continue
        # 删掉这些 pid 在 [起, 尾判+45ms] 内的全部事件
        _drop = set()
        for _e in norm:
            if _e[3] in _pids and _au - 3000 <= _e[0] <= _bu + 45000:
                _drop.add((_e[0], _e[1], _e[2], _e[3]))
        norm = [e for e in norm
                if (e[0], e[1], e[2], e[3]) not in _drop]

        # 分 3 根空闲手指
        _busy = set(e[3] for e in norm if _au - 50000 <= e[0] <= _bu + 50000)
        _free = [c for c in range(MAX_POINTERS) if c not in _busy]
        if len(_free) < 3:
            _free = [c for c in range(MAX_POINTERS) if c not in _busy]
            while len(_free) < 3:
                _free.append(_free[len(_free) % len(_free)])

        for _i, _xx in enumerate(_POS[_sd]):
            _p = _free[_i]
            norm.append((_au + 1000 + _i * 1000, DOWN, _xx, _p, None))
            _kk = 20
            while _au + _kk * 1000 < _bu - 2000:
                norm.append((_au + _kk * 1000, MOVE, _xx, _p, Y_NORM))
                _kk += 20
            norm.append((_bu, UP, _xx, _p, None))
    
    # =========================================================
    # ## #92（99.7~100.4）中间没有 tap/flick（用户指定）
    #
    # 用户对照谱面图确认：#92 里只有「从 #91 延续的长条 + 四根长条」，
    # 长条中间没有任何 tap、没有任何 flick。
    #
    # 解析器在这段多识别出 6 个 flick（异形 hold 的菱形装饰被误判）：
    #   99.9058 lane8 x=2236
    #   99.9241 lane9 x=2341
    #   99.9790 lane8 x=2236
    #   100.1073 lane2 x=871
    #   100.1897 lane0 x=556
    #   100.3270 lane8 x=2236
    #
    # ## 只删它们自己的 DOWN + MOVE，**保留原有 UP**
    #   这样 pid 的占用/释放时间线与基线**完全一致**，
    #   不会波及后续任何段落的分配。
    #   （UP 单独留着无害：没有对应 DOWN 的 UP 对游戏没有影响）
    # =========================================================
    _FAKE_FL = [(99.9058, 2236), (99.9241, 2341), (99.9790, 2236),
                (100.1073, 871), (100.1897, 556), (100.3270, 2236)]
    _del = set()
    for _ft, _fx in _FAKE_FL:
        _fu = int(_ft * 1e6)
        _fxn = _fx / 3200.0
        for _e in norm:
            if _e[1] == UP:
                continue                      # 保留 UP
            if not (_fu - 1000 <= _e[0] <= _fu + 40000):
                continue
            if abs(_e[2] - _fxn) < 0.006:
                _del.add((_e[0], _e[1], _e[2], _e[3]))
    if _del:
        norm = [e for e in norm
                if (e[0], e[1], e[2], e[3]) not in _del]
    norm.sort(key=lambda e: (e[0], e[1]))

    # =========================================================
    # ## #66 同一时刻的 tap/flick 必须与滑动长条用不同手指（用户指定）
    #
    # 排查发现两处「flick 与长条的滑动手指落在同一像素」的冲突：
    #   84.6127  flick x=2761  ← 10.5秒长条的 pid4 此刻也在 2761
    #   85.9314  flick x=451   ← 同一长条的 pid3 此刻也在 451
    #
    # 两条触摸轨迹在同一坐标共存时，设备端会判成同一根手指，
    # flick 的滑动被长条的 MOVE 吞掉 → 命中判定窗口下沿 → Late。
    # （用户两次录屏都在同一处爆 Late，确认是确定性冲突）
    #
    # 修法：把这两处 flick 的 x 横向错开 0.5 轨（105px），
    #   仍在判定范围内（半轨远小于判定宽度），
    #   同时与长条手指彻底分开 → 两条独立轨迹。
    #   只改这两个 flick 的 x，其他一律不动。
    # =========================================================
    _SHIFT = {(84.6127, 2761): -105.0,     # 往左让开长条
              (85.9314, 451): +105.0}      # 往右让开长条

    # 按 (pid, 该 flick 的时长窗口) 整条错开 ——
    # 必须把 DOWN / MOVE / UP 一起挪，否则按下位置与滑动位置不一致，
    # 反而会让 flick 判 MISS。
    _SHIFT_PID = {}
    for _t0, _x0 in ((84.6127, 2761), (85.9314, 451)):
        _t0u = int(_t0 * 1e6)
        for _e in norm:
            if (abs(_e[0] - _t0u) < 1500 and _e[1] == DOWN
                    and abs(_e[2] * 3200 - _x0) < 6):
                _SHIFT_PID[(_e[3], _t0)] = _SHIFT[(_t0, _x0)]
                break

    if _SHIFT_PID:
        _ns = []
        for _e in norm:
            _hit = None
            for (_pid, _t0), _dv in _SHIFT_PID.items():
                _t0u = int(_t0 * 1e6)
                if _e[3] == _pid and _t0u - 2000 <= _e[0] <= _t0u + 80000:
                    _hit = _dv
                    break
            if _hit is not None:
                _nx = (_e[2] * 3200 + _hit) / 3200.0
                _ns.append((_e[0], _e[1], _nx, _e[3], _e[4]))
            else:
                _ns.append(_e)
        norm = _ns
    norm.sort(key=lambda e: (e[0], e[1]))

    return norm


def report(events, chart):
    from collections import Counter
    kinds = Counter(e[1] for e in events)
    print('事件 %d 个（DOWN %d / UP %d / MOVE %d）'
          % (len(events), kinds[DOWN], kinds[UP], kinds[MOVE]))
    if events:
        print('时间 %.2f ~ %.2f s' % (events[0][0] / 1e6, events[-1][0] / 1e6))
    # 非法重复 DOWN
    st = {}
    bad = []
    for t, a, x, pid, y in events:
        if a == DOWN:
            if st.get(pid) == 0:
                bad.append((t / 1e6, pid))
            st[pid] = 0
        elif a == UP:
            st[pid] = 1
    print('重复 DOWN（应为 0）: %d' % len(bad))
    # 峰值并发
    cur = 0
    mx = 0
    for t, a, x, pid, y in events:
        if a == DOWN:
            cur += 1
        elif a == UP:
            cur -= 1
        mx = max(mx, cur)
    print('峰值并发 %d 指（设备上限 %d）' % (mx, MAX_POINTERS))


if __name__ == '__main__':
    chart = json.load(open('data/chart_329_master.json', encoding='utf-8'))
    print('谱面 %d 音符' % len(chart['notes']))
    ev = build_events(chart)
    report(ev, chart)
    if '--dry-run' in sys.argv:
        for e in ev[:10]:
            print('  t=%dms act=%d x=%.0f pid=%d'
                  % (e[0] // 1000, e[1], e[2] * 3200, e[3]))