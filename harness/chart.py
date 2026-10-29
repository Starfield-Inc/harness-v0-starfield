"""Build one horizontal bar chart (SVG, PNG, alt text) from a spec file and a CSV."""
import argparse
import csv
import io
import json
import math
from pathlib import Path

REQUIRED_KEYS = (
    "id", "title", "data", "date_col", "date_text", "value_col", "value_format",
    "axis_label", "label_col", "bars", "note", "links",
)
MEASURED_LABEL = "measured by Starfield"
PUBLISHED_PREFIX = "published by "

W, DEFAULT_H, DPI = 1200, 675, 150
MARGIN = 40
SURFACE, INK, SECONDARY, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e1"
COLORS = {"published": "#2a78d6", "measured": "#eb6834"}
LEGEND_TEXT = {"published": "published by a provider", "measured": MEASURED_LABEL}
MAX_BAR_PX = 22
MAX_SLOT_PX = 104
FOOTER_LINE_PX = 21


class ChartError(ValueError):
    pass


def load_spec(path):
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in REQUIRED_KEYS:
        if key not in spec:
            raise ChartError(f"spec is missing the key '{key}'")
    if not spec["bars"]:
        raise ChartError("spec 'bars' is empty")
    if "height" in spec and not (isinstance(spec["height"], int) and 500 <= spec["height"] <= 1200):
        raise ChartError("spec 'height' must be a whole number of pixels from 500 to 1200")
    for bar in spec["bars"]:
        for key in ("provider", "model", "name"):
            if key not in bar:
                raise ChartError(f"a bar in the spec is missing the key '{key}'")
    return spec


def read_rows(spec, base_dir):
    path = Path(base_dir) / spec["data"]
    try:
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            header = reader.fieldnames or []
    except FileNotFoundError:
        raise ChartError(f"data file not found: {path}") from None
    needed = ["provider", "model", spec["date_col"], spec["value_col"], spec["label_col"]]
    if spec.get("error_col"):
        needed.append(spec["error_col"])
    for column in needed:
        if column not in header:
            raise ChartError(f"column '{column}' is not in {path}")
    return rows


def check_one_date(spec, rows):
    dates = sorted({r[spec["date_col"]][:10] for r in rows})
    if len(dates) != 1:
        raise ChartError(
            f"the data must have one date, found {len(dates)}: {', '.join(dates) or 'none'}")


def to_number(text, column, name):
    try:
        number = float(text)
    except ValueError:
        number = math.nan
    if not math.isfinite(number):
        raise ChartError(f"column '{column}' is not a number for '{name}': '{text}'")
    return number


def label_kind(label, name):
    if label == MEASURED_LABEL:
        return "measured"
    if label.startswith(PUBLISHED_PREFIX) and label[len(PUBLISHED_PREFIX):].strip():
        return "published"
    raise ChartError(
        f"label for '{name}' must be 'published by <source>' or '{MEASURED_LABEL}', got '{label}'")


def load_bars(spec, base_dir):
    rows = read_rows(spec, base_dir)
    check_one_date(spec, rows)
    bars = []
    for want in spec["bars"]:
        name = want["name"]
        found = [r for r in rows if (r["provider"], r["model"]) == (want["provider"], want["model"])]
        if len(found) != 1:
            raise ChartError(
                f"bar '{name}' (provider '{want['provider']}', model '{want['model']}') "
                f"matches {len(found)} rows, expected 1")
        row = found[0]
        label = row[spec["label_col"]].strip()
        if not label:
            raise ChartError(f"column '{spec['label_col']}' is empty for '{name}'")
        error = None
        if spec.get("error_col") and row[spec["error_col"]].strip():
            error = to_number(row[spec["error_col"]], spec["error_col"], name)
        bars.append({
            "name": name,
            "value": to_number(row[spec["value_col"]], spec["value_col"], name),
            "label": label,
            "kind": label_kind(label, name),
            "error": error,
        })
    return bars


def alt_text(spec, bars):
    lines = [spec["title"]]
    for bar in bars:
        value = spec["value_format"].format(bar["value"])
        if bar["error"] is not None:
            value += f" (plus or minus {spec['value_format'].format(bar['error'])})"
        lines.append(f"{bar['name']}: {value} ({spec['axis_label']}), {bar['label']}.")
    lines.append(spec["date_text"])
    return "\n".join(lines) + "\n"


def px_to_pt(px):
    return px * 72 / DPI


def text_width(fig, text, **style):
    probe = fig.text(0, 0, text, **style)
    width = probe.get_window_extent(fig.canvas.get_renderer()).width
    probe.remove()
    return width


def wrap(fig, text, width, **style):
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}" if line else word
        if line and text_width(fig, trial, **style) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    return lines + [line]


def put(fig, x, y_from_top, text, **style):
    return fig.text(x / W, 1 - y_from_top / (fig.get_figheight() * DPI), text, **style)


def bar_path(x_end, y0, y1, rx, ry):
    from matplotlib.path import Path as MplPath
    codes = [MplPath.MOVETO, MplPath.LINETO, MplPath.CURVE3, MplPath.CURVE3, MplPath.LINETO,
             MplPath.CURVE3, MplPath.CURVE3, MplPath.LINETO, MplPath.CLOSEPOLY]
    points = [(0, y0), (x_end - rx, y0), (x_end, y0), (x_end, y0 + ry), (x_end, y1 - ry),
              (x_end, y1), (x_end - rx, y1), (0, y1), (0, y0)]
    return MplPath(points, codes)


def render(spec, bars):
    import matplotlib
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.patches import PathPatch, Rectangle
    from matplotlib.ticker import FuncFormatter, MaxNLocator

    rc = {"svg.fonttype": "none", "svg.hashsalt": "starfield", "font.family": "DejaVu Sans",
          "text.parse_math": False}
    H = spec.get("height", DEFAULT_H)
    with matplotlib.rc_context(rc):
        fig = Figure(figsize=(W / DPI, H / DPI), dpi=DPI, facecolor=SURFACE)
        FigureCanvasAgg(fig)
        name_style = {"fontsize": 11.5, "color": INK}
        label_style = {"fontsize": 9.5, "color": SECONDARY}
        footer_style = {"fontsize": 8, "color": SECONDARY}
        title_style = {"fontsize": 15, "color": INK, "fontweight": "bold"}
        full_width = W - 2 * MARGIN

        top = 28
        for line in wrap(fig, spec["title"], full_width * 0.85, **title_style):
            put(fig, MARGIN, top, line, va="top", ha="left", **title_style)
            top += 38

        kinds = [k for k in COLORS if any(b["kind"] == k for b in bars)]
        if len(kinds) > 1:
            x = MARGIN
            for kind in kinds:
                fig.add_artist(Rectangle((x / W, 1 - (top + 18) / H), 12 / W, 12 / H,
                                         facecolor=COLORS[kind], edgecolor="none"))
                put(fig, x + 20, top + 12, LEGEND_TEXT[kind], va="center", ha="left", **label_style)
                x += 20 + text_width(fig, LEGEND_TEXT[kind], **label_style) + 32
            top += 30
        plot_top = top + 14

        footer = [spec["date_text"], spec["note"], spec["links"]]
        footer_lines = [line for text in footer for line in wrap(fig, text, full_width, **footer_style)]
        footer_top = H - 24 - len(footer_lines) * FOOTER_LINE_PX
        for i, line in enumerate(footer_lines):
            put(fig, MARGIN, footer_top + i * FOOTER_LINE_PX, line, va="top", ha="left", **footer_style)

        slot = min((footer_top - 20 - 84 - plot_top) / len(bars), MAX_SLOT_PX)
        plot_h = slot * len(bars)
        thickness = min(MAX_BAR_PX, slot * 0.4)

        texts = [spec["value_format"].format(b["value"]) for b in bars]
        left = MARGIN + max(max(text_width(fig, b["name"], **name_style),
                                text_width(fig, b["label"], **label_style)) for b in bars) + 28
        right = MARGIN + 8 + max(text_width(fig, t, **name_style) for t in texts)
        axes_w = W - left - right

        ax = fig.add_axes([left / W, 1 - (plot_top + plot_h) / H, axes_w / W, plot_h / H],
                          facecolor="none")
        top_value = max(b["value"] + (b["error"] or 0) for b in bars)
        ticks = MaxNLocator(nbins=5, steps=[1, 2, 5, 10]).tick_values(0, top_value)
        x_max = ticks[-1]
        ppu = axes_w / x_max
        ax.set_xlim(0, x_max)
        ax.set_ylim(plot_h, 0)
        ax.set_xticks([t for t in ticks if t <= x_max])
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
        ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.set_axisbelow(True)
        ax.xaxis.grid(True, color=GRID, linewidth=px_to_pt(0.5), linestyle="solid")
        ax.tick_params(axis="x", length=0, labelsize=9, labelcolor=SECONDARY, pad=8)
        ax.set_xlabel(spec["axis_label"], color=SECONDARY, fontsize=9.5, loc="left", labelpad=10)

        for i, (bar, text) in enumerate(zip(bars, texts)):
            yc = (i + 0.5) * slot
            y0, y1 = yc - thickness / 2, yc + thickness / 2
            path = bar_path(bar["value"], y0, y1, min(4 / ppu, bar["value"] / 2), min(4, thickness / 2))
            ax.add_patch(PathPatch(path, facecolor=COLORS[bar["kind"]], edgecolor="none", linewidth=0))
            tip = bar["value"]
            if bar["error"] is not None:
                lo, hi = bar["value"] - bar["error"], bar["value"] + bar["error"]
                line = {"color": INK, "linewidth": px_to_pt(1), "solid_capstyle": "butt", "zorder": 3}
                ax.plot([lo, hi], [yc, yc], **line)
                for x in (lo, hi):
                    ax.plot([x, x], [yc - 5, yc + 5], **line)
                tip = hi
            ax.text(tip + 8 / ppu, yc, text, va="center", ha="left", clip_on=False, **name_style)
            put(fig, MARGIN, plot_top + yc + 1, bar["name"], va="bottom", ha="left", **name_style)
            put(fig, MARGIN, plot_top + yc + 3, bar["label"], va="top", ha="left", **label_style)

        svg, png = io.BytesIO(), io.BytesIO()
        fig.savefig(svg, format="svg", metadata={"Date": None})
        fig.savefig(png, format="png", dpi=DPI)
    return svg.getvalue(), png.getvalue()


def data_base_dir(spec, spec_path):
    parent = Path(spec_path).resolve().parent
    for base in (parent, *parent.parents):
        if (base / spec["data"]).exists():
            return base
    return parent.parent


def build(spec_path, out_dir=None):
    spec = load_spec(spec_path)
    bars = load_bars(spec, data_base_dir(spec, spec_path))
    svg, png = render(spec, bars)
    alt = alt_text(spec, bars).encode("utf-8")
    out = Path(out_dir) if out_dir else Path(spec_path).resolve().parent
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for suffix, content in ((".svg", svg), (".png", png), (".alt.txt", alt)):
        path = out / f"{spec['id']}{suffix}"
        path.write_bytes(content)
        written.append(path)
    return written


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec")
    parser.add_argument("--out-dir")
    args = parser.parse_args()
    try:
        paths = build(args.spec, args.out_dir)
    except ChartError as error:
        raise SystemExit(f"chart: {error}")
    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
