// Suppress only native menus owned by the already-masked full-pet Mini.
// WinEvent is out-of-context: no DLL is injected into Codex.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

public sealed class BailongNativeMenuShield : IDisposable {
    const string MiniCookie = "BailongNativeMiniRegionOwnerV1";
    const string Prefix = "BailongMiniMenuShieldV1";
    const uint Layered = 0x80000, Alpha = 2;
    readonly IntPtr mini, renderer, miniToken;
    readonly int targetPid;
    readonly IntPtr token;
    readonly long helperStarted;
    readonly int helperPid;
    readonly ManualResetEvent ready = new ManualResetEvent(false);
    readonly Thread thread;
    readonly Dictionary<IntPtr, Saved> owned = new Dictionary<IntPtr, Saved>();
    readonly HashSet<IntPtr> preexistingVisible = new HashSet<IntPtr>();
    readonly object sync = new object();
    volatile bool stop;
    bool disposed;
    IntPtr hook;
    WinEvent callback;
    int popupCount, firstSeenVisible, alphaCorrections, regionCorrections, miniRegionCorrections, remainingVisible;
    string error;

    sealed class Saved {
        public uint Style, Color, Flags;
        public byte Opacity;
        public IntPtr Region;
        public int RegionKind;
    }
    delegate bool EnumProc(IntPtr hwnd, IntPtr unused);
    delegate void WinEvent(IntPtr hook, uint kind, IntPtr hwnd, int objectId, int childId, uint eventThread, uint time);
    [StructLayout(LayoutKind.Sequential)] struct Point { public int x, y; }
    [StructLayout(LayoutKind.Sequential)] struct Msg {
        public IntPtr hwnd; public uint message; public UIntPtr wparam;
        public IntPtr lparam; public uint time; public Point point; public uint privateValue;
    }
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc callback, IntPtr unused);
    [DllImport("user32.dll")] static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] static extern IntPtr GetWindow(IntPtr hwnd, uint command);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr hwnd, StringBuilder text, int size);
    [DllImport("user32.dll", EntryPoint="GetWindowLongW")] static extern int GetStyle(IntPtr hwnd, int index);
    [DllImport("user32.dll", EntryPoint="SetWindowLongW", SetLastError=true)] static extern int SetStyle(IntPtr hwnd, int index, int value);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern IntPtr GetProp(IntPtr hwnd, string name);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern bool SetProp(IntPtr hwnd, string name, IntPtr value);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern IntPtr RemoveProp(IntPtr hwnd, string name);
    [DllImport("user32.dll")] static extern bool GetLayeredWindowAttributes(IntPtr hwnd, out uint color, out byte opacity, out uint flags);
    [DllImport("user32.dll")] static extern bool SetLayeredWindowAttributes(IntPtr hwnd, uint color, byte opacity, uint flags);
    [DllImport("user32.dll")] static extern int GetWindowRgn(IntPtr hwnd, IntPtr region);
    [DllImport("user32.dll")] static extern int SetWindowRgn(IntPtr hwnd, IntPtr region, bool redraw);
    [DllImport("gdi32.dll")] static extern IntPtr CreateRectRgn(int left, int top, int right, int bottom);
    [DllImport("gdi32.dll")] static extern bool DeleteObject(IntPtr handle);
    [DllImport("user32.dll")] static extern IntPtr SetWinEventHook(uint first, uint last, IntPtr module, WinEvent callback, uint pid, uint tid, uint flags);
    [DllImport("user32.dll")] static extern bool UnhookWinEvent(IntPtr hook);
    [DllImport("user32.dll")] static extern bool PeekMessage(out Msg message, IntPtr hwnd, uint first, uint last, uint remove);
    [DllImport("user32.dll")] static extern bool TranslateMessage(ref Msg message);
    [DllImport("user32.dll")] static extern IntPtr DispatchMessage(ref Msg message);
    [DllImport("user32.dll")] static extern uint MsgWaitForMultipleObjectsEx(uint count, IntPtr handles, uint milliseconds, uint mask, uint flags);
    [DllImport("user32.dll")] static extern bool PostMessage(IntPtr hwnd, uint message, IntPtr wparam, IntPtr lparam);

    static bool SameProcess(IntPtr hwnd, int pid) {
        uint actual; return hwnd != IntPtr.Zero && IsWindow(hwnd) && GetWindowThreadProcessId(hwnd, out actual) != 0 && actual == (uint)pid;
    }
    static string Class(IntPtr hwnd) {
        var text = new StringBuilder(256); GetClassName(hwnd, text, text.Capacity); return text.ToString();
    }
    static bool IsMini(IntPtr hwnd, int pid) {
        uint style = unchecked((uint)GetStyle(hwnd, -16)), ex = unchecked((uint)GetStyle(hwnd, -20));
        return SameProcess(hwnd, pid) && Class(hwnd) == "Chrome_WidgetWin_1" && (ex & 0x88) == 0x88 && (style & 0x40C00000) == 0;
    }
    static bool IsPopup(IntPtr hwnd, IntPtr owner, int pid) {
        uint style = unchecked((uint)GetStyle(hwnd, -16)), ex = unchecked((uint)GetStyle(hwnd, -20));
        return hwnd != owner && SameProcess(hwnd, pid) && GetWindow(hwnd, 4) == owner &&
            Class(hwnd) == "Chrome_WidgetWin_1" && (style & 0x80000000) != 0 &&
            (style & 0x40C00000) == 0 && (ex & 0x88) == 0x88;
    }
    static List<IntPtr> Popups(IntPtr owner, int pid) {
        var found = new List<IntPtr>();
        EnumWindows(delegate(IntPtr hwnd, IntPtr unused) { if (IsPopup(hwnd, owner, pid)) found.Add(hwnd); return true; }, IntPtr.Zero);
        return found;
    }
    static int RegionKind(IntPtr hwnd) {
        IntPtr region = CreateRectRgn(0, 0, 0, 0);
        if (region == IntPtr.Zero) return -1;
        try { return GetWindowRgn(hwnd, region); } finally { DeleteObject(region); }
    }
    static bool EmptyRegion(IntPtr hwnd) {
        IntPtr region = CreateRectRgn(0, 0, 0, 0);
        if (region == IntPtr.Zero) return false;
        if (SetWindowRgn(hwnd, region, false) != 0) return true;
        DeleteObject(region); return false;
    }
    static bool PropNumber(IntPtr hwnd, string name, long number) {
        return SetProp(hwnd, Prefix + name, new IntPtr(number + 1));
    }
    static long Number(IntPtr hwnd, string name) { return GetProp(hwnd, Prefix + name).ToInt64() - 1; }
    static void ClearProperties(IntPtr hwnd) {
        foreach (string name in new string[] { "Owner", "Token", "Started", "Style", "Color", "Alpha", "Flags", "RegionKind" }) RemoveProp(hwnd, Prefix + name);
    }
    static void CancelPopup(IntPtr hwnd) {
        PostMessage(hwnd, 0x001F, IntPtr.Zero, IntPtr.Zero); // WM_CANCELMODE
        PostMessage(hwnd, 0x0100, new IntPtr(0x1B), new IntPtr(1));
        PostMessage(hwnd, 0x0101, new IntPtr(0x1B), new IntPtr(1));
    }

    public BailongNativeMenuShield(IntPtr mini, int targetPid, IntPtr renderer) {
        if (!IsMini(mini, targetPid)) throw new ArgumentException("Unverified Mini");
        this.mini = mini; this.targetPid = targetPid; this.renderer = renderer;
        miniToken = GetProp(mini, MiniCookie);
        if (miniToken == IntPtr.Zero) throw new ArgumentException("Mini is not owned by the full-pet mask");
        using (var process = Process.GetCurrentProcess()) { helperPid = process.Id; helperStarted = process.StartTime.ToUniversalTime().Ticks; }
        token = new IntPtr(new Random(Guid.NewGuid().GetHashCode()).Next(1, Int32.MaxValue));
        foreach (IntPtr hwnd in Popups(mini, targetPid)) if (IsWindowVisible(hwnd)) preexistingVisible.Add(hwnd);
        thread = new Thread(Run); thread.IsBackground = true; thread.Name = "Bailong Mini menu shield";
        thread.SetApartmentState(ApartmentState.STA); thread.Start();
        if (!ready.WaitOne(1000) || !IsArmed) { Dispose(); throw new InvalidOperationException("Menu shield did not arm"); }
    }

    public bool IsArmed { get { return hook != IntPtr.Zero && !stop; } }

    bool MiniOwned() { return IsMini(mini, targetPid) && GetProp(mini, MiniCookie) == miniToken; }
    bool Owns(IntPtr hwnd) { return IsPopup(hwnd, mini, targetPid) && GetProp(hwnd, Prefix + "Token") == token; }

    void Conceal(IntPtr hwnd) {
        if (!MiniOwned() || !IsPopup(hwnd, mini, targetPid) || preexistingVisible.Contains(hwnd)) return;
        Saved saved;
        if (!owned.TryGetValue(hwnd, out saved)) {
            if (GetProp(hwnd, Prefix + "Token") != IntPtr.Zero) return;
            saved = new Saved(); saved.Style = unchecked((uint)GetStyle(hwnd, -20));
            GetLayeredWindowAttributes(hwnd, out saved.Color, out saved.Opacity, out saved.Flags);
            saved.Region = CreateRectRgn(0, 0, 0, 0);
            if (saved.Region == IntPtr.Zero) return;
            saved.RegionKind = GetWindowRgn(hwnd, saved.Region);
            if (saved.RegionKind == 0) { DeleteObject(saved.Region); saved.Region = IntPtr.Zero; }
            if (!PropNumber(hwnd, "Owner", helperPid) || !PropNumber(hwnd, "Started", helperStarted) ||
                !PropNumber(hwnd, "Style", saved.Style) || !PropNumber(hwnd, "Color", saved.Color) ||
                !PropNumber(hwnd, "Alpha", saved.Opacity) || !PropNumber(hwnd, "Flags", saved.Flags) ||
                !PropNumber(hwnd, "RegionKind", saved.RegionKind) ||
                !SetProp(hwnd, Prefix + "Token", token)) {
                ClearProperties(hwnd); if (saved.Region != IntPtr.Zero) DeleteObject(saved.Region); return;
            }
            owned.Add(hwnd, saved); popupCount++;
            if (IsWindowVisible(hwnd)) firstSeenVisible++;
        }
        if (!Owns(hwnd)) return;
        // Clip first, before any style transition; alpha then keeps later
        // Chromium layout/region resets from revealing a frame of the menu.
        if (RegionKind(hwnd) != 1) {
            if (!EmptyRegion(hwnd)) error = "menu-region-failed";
            else regionCorrections++;
        }
        uint color, flags; byte opacity;
        uint ex = unchecked((uint)GetStyle(hwnd, -20));
        if ((ex & Layered) == 0) SetStyle(hwnd, -20, unchecked((int)(ex | Layered)));
        if (!GetLayeredWindowAttributes(hwnd, out color, out opacity, out flags) || (flags & Alpha) == 0 || opacity != 0) {
            if (!SetLayeredWindowAttributes(hwnd, 0, 0, Alpha)) error = "menu-alpha-failed";
            else alphaCorrections++;
        }
    }

    void Scan() {
        if (!MiniOwned()) { stop = true; return; }
        if (RegionKind(mini) != 1) {
            if (EmptyRegion(mini)) miniRegionCorrections++; else error = "mini-region-failed";
        }
        foreach (IntPtr hwnd in Popups(mini, targetPid)) Conceal(hwnd);
    }

    void RestoreHidden() {
        foreach (var pair in owned) {
            IntPtr hwnd = pair.Key; Saved saved = pair.Value;
            if (Owns(hwnd) && !IsWindowVisible(hwnd)) {
                if ((saved.Style & Layered) != 0) SetLayeredWindowAttributes(hwnd, saved.Color, saved.Opacity, saved.Flags);
                SetStyle(hwnd, -20, unchecked((int)saved.Style));
                if (SetWindowRgn(hwnd, saved.Region, false) != 0) saved.Region = IntPtr.Zero;
                ClearProperties(hwnd);
            } else if (Owns(hwnd) && IsWindowVisible(hwnd)) remainingVisible++;
            if (saved.Region != IntPtr.Zero) DeleteObject(saved.Region);
        }
    }

    void Pump() {
        Msg message;
        while (PeekMessage(out message, IntPtr.Zero, 0, 0, 1)) { TranslateMessage(ref message); DispatchMessage(ref message); }
    }

    void Run() {
        try {
            callback = delegate(IntPtr ignored, uint kind, IntPtr hwnd, int objectId, int childId, uint eventThread, uint at) {
                if (objectId != 0 || childId != 0 || hwnd == IntPtr.Zero) return;
                lock (sync) { try { Conceal(hwnd); } catch { error = "menu-event-failed"; } }
            };
            hook = SetWinEventHook(0x8000, 0x800B, IntPtr.Zero, callback, (uint)targetPid, 0, 0);
            lock (sync) { Scan(); }
            ready.Set();
            while (!stop) {
                Pump(); lock (sync) { Scan(); }
                MsgWaitForMultipleObjectsEx(0, IntPtr.Zero, 1, 0x04FF, 4);
            }
            // Menu selection normally destroys/hides the popup itself. On a
            // cancelled operation, dismiss only the popup we actually masked.
            lock (sync) { foreach (IntPtr hwnd in owned.Keys) if (Owns(hwnd) && IsWindowVisible(hwnd)) CancelPopup(hwnd); }
            var until = Stopwatch.StartNew();
            while (until.ElapsedMilliseconds < 400) {
                Pump(); bool visible = false;
                lock (sync) {
                    foreach (IntPtr hwnd in owned.Keys) if (Owns(hwnd) && IsWindowVisible(hwnd)) { visible = true; Conceal(hwnd); }
                }
                if (!visible) break;
                MsgWaitForMultipleObjectsEx(0, IntPtr.Zero, 1, 0x04FF, 4);
            }
        } catch { error = "menu-shield-failed"; ready.Set(); }
        finally {
            if (hook != IntPtr.Zero) { UnhookWinEvent(hook); hook = IntPtr.Zero; }
            lock (sync) { RestoreHidden(); }
        }
    }

    public Dictionary<string, object> Snapshot() {
        lock (sync) { return new Dictionary<string, object> {
            {"popupCount", popupCount}, {"firstSeenVisible", firstSeenVisible},
            {"alphaCorrections", alphaCorrections}, {"regionCorrections", regionCorrections},
            {"miniRegionCorrections", miniRegionCorrections}, {"remainingVisible", remainingVisible}, {"error", error}
        }; }
    }

    public void Dispose() {
        if (disposed) return; disposed = true; stop = true;
        if (thread != null && Thread.CurrentThread != thread) thread.Join(1200);
        // ready is deliberately kept alive if a timed-out thread has not exited.
    }

    public static int RecoverAbandoned(IntPtr mini, int targetPid, IntPtr renderer) {
        if (!IsMini(mini, targetPid) || GetProp(mini, MiniCookie) == IntPtr.Zero) return 0;
        int count = 0;
        foreach (IntPtr hwnd in Popups(mini, targetPid)) {
            IntPtr abandonedToken = GetProp(hwnd, Prefix + "Token");
            if (abandonedToken == IntPtr.Zero) continue;
            long owner = Number(hwnd, "Owner"), started = Number(hwnd, "Started");
            bool alive = false;
            try { using (var process = Process.GetProcessById((int)owner)) alive = !process.HasExited && process.StartTime.ToUniversalTime().Ticks == started; } catch { }
            if (alive) continue;
            CancelPopup(hwnd);
            var timer = Stopwatch.StartNew();
            while (IsPopup(hwnd, mini, targetPid) && IsWindowVisible(hwnd) && timer.ElapsedMilliseconds < 200) Thread.Sleep(1);
            if (!IsPopup(hwnd, mini, targetPid)) { count++; continue; }
            if (IsWindowVisible(hwnd)) continue; // Never reveal a stranded menu.
            if (GetProp(hwnd, Prefix + "Token") != abandonedToken) continue;
            // A GDI region belongs to the old helper's process. Never reuse its
            // numeric handle after that process died. Native Mini menus begin
            // with no explicit region; unknown custom shapes stay concealed.
            if (Number(hwnd, "RegionKind") != 0) continue;
            long oldStyle = Number(hwnd, "Style"), oldColor = Number(hwnd, "Color"), oldAlpha = Number(hwnd, "Alpha"), oldFlags = Number(hwnd, "Flags");
            if (oldStyle < 0 || oldColor < 0 || oldAlpha < 0 || oldAlpha > 255 || oldFlags < 0) continue;
            if (((uint)oldStyle & Layered) != 0) SetLayeredWindowAttributes(hwnd, (uint)oldColor, (byte)oldAlpha, (uint)oldFlags);
            SetStyle(hwnd, -20, unchecked((int)(uint)oldStyle)); SetWindowRgn(hwnd, IntPtr.Zero, false);
            ClearProperties(hwnd); count++;
        }
        return count;
    }
}
