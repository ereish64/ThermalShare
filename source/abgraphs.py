"""abgraphs: the six graphs of the A/B test, as one self-contained web page (ThermalShare_ab_graphs.html).

The page is two columns of three graphs. The left column is the control (governor OFF), the right column is the
same test with the governor ON:

    row 1   clock speed, GPU and CPU on one graph (MHz)
    row 2   temperature, GPU and CPU on one graph (C)
    row 3   FPS, with its average in the title

Each test runs several times per side. A graph shows the AVERAGE of the runs as a line, and the lowest-to-highest run
as a pale band, against time since the game started drawing. Both graphs in a row share the same scales, so the
left and right can be compared by eye.

The data comes from the per-run logs the A/B test writes while it runs (<test>_r<round>_<arm>.csv, one row per
second, written by governor.py). ThermalShare keeps them in a scratch folder that it deletes once this page is made,
so the page is the only thing left. At the bottom it also carries the text report with the statistics. The page needs
no internet and no libraries: the graphs are plain SVG made here, plus a little script for the hover read-out. Only
the standard library is used.
"""
import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean

ARMS = ("observe", "governed")                       # control (governor off) first, then governor on
ARM_TITLE = {"observe": "Governor OFF (control)", "governed": "Governor ON"}

# The three rows. series = (column in the run log, name shown, colour slot). The slots are the first three of the
# categorical palette: the GPU is blue and the CPU orange on both the clock and the temperature graph (a colour
# follows what it measures), and FPS, a different thing, takes the third colour.
ROWS = (
    {"id": "clock", "title": "Clock speed", "unit": "MHz", "dec": 0,
     "series": (("gpu_clock", "GPU", 1), ("cpu_clock", "CPU", 2))},
    {"id": "temp", "title": "Temperature", "unit": "°C", "dec": 0,
     "series": (("gpu_temp", "GPU", 1), ("cpu_temp", "CPU", 2))},
    {"id": "fps", "title": "FPS", "unit": "fps", "dec": 1, "series": (("fps", "FPS", 3),)},
)
KEYS = tuple(k for row in ROWS for k, _, _ in row["series"])    # every log column that is graphed

# Size of each graph in SVG units (the page scales it to the card): plot area inside the margins.
VW, VH = 560, 250
ML, MR, MT, MB = 56, 42, 12, 36                  # room on the right is for the end-of-line name labels
PW, PH = VW - ML - MR, VH - MT - MB
GAP_S = 3                                        # a break of more than this many seconds is drawn as a gap


# ---------------------------------------------------------------- reading the run logs

def _num(s):
    """A log cell as a float, or None for blanks, "-" (no reading) and anything that isn't a finite number."""
    try:
        v = float(str(s).strip())
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def clock_seconds(text):
    """"09:20:03" as seconds since midnight, or None if it isn't a time."""
    try:
        h, m, s = (int(x) for x in str(text).strip().split(":"))
    except ValueError:
        return None
    return h * 3600 + m * 60 + s


def read_run(path):
    """One run's log as a list of row dicts."""
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def run_series(rows, keys=KEYS):
    """One run as {column: {second: value}}, with seconds counted from the first row where the game was drawing
    (the "idle" rows before that are left out, like the report does). Several readings in the same second are
    averaged; a missing reading just leaves that second out."""
    out = {k: defaultdict(list) for k in keys}
    t0 = prev = None
    day = 0                                          # added when the clock passes midnight during a run
    for r in rows:
        if r.get("mode") == "idle":
            continue
        s = clock_seconds(r.get("time"))
        if s is None:
            continue
        if prev is not None and s < prev:
            day += 86400
        prev = s
        t = s + day
        if t0 is None:
            t0 = t
        sec = int(t - t0)
        for k in keys:
            v = _num(r.get(k))
            if v is not None:
                out[k][sec].append(v)
    return {k: {sec: mean(vs) for sec, vs in d.items()} for k, d in out.items()}


def aggregate(runs, key):
    """Combine the runs of one side for one column: for every second, the average, lowest and highest value over
    the runs that have one. Returns {"x": [...], "mean": [...], "lo": [...], "hi": [...]}."""
    secs = sorted({s for r in runs for s in r.get(key, {})})
    out = {"x": secs, "mean": [], "lo": [], "hi": []}
    for s in secs:
        vs = [r[key][s] for r in runs if s in r.get(key, {})]
        out["mean"].append(mean(vs))
        out["lo"].append(min(vs))
        out["hi"].append(max(vs))
    return out


def latest_test(results_path, runs_dir):
    """The id of the newest A/B test: the last row of the results file, else the newest run log's name."""
    try:
        with open(results_path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if rows:
            return rows[-1]["test"]
    except OSError:
        pass
    names = sorted(p.name for p in Path(runs_dir).glob("*_r*_*.csv")) if Path(runs_dir).exists() else []
    return names[-1].split("_r")[0] if names else None


def load_test(test, runs_dir):
    """All of one test's run logs, per side: {arm: [run_series, ...]}. Sides with no logs are empty lists."""
    return {arm: [run_series(read_run(p)) for p in sorted(Path(runs_dir).glob(f"{test}_r*_{arm}.csv"))]
            for arm in ARMS}


# ---------------------------------------------------------------- scales

def nice_ticks(lo, hi, target=5):
    """Round-number tick values covering lo..hi (the first and last are the axis ends), about `target` of them."""
    if hi - lo <= 0:
        lo, hi = lo - 1, hi + 1
    raw = (hi - lo) / max(1, target)
    mag = 10 ** math.floor(math.log10(raw))
    step = 10 * mag
    for m in (1, 2, 5, 10):
        if raw <= m * mag:
            step = m * mag
            break
    first, last = math.floor(lo / step) * step, math.ceil(hi / step) * step
    return [round(first + i * step, 10) for i in range(int(round((last - first) / step)) + 1)]


def time_ticks(seconds):
    """Tick positions in seconds for a time axis of this length: whole minutes, 1 / 2 / 5 / 10 apart, at most ~8."""
    minutes = max(1.0, seconds / 60)
    step = next((m for m in (1, 2, 5, 10, 15, 30) if minutes / m <= 8), 60)
    return [60 * step * i for i in range(int(minutes // step) + 1)]


def row_range(row, data):
    """The y-axis ends for a row: the same for both sides, so left and right compare directly. Covers every
    side's lowest and highest run value, plus a little air."""
    vals = [v for arm in ARMS for k, _, _ in row["series"] for key in ("lo", "hi")
            for v in data["arms"][arm]["agg"][k][key]]
    if not vals:
        return None
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.06 or max(abs(hi) * 0.02, 1.0)
    ticks = nice_ticks(lo - pad, hi + pad)
    return ticks[0], ticks[-1], ticks


def build_data(test, runs_dir):
    """Everything the page shows, worked out once: per side the number of runs and each column's combined series,
    the shared time range, and the averages for the table."""
    loaded = load_test(test, runs_dir)
    arms = {}
    for arm in ARMS:
        agg = {k: aggregate(loaded[arm], k) for k in KEYS}
        arms[arm] = {"runs": len(loaded[arm]), "agg": agg,
                     "avg": {k: (mean(a["mean"]) if a["mean"] else None) for k, a in agg.items()}}
    xmax = max([a["x"][-1] for arm in ARMS for a in arms[arm]["agg"].values() if a["x"]] or [60])
    return {"test": test, "arms": arms, "xmax": max(xmax, 60)}


# ---------------------------------------------------------------- drawing

def _fmt(v, dec=0):
    """A number with thousands separators and `dec` decimals."""
    return f"{v:,.{dec}f}"


def _line(xs, ys, xmap, ymap):
    """SVG path text for a line through the points (xs, ys). A hole in the data (more than GAP_S seconds) ends one
    piece and starts the next, so a gap is drawn as a gap rather than a straight line across it."""
    pieces, start = [], 0
    for i in range(1, len(xs) + 1):
        if i == len(xs) or xs[i] - xs[i - 1] > GAP_S:
            pieces.append("M" + " L".join(f"{xmap(xs[j]):.1f},{ymap(ys[j]):.1f}" for j in range(start, i)))
            start = i
    return " ".join(pieces)


def panel(row, arm, data, y_range):
    """One graph. Returns (svg markup, data for the hover script, summary text for screen readers)."""
    pid = f"p-{arm}-{row['id']}"
    x1 = data["xmax"]
    y0, y1, yticks = y_range if y_range else (0, 1, [0, 1])
    xmap = lambda s: ML + s / x1 * PW                        # noqa: E731  seconds -> x in the drawing
    ymap = lambda v: MT + (y1 - v) / (y1 - y0) * PH          # noqa: E731  value -> y in the drawing
    agg = data["arms"][arm]["agg"]
    runs = data["arms"][arm]["runs"]
    out = [f'<svg id="{pid}" viewBox="0 0 {VW} {VH}" role="img" aria-label="__LABEL__">']
    for t in yticks:                                         # gridlines and y labels
        y = ymap(t)
        out.append(f'<line class="grid" x1="{ML}" x2="{ML + PW}" y1="{y:.1f}" y2="{y:.1f}"/>')
        out.append(f'<text class="tick" x="{ML - 8}" y="{y + 4:.1f}" text-anchor="end">{_fmt(t, 0 if t == int(t) else 1)}</text>')
    out.append(f'<line class="axis" x1="{ML}" x2="{ML + PW}" y1="{MT + PH}" y2="{MT + PH}"/>')
    for t in time_ticks(x1):                                 # x labels: minutes into the run
        out.append(f'<text class="tick" x="{xmap(t):.1f}" y="{MT + PH + 16}" text-anchor="middle">{int(t // 60)}</text>')
    out.append(f'<text class="tick" x="{ML + PW / 2}" y="{VH - 4}" text-anchor="middle">minutes into the run</text>')

    xs = sorted({s for k, _, _ in row["series"] for s in agg[k]["x"]})     # every second any series has
    pdata = {"id": pid, "xs": xs, "series": [], "x0": 0, "x1": x1, "y0": y0, "y1": y1,
             "ml": ML, "pw": PW, "mt": MT, "ph": PH, "unit": row["unit"], "dec": row["dec"], "runs": runs}
    if runs == 0:
        out.append(f'<text class="note" x="{ML + PW / 2}" y="{MT + PH / 2}" text-anchor="middle">'
                   'No run was recorded on this side</text>')
    summary = []
    labels = []
    for k, name, slot in row["series"]:
        a = agg[k]
        if not a["x"]:
            continue
        band = _band(a, xmap, ymap) if runs > 1 else ""      # lowest..highest run, only meaningful with 2+ runs
        if band:
            out.append(f'<path class="band s{slot}" d="{band}"/>')
        out.append(f'<path class="line s{slot}" d="{_line(a["x"], a["mean"], xmap, ymap)}"/>')
        last = a["mean"][-1]
        labels.append((ymap(last), name, slot))
        by_x = dict(zip(a["x"], range(len(a["x"]))))
        pdata["series"].append({
            "name": name, "slot": slot,
            "v": [round(a["mean"][by_x[s]], 2) if s in by_x else None for s in xs],
            "lo": [round(a["lo"][by_x[s]], 2) if s in by_x else None for s in xs],
            "hi": [round(a["hi"][by_x[s]], 2) if s in by_x else None for s in xs]})
        summary.append(f"{name} {_fmt(mean(a['mean']), row['dec'])} {row['unit']} average")
    if row["id"] == "fps" and agg["fps"]["mean"]:            # the average, as a thin reference line
        avg = mean(agg["fps"]["mean"])
        out.append(f'<line class="avgline" x1="{ML}" x2="{ML + PW}" y1="{ymap(avg):.1f}" y2="{ymap(avg):.1f}"/>')
    prev = -99.0                                             # end-of-line names, skipping any that would collide
    for y, name, slot in sorted(labels):
        if y - prev >= 14 and len(row["series"]) > 1:
            out.append(f'<text class="endlabel" x="{ML + PW + 6}" y="{y + 4:.1f}">{name}</text>')
            prev = y
    out.append(f'<line class="cross" x1="0" x2="0" y1="{MT}" y2="{MT + PH}" style="display:none"/>')
    out.append(f'<rect class="hit" x="{ML}" y="{MT}" width="{PW}" height="{PH}" fill="transparent"/>')
    out.append("</svg>")
    svg = "".join(out).replace("__LABEL__", f"{row['title']}, {ARM_TITLE[arm].lower()}. " + "; ".join(summary))
    return svg, pdata


def _band(a, xmap, ymap):
    """Path text for the shaded lowest-to-highest band of one series (one closed shape per unbroken stretch)."""
    xs, lo, hi = a["x"], a["lo"], a["hi"]
    shapes, start = [], 0
    for i in range(1, len(xs) + 1):
        if i == len(xs) or xs[i] - xs[i - 1] > GAP_S:
            seg = list(range(start, i))
            if len(seg) > 1:
                top = [f"{xmap(xs[j]):.1f},{ymap(hi[j]):.1f}" for j in seg]
                bottom = [f"{xmap(xs[j]):.1f},{ymap(lo[j]):.1f}" for j in reversed(seg)]
                shapes.append("M" + " L".join(top + bottom) + "Z")
            start = i
    return " ".join(shapes)


# ---------------------------------------------------------------- the page

CSS = """
.viz-root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink-2:#52514e;--muted:#898781;
--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);--series-1:#2a78d6;--series-2:#eb6834;--series-3:#1baf7a;
--good:#006300}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;
--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink-2:#c3c2b7;--grid:#2c2c2a;--axis:#383835;
--border:rgba(255,255,255,.10);--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--good:#0ca30c}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink-2:#c3c2b7;
--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--series-1:#3987e5;--series-2:#d95926;
--series-3:#199e70;--good:#0ca30c}
body{margin:0;background:var(--page)}
.viz-root{background:var(--page);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;
padding:24px 16px 40px;min-height:100vh;box-sizing:border-box}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px;font-weight:600}
.sub{margin:0 0 20px;color:var(--ink-2);max-width:78ch}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.colhead{margin:0;font-size:15px;font-weight:600;display:flex;gap:8px;align-items:baseline}
.colhead span{font-weight:400;color:var(--ink-2);font-size:13px}
.card{margin:0;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 14px 8px;
min-width:0}
.card figcaption{display:flex;flex-wrap:wrap;gap:4px 14px;align-items:baseline;margin-bottom:4px}
.card .t{font-weight:600}
.card .u{color:var(--ink-2);font-size:13px}
.card .d{color:var(--ink-2);font-size:13px}
.card .d b{color:var(--ink);font-weight:600}
.card .d i{font-style:normal}
.legend{display:flex;gap:12px;margin-left:auto;font-size:13px;color:var(--ink-2)}
.legend span{display:inline-flex;align-items:center;gap:6px}
.legend .key{width:14px;height:0;border-top:2px solid var(--c);border-radius:1px}
.legend .none{color:var(--muted)}
svg{display:block;width:100%;height:auto;overflow:visible}
svg .grid{stroke:var(--grid);stroke-width:1}
svg .axis{stroke:var(--axis);stroke-width:1}
svg .tick{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
svg .endlabel{fill:var(--ink-2);font-size:11px}
svg .note{fill:var(--muted);font-size:12px}
svg .avgline{stroke:var(--muted);stroke-width:1}
svg .cross{stroke:var(--muted);stroke-width:1}
svg .line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
svg .band{stroke:none;opacity:.12}
svg .dot{stroke:var(--surface);stroke-width:2;pointer-events:none}
.s1{--c:var(--series-1);stroke:var(--c);fill:var(--c)}
.s2{--c:var(--series-2);stroke:var(--c);fill:var(--c)}
.s3{--c:var(--series-3);stroke:var(--c);fill:var(--c)}
svg .line.s1,svg .line.s2,svg .line.s3{fill:none}
svg .band.s1,svg .band.s2,svg .band.s3{stroke:none}
svg .dot.s1,svg .dot.s2,svg .dot.s3{stroke:var(--surface)}
svg .hit{cursor:crosshair;outline:none}
svg .hit:focus-visible{stroke:var(--ink-2);stroke-width:1}
.foot{margin:18px 0 0;color:var(--ink-2);font-size:13px;max-width:78ch}
details{margin-top:18px;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 14px}
summary{cursor:pointer;font-weight:600}
table{border-collapse:collapse;margin-top:10px;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:6px 10px;text-align:right;border-bottom:1px solid var(--grid)}
th:first-child,td:first-child{text-align:left}
pre{margin:10px 0 0;overflow:auto;font:12px/1.5 ui-monospace,Consolas,"Cascadia Mono",monospace;color:var(--ink)}
th{color:var(--ink-2);font-weight:500;font-size:13px}
#tip{position:fixed;display:none;z-index:10;pointer-events:none;background:var(--surface);color:var(--ink);
border:1px solid var(--border);border-radius:8px;padding:8px 10px;box-shadow:0 4px 14px rgba(0,0,0,.18);
font-size:13px;min-width:150px}
#tip .th{color:var(--ink-2);margin-bottom:4px}
#tip .tr{display:flex;align-items:center;gap:8px;line-height:1.6}
#tip .key{width:12px;height:0;border-top:2px solid var(--c)}
#tip .tv{font-weight:600}
#tip .tn{color:var(--ink-2)}
#tip .tr small{color:var(--muted);margin-left:auto;padding-left:10px}
@media (max-width:760px){.grid2{grid-template-columns:1fr}.legend{margin-left:0}}
@media (forced-colors:active){svg .line{stroke:CanvasText}svg .band{opacity:.25}}
"""

SCRIPT = r"""
(function () {
  var data = JSON.parse(document.getElementById('graph-data').textContent);
  var tip = document.getElementById('tip');
  var NS = 'http://www.w3.org/2000/svg';
  function fmt(v, dec) {
    return v.toLocaleString(undefined, {minimumFractionDigits: dec, maximumFractionDigits: dec});
  }
  function mmss(s) { return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2); }
  function nearest(xs, x) {
    var lo = 0, hi = xs.length - 1;
    while (hi - lo > 1) { var mid = (lo + hi) >> 1; if (xs[mid] <= x) lo = mid; else hi = mid; }
    return Math.abs(xs[lo] - x) <= Math.abs(xs[hi] - x) ? lo : hi;
  }
  data.panels.forEach(function (p) {
    if (!p.xs.length) return;
    var svg = document.getElementById(p.id);
    var hit = svg.querySelector('.hit'), cross = svg.querySelector('.cross');
    var dots = p.series.map(function (s) {
      var c = document.createElementNS(NS, 'circle');
      c.setAttribute('r', 4); c.setAttribute('class', 'dot s' + s.slot); c.style.display = 'none';
      svg.appendChild(c);
      return c;
    });
    var cur = Math.floor(p.xs.length / 2);
    function px(sec) { return p.ml + sec / p.x1 * p.pw; }
    function py(v) { return p.mt + (p.y1 - v) / (p.y1 - p.y0) * p.ph; }
    function hide() {
      tip.style.display = 'none'; cross.style.display = 'none';
      dots.forEach(function (d) { d.style.display = 'none'; });
    }
    function show(i, cx, cy) {
      cur = i;
      var x = px(p.xs[i]);
      cross.setAttribute('x1', x); cross.setAttribute('x2', x); cross.style.display = '';
      tip.textContent = '';
      var head = document.createElement('div');
      head.className = 'th'; head.textContent = mmss(p.xs[i]) + ' into the run';
      tip.appendChild(head);
      p.series.forEach(function (s, k) {
        var v = s.v[i];
        if (v === null) { dots[k].style.display = 'none'; return; }
        dots[k].setAttribute('cx', x); dots[k].setAttribute('cy', py(v)); dots[k].style.display = '';
        var row = document.createElement('div'); row.className = 'tr s' + s.slot;
        var key = document.createElement('span'); key.className = 'key';
        var val = document.createElement('span'); val.className = 'tv';
        val.textContent = fmt(v, p.dec) + ' ' + p.unit;
        var nm = document.createElement('span'); nm.className = 'tn'; nm.textContent = s.name;
        row.appendChild(key); row.appendChild(val); row.appendChild(nm);
        if (p.runs > 1 && s.lo[i] !== null) {
          var rg = document.createElement('small');
          rg.textContent = fmt(s.lo[i], p.dec) + ' to ' + fmt(s.hi[i], p.dec);
          row.appendChild(rg);
        }
        tip.appendChild(row);
      });
      tip.style.display = 'block';
      var w = tip.offsetWidth, h = tip.offsetHeight;
      tip.style.left = Math.max(8, Math.min(cx + 14, window.innerWidth - w - 8)) + 'px';
      tip.style.top = Math.max(8, Math.min(cy - h - 10, window.innerHeight - h - 8)) + 'px';
    }
    function at(e) {
      var r = svg.getBoundingClientRect();
      var sec = ((e.clientX - r.left) / r.width * data.vw - p.ml) / p.pw * p.x1;
      show(nearest(p.xs, sec), e.clientX, e.clientY);
    }
    hit.addEventListener('pointermove', at);
    hit.addEventListener('pointerdown', at);
    hit.addEventListener('pointerleave', hide);
    hit.setAttribute('tabindex', '0');
    function kbd() {
      var r = svg.getBoundingClientRect();
      show(cur, r.left + px(p.xs[cur]) / data.vw * r.width, r.top + 40);
    }
    hit.addEventListener('focus', kbd);
    hit.addEventListener('blur', hide);
    hit.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowRight') { cur = Math.min(p.xs.length - 1, cur + 1); }
      else if (e.key === 'ArrowLeft') { cur = Math.max(0, cur - 1); }
      else { return; }
      e.preventDefault(); kbd();
    });
  });
})();
"""


def _esc(s):
    """Text for HTML (the only free text on the page is the test id, which comes from file names)."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _change(on, off, unit, dec, pct):
    """The ON-minus-OFF difference as table text, e.g. "-120 MHz (-3.1%)", or "-" if either is missing."""
    if on is None or off is None:
        return "-"
    d = on - off
    text = f"{d:+,.{dec}f} {unit}".replace("+-", "-")
    return text + (f" ({100 * d / off:+.1f}%)" if pct and off else "")


def render_page(data, report_text=None):
    """The whole page as HTML text, from build_data()'s result. report_text (synthab's text report, with the
    95% confidence intervals and verdicts) goes in a "Statistics" section at the bottom."""
    arms = data["arms"]
    ranges = {row["id"]: row_range(row, data) for row in ROWS}
    panels, cards = [], {}
    for row in ROWS:
        for arm in ARMS:
            svg, pdata = panel(row, arm, data, ranges[row["id"]])
            panels.append(pdata)
            cards[(row["id"], arm)] = svg

    def legend(row):
        """Line keys for a row's series (none for a single series: the title says what it is)."""
        if len(row["series"]) < 2:
            return ""
        parts = []
        for k, name, slot in row["series"]:
            has = any(arms[a]["agg"][k]["x"] for a in ARMS)
            parts.append(f'<span class="s{slot}"><i class="key"></i>{name}</span>' if has else
                         f'<span class="none">{name}: no reading on this PC</span>')
        return f'<div class="legend">{"".join(parts)}</div>'

    html = []
    for arm in ARMS:                                         # the two column headings share the first grid row
        n = arms[arm]["runs"]
        html.append(f'<h2 class="colhead">{ARM_TITLE[arm]}<span>{n} run{"" if n == 1 else "s"}</span></h2>')
    for row in ROWS:
        for arm in ARMS:
            title = f'<span class="t">{row["title"]}</span><span class="u">{row["unit"]}</span>'
            if row["id"] == "fps":                           # FPS card: the average is part of the title
                avg = arms[arm]["avg"]["fps"]
                if avg is None:
                    extra = ""
                else:
                    extra = f'<span class="d">average <b>{avg:,.1f}</b>'
                    base = arms["observe"]["avg"]["fps"]
                    if arm == "governed" and base:
                        extra += f' <i>({avg - base:+,.1f}, {100 * (avg - base) / base:+.1f}% vs control)</i>'
                    extra += "</span>"
                title += extra
            html.append(f'<figure class="card"><figcaption>{title}{legend(row)}</figcaption>'
                        f'{cards[(row["id"], arm)]}</figure>')

    table = ["<table><thead><tr><th>Average over the run</th><th>Governor OFF</th><th>Governor ON</th>"
             "<th>Change</th></tr></thead><tbody>"]
    for row in ROWS:
        for k, name, _ in row["series"]:
            off, on = arms["observe"]["avg"][k], arms["governed"]["avg"][k]
            label = {"fps": "FPS"}.get(k, f"{name} {row['title'].lower()}")
            cell = lambda v: "no reading" if v is None else f"{v:,.{row['dec']}f}"   # noqa: E731
            table.append(f"<tr><td>{label} ({row['unit']})</td><td>{cell(off)}</td><td>{cell(on)}</td>"
                         f"<td>{_change(on, off, row['unit'], row['dec'], row['id'] != 'temp')}</td></tr>")
    table.append("</tbody></table>")

    runs_text = " and ".join(f'{arms[a]["runs"]} governor-{"off" if a == "observe" else "on"}' for a in ARMS)
    no_cpu_temp = not any(arms[a]["agg"]["cpu_temp"]["x"] for a in ARMS)
    notes = ["Both graphs in a row use the same scale, so the sides compare directly.",
             "The line is the average of the runs; the pale band runs from the lowest to the highest run."]
    if no_cpu_temp:
        notes.append("Windows reports no CPU temperature on this PC, so that line is missing "
                     "(MSI Afterburner, if running, supplies it).")
    stats = ""
    if report_text:
        notes.append("The Statistics section gives a 95% confidence interval and a verdict for each difference.")
        stats = f"<details><summary>Statistics</summary><pre>{_esc(report_text)}</pre></details>"
    payload = json.dumps({"vw": VW, "panels": panels}, separators=(",", ":")).replace("</", "<\\/")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>ThermalShare A/B Graphs</title><style>{CSS}</style></head><body>"
        '<main class="viz-root"><div class="wrap">'
        f'<h1>ThermalShare A/B test</h1><p class="sub">Test {_esc(data["test"])}: {runs_text} runs of the same '
        "synthetic game. Left: governor off (the control). Right: governor on.</p>"
        f'<div class="grid2">{"".join(html)}</div>'
        f'<details><summary>Table view</summary>{"".join(table)}</details>{stats}'
        f'<p class="foot">{" ".join(notes)}</p></div><div id="tip" role="tooltip"></div></main>'
        f'<script type="application/json" id="graph-data">{payload}</script><script>{SCRIPT}</script>'
        "</body></html>")


def write_graphs(test=None, results_path=None, runs_dir=None, out_path=None, report_text=None):
    """Make the page for one test (the newest if test is None) and save it to out_path. Returns the path, or None
    when there are no run logs to draw."""
    runs_dir = Path(runs_dir)
    test = test or latest_test(results_path, runs_dir)
    if not test:
        return None
    data = build_data(test, runs_dir)
    if not any(data["arms"][a]["runs"] for a in ARMS):
        return None
    out = Path(out_path)
    out.write_text(render_page(data, report_text), encoding="utf-8")
    return out


if __name__ == "__main__":
    # Handy for trying it on old logs:  py abgraphs.py <runs folder> <test id> <output .html>
    import sys
    print(write_graphs(sys.argv[2], None, sys.argv[1], sys.argv[3]))
