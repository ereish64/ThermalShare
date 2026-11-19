"""Run synth.exe as a GPU load from another TuneKit tool (gpu_ocscan, mapper) and read back its result rows."""
import csv
import ctypes
import re
import subprocess
import threading
import time
from collections import deque
from ctypes import wintypes

from hwctl import HERE, NO_WINDOW, sh

SYNTH = HERE / "synth.exe"                # the synthetic GPU benchmark, expected next to the other exes
RESULTS = HERE / "synth_results.csv"      # where synth.exe appends one row per finished run
# Duty cycle used to hold the GPU below full load: the process is frozen for DUTY_OFF seconds, then runs for
# DUTY_ON seconds, repeatedly (so it runs only about 43% of the time, i.e. paused ~57%).
DUTY_ON, DUTY_OFF = 0.03, 0.04


class PROCESSENTRY32W(ctypes.Structure):
    """The Windows PROCESSENTRY32W struct filled in by the process-list calls: just the fields we read are the
    process ID, the parent's ID and the exe name, but the full layout must be declared so sizes match."""
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


def process_tree(root):
    """root plus all descendants (synth.exe is a one-file build: a launcher process and the real one)."""
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snap = k32.CreateToolhelp32Snapshot(2, 0)          # 2 = TH32CS_SNAPPROCESS: a snapshot of every process
    e, kids = PROCESSENTRY32W(dwSize=ctypes.sizeof(PROCESSENTRY32W)), {}
    ok = k32.Process32FirstW(ctypes.c_void_p(snap), ctypes.byref(e))
    while ok:                                          # build {parent pid: [child pids]}
        kids.setdefault(e.th32ParentProcessID, []).append(e.th32ProcessID)
        ok = k32.Process32NextW(ctypes.c_void_p(snap), ctypes.byref(e))
    k32.CloseHandle(ctypes.c_void_p(snap))
    out, todo = [], [root]
    while todo:                                        # walk down from root through children, grandchildren, ...
        p = todo.pop()
        out.append(p)
        todo += kids.get(p, [])
    return out


class Suspender:
    """Freezes and unfreezes a process and its children (NtSuspendProcess / NtResumeProcess). Alternating short
    freezes and runs lowers the average load without changing what the program is doing."""

    def __init__(self):
        self.k32, self.nt = ctypes.windll.kernel32, ctypes.windll.ntdll
        self.k32.OpenProcess.restype = ctypes.c_void_p
        self.handles = {}                  # pid -> process handle with permission to suspend/resume
        self.paused = False

    def track(self, root):
        """Open handles for root and any descendants not seen yet (call regularly: children can appear later)."""
        for pid in process_tree(root):
            if pid not in self.handles:
                h = self.k32.OpenProcess(0x0800, False, pid)   # PROCESS_SUSPEND_RESUME
                if h:
                    self.handles[pid] = h

    def pause(self):
        """Freeze every tracked process."""
        for h in self.handles.values():
            self.nt.NtSuspendProcess(ctypes.c_void_p(h))
        self.paused = True

    def resume(self):
        """Let them run again (does nothing if they aren't frozen, so suspend/resume counts stay balanced)."""
        if self.paused:
            for h in self.handles.values():
                self.nt.NtResumeProcess(ctypes.c_void_p(h))
            self.paused = False

    def close(self):
        """Unfreeze first (never leave a process frozen), then release the handles."""
        self.resume()
        for h in self.handles.values():
            self.k32.CloseHandle(ctypes.c_void_p(h))
        self.handles = {}


def read_rows():
    """All result rows synth.exe has written so far, as dicts keyed by column name ([] if there are none)."""
    if not RESULTS.exists():
        return []
    with open(RESULTS, newline="", encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))


class SynthRunner:
    """Starts synth.exe as a GPU load, watches its output, and collects the result row it writes."""

    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()       # kill() may be called from another thread (e.g. the close handler)

    def kill(self):
        """Force-kill synth.exe and everything it started (/T = whole process tree)."""
        with self.lock:
            p = self.proc
        if p and p.poll() is None:
            sh(["taskkill", "/T", "/F", "/PID", str(p.pid)])

    def run(self, label, preset, loops, warmup, duty, timeout, args=("--runs", "1"), on_render=None, probe=None):
        """on_render() runs once synth starts drawing (GPU awake and busy); probe() every 10 s after that.
        duty: pause synth ~57% of the time (gpu_ocscan's top-of-curve test).
        Returns {"exit", "timeout", "rows" (this run's result rows), "tail" (last output lines), "probes"}."""
        before = len(read_rows())          # rows already in the results file; ours are the ones added after these
        tail = deque(maxlen=40)            # the most recent output lines, for progress and error messages
        rendering = threading.Event()      # set once synth prints that it has started drawing
        probes = []

        def reader(stream):
            """Background thread: read synth's output line by line (a full pipe would block synth)."""
            for line in stream:
                line = line.strip()
                if line:
                    tail.append(line)
                    if not rendering.is_set() and re.match(r"(warm-up:|run \d+/\d+:)", line):
                        rendering.set()

        cmd = [str(SYNTH), "run", label, "--mode", "gpu", "--preset", preset, "--warmup", str(int(warmup)),
               "--loops", str(loops), *args]
        # stdin must be empty: synth ends with "Press Enter to close..." when it thinks it owns its console,
        # which it does when started without a window, and would wait there forever.
        p = subprocess.Popen(cmd, cwd=str(HERE), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, errors="replace", creationflags=NO_WINDOW)
        with self.lock:
            self.proc = p
        threading.Thread(target=reader, args=(p.stdout,), daemon=True).start()
        sus = Suspender() if duty else None
        t0, last_print, last_track, timed_out, shown = time.time(), 0.0, 0.0, False, None
        next_probe = None
        try:
            while p.poll() is None:                    # until synth exits
                if time.time() - t0 > timeout:         # give up on a run that hangs
                    timed_out = True
                    break
                if rendering.is_set() and next_probe is None:      # first moment synth is drawing
                    next_probe = time.time() + 10
                    if on_render:
                        on_render()
                elif probe and next_probe and time.time() >= next_probe:   # then sample every 10 s
                    next_probe += 10
                    probes.append(probe())
                if sus and time.time() - t0 > 3:       # duty-cycle mode: after a 3 s start-up, freeze/run in turns
                    if time.time() - last_track > 2:   # re-scan for child processes every couple of seconds
                        sus.track(p.pid)
                        last_track = time.time()
                    sus.pause()
                    time.sleep(DUTY_OFF)
                    sus.resume()
                    time.sleep(DUTY_ON)
                else:
                    time.sleep(0.25)
                if time.time() - last_print > 10 and tail and tail[-1] != shown:
                    last_print, shown = time.time(), tail[-1]
                    print(f"      {shown[:110]}", flush=True)
        finally:
            if sus:
                sus.close()
            if p.poll() is None:
                self.kill()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            with self.lock:
                self.proc = None
        rows = [r for r in read_rows()[before:] if r.get("label") == label]
        return {"exit": None if timed_out else p.returncode, "timeout": timed_out, "rows": rows,
                "tail": list(tail)[-5:], "probes": probes}


def synth_compare(base, cand):
    """Ask synth.exe to compare two of its saved runs (by label); returns its report text."""
    r =subprocess.run([str(SYNTH), "compare", base, cand], cwd=str(HERE), stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, errors="replace", creationflags=NO_WINDOW, timeout=120)
    return (r.stdout or r.stderr).strip()
