"""Exercise the menu shield against only this test process's offscreen windows."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import native_visibility


FIXTURE = r'''
namespace BailongMenuShieldTest {
    using System;
    using System.Collections.Generic;
    using System.Diagnostics;
    using System.Runtime.InteropServices;
    using System.Threading;

    public static class Fixture {
        delegate IntPtr WindowProc(IntPtr hwnd, uint message, IntPtr wparam, IntPtr lparam);
        [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] struct WindowClass {
            public uint size, style;
            public WindowProc procedure;
            public int classExtra, windowExtra;
            public IntPtr instance, icon, cursor, background;
            public string menu, name;
            public IntPtr smallIcon;
        }
        [StructLayout(LayoutKind.Sequential)] struct Point { public int x,y; }
        [StructLayout(LayoutKind.Sequential)] struct Message {
            public IntPtr hwnd; public uint message; public UIntPtr wparam;
            public IntPtr lparam; public uint time; public Point point; public uint privateValue;
        }
        [StructLayout(LayoutKind.Sequential)] struct Rect { public int left,top,right,bottom; }
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode)] static extern IntPtr GetModuleHandle(string name);
        [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern ushort RegisterClassEx(ref WindowClass value);
        [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr CreateWindowEx(
            uint extended,string className,string title,uint style,int x,int y,int width,int height,
            IntPtr owner,IntPtr menu,IntPtr instance,IntPtr data);
        [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern IntPtr DefWindowProc(IntPtr hwnd,uint message,IntPtr wparam,IntPtr lparam);
        [DllImport("user32.dll")] static extern bool DestroyWindow(IntPtr hwnd);
        [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr hwnd,int command);
        [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);
        [DllImport("user32.dll")] static extern bool IsWindow(IntPtr hwnd);
        [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern bool SetProp(IntPtr hwnd,string name,IntPtr value);
        [DllImport("user32.dll",EntryPoint="GetWindowLongW")] static extern int GetStyle(IntPtr hwnd,int index);
        [DllImport("user32.dll")] static extern bool GetLayeredWindowAttributes(IntPtr hwnd,out uint color,out byte alpha,out uint flags);
        [DllImport("user32.dll")] static extern bool SetLayeredWindowAttributes(IntPtr hwnd,uint color,byte alpha,uint flags);
        [DllImport("user32.dll")] static extern int GetWindowRgn(IntPtr hwnd,IntPtr region);
        [DllImport("user32.dll")] static extern int SetWindowRgn(IntPtr hwnd,IntPtr region,bool redraw);
        [DllImport("gdi32.dll")] static extern IntPtr CreateRectRgn(int left,int top,int right,int bottom);
        [DllImport("gdi32.dll")] static extern bool DeleteObject(IntPtr value);
        [DllImport("gdi32.dll")] static extern int GetRgnBox(IntPtr region,out Rect bounds);
        [DllImport("user32.dll")] static extern bool PeekMessage(out Message value,IntPtr hwnd,uint first,uint last,uint remove);
        [DllImport("user32.dll")] static extern bool TranslateMessage(ref Message value);
        [DllImport("user32.dll")] static extern IntPtr DispatchMessage(ref Message value);
        static readonly WindowProc Callback=OnMessage;
        static readonly List<IntPtr> Windows=new List<IntPtr>();
        static readonly HashSet<IntPtr> Stubborn=new HashSet<IntPtr>();
        static IntPtr Instance;
        const uint PopupStyle=0x86000000, PopupExtended=0x08200088;

        static IntPtr OnMessage(IntPtr hwnd,uint message,IntPtr wparam,IntPtr lparam) {
            if(Stubborn.Contains(hwnd) && (message==0x10 || message==0x1F || message==0x100 || message==0x101)) return IntPtr.Zero;
            return DefWindowProc(hwnd,message,wparam,lparam);
        }
        static void Require(bool value,string message) { if(!value) throw new InvalidOperationException(message); }
        static void Register(string name) {
            var value=new WindowClass(); value.size=(uint)Marshal.SizeOf(typeof(WindowClass));
            value.procedure=Callback;value.instance=Instance;value.name=name;
            Require(RegisterClassEx(ref value)!=0,"Cannot register synthetic window class");
        }
        static IntPtr Make(string name,uint style,uint extended,IntPtr owner) {
            IntPtr hwnd=CreateWindowEx(extended,name,"Bailong isolated shield fixture",style,
                -30000,-30000,64,64,owner,IntPtr.Zero,Instance,IntPtr.Zero);
            Require(hwnd!=IntPtr.Zero,"Cannot create synthetic window: "+Marshal.GetLastWin32Error());
            Windows.Add(hwnd); return hwnd;
        }
        static void Pump(int milliseconds) {
            var timer=Stopwatch.StartNew();
            do {
                Message message;
                while(PeekMessage(out message,IntPtr.Zero,0,0,1)) { TranslateMessage(ref message);DispatchMessage(ref message); }
                Thread.Sleep(2);
            } while(timer.ElapsedMilliseconds<milliseconds);
        }
        static void Wait(Func<bool> check,string failure) {
            var timer=Stopwatch.StartNew();
            while(!check() && timer.ElapsedMilliseconds<2500) Pump(10);
            Require(check(),failure);
        }
        static int Kind(IntPtr hwnd) {
            IntPtr region=CreateRectRgn(0,0,0,0);
            try{return GetWindowRgn(hwnd,region);} finally{DeleteObject(region);}
        }
        static bool AlphaZero(IntPtr hwnd) {
            uint color,flags;byte alpha;
            return (unchecked((uint)GetStyle(hwnd,-20))&0x80000)!=0 &&
                GetLayeredWindowAttributes(hwnd,out color,out alpha,out flags) && (flags&2)!=0 && alpha==0;
        }
        static void Shape(IntPtr hwnd,int left,int top,int right,int bottom) {
            IntPtr region=CreateRectRgn(left,top,right,bottom);
            if(SetWindowRgn(hwnd,region,false)==0) {DeleteObject(region);throw new InvalidOperationException("Fixture region assignment failed");}
        }
        static bool ShapeMatches(IntPtr hwnd,int left,int top,int right,int bottom) {
            IntPtr region=CreateRectRgn(0,0,0,0);
            try {
                if(GetWindowRgn(hwnd,region)!=2) return false;
                Rect r;GetRgnBox(region,out r);
                return r.left==left && r.top==top && r.right==right && r.bottom==bottom;
            } finally {DeleteObject(region);}
        }
        public static Dictionary<string,object> Run(string mode) {
            Instance=GetModuleHandle(null);Register("Chrome_WidgetWin_1");Register("Chrome_RenderWidgetHostHWND");
            BailongNativeMenuShield shield=null;
            var report=new Dictionary<string,object>();
            try {
                IntPtr mini=Make("Chrome_WidgetWin_1",0x84000000,0x08080088,IntPtr.Zero);
                Shape(mini,0,0,0,0);SetProp(mini,"BailongNativeMiniRegionOwnerV1",new IntPtr(98765));
                ShowWindow(mini,4);
                IntPtr renderer=Make("Chrome_RenderWidgetHostHWND",0x50000000,0,mini);
                int pid=Process.GetCurrentProcess().Id;
                if(mode=="restore") {
                    IntPtr baseline=Make("Chrome_WidgetWin_1",PopupStyle,PopupExtended,mini);
                    Shape(baseline,2,3,21,22);ShowWindow(baseline,4);
                    IntPtr hidden=Make("Chrome_WidgetWin_1",PopupStyle,PopupExtended,mini);
                    Shape(hidden,3,4,25,26);
                    IntPtr otherMini=Make("Chrome_WidgetWin_1",0x84000000,0x08080088,IntPtr.Zero);
                    shield=new BailongNativeMenuShield(mini,pid,renderer);
                    Require(shield.IsArmed,"Shield did not arm");
                    IntPtr unrelated=Make("Chrome_WidgetWin_1",PopupStyle,PopupExtended,otherMini);
                    Shape(unrelated,4,5,27,28);ShowWindow(unrelated,4);
                    IntPtr popup=Make("Chrome_WidgetWin_1",PopupStyle,PopupExtended,mini);
                    ShowWindow(popup,4);
                    Wait(()=>Kind(popup)==1 && AlphaZero(popup),"New direct owned popup was not concealed");
                    Wait(()=>Kind(hidden)==1 && AlphaZero(hidden),"Hidden direct owned popup was not premasked");
                    Require(IsWindowVisible(popup),"Shield changed native popup visibility");
                    SetLayeredWindowAttributes(popup,0,255,2);SetWindowRgn(popup,IntPtr.Zero,false);
                    Wait(()=>Kind(popup)==1 && AlphaZero(popup),"App redraw reset escaped the armed shield");
                    Require(ShapeMatches(unrelated,4,5,27,28),"Unrelated same-process popup was modified");
                    Require(unchecked((uint)GetStyle(unrelated,-20))==PopupExtended,"Unrelated popup style changed");
                    Require(ShapeMatches(baseline,2,3,21,22),"Already visible owned popup was modified");
                    Require(unchecked((uint)GetStyle(baseline,-20))==PopupExtended,"Baseline popup style changed");
                    ShowWindow(popup,0);Pump(30);
                    shield.Dispose();
                    Require(Kind(popup)==0,"No-region popup was not restored after hidden");
                    Require(ShapeMatches(hidden,3,4,25,26),"Original custom region was not restored");
                    Require(unchecked((uint)GetStyle(popup,-20))==PopupExtended,"Original popup style was not restored");
                    Require(unchecked((uint)GetStyle(hidden,-20))==PopupExtended,"Original hidden-popup style was not restored");
                    Require(Kind(mini)==1,"Shield changed original full-pet Mini mask");
                    report["checks"]=9;
                } else if(mode=="visible-failure") {
                    shield=new BailongNativeMenuShield(mini,pid,renderer);
                    IntPtr popup=Make("Chrome_WidgetWin_1",PopupStyle,PopupExtended,mini);
                    Stubborn.Add(popup);ShowWindow(popup,4);
                    Wait(()=>Kind(popup)==1 && AlphaZero(popup),"Popup was not concealed");
                    shield.Dispose();Pump(50);
                    Require(IsWindow(popup) && IsWindowVisible(popup),"Fixture did not remain visible to exercise failure path");
                    Require(Kind(popup)==1 && AlphaZero(popup),"Dispose exposed a still-visible popup");
                    report["checks"]=3;
                } else throw new ArgumentException("Unknown fixture mode");
                report["shield"]=shield.Snapshot();report["passed"]=true;report["realCodexAccessed"]=false;
                return report;
            } finally {
                if(shield!=null) shield.Dispose();
                for(int i=Windows.Count-1;i>=0;i--) if(IsWindow(Windows[i])) DestroyWindow(Windows[i]);
                Windows.Clear();Stubborn.Clear();
            }
        }
    }
}
'''


@unittest.skipUnless(os.name == "nt", "Windows menu shield")
class NativeMenuShieldTests(unittest.TestCase):
    def run_fixture(self, mode):
        source = Path(native_visibility.__file__).with_name("native_menu_shield.cs")
        with tempfile.TemporaryDirectory(prefix="bailong-menu-shield-test-") as directory:
            fixture = Path(directory) / "fixture.cs"
            fixture.write_text(FIXTURE, encoding="utf-8-sig")
            script = Path(directory) / "run.ps1"
            script.write_text(
                "param([string]$Source,[string]$Fixture,[string]$Mode)\n"
                "$ErrorActionPreference='Stop'\n"
                "Add-Type -TypeDefinition ([IO.File]::ReadAllText($Source)+[Environment]::NewLine+[IO.File]::ReadAllText($Fixture))\n"
                "[BailongMenuShieldTest.Fixture]::Run($Mode)|ConvertTo-Json -Depth 5 -Compress\n",
                encoding="utf-8-sig")
            result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(script), "-Source", str(source), "-Fixture", str(fixture), "-Mode", mode],
                capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        value = json.loads(result.stdout)
        self.assertTrue(value["passed"])
        self.assertFalse(value["realCodexAccessed"])
        return value

    def test_only_new_owned_popups_are_suppressed_and_hidden_state_restores(self):
        self.run_fixture("restore")

    def test_dispose_never_exposes_a_visible_popup_when_cancellation_fails(self):
        self.run_fixture("visible-failure")


if __name__ == "__main__":
    unittest.main()
