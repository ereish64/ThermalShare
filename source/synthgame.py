"""synthgame: a repeatable synthetic game for testing the governor without War Thunder.

It draws to a real window with Direct3D 11 on the NVIDIA GPU, so PresentMon and the governor see its frames the
way they see a game's. Every frame does the same work: a heavy pixel shader at a fixed resolution on the GPU, and
a fixed amount of math on worker threads on the CPU (a game's simulation: matrix multiplies, which keep the cores
busy and scale with clock speed like game code). Runs are identical, so FPS, temperatures and clocks can be
compared between them.

  synthgame.exe calibrate [--fps 90] [--cpu-watts 34]      size the GPU and CPU work for this laptop (~3 min)
  synthgame.exe run [--minutes 10] [--latency reflex|normal] [--out result.json] [--windowed]

--latency reflex (default) waits for the GPU inside each frame, as War Thunder does with NVIDIA Reflex: the render
thread spins until the GPU is nearly done, so PresentMon reads CPU busy as the whole frame and the CPU stays at
full clock and power like War Thunder's. normal lets the CPU run ahead and wait in Present instead.
Esc closes the window and ends a run early.
"""
import argparse
import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes as w
from statistics import median

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")     # one BLAS thread per worker: the workers are the parallelism
os.environ.setdefault("OMP_NUM_THREADS", "1")
import numpy as np                                      # noqa: E402

from hwctl import AB_CPU_POWER, HERE, Afterburner, atomic_json, run_app  # noqa: E402

CALIB = HERE / "synthgame.json"                         # saved calibration: how much GPU/CPU work per frame
CALIB_VERSION = 2                                       # 2: matrix-multiply CPU work, spinning Reflex wait
LAST = HERE / "synthgame_last.json"                     # default place for a run's result
# Short names for the C types used when calling Windows / DirectX through ctypes.
PTR = ctypes.c_void_p
HRESULT = ctypes.c_long                                 # DirectX's success/failure code: negative = failure
UINT = ctypes.c_uint
NVIDIA = 0x10DE                                         # PCI vendor ID of NVIDIA
IID_FACTORY1 = "770aae78-f26f-4dba-a829-253c83d1b387"   # COM interface IDs: IDXGIFactory1 and ID3D11Texture2D
IID_TEX2D = "6f15aaf2-d208-4e89-9ab4-489535d34f9c"
S_FALSE = 1                                             # "not ready yet" (a success code, unlike failures)
EXIT_ABORTED = 2                                        # exit code when the user closes the window early
ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x1, 0x2    # SetThreadExecutionState flags


def keep_awake(on):
    """Like a game: no sleep and no display-off while running (a dark display can change how frames are shown)."""
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ((ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED) if on else 0))
    except Exception:
        pass

# The two shaders (small GPU programs, written in HLSL) that make up the load. They are compiled when the program
# starts. The vertex shader just covers the whole screen with one big triangle; the pixel shader does the work.
VS = b"""
float4 main(uint id : SV_VertexID) : SV_Position {
    float2 uv = float2((id << 1) & 2, id & 2);
    return float4(uv * float2(2, -2) + float2(-1, 1), 0, 1);
}
"""
# ALU-heavy and bounded (frac), so the driver can't skip it and every pixel costs the same in every frame.
PS = b"""
cbuffer C : register(b0) { float t; uint iters; float2 res; };
float4 main(float4 p : SV_Position) : SV_Target {
    float2 uv = p.xy / res;
    float4 a = float4(uv, uv.yx) * 1.7 + frac(t * 0.05);
    float4 b = float4(0.3, 0.7, 1.1, 1.3) + uv.xyxy;
    float4 c = b.wzyx;
    [loop] for (uint i = 0; i < iters; i++) {
        a = a * b + c;
        b = b * c - a * 0.25;
        c = frac(c * a + 0.618);
        a = frac(a) + 0.5;
        b = frac(b) + 0.25;
    }
    float v = dot(a + b + c, 0.083);
    return float4(frac(v), frac(v * 1.3 + uv.x), frac(v * 0.7 + uv.y), 1);
}
"""


# ---------------------------------------------------------------- pure helpers (tested)

def pick_adapter(descs, hint=None):
    """Index of the GPU to render on: one whose name contains `hint`, else the NVIDIA one, else the first
    hardware adapter. descs: [{"name", "vendor", "flags"}]."""
    hw = [i for i, d in enumerate(descs) if not d.get("flags", 0) & 2]          # DXGI_ADAPTER_FLAG_SOFTWARE
    if hint:
        for i in hw:
            if hint.lower() in descs[i]["name"].lower():
                return i
    for i in hw:
        if descs[i].get("vendor") == NVIDIA:
            return i
    return hw[0] if hw else None


def frame_stats(stamps):
    """FPS, 1% and 5% lows and mean frame time from present timestamps in seconds."""
    if len(stamps) < 3 or stamps[-1] <= stamps[0]:
        return None
    ft = np.diff(np.asarray(stamps, dtype=np.float64)) * 1000.0
    return {"frames": int(ft.size), "fps": round(ft.size / (stamps[-1] - stamps[0]), 2),
            "low1": round(1000.0 / float(np.percentile(ft, 99)), 2),
            "low5": round(1000.0 / float(np.percentile(ft, 95)), 2), "ft_ms": round(float(ft.mean()), 3)}


def split_by_time(stamps, values, edges):
    """Split stamps (and a parallel list of values) into the windows [edges[i], edges[i+1])."""
    out, j = [], 0                       # j only moves forward, since stamps and edges are both in time order
    for lo, hi in zip(edges, edges[1:]):
        s, v = [], []
        while j < len(stamps) and stamps[j] < lo:      # skip frames before this window
            j += 1
        k = j
        while k < len(stamps) and stamps[k] < hi:
            s.append(stamps[k])
            v.append(values[k] if values else None)
            k += 1
        out.append((s, v))
    return out


def summarize(stamps, sims, waits, seconds_asked):
    """Whole-run, per-half and per-minute numbers for a run's result file."""
    st = frame_stats(stamps) or {}
    out = dict(st)
    if not stamps:
        return out
    t0, t1 = stamps[0], stamps[-1]
    mid = t0 + (t1 - t0) / 2
    # First half (h1) and second half (h2): the second half shows the steady, heat-soaked state.
    for name, (s, v) in zip(("h1", "h2"), split_by_time(stamps, sims, [t0, mid, t1 + 1e-9])):
        h = frame_stats(s) or {}
        for k in ("fps", "low1", "low5"):
            out[f"{k}_{name}"] = h.get(k)
        vv = [x for x in v if x is not None]
        out[f"sim_ms_{name}"] = round(1000 * sum(vv) / len(vv), 3) if vv else None
    minutes = int((t1 - t0) // 60)
    edges = [t0 + 60 * i for i in range(minutes + 1)]
    out["per_minute_fps"] = [(frame_stats(s) or {}).get("fps") for s, _ in split_by_time(stamps, None, edges)] \
        if minutes else []
    out["sim_ms"] = round(1000 * sum(sims) / len(sims), 3) if sims else None
    out["gpu_wait_ms"] = round(1000 * sum(waits) / len(waits), 3) if waits else None
    out["seconds"] = round(t1 - t0, 1)
    out["seconds_asked"] = seconds_asked
    return out


def next_units(units, measured_ms, target_ms):
    """New CPU work size (in units) so that the work takes target_ms, scaled from how long `units` took."""
    return max(0.25, round(units * target_ms / measured_ms, 2)) if measured_ms > 0 else units


def next_iters(iters, fps, target_fps):
    """GPU time per frame scales with the shader's loop count; at most 4x per step so a bad probe can't send it
    somewhere silly."""
    if not fps or fps <= 0:                     # no usable measurement: halve the work and try again
        return max(8, iters // 2)
    # Scale by (measured FPS / target FPS): too fast means more loops are needed, too slow means fewer.
    return int(min(max(iters * fps / target_fps, iters / 4, 8), iters * 4, 200000))


# ---------------------------------------------------------------- COM and Win32 plumbing
# DirectX is a COM API: an interface pointer points at a table of function pointers (the "vtable"), and a method is
# called by its position in that table. The C structs below mirror DirectX's own so ctypes can pass them in. Without
# a C compiler or DirectX binding library, this talks to DirectX directly through ctypes. The numbers passed as
# `slot` below are those vtable positions (e.g. Release is always slot 2).

class GUID(ctypes.Structure):
    """A 128-bit Windows identifier (used to name COM interfaces)."""
    _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16), ("d3", ctypes.c_uint16), ("d4", ctypes.c_ubyte * 8)]


def guid(s):
    """A GUID struct from its text form "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"."""
    h = s.replace("-", "")
    return GUID(int(h[:8], 16), int(h[8:12], 16), int(h[12:16], 16), (ctypes.c_ubyte * 8)(*bytes.fromhex(h[16:])))


class Com:
    """A COM interface pointer. com(slot, restype, *argtypes) returns that vtable method bound to the object."""

    def __init__(self, p):
        self.p = PTR(p.value if isinstance(p, PTR) else p)
        self.fns = {}                           # cache of already-bound methods, by slot

    def __call__(self, slot, restype, *argtypes):
        """The method at vtable position `slot`, as a callable that already has this object as its first
        argument (COM's hidden "this")."""
        f = self.fns.get(slot)
        if f is None:
            # The object's first word points to its vtable; read that table and wrap entry `slot` as a function.
            vtbl = ctypes.cast(self.p, ctypes.POINTER(ctypes.POINTER(PTR))).contents
            f = self.fns[slot] = ctypes.WINFUNCTYPE(restype, PTR, *argtypes)(vtbl[slot])
        p = self.p
        return lambda *a: f(p, *a)

    def release(self):
        """Drop our reference (COM's IUnknown::Release, slot 2) so DirectX can free the object. Safe to repeat."""
        if self.p:
            self(2, ctypes.c_ulong)()
            self.p = PTR()


def check(hr, what):
    """Raise a readable error if a DirectX call returned a failure code (negative HRESULT); else pass it through."""
    if hr < 0:
        raise RuntimeError(f"{what} failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")
    return hr


# The structs below mirror DirectX / Win32 structures of the same names, field for field.
class LUID(ctypes.Structure):
    _fields_ = [("Low", ctypes.c_uint32), ("High", ctypes.c_int32)]


class DXGI_ADAPTER_DESC1(ctypes.Structure):          # describes one graphics adapter: its name, vendor and flags
    _fields_ = [("Description", ctypes.c_wchar * 128), ("VendorId", UINT), ("DeviceId", UINT), ("SubSysId", UINT),
                ("Revision", UINT), ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t), ("SharedSystemMemory", ctypes.c_size_t),
                ("AdapterLuid", LUID), ("Flags", UINT)]


class DXGI_RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", UINT), ("Denominator", UINT)]


class DXGI_MODE_DESC(ctypes.Structure):
    _fields_ = [("Width", UINT), ("Height", UINT), ("RefreshRate", DXGI_RATIONAL), ("Format", UINT),
                ("ScanlineOrdering", UINT), ("Scaling", UINT)]


class DXGI_SAMPLE_DESC(ctypes.Structure):
    _fields_ = [("Count", UINT), ("Quality", UINT)]


class DXGI_SWAP_CHAIN_DESC(ctypes.Structure):
    _fields_ = [("BufferDesc", DXGI_MODE_DESC), ("SampleDesc", DXGI_SAMPLE_DESC), ("BufferUsage", UINT),
                ("BufferCount", UINT), ("OutputWindow", PTR), ("Windowed", ctypes.c_int), ("SwapEffect", UINT),
                ("Flags", UINT)]


class D3D11_VIEWPORT(ctypes.Structure):
    _fields_ = [(n, ctypes.c_float) for n in ("TopLeftX", "TopLeftY", "Width", "Height", "MinDepth", "MaxDepth")]


class D3D11_BUFFER_DESC(ctypes.Structure):
    _fields_ = [(n, UINT) for n in ("ByteWidth", "Usage", "BindFlags", "CPUAccessFlags", "MiscFlags",
                                    "StructureByteStride")]


class D3D11_QUERY_DESC(ctypes.Structure):
    _fields_ = [("Query", UINT), ("MiscFlags", UINT)]


class CB(ctypes.Structure):
    """The shader's constant buffer: time, loop count and resolution. Its layout must match `cbuffer C` in PS."""
    _fields_ = [("t", ctypes.c_float), ("iters", UINT), ("w", ctypes.c_float), ("h", ctypes.c_float)]


WNDPROC =ctypes.WINFUNCTYPE(ctypes.c_ssize_t, PTR, UINT, ctypes.c_size_t, ctypes.c_ssize_t)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", UINT), ("style", UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", PTR), ("hIcon", PTR), ("hCursor", PTR),
                ("hbrBackground", PTR), ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p),
                ("hIconSm", PTR)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", PTR), ("message", UINT), ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                ("time", ctypes.c_uint32), ("pt", w.POINT), ("lPrivate", ctypes.c_uint32)]


class Window:
    """The window the frames are shown in: borderless fullscreen (like a game) or a plain window. Esc closes it."""
    CLASS = "TuneKitSynthGame"

    def __init__(self, size, fullscreen=True, title="synthgame"):
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        self.u = u
        try:
            u.SetProcessDpiAwarenessContext(PTR(-4))             # physical pixels
        except Exception:
            pass
        # Declare argument/return types for each Win32 call: ctypes would otherwise assume 32-bit values and
        # truncate the 64-bit handles and pointers.
        u.DefWindowProcW.restype = ctypes.c_ssize_t
        u.DefWindowProcW.argtypes = [PTR, UINT, ctypes.c_size_t, ctypes.c_ssize_t]
        u.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
        u.CreateWindowExW.restype = PTR
        u.CreateWindowExW.argtypes = [w.DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p, w.DWORD, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, PTR, PTR, PTR, PTR]
        u.ShowWindow.argtypes = [PTR, ctypes.c_int]
        u.SetForegroundWindow.argtypes = [PTR]
        u.DestroyWindow.argtypes = [PTR]
        u.PeekMessageW.argtypes = [ctypes.POINTER(MSG), PTR, UINT, UINT, UINT]
        u.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
        u.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
        u.DispatchMessageW.restype = ctypes.c_ssize_t
        u.LoadCursorW.restype = PTR
        u.LoadCursorW.argtypes = [PTR, PTR]
        u.SetWindowTextW.argtypes = [PTR, ctypes.c_wchar_p]
        k.GetModuleHandleW.restype = PTR
        k.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        self.closed = False
        self._proc = WNDPROC(self._wndproc)       # kept on self so the callback isn't garbage-collected while in use
        hinst = k.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.hCursor = u.LoadCursorW(None, PTR(32512))             # IDC_ARROW
        wc.lpszClassName = self.CLASS
        u.RegisterClassExW(ctypes.byref(wc))                    # fails harmlessly if already registered
        if fullscreen:                                          # borderless, covering the primary monitor
            style, x, y, ww, hh = 0x80000000 | 0x10000000, 0, 0, u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        else:
            style, x, y, (ww, hh) = 0x00CF0000 | 0x10000000, 60, 60, size
        self.hwnd = u.CreateWindowExW(0, self.CLASS, title, style, x, y, ww, hh, None, None, hinst, None)
        if not self.hwnd:
            raise ctypes.WinError()
        u.ShowWindow(self.hwnd, 5)
        u.SetForegroundWindow(self.hwnd)
        self.fullscreen = fullscreen
        if fullscreen:
            u.ShowCursor(False)

    def _wndproc(self, hwnd, msg, wp, lp):
        """Windows calls this for every message sent to the window: handle Esc and closing, pass the rest on."""
        if msg == 0x0100 and wp == 0x1B:                        # WM_KEYDOWN, Esc
            self.u.DestroyWindow(hwnd)
            return 0
        if msg == 0x0002:                                       # WM_DESTROY
            self.closed = True
            self.u.PostQuitMessage(0)
            return 0
        return self.u.DefWindowProcW(hwnd, msg, wp, lp)

    def pump(self):
        """Process any waiting window messages (needed every frame, or Windows treats the program as hung).
        Returns False once the window has been closed."""
        m = MSG()
        while self.u.PeekMessageW(ctypes.byref(m), None, 0, 0, 1):
            if m.message == 0x0012:                             # WM_QUIT
                self.closed = True
            self.u.TranslateMessage(ctypes.byref(m))
            self.u.DispatchMessageW(ctypes.byref(m))
        return not self.closed

    def title(self, s):
        self.u.SetWindowTextW(self.hwnd, s)

    def close(self):
        if self.fullscreen:
            self.u.ShowCursor(True)
        if not self.closed:
            self.u.DestroyWindow(self.hwnd)
            self.pump()


def compile_hlsl(src, target):
    """Compile HLSL source for a shader target such as b"ps_5_0" using Windows' d3dcompiler_47.dll. Returns the
    compiled bytecode as a COM blob; raises with the compiler's message if the source is wrong."""
    dc = ctypes.windll.d3dcompiler_47
    dc.D3DCompile.restype = HRESULT
    dc.D3DCompile.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, PTR, PTR, ctypes.c_char_p,
                              ctypes.c_char_p, UINT, UINT, ctypes.POINTER(PTR), ctypes.POINTER(PTR)]
    code, err = PTR(), PTR()
    hr = dc.D3DCompile(src, len(src), b"synthgame", None, None, b"main", target, 1 << 15, 0,
                       ctypes.byref(code), ctypes.byref(err))
    msg = ""
    if err.value:       # the compiler's error text, as a blob: slot 3 = GetBufferPointer, slot 4 = GetBufferSize
        e = Com(err)
        msg = ctypes.string_at(e(3, PTR)(), e(4, ctypes.c_size_t)()).decode(errors="replace")
        e.release()
    if hr < 0:
        raise RuntimeError(f"Shader compile failed ({target.decode()}): {msg}")
    return Com(code)


class Renderer:
    """Direct3D 11 on the chosen GPU: one full-screen triangle with the heavy pixel shader per frame."""

    def __init__(self, hwnd, res, hint=None):
        self.rw, self.rh = res                  # render resolution (the shader's work scales with this)
        self.k = 0                              # frames drawn so far
        self.objs = []                          # every DirectX object created, released in reverse order on close
        dxgi, d3d = ctypes.windll.dxgi, ctypes.windll.d3d11
        dxgi.CreateDXGIFactory1.restype = HRESULT
        dxgi.CreateDXGIFactory1.argtypes = [ctypes.POINTER(GUID), ctypes.POINTER(PTR)]
        fp = PTR()
        check(dxgi.CreateDXGIFactory1(ctypes.byref(guid(IID_FACTORY1)), ctypes.byref(fp)), "CreateDXGIFactory1")
        factory = Com(fp)
        # List every graphics adapter (on an Optimus laptop: the Intel one and the NVIDIA one) so the right one
        # can be picked; synthgame must render on the NVIDIA GPU or it would measure the wrong chip.
        descs, adapters = [], []
        enum = factory(12, HRESULT, UINT, ctypes.POINTER(PTR))     # IDXGIFactory1::EnumAdapters1
        while True:
            ap = PTR()
            if enum(len(adapters), ctypes.byref(ap)) < 0:
                break
            a = Com(ap)
            d = DXGI_ADAPTER_DESC1()
            check(a(10, HRESULT, ctypes.POINTER(DXGI_ADAPTER_DESC1))(ctypes.byref(d)), "GetDesc1")
            descs.append({"name": d.Description, "vendor": d.VendorId, "flags": d.Flags})
            adapters.append(a)
        idx = pick_adapter(descs, hint)
        if idx is None:
            raise RuntimeError("No hardware GPU found.")
        self.adapter = descs[idx]["name"]
        for i, a in enumerate(adapters):
            if i != idx:
                a.release()
        levels = (UINT * 1)(0xB000)             # ask for Direct3D feature level 11.0
        d3d.D3D11CreateDeviceAndSwapChain.restype = HRESULT
        d3d.D3D11CreateDeviceAndSwapChain.argtypes = [PTR, UINT, PTR, UINT, ctypes.POINTER(UINT), UINT, UINT,
                                                      ctypes.POINTER(DXGI_SWAP_CHAIN_DESC), ctypes.POINTER(PTR),
                                                      ctypes.POINTER(PTR), ctypes.POINTER(UINT), ctypes.POINTER(PTR)]
        # Flip model with tearing allowed, like a game with V-Sync off: Present never waits for the display, so
        # frame times follow the GPU. The old blit model (last resort) lets the compositor hold frames until the
        # next refresh, which snaps frame times to refresh steps and wrecks the 1%/5% lows.
        for mode, effect, buffers, flags, pflags in (("flip+tearing", 4, 2, 2048, 0x200), ("flip", 4, 2, 0, 0),
                                                     ("blit", 0, 1, 0, 0)):
            # effect 4 = flip model (DXGI_SWAP_EFFECT_FLIP_DISCARD), 0 = old blit model; flags 2048 / pflags 0x200
            # = ALLOW_TEARING, which lets Present return without waiting for the display.
            scd = DXGI_SWAP_CHAIN_DESC()
            scd.BufferDesc.Width, scd.BufferDesc.Height, scd.BufferDesc.Format = self.rw, self.rh, 28  # RGBA8
            scd.SampleDesc.Count = 1
            scd.BufferUsage, scd.BufferCount, scd.OutputWindow, scd.Windowed = 0x20, buffers, hwnd, 1
            scd.SwapEffect, scd.Flags = effect, flags
            sc, dev, ctx, got = PTR(), PTR(), PTR(), UINT()
            hr = d3d.D3D11CreateDeviceAndSwapChain(adapters[idx].p, 0, None, 0x1, levels, 1, 7, ctypes.byref(scd),
                                                   ctypes.byref(sc), ctypes.byref(dev), ctypes.byref(got),
                                                   ctypes.byref(ctx))
            if hr >= 0:
                self.present_mode, self.present_flags = mode, pflags
                break
        check(hr, "D3D11CreateDeviceAndSwapChain")
        adapters[idx].release()
        factory(8, HRESULT, PTR, UINT)(PTR(hwnd), 2)            # MakeWindowAssociation: no Alt+Enter
        factory.release()
        self.sc, self.dev, self.ctx = Com(sc), Com(dev), Com(ctx)
        tex, rtv = PTR(), PTR()
        check(self.sc(9, HRESULT, UINT, ctypes.POINTER(GUID), ctypes.POINTER(PTR))(
            0, ctypes.byref(guid(IID_TEX2D)), ctypes.byref(tex)), "GetBuffer")
        check(self.dev(9, HRESULT, PTR, PTR, ctypes.POINTER(PTR))(tex, None, ctypes.byref(rtv)),
              "CreateRenderTargetView")
        Com(tex).release()
        self.rtv = self._keep(rtv)
        shaders = []        # slot 12 = CreateVertexShader, 15 = CreatePixelShader (both on the device)
        for src, target, slot in ((VS, b"vs_5_0", 12), (PS, b"ps_5_0", 15)):
            blob, out = compile_hlsl(src, target), PTR()
            check(self.dev(slot, HRESULT, PTR, ctypes.c_size_t, PTR, ctypes.POINTER(PTR))(
                blob(3, PTR)(), blob(4, ctypes.c_size_t)(), None, ctypes.byref(out)), "Create shader")
            blob.release()
            shaders.append(self._keep(out))
        cb = PTR()          # the constant buffer: 16 bytes (matches CB), bind flag 4 = constant buffer
        check(self.dev(3, HRESULT, ctypes.POINTER(D3D11_BUFFER_DESC), PTR, ctypes.POINTER(PTR))(
            ctypes.byref(D3D11_BUFFER_DESC(16, 0, 4, 0, 0, 0)), None, ctypes.byref(cb)), "CreateBuffer")
        self.cb = self._keep(cb)
        # Three "event" queries (type 0) that the GPU signals when it finishes a frame. wait_gpu() polls these to
        # know how far behind the GPU is; three are used round-robin so a frame's query isn't reused too soon.
        self.queries = []
        for _ in range(3):
            q = PTR()
            check(self.dev(24, HRESULT, ctypes.POINTER(D3D11_QUERY_DESC), ctypes.POINTER(PTR))(
                ctypes.byref(D3D11_QUERY_DESC(0, 0)), ctypes.byref(q)), "CreateQuery")
            self.queries.append(self._keep(q))
        # One-time pipeline setup: everything that never changes between frames is set here; frame() then only
        # updates the constant buffer, draws and presents. (The bound methods are kept for speed.)
        c = self.ctx
        self._vp = D3D11_VIEWPORT(0, 0, self.rw, self.rh, 0, 1)
        self._rtvs = (PTR * 1)(self.rtv.p)
        self._cbs = (PTR * 1)(self.cb.p)
        c(44, None, UINT, ctypes.POINTER(D3D11_VIEWPORT))(1, ctypes.byref(self._vp))     # RSSetViewports
        c(24, None, UINT)(4)                                                              # triangle list
        c(11, None, PTR, PTR, UINT)(shaders[0].p, None, 0)                                # VSSetShader
        c(9, None, PTR, PTR, UINT)(shaders[1].p, None, 0)                                 # PSSetShader
        c(16, None, UINT, UINT, PTR)(0, 1, ctypes.byref(self._cbs))                       # PSSetConstantBuffers
        self._om = c(33, None, UINT, PTR, PTR)                                            # OMSetRenderTargets
        self._update = c(48, None, PTR, UINT, PTR, PTR, UINT, UINT)                       # UpdateSubresource
        self._draw = c(13, None, UINT, UINT)                                              # Draw
        self._end = c(28, None, PTR)                                                      # End (signal a query)
        self._getdata = c(29, HRESULT, PTR, PTR, UINT, UINT)                              # GetData (poll a query)
        self._present = self.sc(8, HRESULT, UINT, UINT)                                   # IDXGISwapChain::Present
        self.data = CB(0.0, 64, float(self.rw), float(self.rh))                           # shader inputs (start: 64 loops)

    def _keep(self, p):
        """Wrap a raw COM pointer and remember it, so close() releases it."""
        o = Com(p)
        self.objs.append(o)
        return o

    @property
    def iters(self):
        """How many times the pixel shader's loop runs per pixel: the knob that sets the GPU load per frame."""
        return self.data.iters

    @iters.setter
    def iters(self, n):
        self.data.iters = int(n)

    def wait_gpu(self, back=2):
        """Spin until frame k-back has finished on the GPU: a Reflex-style wait inside the frame. Spinning, not
        sleeping, keeps this thread's core busy the way a game's render thread does, so the CPU stays at full
        clock between bursts of work instead of dropping to an idle clock (which would make a CPU cap look free).
        The workers are parked at this point, so the spin doesn't take the GIL from them."""
        if self.k < back:
            return 0.0
        q = self.queries[(self.k - back) % 3].p
        clock = time.perf_counter
        t0 = clock()
        while self._getdata(q, None, 0, 1) == S_FALSE:          # D3D11_ASYNC_GETDATA_DONOTFLUSH
            # Ask the driver only every 0.1 ms: polling it flat out stalled the GPU now and then (25 ms frames).
            until = clock() + 0.0001
            while clock() < until:
                pass
        return clock() - t0

    def frame(self, t):
        """Draw and present one frame at time t (seconds since start; only varies the picture)."""
        self.data.t = t
        self._om(1, ctypes.byref(self._rtvs), None)                    # draw into the back buffer
        self._update(self.cb.p, 0, None, ctypes.byref(self.data), 0, 0)    # send time and loop count to the shader
        self._draw(3, 0)                                               # 3 vertices = one triangle covering the screen
        self._end(self.queries[self.k % 3].p)                          # mark "GPU finished this frame" for wait_gpu
        hr = self._present(0, self.present_flags)                      # sync interval 0: don't wait for the display
        if hr < 0:
            raise RuntimeError(f"Present failed (HRESULT 0x{hr & 0xFFFFFFFF:08X}): the GPU driver reset or the "
                               "device was removed")
        self.k += 1

    def close(self):
        """Release every DirectX object (the device last), so the GPU and window are freed cleanly."""
        try:
            self.ctx(110, None)()                               # ClearState
        except Exception:
            pass
        for o in reversed(self.objs):
            o.release()
        for o in (self.ctx, self.sc, self.dev):
            o.release()


class Sim:
    """Worker threads that each do the same math every frame, like a game's simulation: 192x192 matrix
    multiplies. They are compute-bound (the cores stay busy, time scales with clock speed, power like real game
    code), and numpy releases the GIL inside each one, so the workers really run in parallel."""

    N = 192                                                     # ~0.15 ms per unit per thread at 4 GHz

    def __init__(self, workers, units):
        self.whole = int(units)                                 # fractional units: the last one uses fewer rows
        self.part = int(round((units - self.whole) * self.N))
        self.stop = False
        # Two barriers keep the workers in step with the render thread: "go" releases them all to start a frame's
        # work together, "done" makes the render thread wait until the last one has finished (+1 = the caller).
        self.go = threading.Barrier(workers + 1)
        self.done = threading.Barrier(workers + 1)
        self.threads = [threading.Thread(target=self._work, args=(i,), daemon=True) for i in range(workers)]
        for t in self.threads:
            t.start()

    def _work(self, seed):
        """One worker thread: wait for "go", do this frame's matrix multiplies, report "done", repeat."""
        rng = np.random.default_rng(seed)
        a = rng.random((self.N, self.N), dtype=np.float32)
        b = rng.random((self.N, self.N), dtype=np.float32)
        c = np.empty_like(a)
        jobs = [(a, c)] * self.whole + ([(a[:self.part], c[:self.part])] if self.part else [])
        while True:
            try:
                self.go.wait()
            except threading.BrokenBarrierError:
                return
            if self.stop:
                return
            # Same inputs every pass (a and b are never written), so the cost per unit can't drift during a run.
            for x, z in jobs:
                np.matmul(x, b, out=z)
            try:
                self.done.wait()
            except threading.BrokenBarrierError:
                return

    def frame(self):
        """Run one frame's CPU work on all workers and return when they have all finished."""
        self.go.wait()
        self.done.wait()

    def close(self):
        """Stop the workers: aborting the barriers wakes any that are waiting, and they exit."""
        self.stop = True
        self.go.abort()
        self.done.abort()


# ---------------------------------------------------------------- running

class Game:
    """Window + renderer + CPU workers together: the synthetic game. cfg (from calibration) holds the resolution,
    the shader loop count (gpu_iters), the number of worker threads and the CPU work per frame (cpu_units).
    ThermalShare's load test uses it with workers=0 and cpu_units=0 for a GPU-only load."""

    def __init__(self, cfg, windowed=False, title="synthgame"):
        res = tuple(cfg["res"])
        keep_awake(True)
        self.win = Window((1280, 720), fullscreen=not windowed, title=title)
        try:
            self.rnd = Renderer(self.win.hwnd, res, cfg.get("adapter_hint"))
            self.rnd.iters = cfg["gpu_iters"]
            self.sim = Sim(cfg["workers"], cfg["cpu_units"])
        except Exception:
            self.win.close()
            raise
        self.t0 = time.perf_counter()

    def drive(self, seconds, latency="reflex", progress=None, every=30.0):
        """Render for `seconds`. Returns (present stamps, sim seconds per frame, GPU waits, aborted)."""
        stamps, sims, waits = [], [], []
        start = last = time.perf_counter()
        while True:
            if not self.win.pump():
                return stamps, sims, waits, True
            if latency == "reflex":
                waits.append(self.rnd.wait_gpu(2))
            s0 = time.perf_counter()
            self.sim.frame()
            sims.append(time.perf_counter() - s0)
            self.rnd.frame(s0 - self.t0)
            now = time.perf_counter()
            stamps.append(now)
            if now - start >= seconds:
                return stamps, sims, waits, False
            if progress and now - last >= every:
                last = now
                recent = [t for t in stamps if t >= now - every]
                n = min(len(recent), len(sims))
                progress(now - start, (len(recent) - 1) / (recent[-1] - recent[0]) if len(recent) > 2 else 0,
                         1000 * sum(sims[-n:]) / n if n else 0)

    def set_units(self, units, workers=None):
        """Change the CPU work per frame by replacing the worker threads."""
        self.sim.close()
        self.sim = Sim(workers or len(self.sim.threads), units)

    def measure(self, seconds, latency, ab=None, skip=6.0):
        """drive() for `seconds` while sampling Afterburner's CPU power and clock once a second (after `skip` s)."""
        samples, stop = [], threading.Event()

        def poll():
            """Background sampler: record CPU power and clock once a second, after the first `skip` seconds."""
            t0 = time.time()
            while not stop.wait(1.0):
                if time.time() - t0 >= skip:
                    samples.append((ab.get(*AB_CPU_POWER), ab.get("CPU clock")))
        th = threading.Thread(target=poll, daemon=True) if ab else None
        if th:
            th.start()
        try:
            stamps, sims, waits, aborted = self.drive(seconds, latency)
        finally:
            stop.set()
        if aborted:
            raise SystemExit("Calibration stopped (window closed).")
        # Drop the first fifth of the run (clocks and caches still settling) before summarising.
        st = summarize(stamps[len(stamps) // 5:], sims[len(sims) // 5:], waits[len(waits) // 5:], seconds)
        w = [p for p, _ in samples if p is not None]
        c = [m for _, m in samples if m is not None]
        st["cpu_watts"] = round(sum(w) / len(w), 1) if w else None
        st["cpu_mhz"] = round(sum(c) / len(c)) if c else None
        return st

    def close(self):
        self.sim.close()
        self.rnd.close()
        self.win.close()
        keep_awake(False)


def load_calib():
    """The saved calibration, or None if there is none or it's from an older, incompatible version."""
    import json
    if not CALIB.exists():
        return None
    cfg = json.loads(CALIB.read_text(encoding="utf-8"))
    return cfg if cfg.get("version", 1) >= CALIB_VERSION else None


def next_units_for_power(points, target_w, u_max, u_min=0.5):
    """CPU work units to try next for a CPU power target, from [(units, watts)] measured so far. Power rises
    roughly linearly with the work on top of a base (the spinning render thread, the GPU driver), so: a 40% step
    from one point, then a straight line through the last two. Clamped to [u_min, u_max]."""
    u, p = points[-1]
    if len(points) == 1 or points[-1][0] == points[-2][0]:
        new = u * (1.4 if p < target_w else 0.6)
    else:
        (u0, p0), (u1, p1) = points[-2], points[-1]
        slope = (p1 - p0) / (u1 - u0)
        new = u1 + (target_w - p1) / slope if slope > 0.05 else u * (1.2 if p < target_w else 0.8)
        new = min(max(new, u * 0.4), u * 2.5)
    return round(min(max(new, u_min), u_max), 2)


def max_units_for_floor(units, sim_ms, cpu_mhz, frame_ms, floor_mhz=2400, budget=0.8):
    """Most work that still fits in `budget` of the frame with the CPU held at floor_mhz (the governor's floor),
    so a capped CPU never becomes the bottleneck, as in War Thunder's fights."""
    if not sim_ms or not units:
        return units
    slow = max(1.0, (cpu_mhz or 4000) / floor_mhz)
    return round(units * budget * frame_ms / (sim_ms * slow), 2)


def measure_sim(workers, units, frames=40):
    """Median time (ms) of one frame's CPU work for this size, measured without any GPU work."""
    sim = Sim(workers, units)
    try:
        for _ in range(10):                     # warm-up frames, not timed
            sim.frame()
        ts = []
        for _ in range(frames):
            s = time.perf_counter()
            sim.frame()
            ts.append(time.perf_counter() - s)
    finally:
        sim.close()
    return 1000 * median(ts)


def fit_gpu(game, target_fps, latency, say, tries=10):
    """Adjust the shader's loop count until the game runs within 2% of target_fps (GPU load tuned to this GPU).
    Each try renders for 4 seconds, measures FPS over the last three quarters of them, and corrects the loop
    count with next_iters(). Returns the last FPS measured. (ThermalShare calls this for its GPU load test.)"""
    for _ in range(tries):
        stamps, sims, _, aborted = game.drive(4, latency)
        if aborted:
            raise SystemExit("Calibration stopped (window closed).")
        fps = (frame_stats(stamps[len(stamps) // 4:]) or {}).get("fps", 0)
        say(f"  GPU: {game.rnd.iters} iterations -> {fps:.1f} fps (CPU work {1000 * median(sims):.2f} ms)", flush=True)
        if abs(fps - target_fps) <= target_fps * 0.02:
            return fps
        game.rnd.iters = next_iters(game.rnd.iters, fps, target_fps)
    return fps


def calibrate(target_fps=90.0, cpu_watts=34.0, cpu_share=0.3, workers=6, res=(1920, 1080), latency="reflex",
              windowed=False, hint=None, floor_mhz=2400, probe_s=20.0, say=print):
    """GPU work for target_fps, then CPU work for cpu_watts of CPU power at full speed (War Thunder's ~34 W in
    the logs), read from MSI Afterburner. Without Afterburner the CPU work is cpu_share of the frame instead."""
    frame_ms = 1000.0 / target_fps
    ab = Afterburner()
    by_watts = bool(cpu_watts) and latency == "reflex" and ab.get(*AB_CPU_POWER) is not None
    say(f"Calibrating for {target_fps:.0f} fps at {res[0]}x{res[1]} on {workers} CPU threads, CPU power "
        + (f"{cpu_watts:g} W at full speed." if by_watts else
           f"not measured ({'MSI Afterburner not running' if cpu_watts else 'off'}): CPU work {cpu_share:.0%} of "
           "the frame.") + " Leave the laptop alone for about 3 minutes.", flush=True)
    units = 2.0
    for _ in range(6):                                          # starting point: cpu_share of the frame
        ms = measure_sim(workers, units)
        new = next_units(units, ms, cpu_share * frame_ms)
        if abs(new - units) <= units * 0.03:
            break
        units = new
    cfg = {"version": CALIB_VERSION, "target_fps": target_fps, "cpu_watts_target": cpu_watts if by_watts else None,
           "cpu_share": cpu_share, "workers": workers, "cpu_units": units, "gpu_iters": 256, "res": list(res),
           "adapter_hint": hint, "latency": latency}
    game = Game(cfg, windowed, title="synthgame - calibrating")
    try:
        say(f"  GPU: {game.rnd.adapter} ({game.rnd.present_mode} presentation)")
        game.drive(3, latency)                                  # wake the GPU up
        fit_gpu(game, target_fps, latency, say)
        if by_watts:
            points = []
            for _ in range(6):
                st = game.measure(probe_s, latency, ab)
                u_max = max_units_for_floor(units, st.get("sim_ms"), st.get("cpu_mhz"), frame_ms, floor_mhz)
                say(f"  CPU: {units:g} units -> {st['cpu_watts']} W at {st['cpu_mhz']} MHz, CPU work "
                    f"{st['sim_ms']} ms per frame (most that fits at {floor_mhz} MHz: {u_max:g} units)", flush=True)
                if st["cpu_watts"] is None:
                    break
                points.append((units, st["cpu_watts"]))
                if abs(st["cpu_watts"] - cpu_watts) <= 1.5:
                    break
                new = next_units_for_power(points, cpu_watts, u_max)
                if new == units:
                    say(f"  Can't reach {cpu_watts:g} W without the CPU work outgrowing the frame at {floor_mhz} MHz;"
                        " keeping the most that fits.")
                    break
                units = new
                game.set_units(units)
            fit_gpu(game, target_fps, latency, say, tries=4)
        final = game.measure(12, latency, ab)
        cfg["gpu_iters"], cfg["cpu_units"], cfg["adapter"] = game.rnd.iters, units, game.rnd.adapter
        cfg["present_mode"] = game.rnd.present_mode
    finally:
        game.close()
    cfg["measured"] = {k: final.get(k) for k in ("fps", "low1", "low5", "sim_ms", "gpu_wait_ms", "cpu_watts",
                                                 "cpu_mhz")}
    cfg["when"] = time.strftime("%Y-%m-%d %H:%M:%S")
    atomic_json(CALIB, cfg)
    say(f"Saved {CALIB.name}: GPU {cfg['gpu_iters']} iterations, CPU {units:g} units x {workers} threads -> "
        f"{final.get('fps')} fps (1% low {final.get('low1')}), CPU work {final.get('sim_ms')} ms per frame"
        + (f", CPU {final['cpu_watts']} W at {final['cpu_mhz']} MHz." if final.get("cpu_watts") else "."))
    return cfg


def run(minutes=10.0, latency="reflex", out=None, windowed=False, seconds=None, say=print, cfg=None):
    """Render for a fixed time using the saved calibration, print progress every 30 s, and save the summary
    (FPS, lows, per-half and per-minute numbers) as JSON to `out`. Returns the summary."""
    cfg = cfg or load_calib()
    if not cfg:
        raise SystemExit(("synthgame.json is from an older synthgame" if CALIB.exists() else "Not calibrated yet")
                         + ": run  synthgame.exe calibrate  first.")
    secs = seconds if seconds is not None else minutes * 60
    say(f"synthgame: {secs / 60:.1f} min, {cfg['res'][0]}x{cfg['res'][1]}, GPU {cfg['gpu_iters']} iterations, CPU "
        f"{cfg['cpu_units']} units x {cfg['workers']} threads, latency {latency}. Esc stops.")
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    game = Game(cfg, windowed)
    try:
        def progress(el, fps, sim_ms):
            say(f"  {el / 60:5.1f} min  {fps:6.1f} fps  CPU work {sim_ms:5.2f} ms per frame", flush=True)
            game.win.title(f"synthgame  {el / 60:.1f} / {secs / 60:.1f} min  {fps:.0f} fps")
        stamps, sims, waits, aborted = game.drive(secs, latency, progress)
        adapter, present_mode = game.rnd.adapter, game.rnd.present_mode
    finally:
        game.close()
    res = summarize(stamps, sims, waits, secs)
    res.update({"started": started, "latency": latency, "aborted": aborted, "adapter": adapter,
                "gpu_iters": cfg["gpu_iters"], "cpu_units": cfg["cpu_units"], "workers": cfg["workers"],
                "res": cfg["res"], "present_mode": present_mode})
    atomic_json(out or LAST, res)
    say(f"{'Stopped early' if aborted else 'Done'}: {res.get('fps')} fps (1% low {res.get('low1')}, 5% low "
        f"{res.get('low5')}), second half {res.get('fps_h2')} fps, CPU work {res.get('sim_ms')} ms per frame.")
    return res


def main():
    """Command line: `calibrate` sizes the load for this laptop, `run` renders for a set time and saves the
    result; with neither (double-click) it calibrates if needed and does a 2-minute run."""
    ap = argparse.ArgumentParser(description="Repeatable synthetic game (windowed D3D11 + CPU work per frame)")
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("calibrate", help="size the GPU and CPU work for this laptop (~3 min)")
    c.add_argument("--fps", type=float, default=90.0, help="frame rate to aim for with a cool laptop (default 90)")
    c.add_argument("--cpu-watts", type=float, default=34.0,
                   help="CPU power at full speed to aim for, read from MSI Afterburner (default 34, War Thunder's "
                        "in the logs; 0 = use --cpu-share instead)")
    c.add_argument("--cpu-share", type=float, default=0.3,
                   help="CPU work per frame as a share of the frame at full speed, when not aiming for watts "
                        "(default 0.3)")
    c.add_argument("--floor", type=float, default=2400.0,
                   help="the governor's CPU floor in MHz: the CPU work must still fit in the frame there")
    c.add_argument("--workers", type=int, default=6)
    c.add_argument("--res", default="1920x1080")
    c.add_argument("--adapter", help="part of the GPU's name to render on (default: the NVIDIA one)")
    for p in (c, sub.add_parser("run", help="render for a fixed time and save the result")):
        p.add_argument("--latency", choices=["reflex", "normal"], default="reflex")
        p.add_argument("--windowed", action="store_true", help="a 1280x720 window instead of borderless fullscreen")
    r = sub.choices["run"]
    r.add_argument("--minutes", type=float, default=10.0)
    r.add_argument("--seconds", type=float, help=argparse.SUPPRESS)
    r.add_argument("--out", help="result file (default synthgame_last.json)")
    a = ap.parse_args()
    if a.cmd == "calibrate":
        wd, ht = (int(x) for x in a.res.lower().split("x"))
        calibrate(a.fps, a.cpu_watts, a.cpu_share, a.workers, (wd, ht), a.latency, a.windowed, a.adapter, a.floor)
    elif a.cmd == "run":
        res = run(a.minutes, a.latency, a.out, a.windowed, a.seconds)
        if res.get("aborted"):
            sys.exit(EXIT_ABORTED)
    else:
        if not load_calib():
            calibrate()
        run(minutes=2)


if __name__ == "__main__":
    run_app(main)
