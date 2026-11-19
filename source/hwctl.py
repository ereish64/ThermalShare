"""hwctl: the hardware and Windows plumbing every other module builds on.

  - running commands (sh), small helpers, safe JSON writing, admin checks, a wrapper that keeps the window open
  - GPU: live readings from nvidia-smi (GpuSensor), setting/clearing a GPU clock limit
  - CPU: setting the power plan's "maximum processor frequency", and saving/restoring the original values
  - MSI Afterburner's shared memory (CPU temperature/power/clock), PresentMon (frame timing) and its columns
  - the profile maths: turning measured points into "speed" and picking the lowest limit that keeps up
"""
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from collections import Counter, deque
from pathlib import Path
from statistics import median

FROZEN = getattr(sys, "frozen", False)     # True when running as a PyInstaller exe rather than as a .py


def kit_dir():
    """The folder that data files live in: next to the exe when built, else beside the source (or one level up
    when running from TuneKit\\source next to governor.exe)."""
    if FROZEN:
        return Path(sys.executable).resolve().parent
    here = Path(__file__).resolve().parent
    # Running from TuneKit\source: share the map, restore file and log with the exes one level up.
    if here.name.lower() == "source" and (here.parent / "governor.exe").exists():
        return here.parent
    return here


HERE = kit_dir()
TOOLS = HERE / "tools"                          # where PresentMon.exe lives (downloaded on first use)
# thermalshare.py repoints HERE, TOOLS, MAP_FILE and RESTORE_FILE at its own names before the other modules read them.
MAP_FILE = HERE / "gamegov_map.json"            # the measured clock-limit profile (TuneKit's own tools)
RESTORE_FILE = HERE / "gamegov_restore.json"    # the settings to put back; exists only while limits are applied
# Windows power-plan setting IDs (powercfg): maximum processor frequency in MHz (0 = no limit), and maximum
# processor state in percent (100 = allowed to boost).
PROCFREQMAX = "75b0ae3f-bce0-45a7-8c89-c9611c25e100"
PROCTHROTTLEMAX = "bc5038f7-23e0-4960-96da-33abaf5935ec"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0      # CREATE_NO_WINDOW: don't flash a console for each command
MAHM_SIG = 0x4D41484D                           # "MAHM": marks MSI Afterburner's shared-memory block
PM_RELEASES = "https://api.github.com/repos/GameTechDev/PresentMon/releases/latest"
# nvidia-smi fields we ask for, mapped to our short names.
GPU_FIELDS = {"clocks.gr": "clock", "clocks.max.gr": "max_clock", "power.draw": "power",
              "temperature.gpu": "temp", "utilization.gpu": "util"}
# nvidia-smi renamed this field between driver versions, so both names are tried.
REASON_FIELDS = ("clocks_event_reasons.active", "clocks_throttle_reasons.active")
# Bits of that field: thermal slowdown (0x8 hardware, 0x20 software, 0x40 hardware-thermal) and power cap (0x4).
REASON_THERMAL = 0x8 | 0x20 | 0x40
REASON_POWER = 0x4


def sh(cmd, check=False):
    """Run a command and capture its output. With check=True a non-zero exit raises RuntimeError with the output."""
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", creationflags=NO_WINDOW)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(map(str, cmd))} -> {r.returncode}: {(r.stderr or r.stdout).strip()}")
    return r


def fnum(s):
    """Text to a float, or None if it isn't a number (nvidia-smi prints "[N/A]" for missing values)."""
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return None


def mean(xs):
    """Average of the values that exist (Nones are skipped); None if there are none."""
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def atomic_json(path, data):
    """Write JSON so the file is always complete: write a .tmp file, force it to disk, then swap it into place.
    A crash or power loss leaves either the old file or the new one, never half of one."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def is_admin():
    """True if running with Administrator rights (False on any error, or off Windows)."""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def own_console():
    """True if this program opened its own console window (double-clicked), as opposed to being started from a
    terminal the user already had open. Windows counts the processes attached to the console: just us (or us plus
    the PyInstaller launcher, for an exe) means the window is ours and would vanish when we exit."""
    try:
        import ctypes
        arr = (ctypes.c_uint * 8)()
        return ctypes.windll.kernel32.GetConsoleProcessList(arr, 8) <= (2 if FROZEN else 1)
    except Exception:
        return False


def run_app(fn):
    """Run fn() as the program's main: print errors, exit with a proper code, and in a double-clicked exe wait for
    Enter first so the user can read the last message before the window closes."""
    import traceback
    code = 0
    try:
        fn()
    except SystemExit as e:
        if isinstance(e.code, str):         # SystemExit("message"): show the message and count it as a failure
            print(e.code)
            code = 1
        else:
            code = e.code or 0
    except KeyboardInterrupt:
        code = 130                          # the conventional exit code for Ctrl+C
    except Exception:
        traceback.print_exc()
        code = 1
    if FROZEN and own_console() and sys.stdin and sys.stdin.isatty():   # not when started in the background
        try:
            input("\nPress Enter to close...")
        except EOFError:
            pass
    sys.exit(code)


def ask(prompt, default=None):
    """Prompt for a line of input; an empty answer gives the default. Surrounding quotes (from pasted paths) go."""
    v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip().strip('"')
    return v or default


def require_windows_admin():
    """Exit with a message unless this is Windows with Administrator rights."""
    if os.name != "nt":
        sys.exit("Windows only.")
    if not is_admin():
        sys.exit("Run from an elevated (Administrator) terminal.")


def parse_gpu_line(line, fields):
    """One CSV line from nvidia-smi into {"clock", "power", "temp", "util", ...}. Returns None for a malformed line
    or one with no clock value (so half-written lines at startup are skipped)."""
    parts = [p.strip() for p in line.strip().split(",")]
    if len(parts) != len(fields):
        return None
    d = {}
    for f, p in zip(fields, parts):
        if f in REASON_FIELDS:
            try:
                d["reasons"] = int(p, 16)           # the throttle-reason bitmask is printed as hex, e.g. 0x0000000000000004
            except ValueError:
                d["reasons"] = None
        else:
            d[GPU_FIELDS[f]] = fnum(p)
    return d if d.get("clock") is not None else None


def gpu_query_fields():
    """Find which nvidia-smi fields this driver supports. Tries with each throttle-reason field name, then without
    one, and returns the first list that gives a parseable answer."""
    for extra in REASON_FIELDS + (None,):
        fields = list(GPU_FIELDS) + ([extra] if extra else [])
        r = sh(["nvidia-smi", f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"])
        lines = r.stdout.strip().splitlines()
        if r.returncode == 0 and lines and parse_gpu_line(lines[0], fields):
            return fields
    raise RuntimeError("nvidia-smi query failed. Is the NVIDIA driver installed and nvidia-smi on PATH?")


class GpuSensor(threading.Thread):
    """Background thread that keeps one nvidia-smi process streaming GPU readings (clock, power, temperature,
    utilization, throttle reasons) twice a second into a rolling history the rest of the program reads."""
    BLANK_RESTART = 6      # busy samples in a row with no power reading before nvidia-smi is restarted
    MAX_RESTARTS = 10

    def __init__(self, period_ms=500, fields=None):
        super().__init__(daemon=True)           # daemon: never keeps the program alive on its own
        self.fields = fields or gpu_query_fields()
        self.period_ms = period_ms
        self.hist = deque(maxlen=600)           # about the last 5 minutes at 500 ms
        self.lock = threading.Lock()            # guards hist: this thread writes, others read
        self.proc = None
        self.stopped = False
        self.restarts = 0

    def run(self):
        while not self.stopped:
            # "-lms N" makes nvidia-smi print a new line every N milliseconds until it is killed.
            self.proc = subprocess.Popen(
                ["nvidia-smi", f"--query-gpu={','.join(self.fields)}", "--format=csv,noheader,nounits",
                 "-lms", str(self.period_ms)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, creationflags=NO_WINDOW)
            blank = 0
            for line in self.proc.stdout:
                d = parse_gpu_line(line, self.fields)
                if not d:
                    continue
                d["t"] = time.time()
                with self.lock:
                    self.hist.append(d)
                # On this Optimus laptop an nvidia-smi session sometimes reports power as N/A for its whole life
                # (seen 2026-11-05/06); a fresh session reads it again.
                if d.get("power") is None and (d.get("util") or 0) >= 10:
                    blank += 1
                    if blank >= self.BLANK_RESTART and self.restarts < self.MAX_RESTARTS:
                        self.restarts += 1
                        self.proc.kill()
                        break
                else:
                    blank = 0
            if not self.stopped:
                time.sleep(0.5)

    def window(self, seconds):
        """The readings from the last `seconds` seconds, oldest first."""
        cut = time.time() - seconds
        with self.lock:
            return [d for d in self.hist if d["t"] >= cut]

    def latest(self):
        """The newest reading, or None if nothing has arrived yet."""
        with self.lock:
            return self.hist[-1] if self.hist else None

    def summary(self, seconds):
        """Averages over the last `seconds` seconds, plus whether the GPU reported thermal or power throttling at
        any point in that window (the reason bits are OR-ed together)."""
        w = self.window(seconds)
        out = {k: mean([d.get(k) for d in w]) for k in ("clock", "power", "temp", "util")}
        reasons = 0
        for d in w:
            reasons |= d.get("reasons") or 0
        out["thermal_limited"] = bool(reasons & REASON_THERMAL)
        out["power_limited"] = bool(reasons & REASON_POWER)
        out["max_clock"] = w[-1].get("max_clock") if w else None
        return out

    def stop(self):
        """Stop the loop and kill the nvidia-smi process."""
        self.stopped = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


def parse_supported_clocks(text):
    """The graphics clocks (MHz) listed in `nvidia-smi -q -d SUPPORTED_CLOCKS` output, sorted, without repeats."""
    return sorted({int(x) for x in re.findall(r"Graphics\s*:\s*(\d+)\s*MHz", text)})


def gpu_supported_clocks():
    """Every graphics clock the driver will accept as a limit."""
    return parse_supported_clocks(sh(["nvidia-smi", "-q", "-d", "SUPPORTED_CLOCKS"]).stdout)


def process_names():
    """Running processes as {lowercase image name: image name as Windows reports it}."""
    out = {}
    for line in sh(["tasklist", "/NH", "/FO", "CSV"]).stdout.splitlines():
        if line.startswith('"'):
            name = line.split('","', 1)[0].strip('"')
            out[name.lower()] = name
    return out


def is_fullscreen(win, mon, caption, zoomed, slack=2):
    """Exclusive-fullscreen and borderless game windows cover their whole monitor, taskbar included. A maximized
    normal window keeps its caption and overhangs the monitor by its border; a maximized frameless app (Discord,
    VS Code) is zoomed and stops at the taskbar."""
    exact = all(abs(a - b) <= slack for a, b in zip(win, mon))
    covers = (win[0] <= mon[0] + slack and win[1] <= mon[1] + slack and
              win[2] >= mon[2] - slack and win[3] >= mon[3] - slack)
    return exact or (covers and not caption and not zoomed)


_dpi_set = False


def foreground_app():
    """{"exe", "pid", "fullscreen"} for the foreground window, or None."""
    global _dpi_set
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes as w
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        if not _dpi_set:
            _dpi_set = True
            try:  # physical pixels for both the window and the monitor, whatever the scaling
                u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            except Exception:
                pass
        u.GetForegroundWindow.restype = ctypes.c_void_p
        u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.DWORD)]
        u.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.RECT)]
        u.MonitorFromWindow.argtypes = [ctypes.c_void_p, w.DWORD]
        u.MonitorFromWindow.restype = ctypes.c_void_p
        u.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        u.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u.IsZoomed.argtypes = [ctypes.c_void_p]
        k.OpenProcess.restype = ctypes.c_void_p
        k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, w.DWORD, ctypes.c_wchar_p, ctypes.POINTER(w.DWORD)]
        k.CloseHandle.argtypes = [ctypes.c_void_p]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("dwFlags", w.DWORD)]

        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return None
        pid = w.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        r = w.RECT()
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        if not u.GetWindowRect(hwnd, ctypes.byref(r)) or \
                not u.GetMonitorInfoW(u.MonitorFromWindow(hwnd, 2), ctypes.byref(mi)):
            return None
        m = mi.rcMonitor
        caption = (u.GetWindowLongW(hwnd, -16) & 0x00C00000) == 0x00C00000      # GWL_STYLE, WS_CAPTION
        exe = None
        hp = k.OpenProcess(0x1000, False, pid.value)                           # PROCESS_QUERY_LIMITED_INFORMATION
        if hp:
            try:
                buf, n = ctypes.create_unicode_buffer(1024), w.DWORD(1024)
                if k.QueryFullProcessImageNameW(hp, 0, buf, ctypes.byref(n)):
                    exe = Path(buf.value).name
            finally:
                k.CloseHandle(hp)
        if not exe:  # protected (anti-cheat) processes refuse OpenProcess; tasklist still knows the name
            out = sh(["tasklist", "/NH", "/FO", "CSV", "/FI", f"PID eq {pid.value}"]).stdout.strip()
            if out.startswith('"'):
                exe = out.split('","', 1)[0].strip('"')
        return {"exe": exe, "pid": pid.value,
                "fullscreen": is_fullscreen((r.left, r.top, r.right, r.bottom), (m.left, m.top, m.right, m.bottom),
                                            caption, bool(u.IsZoomed(hwnd)))}
    except Exception:
        return None


def display_refresh_hz():
    """Primary display refresh rate, or None if Windows won't say."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        u, g = ctypes.windll.user32, ctypes.windll.gdi32
        u.GetDC.restype = ctypes.c_void_p
        u.GetDC.argtypes = [ctypes.c_void_p]
        u.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        g.GetDeviceCaps.argtypes = [ctypes.c_void_p, ctypes.c_int]
        hdc = u.GetDC(None)
        if not hdc:
            return None
        try:
            hz = g.GetDeviceCaps(hdc, 116)  # VREFRESH
        finally:
            u.ReleaseDC(None, hdc)
        return hz if hz > 1 else None
    except Exception:
        return None


def gpu_lock(max_mhz, min_mhz=300):
    """Limit the GPU's graphics clock to between min and max MHz (nvidia-smi -lgc). Needs admin."""
    sh(["nvidia-smi", "-lgc", f"{int(min_mhz)},{int(max_mhz)}"], check=True)


def gpu_reset():
    """Remove any GPU clock limit (nvidia-smi -rgc). Harmless if none is set."""
    sh(["nvidia-smi", "-rgc"])


def parse_powercfg_query(text):
    """The plugged-in (AC) value from `powercfg /qh` output, as a number; None if it isn't there. Only the AC
    value is used, since the governor and the load test only run on the charger."""
    m = re.search(r"Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)", text)
    return int(m.group(1), 16) if m else None


def powercfg_get(guid):
    # /qh, not /query: Maximum processor frequency is a hidden setting, and /query prints nothing for it.
    return parse_powercfg_query(sh(["powercfg", "/qh", "SCHEME_CURRENT", "SUB_PROCESSOR", guid]).stdout)


def powercfg_set(guid, value):
    """Set a processor power-plan value (plugged in) and re-activate the plan so it takes effect straight away."""
    sh(["powercfg", "/setacvalueindex", "SCHEME_CURRENT", "SUB_PROCESSOR", guid, str(int(value))], check=True)
    sh(["powercfg", "/setactive", "SCHEME_CURRENT"], check=True)


def cpu_cap(mhz):
    """Limit the CPU to this many MHz (0 removes the limit) through the power plan's maximum processor frequency."""
    powercfg_set(PROCFREQMAX, mhz)


def save_restore():
    """Record the current CPU power-plan values in RESTORE_FILE before anything is changed, so restore_all() can
    put them back later, including after a crash or power loss. An existing file is kept: it holds the true
    originals, and overwriting it with already-limited values would lose them. Also makes sure the CPU is allowed
    to boost (maximum processor state 100%) so the limit tests see the CPU's real speed."""
    if not RESTORE_FILE.exists():
        atomic_json(RESTORE_FILE, {"procfreqmax": powercfg_get(PROCFREQMAX),
                                   "procthrottlemax": powercfg_get(PROCTHROTTLEMAX)})
    if powercfg_get(PROCTHROTTLEMAX) != 100:
        powercfg_set(PROCTHROTTLEMAX, 100)


def restore_all():
    """Undo everything: clear the GPU limit and put the CPU power-plan values back from RESTORE_FILE (or just
    remove the frequency limit if there is no file), then delete the file."""
    gpu_reset()
    if RESTORE_FILE.exists():
        d = json.loads(RESTORE_FILE.read_text(encoding="utf-8"))
        powercfg_set(PROCFREQMAX, d.get("procfreqmax") or 0)       # 0 = no limit, also used if the value is missing
        if d.get("procthrottlemax") is not None:
            powercfg_set(PROCTHROTTLEMAX, d["procthrottlemax"])
        RESTORE_FILE.unlink()
    else:
        powercfg_set(PROCFREQMAX, 0)


def install_close_handler(fn):
    """Call fn() when the console is closed, or on Ctrl+C / logoff / shutdown, so limits are put back even when
    the user closes the window instead of exiting cleanly."""
    if os.name != "nt":
        return
    import ctypes
    HANDLER = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

    def h(evt):
        try:
            fn()
        finally:
            return 0                            # 0 = not handled here, so Windows still runs its default action

    # Keep every callback alive: Windows still calls earlier ones after a later one is installed.
    ref = HANDLER(h)
    install_close_handler.refs = getattr(install_close_handler, "refs", []) + [ref]
    ctypes.windll.kernel32.SetConsoleCtrlHandler(ref, True)


def parse_mahm(buf):
    """Decode MSI Afterburner's shared-memory block into {lowercase sensor name: (value, units)}.
    Layout: a header (signature, version, header size, entry count, entry size), then `count` fixed-size entries.
    Each entry starts with the sensor name (260 bytes), its units (260 bytes) and then more text fields before the
    float value at byte 5*260, which is why entries must be at least 5*260+4 bytes."""
    if len(buf) < 20:
        return {}
    sig, ver, hsize, n, esize = struct.unpack_from("<5I", buf, 0)
    if sig != MAHM_SIG or n == 0 or esize < 5 * 260 + 4:
        return {}
    out = {}
    for i in range(n):
        off = hsize + i * esize
        if off + 5 * 260 + 4 > len(buf):                # truncated buffer: stop rather than read past the end
            break
        name = buf[off:off + 260].split(b"\0", 1)[0].decode("latin-1").strip().lower()
        units = buf[off + 260:off + 520].split(b"\0", 1)[0].decode("latin-1").strip()
        (val,) = struct.unpack_from("<f", buf, off + 5 * 260)
        # Skip unnamed entries, repeated names (first one wins) and the huge sentinel value for "no data".
        if name and name not in out and abs(val) < 3.0e38:
            out[name] = (val, units)
    return out


class Afterburner:
    """Reads CPU (and GPU) sensors that MSI Afterburner publishes in shared memory while it is running. It's how
    the CPU temperature and power are obtained on laptops where Windows doesn't expose them. Returns empty results
    when Afterburner isn't running."""
    NAME = "MAHMSharedMemory"

    def __init__(self):
        self.ok = os.name == "nt"
        if self.ok:
            import ctypes
            from ctypes import wintypes
            k = ctypes.windll.kernel32
            k.OpenFileMappingW.restype = wintypes.HANDLE
            k.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
            k.MapViewOfFile.restype = ctypes.c_void_p
            k.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
            k.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
            k.CloseHandle.argtypes = [wintypes.HANDLE]
            self.k, self.ct = k, ctypes

    def read(self):
        """All sensors right now as {name: (value, units)}; {} if Afterburner isn't running or the block is bad."""
        if not self.ok:
            return {}
        h = self.k.OpenFileMappingW(4, False, self.NAME)       # 4 = FILE_MAP_READ; fails if Afterburner isn't running
        if not h:
            return {}
        try:
            v = self.k.MapViewOfFile(h, 4, 0, 0, 0)            # map the whole block read-only into this process
            if not v:
                return {}
            try:
                hdr = self.ct.string_at(v, 32)
                sig, ver, hsize, n, esize, t, ng, gsize = struct.unpack("<8I", hdr)
                if sig != MAHM_SIG:
                    return {}
                return parse_mahm(self.ct.string_at(v, hsize + n * esize))
            finally:
                self.k.UnmapViewOfFile(v)
        finally:
            self.k.CloseHandle(h)

    def get(self, *names):
        """The value of the first of these sensor names that exists (case-insensitive), or None."""
        d = self.read()
        for n in names:
            if n.lower() in d:
                return d[n.lower()][0]
        return None


# Afterburner's names for each reading, in order of preference (the exact name varies with the version/hardware).
AB_CPU_TEMP = ("CPU temperature", "CPU package temperature")
AB_CPU_POWER = ("CPU power", "CPU package power")
AB_CPU_CLOCK = ("CPU clock",)
AB_GPU_VOLT = ("GPU voltage", "GPU1 voltage")


def pick_presentmon_asset(assets):
    """The console exe (PresentMon-<version>-x64.exe) among a GitHub release's files; error if there isn't one."""
    c = [a for a in assets if re.fullmatch(r"PresentMon-[\d.]+-x64\.exe", a.get("name", ""), re.I)]
    if not c:
        raise RuntimeError("No PresentMon console exe in the latest release. Download it manually into tools/.")
    return c[0]


def ensure_presentmon(path=None):
    """Return the path to PresentMon (the tool that measures each game's frame times). Uses `path` if given, else
    the copy in tools/, else downloads the latest release from GitHub: checking its size and, when GitHub
    provides one, its SHA-256, so a corrupt or tampered download is rejected."""
    if path:
        p = Path(path)
        if not p.exists():
            sys.exit(f"PresentMon not found at {p}")
        return p
    TOOLS.mkdir(exist_ok=True)
    have = sorted(TOOLS.glob("PresentMon*.exe"))
    if have:
        return have[-1]
    req = urllib.request.Request(PM_RELEASES, headers={"User-Agent": "gamegov",
                                                       "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        rel = json.load(r)
    a = pick_presentmon_asset(rel.get("assets", []))
    print(f"Downloading {a['name']} ({a['size'] / 1e6:.1f} MB) from GameTechDev/PresentMon {rel.get('tag_name')}")
    dst = TOOLS / a["name"]
    tmp = dst.with_name(dst.name + ".part")             # download to .part, so a half-finished file is never used
    req = urllib.request.Request(a["browser_download_url"], headers={"User-Agent": "gamegov"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 16):
            f.write(chunk)
    data = tmp.read_bytes()
    if len(data) != a["size"]:
        tmp.unlink()
        raise RuntimeError("PresentMon download size mismatch")
    digest = a.get("digest") or ""
    if digest.startswith("sha256:") and hashlib.sha256(data).hexdigest() != digest[7:].lower():
        tmp.unlink()
        raise RuntimeError("PresentMon download hash mismatch")
    os.replace(tmp, dst)
    return dst


class PMColumns:
    """Works out which CSV column holds what in PresentMon's output. Column names differ between PresentMon
    versions, so each value is looked up by a list of possible names instead of a fixed position."""

    def __init__(self, header):
        cols = [c.strip() for c in header.strip().split(",")]
        idx = {c.lower(): i for i, c in enumerate(cols)}

        def pick(*names):
            """Index of the first of these column names present in the header, or None."""
            return next((idx[n.lower()] for n in names if n.lower() in idx), None)

        self.n = len(cols)
        self.app = pick("Application", "ProcessName")
        self.cpu_busy = pick("MsCPUBusy", "CPUBusy")
        self.cpu_wait = pick("MsCPUWait", "CPUWait")
        self.gpu_busy = pick("MsGPUBusy", "GPUBusy", "MsGPUActive")
        self.frame = pick("MsBetweenAppStart", "FrameTime", "MsBetweenPresents")

    def parse(self, line):
        """One PresentMon data row into {"app", "ft" (frame time ms), "cpu" (CPU busy ms), "gpu" (GPU busy ms)}.
        Returns None for rows that don't fit (wrong column count, or a frame time that is zero or over a second)."""
        p = line.rstrip("\r\n").split(",")
        if len(p) != self.n:
            return None

        def g(i):
            return fnum(p[i]) if i is not None else None

        cb, cw = g(self.cpu_busy), g(self.cpu_wait)
        # Frame time is CPU busy + CPU wait when both exist, otherwise the frame-to-frame time column.
        ft = cb + cw if cb is not None and cw is not None else g(self.frame)
        if not ft or ft <= 0 or ft > 1000:
            return None
        return {"app": p[self.app] if self.app is not None else "", "ft": ft, "cpu": cb, "gpu": g(self.gpu_busy)}


def is_pm_header(line):
    """True for PresentMon's CSV header row (it has an "Application" column), as opposed to a message or data row."""
    return "," in line and re.search(r"(^|,)\s*Application\s*(,|$)", line) is not None


class PresentMonReader(threading.Thread):
    """Runs PresentMon in the background and collects per-frame timings for the game being watched. With a
    process_name it watches just that program (and exits when it does); without one it watches every program
    except the desktop compositor."""

    def __init__(self, exe, process_name=None, session="GameGov"):
        super().__init__(daemon=True)
        # Each tool needs its own ETW session name: --stop_existing_session kills any session with the same
        # name, so two tools sharing one would keep cutting off each other's capture.
        self.session = session
        cmd = [str(exe), "--output_stdout", "--no_console_stats", "--stop_existing_session",
               "--session_name", session, "--v2_metrics", "--no_track_input"]
        if process_name:
            cmd += ["--process_name", process_name, "--terminate_on_proc_exit"]
        else:
            cmd += ["--exclude", "dwm.exe"]
        self.cmd = cmd
        self.frames = deque(maxlen=20000)       # recent frames, each stamped with the time it arrived
        self.msgs = deque(maxlen=20)            # the last few non-data lines PresentMon printed (errors, warnings)
        self.lock = threading.Lock()
        self.proc = None
        self.cols = None                        # column layout, learnt from the header row

    def run(self):
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, errors="replace", bufsize=1, creationflags=NO_WINDOW)
        for line in self.proc.stdout:
            if self.cols is None:               # still waiting for the header; anything before it is a message
                if is_pm_header(line):
                    self.cols = PMColumns(line)
                else:
                    self.msgs.append(line.strip())
                continue
            f = self.cols.parse(line)
            if f:
                f["t"] = time.time()
                with self.lock:
                    self.frames.append(f)
            elif line.strip():
                self.msgs.append(line.strip())

    def alive(self):
        """True while PresentMon is running (or hasn't been started yet)."""
        return self.proc is None or self.proc.poll() is None

    def window(self, seconds, app=None):
        """Frames from the last `seconds` seconds for one app. If no app is named, the app that produced the most
        frames in the window is used (the game, not a background program)."""
        cut = time.time() - seconds
        with self.lock:
            fr = [f for f in self.frames if f["t"] >= cut]
        if app is None and fr:
            app = Counter(f["app"] for f in fr).most_common(1)[0][0]
        return [f for f in fr if f["app"] == app] if app else fr

    def stop(self):
        """Kill PresentMon and also end its Windows tracing session, which would otherwise outlive the process."""
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        sh([self.cmd[0], "--terminate_existing_session", "--session_name", self.session])


def frame_stats(frames, min_frames=10):
    """Summarise a list of frames: median frame time and FPS, the "1% low" and "5% low" FPS (the FPS of the
    slowest 1% / 5% of frames, which show stutter that an average hides), and median CPU/GPU busy times.
    None if there are too few frames to mean anything."""
    if len(frames) < min_frames:
        return None
    ft = sorted(f["ft"] for f in frames)
    cpu = [f["cpu"] for f in frames if f["cpu"] is not None]
    gpu = [f["gpu"] for f in frames if f["gpu"] is not None]
    m = median(ft)
    p99 = ft[int(0.99 * (len(ft) - 1))]          # frame time that 99% of frames beat (long = slow)
    p95 = ft[int(0.95 * (len(ft) - 1))]
    return {"n": len(frames), "ft": m, "fps": 1000.0 / m, "low1": 1000.0 / p99, "low5": 1000.0 / p95,
            "cpu": median(cpu) if len(cpu) >= min_frames else None,
            "gpu": median(gpu) if len(gpu) >= min_frames else None}


def finalize_speed(points, perf_key, inverse):
    """Give every measured point a "speed": its performance relative to the unlimited run (1.0 = full speed).
    Points are sorted by "order" (lowest limit first); the last one, the unlimited run, is the reference.
      - perf_key is the measurement to compare (FPS, throughput, ...). With inverse=True lower is better
        (e.g. frame time), so the ratio is flipped.
      - If a point lacks that measurement, its clock speed relative to the reference clock is used instead.
      - Speeds are then forced to never decrease as the limit rises: measurement noise can make a lower limit look
        faster, and the governor relies on a lower limit never being rated faster.
    Points with no usable speed are dropped."""
    pts = sorted(points, key=lambda p: p["order"])
    top = pts[-1]
    for p in pts:
        a, b = p.get(perf_key), top.get(perf_key)
        if a and b:
            p["speed"] = (b / a) if inverse else (a / b)
        elif p.get("clock") and top.get("clock"):
            p["speed"] = p["clock"] / top["clock"]
        else:
            p["speed"] = None
    known = [p for p in pts if p["speed"]]
    best = 0.0
    for p in known:
        best = max(best, p["speed"])             # running maximum, so each point is at least as fast as the one below
        p["speed"] = round(best, 4)
    return [p for p in pts if p["speed"]]


def pick_index(points, idx_now, busy_now, target):
    """Choose the lowest limit (index into points, lowest first) that should still keep up with the game.
    busy_now is how long the chip spends on each frame at the current point (idx_now), in ms. At another point
    with speed s the same work takes busy_now * s_now / s, so this finds the first (lowest) point where that fits
    within `target`, the time budget per frame in ms (the governor passes a margin below the frame time or the
    other chip's busy time). If none does, the highest limit is returned."""
    s_now = points[idx_now]["speed"]
    for i, p in enumerate(points):
        if busy_now * s_now / p["speed"] <= target:
            return i
    return len(points) - 1


def load_map():
    """The whole saved map file as a dict ({} if it doesn't exist yet)."""
    if not MAP_FILE.exists():
        return {}
    return json.loads(MAP_FILE.read_text(encoding="utf-8"))


def save_map_section(name, section):
    """Replace one named section of the map file and keep the others, writing safely."""
    m = load_map()
    m[name] = section
    atomic_json(MAP_FILE, m)
