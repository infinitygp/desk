// Compiled on demand by Windows PowerShell 5.1 (C# 5) in the desktop session.
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Threading;

public static class WinDisplay {
    public sealed class Unavailable : Exception {
        public Unavailable(string message) : base(message) {}
    }
    public sealed class Reading {
        public int width, height, scale;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct Rect { public int left, top, right, bottom; }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct MonitorInfo {
        public int size;
        public Rect bounds, work;
        public int flags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string device;
    }
    delegate bool MonitorProc(IntPtr monitor, IntPtr dc, IntPtr rect, IntPtr data);
    [DllImport("user32")] static extern bool EnumDisplayMonitors(IntPtr dc, IntPtr clip, MonitorProc proc, IntPtr data);
    [DllImport("user32", CharSet = CharSet.Unicode)] static extern bool GetMonitorInfo(IntPtr monitor, ref MonitorInfo info);
    [DllImport("user32")] static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
    [DllImport("shcore")] static extern int GetDpiForMonitor(IntPtr monitor, int type, out uint x, out uint y);

    // Only the fields we use of DEVMODEW; start with the driver's current mode.
    [StructLayout(LayoutKind.Explicit, Size = 220)]
    struct Mode {
        [FieldOffset(68)] public short size;
        [FieldOffset(72)] public int fields;
        [FieldOffset(76)] public int x;
        [FieldOffset(80)] public int y;
        [FieldOffset(172)] public int width;
        [FieldOffset(176)] public int height;
    }
    [DllImport("user32", CharSet = CharSet.Unicode)] static extern bool EnumDisplaySettings(string device, int which, ref Mode mode);
    [DllImport("user32", CharSet = CharSet.Unicode)] static extern int ChangeDisplaySettingsEx(string device, ref Mode mode, IntPtr hwnd, int flags, IntPtr data);
    [DllImport("user32", CharSet = CharSet.Unicode)] static extern int ChangeDisplaySettingsEx(string device, IntPtr mode, IntPtr hwnd, int flags, IntPtr data);

    struct Adapter { public uint low; public int high; }
    // DISPLAYCONFIG_PATH_INFO starts with its source adapter and source id.
    [StructLayout(LayoutKind.Sequential, Size = 72)]
    struct PathInfo { public Adapter adapter; public uint source; }
    [StructLayout(LayoutKind.Sequential, Size = 64)]
    struct ModeInfo { public uint type; }
    struct Header {
        public int type, size;
        public Adapter adapter;
        public uint source;
    }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct SourceName {
        public Header header;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string name;
    }
    struct DpiRange { public Header header; public int min, current, max; }
    struct DpiSet { public Header header; public int relative; }
    [DllImport("user32")] static extern int GetDisplayConfigBufferSizes(uint flags, out uint paths, out uint modes);
    [DllImport("user32")] static extern int QueryDisplayConfig(uint flags, ref uint pathCount, [Out] PathInfo[] paths,
        ref uint modeCount, [Out] ModeInfo[] modes, IntPtr topology);
    [DllImport("user32")] static extern int DisplayConfigGetDeviceInfo(ref SourceName name);
    [DllImport("user32")] static extern int DisplayConfigGetDeviceInfo(ref DpiRange range);
    [DllImport("user32")] static extern int DisplayConfigSetDeviceInfo(ref DpiSet scale);

    sealed class Screen {
        public MonitorInfo info;
        public Reading reading;
    }
    static List<Screen> Screens(int count) {
        var handles = new List<IntPtr>();
        if (!EnumDisplayMonitors(IntPtr.Zero, IntPtr.Zero,
            delegate(IntPtr m, IntPtr dc, IntPtr rect, IntPtr data) { handles.Add(m); return true; }, IntPtr.Zero)
            || handles.Count != count)
            throw new Exception("Expected " + count + " active monitors, found " + handles.Count);
        var screens = new List<Screen>();
        foreach (var m in handles) {
            var info = new MonitorInfo();
            info.size = Marshal.SizeOf(info);
            if (!GetMonitorInfo(m, ref info)) throw new Exception("Could not identify monitor");
            uint dx, dy;
            if (GetDpiForMonitor(m, 0, out dx, out dy) != 0 || dx == 0 || dx != dy)
                throw new Exception("Could not read effective DPI of " + info.device);
            var r = info.bounds;
            screens.Add(new Screen { info = info, reading = new Reading { width = r.right - r.left,
                height = r.bottom - r.top, scale = checked((int)Math.Round(dx * 100.0 / 96)) } });
        }
        screens.Sort(delegate(Screen a, Screen b) { return a.info.bounds.left.CompareTo(b.info.bounds.left); });
        return screens;
    }
    static void Arrange(List<Screen> screens, int monitor, int width, int height) {
        int x = 0;
        for (int i = 0; i < screens.Count; ++i) {
            var s = screens[i];
            var mode = new Mode { size = (short)Marshal.SizeOf(typeof(Mode)) };
            if (!EnumDisplaySettings(s.info.device, -1, ref mode)) throw new Exception("Could not read " + s.info.device);
            if (monitor == 0 || monitor == i+1) { mode.width = width; mode.height = height; }
            mode.x = x; mode.y = 0;
            mode.fields = 0x20 | 0x80000 | 0x100000; // POSITION | PELSWIDTH | PELSHEIGHT
            x = checked(x + mode.width);
            // Store the whole arrangement before applying it; changing one at a time lets
            // Windows move other monitors to keep them touching.
            int err = ChangeDisplaySettingsEx(s.info.device, ref mode, IntPtr.Zero, 0x10000001, IntPtr.Zero);
            if (err != 0) throw new Exception(s.info.device + ": refused " + mode.width + "x" + mode.height + " (" + err + ")");
        }
        int apply = ChangeDisplaySettingsEx(null, IntPtr.Zero, IntPtr.Zero, 0, IntPtr.Zero);
        if (apply != 0) throw new Exception("Windows refused the display arrangement (" + apply + ")");
    }

    static void ConfigError(string what, int err, bool privateQuery) {
        if (err == 0) return;
        string message = what + " failed (" + err + ")";
        // ERROR_INVALID_PARAMETER on the private GET with a known live source can mean
        // this driver does not implement it. On SET it means refusal, not permission to reboot.
        if (err == 50 || err == 120 || (privateQuery && err == 87)) throw new Unavailable(message);
        throw new Exception(message);
    }
    static Dictionary<string, Header> Sources() {
        for (int attempt = 0; attempt < 4; ++attempt) {
            uint np, nm;
            ConfigError("GetDisplayConfigBufferSizes", GetDisplayConfigBufferSizes(2, out np, out nm), false);
            if (np == 0 || nm == 0 || np > 4096 || nm > 16384) throw new Exception("Invalid display configuration counts");
            var paths = new PathInfo[np];
            var modes = new ModeInfo[nm];
            int err = QueryDisplayConfig(2, ref np, paths, ref nm, modes, IntPtr.Zero);
            if (err == 122) continue; // topology changed after sizing
            ConfigError("QueryDisplayConfig", err, false);
            var sources = new Dictionary<string, Header>(StringComparer.OrdinalIgnoreCase);
            for (int i = 0; i < np; ++i) {
                var name = new SourceName { header = new Header { adapter = paths[i].adapter, source = paths[i].source,
                    type = 1, size = Marshal.SizeOf(typeof(SourceName)) } };
                ConfigError("Get source name", DisplayConfigGetDeviceInfo(ref name), false);
                sources.Add(name.name, name.header);
            }
            return sources;
        }
        throw new Exception("Display configuration kept changing");
    }
    static readonly int[] Steps = {100,125,150,175,200,225,250,300,350,400,450,500};
    static void Scale(List<Screen> screens, int monitor, int scale) {
        var sets = new List<DpiSet>();
        Dictionary<string, Header> sources = null;
        for (int i = 0; i < screens.Count; ++i) {
            var s = screens[i];
            if ((monitor != 0 && monitor != i+1) || s.reading.scale == scale) continue;
            if (sources == null) {
                // Old desk images may be signed in with one custom scale for all displays.
                // Windows disables its live per-display controls in that mode.
                object global = Microsoft.Win32.Registry.GetValue(@"HKEY_CURRENT_USER\Control Panel\Desktop",
                    "Win8DpiScaling", 0);
                if (Object.Equals(global, 1))
                    throw new Unavailable("Windows uses one custom sign-in scale (currently " + s.reading.scale + " %)");
                sources = Sources();
            }
            Header h;
            if (!sources.TryGetValue(s.info.device, out h)) throw new Exception("No active source for " + s.info.device);
            h.type = -3; h.size = Marshal.SizeOf(typeof(DpiRange));
            var r = new DpiRange { header = h };
            ConfigError("Get scale range for " + s.info.device, DisplayConfigGetDeviceInfo(ref r), true);
            // Relative offsets from the recommended step, with 100% at min. Compute in
            // 64 bits: e.g. custom scaling may return a sentinel rather than a valid step.
            long last = (long)r.max - r.min, current = (long)r.current - r.min;
            if (r.min > 0 || r.max < 0 || last >= Steps.Length || current < 0 || current > last
                || Steps[current] != s.reading.scale)
                throw new Exception(s.info.device + ": invalid live scale range at " + s.reading.scale + " %");
            int index = Array.IndexOf(Steps, scale);
            if (index < 0 || index > last)
                throw new Exception(s.info.device + ": cannot set " + scale + " % (currently " + s.reading.scale + " %, maximum " + Steps[last] + " %)");
            h.type = -4; h.size = Marshal.SizeOf(typeof(DpiSet));
            sets.Add(new DpiSet { header = h, relative = index + r.min });
        }
        foreach (var wanted in sets) {
            var set = wanted;
            int err = DisplayConfigSetDeviceInfo(ref set);
            if (err != 0) {
                var now = Screens(screens.Count);
                string actual = String.Join(", ", now.ConvertAll(s => s.reading.scale + " %").ToArray());
                ConfigError("Set " + scale + " % (monitors now at " + actual + ")", err, false);
            }
        }
    }
    static List<Screen> Verify(List<Screen> before, int width, int height, int scale, int monitor) {
        string mismatch = "";
        for (int attempt = 0; attempt < 20; ++attempt) {
            var now = Screens(before.Count);
            mismatch = "";
            int x = 0;
            for (int i = 0; i < now.Count; ++i) {
                var s = now[i];
                bool selected = monitor == 0 || monitor == i+1;
                int w = selected && width != 0 ? width : before[i].reading.width;
                int h = selected && height != 0 ? height : before[i].reading.height;
                if (s.info.device != before[i].info.device || s.reading.width != w || s.reading.height != h ||
                    s.info.bounds.left != x || s.info.bounds.top != 0)
                    mismatch = "Monitor " + (i+1) + " is " + s.reading.width + "x" + s.reading.height +
                        " at (" + s.info.bounds.left + "," + s.info.bounds.top + "), expected " + w + "x" + h + " at (" + x + ",0)";
                int want = selected && scale != 0 ? scale : before[i].reading.scale;
                if (!(selected && scale == -1) && s.reading.scale != want)
                    mismatch = "Monitor " + (i+1) + " is at " + s.reading.scale + " % instead of " + want + " %";
                x = checked(x + w);
                if (mismatch != "") break;
            }
            if (mismatch == "") return now;
            Thread.Sleep(150);
        }
        throw new Exception(mismatch);
    }
    public static Reading[] Change(int count, int width, int height, int scale, int monitor) {
        var old = SetThreadDpiAwarenessContext(new IntPtr(-4));
        if (old.Equals(IntPtr.Zero)) throw new Exception("Could not enable per-monitor DPI awareness");
        try {
            var before = Screens(count);
            if (width == 0 && scale == 0) return before.ConvertAll(s => s.reading).ToArray();
            var now = before;
            if (width != 0) {
                Arrange(now, monitor, width, height);
                now = Verify(before, width, height, scale == 0 ? 0 : -1, monitor);
            }
            if (scale != 0) Scale(now, monitor, scale);
            return Verify(now, 0, 0, scale, monitor).ConvertAll(s => s.reading).ToArray();
        } finally { SetThreadDpiAwarenessContext(old); }
    }
}
