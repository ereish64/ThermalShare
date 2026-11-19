"""CPU clock, power and temperature from Windows' own performance counters: no Afterburner, no driver.

Clock: % Processor Performance x the base clock (what Task Manager shows). Power: the Energy Meter counters,
which Windows fills from the CPU's RAPL energy counters (package power, the number ThrottleStop and Afterburner
show). Temperature: ACPI thermal zones, which some laptops fill with a real CPU reading and others leave at a
fixed value; a zone that never moves is not reported."""
import ctypes
import threading
import time
from ctypes import wintypes

# PDH is Windows' Performance Data Helper: the API behind Performance Monitor. Constants from pdh.h / pdhmsg.h.
PDH_FMT_DOUBLE = 0x00000200       # return counter values as a double
PDH_FMT_NOCAP100 = 0x00008000     # don't clamp percentages to 100 (needed: "% Processor Performance" exceeds 100 when boosting)
PDH_MORE_DATA = 0x800007D2        # "the buffer was too small": the first call of the usual size-then-fill pattern


class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    """The C struct PdhGetFormattedCounterValue fills in: a status code, padding, and the value as a double."""
    _fields_ = [("CStatus", wintypes.DWORD), ("pad", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


def _pdh():
    """Load the PDH library and declare each function's argument and return types. ctypes assumes 32-bit ints
    unless told otherwise, which would truncate the 64-bit handles on a 64-bit Windows."""
    pdh = ctypes.windll.pdh
    pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
    pdh.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_size_t,
                                          ctypes.POINTER(ctypes.c_void_p)]
    pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
    pdh.PdhGetFormattedCounterValue.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                                ctypes.POINTER(PDH_FMT_COUNTERVALUE)]
    pdh.PdhExpandWildCardPathW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPWSTR,
                                           ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
    pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
    for f in (pdh.PdhOpenQueryW, pdh.PdhAddEnglishCounterW, pdh.PdhCollectQueryData,
              pdh.PdhGetFormattedCounterValue, pdh.PdhExpandWildCardPathW, pdh.PdhCloseQuery):
        f.restype = wintypes.DWORD
    return pdh


def expand(pdh, path):
    """Instance paths matching a wildcard counter path (English names work on every Windows language)."""
    n = wintypes.DWORD(0)
    # First call with no buffer: Windows reports (in n) how big a buffer it needs.
    r = pdh.PdhExpandWildCardPathW(None, path, None, ctypes.byref(n), 0)
    if r not in (0, PDH_MORE_DATA) or n.value == 0:
        return []
    buf = ctypes.create_unicode_buffer(n.value + 2)           # second call fills it; the result is a list of
                                                              # names separated by NULs, ended by a double NUL
    if pdh.PdhExpandWildCardPathW(None, path, buf, ctypes.byref(n), 0) != 0:
        return []
    return [s for s in ctypes.wstring_at(ctypes.addressof(buf), n.value).split("\0") if s]


def pick_power_instance(paths):
    """The package power counter (RAPL_Package0_PKG on Intel); else any package-level one."""
    low = [(p, p.lower()) for p in paths]
    for want in ("_pkg)", "package0)", "pkg"):
        for p, l in low:
            if want in l:
                return p
    return None


def kelvin_tenths_to_c(v):
    """Windows' thermal-zone counter is in tenths of a kelvin (3000 = 300.0 K); convert to degrees Celsius.
    None for a missing or non-positive reading."""
    return None if v is None or v <= 0 else round(v / 10.0 - 273.15, 1)


class WinCpuSensors:
    """read() -> {"cpu clock": (MHz, "MHz"), "cpu power": (W, "W"), "cpu temperature": (C, "C")}, with keys only
    for readings that exist, like hwctl.Afterburner, so either can stand in for the other."""

    def __init__(self, base_mhz=None):
        self.ok = False                       # stays False (and read() returns {}) if anything below fails
        self.base = base_mhz or cpu_base_mhz()
        self.lock = threading.Lock()          # read() may be called from more than one thread
        self.zone_seen = {}                   # per thermal zone: the distinct (rounded) temperatures seen so far
        try:
            self.pdh = _pdh()
            self.q = ctypes.c_void_p()        # one PDH "query" holds all the counters and is collected together
            if self.pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.q)) != 0:
                return
            # Clock = performance % x frequency; the % can exceed 100 when the CPU boosts above its base clock.
            self.c_perf = self._add(r"\Processor Information(_Total)\% Processor Performance")
            self.c_freq = self._add(r"\Processor Information(_Total)\Processor Frequency")
            pw = pick_power_instance(expand(self.pdh, r"\Energy Meter(*)\Power"))
            self.c_power = self._add(pw) if pw else None
            zones = expand(self.pdh, r"\Thermal Zone Information(*)\High Precision Temperature")
            self.c_zones = [(z, h) for z, h in ((z, self._add(z)) for z in zones) if h]
            self.pdh.PdhCollectQueryData(self.q)        # first collection: rate counters need two to give a value
            self.last = time.time()
            self.ok = True
        except Exception:
            self.ok = False

    def _add(self, path):
        """Add one counter to the query; returns its handle, or None if this PC doesn't have that counter."""
        h = ctypes.c_void_p()
        return h if self.pdh.PdhAddEnglishCounterW(self.q, path, 0, ctypes.byref(h)) == 0 else None

    def _val(self, h, nocap=False):
        """The counter's latest value as a float, or None if there is no counter or no valid reading."""
        if not h:
            return None
        v = PDH_FMT_COUNTERVALUE()
        fmt = PDH_FMT_DOUBLE | (PDH_FMT_NOCAP100 if nocap else 0)
        # CStatus 0 / 1 are PDH's "valid data" / "new data" codes; anything else is not a usable value.
        if self.pdh.PdhGetFormattedCounterValue(h, fmt, None, ctypes.byref(v)) != 0 or v.CStatus not in (0, 1):
            return None
        return v.doubleValue

    def read(self):
        """Current readings as {"cpu clock": (MHz, "MHz"), "cpu power": (W, "W"), "cpu temperature": (C, "C")};
        a key is left out when its sensor doesn't exist or has no trustworthy value."""
        if not self.ok:
            return {}
        with self.lock:
            if time.time() - self.last >= 0.2:            # rate counters need time between collections
                self.pdh.PdhCollectQueryData(self.q)
                self.last = time.time()
            out = {}
            perf = self._val(self.c_perf, nocap=True)
            base = self._val(self.c_freq) or self.base      # base clock, from the counter or the registry
            if perf is not None and base:
                out["cpu clock"] = (round(perf * base / 100.0), "MHz")
            mw = self._val(self.c_power)                    # the Energy Meter counter is in milliwatts
            if mw is not None and mw > 0:
                out["cpu power"] = (round(mw / 1000.0, 2), "W")
            temps = []
            for z, h in self.c_zones:
                t = kelvin_tenths_to_c(self._val(h))
                if t is None:
                    continue
                # Some laptops report a fixed made-up temperature. Only trust a zone once it has shown at least
                # four different values (seen is capped at 4, so it is cheap to keep).
                seen = self.zone_seen.setdefault(z, set())
                if len(seen) < 4:
                    seen.add(round(t))
                if len(seen) >= 4 and 20 < t < 110:       # a zone that has moved: a real sensor, not a constant
                    temps.append(t)
            if temps:
                out["cpu temperature"] = (max(temps), "C")  # the hottest trustworthy zone
            return out

    def get(self, *names):
        """The value of the first of these reading names that exists, or None (same as Afterburner.get)."""
        d = self.read()
        for n in names:
            if n.lower() in d:
                return d[n.lower()][0]
        return None

    def close(self):
        """Release the PDH query. read() returns {} afterwards."""
        if self.ok:
            self.pdh.PdhCloseQuery(self.q)
            self.ok = False


def _cpu_reg(value):
    """A value from the first CPU's registry key (the key Windows fills in from the CPU at boot), or None."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            return winreg.QueryValueEx(k, value)[0]
    except Exception:
        return None


def cpu_base_mhz():
    """Base clock (the registry's ~MHz, the same value Processor Frequency reports)."""
    v = _cpu_reg("~MHz")
    return int(v) if v else None


def cpu_name():
    """The CPU's marketing name, e.g. "Intel(R) Core(TM) i7-9750H CPU @ 2.60GHz", with extra spaces squeezed out."""
    v = _cpu_reg("ProcessorNameString")
    return " ".join(str(v).split()) if v else None


class CpuSensors:
    """Afterburner's CPU readings when it is running and has them; Windows' own for anything missing."""

    KEYS = ((("cpu temperature", "cpu package temperature"), "cpu temperature"),
            (("cpu power", "cpu package power"), "cpu power"), (("cpu clock",), "cpu clock"))

    def __init__(self, ab=None, win=None):
        if ab is None:
            from hwctl import Afterburner      # imported here to avoid a circular import with hwctl
            ab = Afterburner()
        self.ab = ab
        self.win = win if win is not None else WinCpuSensors()     # both can be replaced, which the tests do

    def read(self):
        """Windows' readings first, then Afterburner's on top where it has them (it has the CPU temperature that
        Windows doesn't expose on every laptop). KEYS maps Afterburner's alternate names to our standard keys."""
        d = dict(self.win.read())
        ab = self.ab.read()
        for names, key in self.KEYS:
            v = next((ab[n] for n in names if n in ab), None)
            if v is not None:
                d[key] = v
        for k, v in ab.items():                # keep any other Afterburner readings too, without overwriting
            d.setdefault(k, v)
        return d

    def get(self, *names):
        """The value of the first of these reading names that exists, or None."""
        d = self.read()
        for n in names:
            if n.lower() in d:
                return d[n.lower()][0]
        return None
