import android.view.InputEvent;
import android.view.MotionEvent;
import android.view.InputDevice;
import java.io.*;
import java.lang.reflect.*;

/**
 * Multi-touch touch injector (device side, app_process, no root).
 *
 * ## Why this rewrite
 *
 * The previous version sent ONE pointer per MotionEvent. That is wrong for
 * Android multi-touch: EVERY MotionEvent must carry the COMPLETE set of
 * currently-active pointers. Sending a lone pointer with ACTION_POINTER_DOWN
 * makes the framework see an inconsistent stream, and the game only registers
 * the first finger; the rest are dropped.
 *
 * Measured on Yaminabe MASTER: 55% of notes (806/1464) are simultaneous
 * 2-finger or more, so single-pointer injection can never FC.
 *
 * Now: maintain an active-pointer map, and on every event build the full
 * PointerProperties[]/PointerCoords[] arrays exactly as Android expects.
 *
 * ## Protocol (stdin, one command per line)
 *   READY
 *   T0 <uptime_ms>          absolute base
 *   T0D <delay_ms>          base = my uptimeMillis + delay
 *   E <t_us> <action> <x_px> <y_px> <pid>
 *        action: 0=DOWN 1=UP 2=MOVE
 *   GO
 *   BYE
 */
public class Injector {
    static void p(String s) { System.out.println(s); System.out.flush(); }

    static Object inputManager;
    static Method injectM;

    static final java.util.TreeMap<Integer, float[]> active =
            new java.util.TreeMap<Integer, float[]>();
    static long downTimeMs = 0;

    public static void main(String[] args) {
        try { run(); }
        catch (Throwable t) {
            p("FATAL " + t);
            for (Throwable c = t.getCause(); c != null; c = c.getCause()) p("  caused " + c);
        }
    }

    static void run() throws Exception {
        android.os.Looper.prepareMainLooper();
        Class<?> at = Class.forName("android.app.ActivityThread");
        at.getMethod("systemMain").invoke(null);

        Class<?> imCls = Class.forName("android.hardware.input.InputManager");
        Object im;
        try {
            im = imCls.getMethod("getInstance").invoke(null);
        } catch (Throwable t) {
            Class<?> g = Class.forName("android.hardware.input.InputManagerGlobal");
            im = g.getMethod("getInstance").invoke(null);
            imCls = g;
        }
        inputManager = im;
        injectM = imCls.getMethod("injectInputEvent", InputEvent.class, int.class);

        p("READY uptime=" + android.os.SystemClock.uptimeMillis()
                + " uid=" + android.os.Process.myUid()
                + " im=" + imCls.getName()
                + " multitouch=1");

        BufferedReader r = new BufferedReader(new InputStreamReader(System.in));
        long t0 = -1;
        java.util.List<long[]> sched = new java.util.ArrayList<long[]>();

        String line;
        while ((line = r.readLine()) != null) {
            line = line.trim();
            if (line.isEmpty()) continue;
            String[] f = line.split("\\s+");
            if (f[0].equals("T0")) {
                t0 = Long.parseLong(f[1]);
                p("T0SET " + t0);
            } else if (f[0].equals("T0D")) {
                // Base must be SystemClock.uptimeMillis(), NOT /proc/uptime:
                // they differ by ~14 days (deep sleep counted differently).
                long delay = Long.parseLong(f[1]);
                t0 = android.os.SystemClock.uptimeMillis() + delay;
                p("T0SET " + t0 + " via T0D(" + delay + ")");
            } else if (f[0].equals("E")) {
                sched.add(new long[]{Long.parseLong(f[1]), Long.parseLong(f[2]),
                        (long) (Double.parseDouble(f[3]) * 1000),
                        (long) (Double.parseDouble(f[4]) * 1000),
                        Long.parseLong(f[5])});
            } else if (f[0].equals("GO")) {
                if (t0 < 0) { p("ERR no T0"); continue; }
                p("GO " + sched.size());
                fire(sched, t0);
                p("DONE sent=" + sched.size());
            } else if (f[0].equals("BYE")) {
                p("BYE");
                return;
            }
        }
    }

    static int indexOfPid(int pid) {
        int i = 0;
        for (Integer k : active.keySet()) {
            if (k == pid) return i;
            i++;
        }
        return 0;
    }

    /** Build and inject one MotionEvent carrying ALL active pointers. */
    static void injectOne(long eventTimeMs, int actionCode) throws Exception {
        int n = active.size();
        if (n == 0) return;
        MotionEvent.PointerProperties[] props = new MotionEvent.PointerProperties[n];
        MotionEvent.PointerCoords[] coords = new MotionEvent.PointerCoords[n];
        int i = 0;
        for (java.util.Map.Entry<Integer, float[]> e : active.entrySet()) {
            MotionEvent.PointerProperties pp = new MotionEvent.PointerProperties();
            pp.id = e.getKey();
            pp.toolType = MotionEvent.TOOL_TYPE_FINGER;
            props[i] = pp;

            MotionEvent.PointerCoords pc = new MotionEvent.PointerCoords();
            pc.x = e.getValue()[0];
            pc.y = e.getValue()[1];
            pc.pressure = 1.0f;
            pc.size = 1.0f;
            coords[i] = pc;
            i++;
        }
        MotionEvent ev = MotionEvent.obtain(
                downTimeMs, eventTimeMs, actionCode, n,
                props, coords, 0, 0,
                1.0f, 1.0f, 0, 0,
                InputDevice.SOURCE_TOUCHSCREEN, 0);
        try {
            Object r = injectM.invoke(inputManager, ev, 0);
            if (r instanceof Boolean && !((Boolean) r)) injectErr++;
        } catch (Throwable t) {
            // A single failed injection must NOT kill the whole schedule.
            // Previously an exception here propagated out of fire() and
            // terminated the process, so playback stopped mid-song.
            injectErr++;
            if (injectErr <= 5) p("INJFAIL " + t);
        } finally {
            ev.recycle();
        }
    }

    static int injectErr = 0;

    static void fire(java.util.List<long[]> sched, long t0) throws Exception {
        int sent = 0, late = 0, skipped = 0;
        long maxLate = 0;
        for (long[] e : sched) {
            long targetMs = t0 + e[0] / 1000;
            long nowMs = android.os.SystemClock.uptimeMillis();
            if (targetMs + 120 < nowMs) { skipped++; continue; }
            long wait = targetMs - nowMs - 2;
            if (wait > 0) Thread.sleep(wait);
            while (android.os.SystemClock.uptimeMillis() < targetMs) { /* spin */ }

            long now = android.os.SystemClock.uptimeMillis();
            if (now - targetMs > 1) { late++; maxLate = Math.max(maxLate, now - targetMs); }

            int action = (int) e[1];
            float x = e[2] / 1000f;
            float y = e[3] / 1000f;
            int pid = (int) e[4];

            if (action == 0) {
                if (active.isEmpty()) downTimeMs = targetMs;
                active.put(pid, new float[]{x, y});
                int idx = indexOfPid(pid);
                int code = (active.size() == 1)
                        ? MotionEvent.ACTION_DOWN
                        : (MotionEvent.ACTION_POINTER_DOWN
                           | (idx << MotionEvent.ACTION_POINTER_INDEX_SHIFT));
                injectOne(targetMs, code);
            } else if (action == 1) {
                if (!active.containsKey(pid)) { continue; }
                int idx = indexOfPid(pid);
                int code = (active.size() == 1)
                        ? MotionEvent.ACTION_UP
                        : (MotionEvent.ACTION_POINTER_UP
                           | (idx << MotionEvent.ACTION_POINTER_INDEX_SHIFT));
                injectOne(targetMs, code);
                active.remove(pid);
                if (active.isEmpty()) downTimeMs = 0;
            } else {
                if (!active.containsKey(pid)) { continue; }
                active.put(pid, new float[]{x, y});
                injectOne(targetMs, MotionEvent.ACTION_MOVE);
            }
            sent++;
            if (sent % 200 == 0) p("PROGRESS " + sent + "/" + sched.size());
        }
        p("STAT sent=" + sent + " skipped=" + skipped
                + " injectErr=" + injectErr
                + " late(>1ms)=" + late + " max=" + maxLate + "ms");
    }
}