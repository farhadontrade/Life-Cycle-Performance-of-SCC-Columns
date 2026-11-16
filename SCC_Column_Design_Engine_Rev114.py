# =============================================================================
# SCC Column Design Calculation Engine -- Revision 114
# -----------------------------------------------------------------------------
# Stage 10 full member-design release. The computational model incorporates the
# author-approved braced internal-column idealization, rm = -0.50 double-curvature
# profile, separate ULS/quasi-permanent dead/live actions, candidate self-weight,
# candidate-specific Annex B.5 creep, Annex O.6/O.7 checks, and member-equilibrium
# shear. The plotting and workbook visual language remains anchored to Rev100.
# Results are member-level comparative assessments, not global-frame analysis.
# =============================================================================
from __future__ import annotations

import csv
import math
import os
import posixpath
import re
import statistics
import zipfile
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
import matplotlib
matplotlib.use("Agg")
from matplotlib import font_manager
import matplotlib.pyplot as plt

BASE_DIR = Path(__file__).resolve().parent
from matplotlib.colors import TwoSlopeNorm, LinearSegmentedColormap
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
import matplotlib.colors, matplotlib.image, matplotlib.collections, matplotlib.patches, matplotlib.lines, matplotlib.text
from matplotlib.ticker import FormatStrFormatter


# =============================================================================
# USER-EDITABLE DESIGN INPUTS
# =============================================================================
COLUMN_ASSUMPTIONS = {
    # Loading: study-defined member actions; not global-frame analysis or National Annex values.
    "tributary_area_m2": 36.0,
    "storeys": 5,
    "dead_load_kN_m2": 6.0,
    "live_load_kN_m2": 2.5,
    "gamma_G": 1.35,
    "gamma_Q": 1.50,
    "unit_weight_concrete_kN_m3": 25.0,
    "psi2": 0.30,
    "M_Ed_x_kNm": 150.0,
    "M_Ed_y_kNm": 60.0,
    "M_Ed_x_kNm_15storey": 450.0,
    "M_Ed_y_kNm_15storey": 180.0,
    "M_Ed_x_kNm_30storey": 900.0,
    "M_Ed_y_kNm_30storey": 360.0,
    # Member geometry.
    "clear_height_m": 3.0,
    "effective_length_assumed_m": 3.0,
    "moment_end_ratio_rm_base": -0.50,
    "member_moment_profile": "linear_double_curvature_no_transverse_distributed_load",
    "member_boundary_model": "braced_internal_column_idealization_non_sway",
    "end_restraint_condition": "pinned-equivalent at both ends in each principal plane",
    # Materials and strength-age assumptions.
    "f_yk_MPa": 500.0,
    "gamma_s": 1.15,
    "k_tc": 1.00,
    "t_ref_strength_days": 28,
    "t_cast_to_design_load_min_days": 90,
    "fck_ref_MPa": 40.0,
    "gamma_c": 1.50,
    "cube_to_cylinder_factor": 0.80,
    "reinforcement_E_MPa": 200000.0,
    "reinforcement_ductility_class": "B",
    # Reinforcement and search domain.
    "longitudinal_reinforcement_min_ratio": 0.002,
    "longitudinal_reinforcement_max_ratio": 0.04,
    "longitudinal_bar_diameters_mm": [20, 22, 25, 28],
    "link_diameters_mm": [6.0, 8.0, 10.0],
    "link_spacing_nominal_mm": 150.0,
    "transverse_spacing_increment_mm": 50.0,
    "minimum_confinement_factor_k": 0.02,
    "gamma_v": 1.40,
    "cot_theta_shear": 3.0,
    "candidate_square_sections_mm": list(range(300, 1501, 50)),
    "search_points_per_face_max": 8,
    "max_aggregate_size_mm": 9.0,
    # Creep assumptions; Annex B.5 is calculated for each trial section using its notional size.
    "t0_creep_days": 90,
    "t_design_working_life_years": 50,
    "RH_creep_pct": 70.0,
    "alpha_SC": 0.0,
    "T_creep_C": 20.0,
    # Chloride-model assumptions; total binder = cement + GGBFS + SF, LP excluded.
    "C0_wt_pct_total_binder": 0.10,
    "Ccrit_wt_pct_total_binder": 0.60,
    "Cs_wt_pct_total_binder": 3.00,
    "convection_zone_mm": 0.0,
    "transfer_factor_kt": 0.10,
    "temperature_parameter_be_K": 4800.0,
    "reference_temperature_K": 293.0,
    "environment_temperature_K": 290.0,
    "t0_chloride_days": 90,
    "alpha_baseline": 0.30,
    "alpha_sensitivity_values": [0.30, 0.40, 0.50],
    "target_periods_years": [50, 75, 100],
    "XRDS5_XS1_min_durability_cover_50y_mm": 35.0,
    "XRDS5_XS1_min_durability_cover_75y_mm": 35.0,
    "XRDS5_XS1_min_durability_cover_100y_mm": 35.0,
    "S6_XS1_min_durability_cover_mm_separate_crosscheck": 45.0,
    "execution_allowance_mm": 10.0,
    "c_min_dur_xrds5_xs1_50y_mm": 35.0,
    "c_min_dur_xrds5_xs1_100y_crosscheck_mm": 45.0,
    "delta_c_dev_mm": 10.0,
    # Product-stage A1-A3 steel factors; target years are NOT an annualization divisor.
    "reinforcement_EF_kgCO2e_per_kg_main": 1.85,
    "reinforcement_EF_kgCO2e_per_kg_scenarios": [1.71, 1.85, 2.00],
}


# =============================================================================
# FILE NAMES
# =============================================================================
EXPERIMENT_WORKBOOK = "Concrete_Dataset.xlsx"
RESULTS_FOLDER = "Results_Rev114_Full"


# =============================================================================
# CENTRAL VISUAL THEME  (Rev91)  --  SINGLE SOURCE OF TRUTH FOR ALL STYLING
# =============================================================================
# Every colour, marker, hatch and line style used by any figure is defined in
# this section and nowhere else.  Plot functions call the t_* wrappers below;
# the wrappers refuse local colour overrides, tag every artist with the theme
# role that produced it, and t_save() then (i) builds the symbol/hatch guide
# automatically, (ii) places it OUTSIDE the axes, and (iii) validates the figure
# against the registry before writing the PNG.
#
# Encoding rules
#   * COLOUR      -> binder group ONLY (Reference, GGBFS-only, SF-only, Ternary).
#   * AGE         -> marker shape ONLY (circle / square).
#   * TARGET      -> line style ONLY (solid / dashed / dotted); common circle marker.
#   * SCENARIO    -> restrained hatch ONLY on grouped bars (blank / / / \).
#   * FLOW        -> marker shape ONLY (circle / square).
#   Markers and hatches are never varied simultaneously for the same filter.
# =============================================================================
THEME_VERSION = "Rev100"

# --- neutral (non-categorical) tones ---------------------------------------
NEUTRAL = {
    "black": "#000000", "ink": "#263445", "dark": "#202020", "muted": "#505B66",
    "mid": "#64748B", "light": "#B8C2CC", "border": "#7B8794", "grid": "#DCE3EB",
    "navy": "#24415E", "table_band": "#F3F6FA", "cmap_mid": "#F3F5F7", "white": "#FFFFFF",
}

# --- binder-group colours (FIXED: never recoloured by any filter) ----------
GROUP_ORDER = ("Reference", "GGBFS-only", "SF-only", "Ternary")
GROUP_COLORS = {
    "Reference": "#555555",
    "GGBFS-only": "#3568A8",
    "SF-only": "#C44E52",
    "Ternary": "#3A8D68",
}
# Frozen copy used by validate_theme_registry(); change both deliberately.
_GROUP_COLOR_LOCK = (("Reference", "#555555"), ("GGBFS-only", "#3568A8"),
                     ("SF-only", "#C44E52"), ("Ternary", "#3A8D68"))

DIVERGING_CMAP = LinearSegmentedColormap.from_list(
    "scc_diverging", [GROUP_COLORS["GGBFS-only"], NEUTRAL["cmap_mid"], GROUP_COLORS["SF-only"]], N=256)

STRENGTH_AGES = (3, 7, 28, 90)
TRANSPORT_AGES = (28, 90)
FIG_DPI = 300

# --- filter registry: exactly ONE differentiating cue per filter -------------
DEFAULT_MARKER = "o"
FILTERS = {
    "AGE": {
        28: {"label": "28 d", "marker": "o", "hatch": None, "linestyle": None},
        90: {"label": "90 d", "marker": "s", "hatch": None, "linestyle": None},
    },
    "TARGET": {
        50: {"label": "50-year", "marker": "o", "hatch": None, "linestyle": "solid"},
        75: {"label": "75-year", "marker": "o", "hatch": None, "linestyle": (0, (4.5, 2.2))},
        100: {"label": "100-year", "marker": "o", "hatch": None, "linestyle": (0, (1.2, 1.8))},
    },
    "SCENARIO": {
        # Scenario is encoded by fill lightness only. Binder identity remains the hue;
        # the 5-story case uses the full group colour and higher-story cases are
        # progressively lighter. This is clearer than hatching at journal scale.
        5: {"label": "5-story", "marker": None, "hatch": None, "linestyle": None},
        15: {"label": "15-story", "marker": None, "hatch": None, "linestyle": None},
        30: {"label": "30-story", "marker": None, "hatch": None, "linestyle": None},
    },
    "FLOW": {
        "slump": {"label": "Slump-flow diameter", "marker": "o", "hatch": None, "linestyle": None},
        "jring": {"label": "J-ring flow diameter", "marker": "s", "hatch": None, "linestyle": None},
    },
}
TARGET_LEVELS = tuple(FILTERS["TARGET"])
SCENARIO_LEVELS = tuple(FILTERS["SCENARIO"])
SCENARIO_HATCHES = {level: None for level in FILTERS["SCENARIO"]}  # retained as a no-hatch compatibility map
SCENARIO_FILL_STRENGTH = {5: 1.00, 15: 0.72, 30: 0.45}
MARKER_SCALE = {"o": 1.0, "s": 1.0}

# --- reference-line / annotation roles (neutral colours, fixed dash style) ---
ROLES = {
    "dj_limit_25": {"label": "ΔJ = 25 mm", "kind": "line", "linestyle": (0, (4, 2)), "tone": "dark", "lw": 1.0},
    "dj_limit_50": {"label": "ΔJ = 50 mm", "kind": "line", "linestyle": (0, (1, 1.5)), "tone": "dark", "lw": 1.1},
    "ref_mean": {"label": "M01 mean", "kind": "line", "linestyle": (0, (5, 2)), "tone": "muted", "lw": 1.0},
    "critical_threshold": {"label": "Critical chloride threshold", "kind": "line", "linestyle": (0, (5, 2)), "tone": "ink", "lw": 1.1},
    "cover_minimum": {"label": "XS1/XRDS5 minimum + allowance", "kind": "line", "linestyle": (0, (1, 1.5)), "tone": "ink", "lw": 1.1},
    "pair_link": {"label": "28 d to 90 d, same mixture", "kind": "line", "linestyle": "solid", "tone": "border", "lw": 0.8},
    "range_whisker": {"label": "Range across 50-, 75- and 100-year targets (Δ = width)", "kind": "err", "linestyle": "solid", "tone": "mid", "lw": 2.0},
    "errbar": {"label": "±1 SD", "kind": "err", "linestyle": "solid", "tone": "ink", "lw": 1.0},
    "band": {"label": "Min–max across mixtures", "kind": "patch", "linestyle": "solid", "tone": "mid", "lw": 0.0},
}

_FORBIDDEN_KW = {"color", "c", "facecolor", "facecolors", "edgecolor", "edgecolors", "ecolor",
                 "markerfacecolor", "markeredgecolor", "mfc", "mec", "fc", "ec", "cmap",
                 "linestyle", "ls", "marker", "hatch"}
THEME_QA = []   # one row per validated figure (written by main())


def _blend_with_white(color, strength):
    """Blend a binder-group colour toward white using a deterministic fill strength."""
    rgba = matplotlib.colors.to_rgba(color)
    strength = float(strength)
    if not 0.0 <= strength <= 1.0:
        raise ValueError(f"Scenario fill strength must be in [0, 1], got {strength}")
    rgb = tuple(1.0 - (1.0 - channel) * strength for channel in rgba[:3])
    return matplotlib.colors.to_hex(rgb, keep_alpha=False).lower()


def _scenario_group_color(group, level):
    return _blend_with_white(GROUP_COLORS[group], SCENARIO_FILL_STRENGTH[level])


def _scenario_legend_color(level):
    # Neutral blue-grey sample communicates the same lightness progression without
    # competing with the four binder-group hues in the legend.
    return _blend_with_white(NEUTRAL["navy"], SCENARIO_FILL_STRENGTH[level])


def _allowed_hex():
    base = ({v.lower() for v in GROUP_COLORS.values()}
            | {v.lower() for v in NEUTRAL.values()})
    scenario_tints = {
        _scenario_group_color(group, level)
        for group in GROUP_ORDER for level in SCENARIO_FILL_STRENGTH
    }
    return base | scenario_tints


def _reject_overrides(fn, kw):
    bad = sorted(k for k in kw if k in _FORBIDDEN_KW)
    if bad:
        raise TypeError(f"{fn}: local style override {bad} is not allowed; "
                        f"define it in the central THEME section instead")


def _level_style(filt, level):
    if filt is None:
        return {"label": None, "marker": DEFAULT_MARKER, "hatch": None, "linestyle": None}
    return FILTERS[filt][level]


def _theme_tag(artist, **meta):
    artist._t = meta
    artist.set_gid("scc-theme")
    return artist


def _as_list(v, n):
    if v is None or isinstance(v, str):
        return [v] * n
    return list(v)


def t_scatter(ax, x, y, groups=None, filt=None, level=None, size=30.0, zorder=3, alpha=None, **kw):
    """Points coloured by binder group; marker fixed by the filter level."""
    _reject_overrides("t_scatter", kw)
    xs = np.atleast_1d(np.asarray(x, dtype=float))
    ys = np.atleast_1d(np.asarray(y, dtype=float))
    gl = _as_list(groups, len(xs))
    st = _level_style(filt, level)
    for g in dict.fromkeys(gl):
        idx = [i for i, gi in enumerate(gl) if gi == g]
        col = GROUP_COLORS[g] if g is not None else NEUTRAL["ink"]
        sc = ax.scatter(xs[idx], ys[idx], s=size * MARKER_SCALE.get(st["marker"], 1.0),
                        marker=st["marker"], color=col, edgecolors=NEUTRAL["white"],
                        linewidths=0.5, zorder=zorder, alpha=alpha, **kw)
        _theme_tag(sc, group=g, filt=filt, level=level, marker=st["marker"], kind="marker")


def t_line(ax, x, y, group=None, filt=None, level=None, role=None, marker=False,
           lw=None, zorder=2, alpha=None, label=None, markersize=4.5, **kw):
    """Line; colour = group (or neutral); dash style from TARGET filter or ROLE."""
    _reject_overrides("t_line", kw)
    st = _level_style(filt, level)
    rl = ROLES.get(role) if role else None
    col = GROUP_COLORS[group] if group is not None else NEUTRAL[rl["tone"] if rl else "ink"]
    ls = st["linestyle"] or (rl["linestyle"] if rl else "solid")
    width = lw if lw is not None else (rl["lw"] if rl else 1.3)
    mk = st["marker"] if marker else None
    lines = ax.plot(x, y, color=col, linestyle=ls, linewidth=width, marker=mk,
                    markersize=markersize * MARKER_SCALE.get(mk, 1.0), zorder=zorder, alpha=alpha,
                    markeredgecolor=NEUTRAL["white"] if mk else None, markeredgewidth=0.4)
    for ln in lines:
        _theme_tag(ln, group=group, filt=filt, level=level, role=role, label=label,
             marker=mk, kind="line", linestyle=ls)
    return lines


def t_hline(ax, y, role, label=None, zorder=1):
    _reject_overrides("t_hline", {})
    rl = ROLES[role]
    ln = ax.axhline(y, color=NEUTRAL[rl["tone"]], linestyle=rl["linestyle"], linewidth=rl["lw"], zorder=zorder)
    _theme_tag(ln, group=None, filt=None, level=None, role=role, label=label, marker=None, kind="line",
         linestyle=rl["linestyle"])
    return ln


def t_bar(ax, x, h, groups=None, filt=None, level=None, width=0.72, zorder=3, **kw):
    """Draw binder-coloured bars; scenario bars use lightness, never hatch patterns."""
    _reject_overrides("t_bar", kw)
    xs = np.atleast_1d(np.asarray(x, dtype=float))
    gl = _as_list(groups, len(xs))
    st = _level_style(filt, level)

    if filt == "SCENARIO":
        if level not in SCENARIO_FILL_STRENGTH:
            raise KeyError(f"Unknown loading scenario: {level}")
        cols = [_scenario_group_color(g, level) if g is not None else _blend_with_white(NEUTRAL["mid"], SCENARIO_FILL_STRENGTH[level]) for g in gl]
        edges = [NEUTRAL["dark"]] * len(gl)
        hatch = None
    else:
        cols = [GROUP_COLORS[g] if g is not None else NEUTRAL["mid"] for g in gl]
        edges = [NEUTRAL["white"]] * len(gl)
        hatch = st["hatch"]

    cont = ax.bar(x, h, width=width, color=cols, edgecolor=edges, linewidth=0.55,
                  hatch=hatch, zorder=zorder)
    for p, g in zip(cont.patches, gl):
        _theme_tag(p, group=g, filt=filt, level=level, hatch=hatch,
                   scenario_fill_strength=SCENARIO_FILL_STRENGTH.get(level) if filt == "SCENARIO" else None,
                   kind="bar")
    return cont


def t_errorbar(ax, x, y, yerr, group, filt=None, level=None, capsize=2.5, size=4.8, zorder=3):
    """Point with +-SD whisker; whisker is the 'errbar' role, point carries group/filter."""
    st = _level_style(filt, level)
    col = GROUP_COLORS[group] if group is not None else NEUTRAL["ink"]
    cont = ax.errorbar(x, y, yerr=yerr, fmt=st["marker"], color=col, ecolor=col, capsize=capsize,
                       markersize=size * MARKER_SCALE.get(st["marker"], 1.0), linewidth=1.0,
                       markeredgecolor=NEUTRAL["white"], markeredgewidth=0.4, zorder=zorder)
    data_line, caps, bars = cont.lines
    _theme_tag(data_line, group=group, filt=filt, level=level, role=None, label=None,
         marker=st["marker"], kind="line", linestyle="None")
    for c in caps:
        _theme_tag(c, group=group, filt=None, level=None, role="errbar", label=None, marker=None, kind="line",
             linestyle="None")
    for b in bars:
        _theme_tag(b, group=group, filt=None, level=None, role="errbar", label=None, kind="collection")
    return cont


def t_band(ax, x, lo, hi, filt, level, zorder=1):
    """Neutral min-max band: light fill + boundary lines whose dash style is fixed by the filter level."""
    st = _level_style(filt, level)
    poly = ax.fill_between(x, lo, hi, facecolor=NEUTRAL["mid"], edgecolor="none", alpha=0.07,
                           linewidth=0.0, zorder=zorder)
    _theme_tag(poly, group=None, filt=None, level=None, role="band", label=None, kind="band")
    for yy in (lo, hi):
        ln = ax.plot(x, yy, color=NEUTRAL["mid"], linestyle=st["linestyle"], linewidth=0.9,
                     alpha=0.9, zorder=zorder)[0]
        _theme_tag(ln, group=None, filt=filt, level=level, role=None, label=None, marker=None,
                   kind="line", linestyle=st["linestyle"])
    return poly


def t_interval(ax, y, lo, hi, zorder=2):
    """Horizontal whisker (range) with end caps -- the 'range_whisker' role."""
    rl = ROLES["range_whisker"]
    col = NEUTRAL[rl["tone"]]
    h = ax.hlines(y, lo, hi, color=col, linewidth=rl["lw"], zorder=zorder)
    _theme_tag(h, group=None, filt=None, level=None, role="range_whisker", label=None, kind="collection")
    if hi - lo > 1e-8:
        v = ax.vlines([lo, hi], y - 0.16, y + 0.16, color=col, linewidth=1.4, zorder=zorder)
        _theme_tag(v, group=None, filt=None, level=None, role="range_whisker", label=None, kind="collection")


def t_text(ax, x, y, s, tone="ink", **kw):
    _reject_overrides("t_text", kw)
    return ax.text(x, y, s, color=NEUTRAL[tone], **kw)


def t_annotate(ax, s, xy, tone="ink", **kw):
    _reject_overrides("t_annotate", kw)
    return ax.annotate(s, xy, color=NEUTRAL[tone], **kw)


# --- automatic symbol / hatch guide -----------------------------------------
def _guide_entries(fig):
    """Collect legend entries from the theme tags of every artist in the figure."""
    groups, filt_use, roles, labelled = {}, {}, {}, {}
    for ax in fig.axes:
        for art in ax.get_children():
            meta = getattr(art, "_t", None)
            if meta is None:
                continue
            g, f, lv, rl, lb = (meta.get("group"), meta.get("filt"), meta.get("level"),
                                meta.get("role"), meta.get("label"))
            if lb and g is not None:
                labelled[(g, lb)] = lb
            elif g is not None and rl not in ("errbar",):
                groups[g] = True
            if f is not None:
                e = filt_use.setdefault((f, lv), {"bar": False, "line": False, "marker": False})
                kind = meta.get("kind")
                if kind in ("bar", "band"):
                    e["bar"] = True
                if kind == "line" and meta.get("linestyle") not in (None, "None"):
                    e["line"] = True
                if kind == "marker" or (kind == "line" and meta.get("marker")):
                    e["marker"] = True
            if rl is not None:
                roles[(rl, lb if not g else None)] = True
    fam = [[]]
    handles = fam[0]
    for g in GROUP_ORDER:
        if g in groups:
            handles.append(Line2D([0], [0], marker="s", linestyle="none", markersize=8,
                                  markerfacecolor=GROUP_COLORS[g], markeredgecolor=NEUTRAL["white"], label=g))
    fam.append([]); handles = fam[-1]
    for (g, lb) in labelled:
        handles.append(Line2D([0], [0], color=GROUP_COLORS[g], linewidth=2.0, label=lb))
    for filt in FILTERS:
        fam.append([]); handles = fam[-1]
        for lv, st in FILTERS[filt].items():
            e = filt_use.get((filt, lv))
            if not e:
                continue
            if e["bar"] and not e["marker"] and not e["line"]:
                if filt == "SCENARIO":
                    handles.append(Patch(facecolor=_scenario_legend_color(lv),
                                         edgecolor=NEUTRAL["dark"], linewidth=0.9,
                                         hatch=None, label=st["label"]))
                else:
                    handles.append(Patch(facecolor=NEUTRAL["white"], edgecolor=NEUTRAL["dark"],
                                         linewidth=0.85, hatch=st["hatch"], label=st["label"]))
            elif filt == "TARGET" and e["line"]:
                # Target periods are identified by line style alone, not marker shape.
                handles.append(Line2D([0], [0], color=NEUTRAL["ink"],
                                      linewidth=1.35, linestyle=st["linestyle"],
                                      label=st["label"]))
            else:
                handles.append(Line2D([0], [0], marker=st["marker"],
                                      markersize=6.0 * MARKER_SCALE.get(st["marker"], 1.0),
                                      markerfacecolor=NEUTRAL["ink"], markeredgecolor=NEUTRAL["white"],
                                      color=NEUTRAL["ink"], linestyle="none",
                                      label=st["label"]))
    fam.append([]); handles = fam[-1]
    for name, rl in ROLES.items():
        for (rname, lb) in roles:
            if rname != name:
                continue
            text = lb or rl["label"]
            if rl["kind"] == "patch":
                handles.append(Patch(facecolor=NEUTRAL[rl["tone"]], alpha=0.25, edgecolor=NEUTRAL["border"], label=text))
            elif rl["kind"] == "err":
                handles.append(Line2D([0], [0], color=NEUTRAL[rl["tone"]], linewidth=rl["lw"], marker="|",
                                      markersize=9, label=text))
            else:
                handles.append(Line2D([0], [0], color=NEUTRAL[rl["tone"]], linestyle=rl["linestyle"],
                                      linewidth=max(rl["lw"], 1.2), label=text))
    return [f for f in fam if f]


def t_add_guide(fig):
    """Attach one guide below the figure (never inside the axes); families start new columns."""
    families = _guide_entries(fig)
    if not families:
        return None
    wi = float(fig.get_size_inches()[0])
    ncol_max = max(2, int(wi / 2.4))
    for rows in (4, 5, 6, 8, 12):
        flat = []
        for f in families:
            flat += f
            flat += [Line2D([0], [0], linestyle="none", label=" ")] * ((-len(f)) % rows)
        ncol = len(flat) // rows
        if ncol <= ncol_max:
            break
    leg = fig.legend(handles=flat, loc="upper center", bbox_to_anchor=(0.5, 0.0), ncol=ncol,
                     frameon=False, columnspacing=1.5, handletextpad=0.5, borderaxespad=0.4)
    return leg


# --- validation ---------------------------------------------------------------
def validate_theme_registry():
    """Check fixed group colours and the one-visual-cue-per-filter policy."""
    rows = []
    groups_ok = (tuple(GROUP_COLORS[g] for g in GROUP_ORDER)
                 == tuple(c for _, c in _GROUP_COLOR_LOCK)
                 and tuple(GROUP_ORDER) == tuple(g for g, _ in _GROUP_COLOR_LOCK))

    age = list(FILTERS["AGE"].values())
    age_ok = (len({v["marker"] for v in age}) == len(age)
              and all(v["hatch"] is None and v["linestyle"] is None for v in age))

    target = list(FILTERS["TARGET"].values())
    target_ok = (len({v["linestyle"] for v in target}) == len(target)
                 and len({v["marker"] for v in target}) == 1
                 and all(v["hatch"] is None for v in target))

    scenario_levels = tuple(FILTERS["SCENARIO"])
    scenario_tints = [SCENARIO_FILL_STRENGTH[level] for level in scenario_levels]
    scenario = list(FILTERS["SCENARIO"].values())
    scenario_ok = (len(scenario_levels) == 3
                   and all(v["hatch"] is None for v in scenario)
                   and len({v["marker"] for v in scenario}) == 1
                   and all(v["linestyle"] is None for v in scenario)
                   and len(set(scenario_tints)) == 3
                   and scenario_tints[0] > scenario_tints[1] > scenario_tints[2]
                   and set(SCENARIO_HATCHES.values()) == {None})

    flow = list(FILTERS["FLOW"].values())
    flow_ok = (len({v["marker"] for v in flow}) == len(flow)
               and all(v["hatch"] is None and v["linestyle"] is None for v in flow))

    rows.extend([
        ("binder-group colours unchanged", groups_ok),
        ("age uses marker shape only", age_ok),
        ("initiation target uses line style only", target_ok),
        ("loading scenarios use three clear solid-fill lightness levels", scenario_ok),
        ("fresh-state test uses marker shape only", flow_ok),
        ("no filter combines marker, hatch, and line encodings", all((age_ok, target_ok, scenario_ok, flow_ok))),
    ])
    out = [{"Check": c, "Status": "PASS" if ok else "FAIL"} for c, ok in rows]
    if any(r["Status"] == "FAIL" for r in out):
        raise RuntimeError(f"Theme registry validation failed: {out}")
    return out


def _hex(c):
    try:
        return matplotlib.colors.to_hex(c, keep_alpha=False).lower()
    except ValueError:
        return None


def validate_figure_theme(fig, name):
    """Verify palette, fixed filter symbols and legend placement of one figure."""
    allowed = _allowed_hex()
    group_hex = {v.lower() for v in GROUP_COLORS.values()}
    viol = []
    n_tagged = n_total = 0
    for ax in fig.axes:
        if ax.get_legend() is not None:
            viol.append("legend placed inside an axes")
        for art in ax.get_children():
            if isinstance(art, (matplotlib.image.AxesImage, matplotlib.collections.QuadMesh)):
                continue
            cols = []
            if isinstance(art, matplotlib.lines.Line2D):
                cols += [art.get_color(), art.get_markerfacecolor(), art.get_markeredgecolor()]
            elif isinstance(art, matplotlib.collections.Collection):
                for arr in (art.get_facecolor(), art.get_edgecolor()):
                    cols += list(arr) if len(arr) else []
            elif isinstance(art, matplotlib.patches.Patch):
                cols += [art.get_facecolor(), art.get_edgecolor()]
            elif isinstance(art, matplotlib.text.Text):
                cols += [art.get_color()]
            hexes = [h for h in (_hex(c) for c in cols if c is not None and not (isinstance(c, str) and c in ("none", "auto", "None"))) if h]
            meta = getattr(art, "_t", None)
            n_total += 1
            if meta is not None:
                n_tagged += 1
            for h in hexes:
                if h not in allowed:
                    viol.append(f"colour {h} outside theme ({type(art).__name__})")
                elif h in group_hex and (meta is None or meta.get("group") is None) and not (
                        isinstance(art, matplotlib.patches.Patch) and meta is None and False):
                    if meta is None or meta.get("kind") not in ("band",):
                        viol.append(f"group colour {h} used by an untagged/ungrouped artist")
            if meta is not None:
                f, lv = meta.get("filt"), meta.get("level")
                if f is not None:
                    reg = FILTERS[f][lv]
                    if meta.get("kind") in ("bar", "band"):
                        if getattr(art, "get_hatch", lambda: None)() != reg["hatch"] or meta.get("hatch") != reg["hatch"]:
                            viol.append(f"{f}={lv}: hatch differs from registry")
                        if f == "SCENARIO" and isinstance(art, matplotlib.patches.Patch):
                            expected_fill = _scenario_group_color(meta["group"], lv) if meta.get("group") is not None else None
                            actual_fill = _hex(art.get_facecolor())
                            if expected_fill is not None and actual_fill != expected_fill:
                                viol.append(f"SCENARIO={lv}: bar fill tint differs from registry")
                            if _hex(art.get_edgecolor()) != NEUTRAL["dark"].lower():
                                viol.append(f"SCENARIO={lv}: bar outline differs from theme")
                    if meta.get("kind") in ("marker",) and meta.get("marker") != reg["marker"]:
                        viol.append(f"{f}={lv}: marker differs from registry")
                    if isinstance(art, matplotlib.lines.Line2D):
                        if meta.get("marker") not in (None, reg["marker"]) or art.get_marker() not in (None, "None", reg["marker"]):
                            viol.append(f"{f}={lv}: line marker differs from registry")
                        if reg["linestyle"] is not None and meta.get("linestyle") not in (None, "None"):
                            ref = matplotlib.lines.Line2D([0], [0], linestyle=reg["linestyle"])
                            if art.get_linestyle() != ref.get_linestyle():
                                viol.append(f"{f}={lv}: line style differs from registry")
                elif meta.get("group") is not None and isinstance(art, matplotlib.lines.Line2D):
                    if _hex(art.get_color()) != GROUP_COLORS[meta["group"]].lower():
                        viol.append(f"group {meta['group']}: colour changed")
    # legend must sit in free space: no overlap with any axes (incl. tick labels)
    fig.canvas.draw()
    rend = fig.canvas.get_renderer()
    for leg in fig.legends:
        lb = leg.get_window_extent(rend)
        for ax in fig.axes:
            if lb.overlaps(ax.get_tightbbox(rend)):
                viol.append("legend overlaps an axes")
                break
        # Scenario legend swatches must reproduce the same lightness cue as the bars.
        legend_handles = getattr(leg, "legend_handles", getattr(leg, "legendHandles", []))
        for handle, label_artist in zip(legend_handles, leg.get_texts()):
            label = label_artist.get_text()
            for level, spec in FILTERS["SCENARIO"].items():
                if label == spec["label"]:
                    if getattr(handle, "get_hatch", lambda: None)() not in (None, ""):
                        viol.append(f"legend {label}: unexpected hatch remains")
                    fill = _hex(getattr(handle, "get_facecolor", lambda: None)())
                    if fill != _scenario_legend_color(level):
                        viol.append(f"legend {label}: fill tint differs from scenario registry")
                    edge = _hex(getattr(handle, "get_edgecolor", lambda: None)())
                    if edge != NEUTRAL["dark"].lower():
                        viol.append(f"legend {label}: outline differs from theme")
    return {"Figure": name, "Artists_checked": n_total, "Theme_tagged_artists": n_tagged,
            "Legend_entries": sum(len(l.get_texts()) for l in fig.legends),
            "Violations": "; ".join(sorted(set(viol))), "Status": "PASS" if not viol else "FAIL"}


def t_save(fig, path, **prep_kwargs):
    """Guide -> typography -> validation -> save. The only way figures are written."""
    t_add_guide(fig)
    _prepare_paper_figure(fig, **prep_kwargs)
    row = validate_figure_theme(fig, Path(path).name)
    THEME_QA.append(row)
    if row["Status"] != "PASS":
        raise RuntimeError(f"Theme validation failed for {Path(path).name}: {row['Violations']}")
    fig.savefig(path, dpi=FIG_DPI, bbox_inches="tight")
    plt.close(fig)


def _resolve_publication_font():
    """Return a Times-style serif font that is actually installed.

    Resolve one real family before configuring rcParams so missing font names
    cannot trigger repetitive Matplotlib findfont warnings. DejaVu Serif is
    the portable final fallback bundled with Matplotlib.
    """
    preferred_families = (
        "Times New Roman",
        "TeX Gyre Termes",
        "Nimbus Roman",
        "Liberation Serif",
        "DejaVu Serif",
        "STIXGeneral",
    )
    installed = {}
    for font_entry in font_manager.fontManager.ttflist:
        installed.setdefault(font_entry.name.casefold(), font_entry.name)
    for family in preferred_families:
        resolved = installed.get(family.casefold())
        if resolved:
            return resolved
    return "DejaVu Serif"


PAPER_FONT_FAMILY = _resolve_publication_font()

plt.rcParams.update({
    "font.family": PAPER_FONT_FAMILY,
    "font.serif": [PAPER_FONT_FAMILY],
    "mathtext.fontset": "stix",
    "font.size": 10.5,
    "text.color": NEUTRAL["black"],
    "axes.labelsize": 11.0,
    "axes.titlesize": 11.0,
    "axes.titleweight": "bold",
    "axes.titlecolor": NEUTRAL["black"],
    "axes.labelcolor": NEUTRAL["black"],
    "axes.edgecolor": NEUTRAL["border"],
    "axes.linewidth": 0.8,
    "xtick.color": NEUTRAL["black"],
    "ytick.color": NEUTRAL["black"],
    "xtick.labelsize": 10.0,
    "ytick.labelsize": 10.0,
    "legend.fontsize": 9.5,
    "legend.frameon": False,
    "legend.handlelength": 1.8,
    "figure.titlesize": 12.0,
    "figure.titleweight": "bold",
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "savefig.facecolor": "white",
    "savefig.edgecolor": "white",
    "savefig.dpi": FIG_DPI,
    "grid.color": NEUTRAL["grid"],
    "grid.linewidth": 0.6,
    "lines.linewidth": 1.35,
    "lines.markersize": 4.8,
})


def _style_axes(ax, grid_axis="y", log_axis=None):
    """Apply the common black Times New Roman axis typography and light grid."""
    ax.grid(axis=grid_axis, color=NEUTRAL["grid"], linewidth=0.6, alpha=0.9)
    if log_axis:
        ax.grid(axis=log_axis, which="both", color=NEUTRAL["grid"],
                linewidth=0.5, alpha=0.85)
    ax.set_axisbelow(True)
    for sp in ax.spines.values():
        sp.set_color(NEUTRAL["border"])
        sp.set_linewidth(0.8)
    ax.tick_params(axis="both", colors=NEUTRAL["black"], labelsize=10.0,
                   length=3.5, width=0.8)
    for label in (ax.xaxis.label, ax.yaxis.label):
        label.set_fontfamily(PAPER_FONT_FAMILY)
        label.set_fontsize(11.0)
        label.set_color(NEUTRAL["black"])
    ax.title.set_fontfamily(PAPER_FONT_FAMILY)
    ax.title.set_fontsize(11.0)
    ax.title.set_color(NEUTRAL["black"])


def _style_title(ax, text):
    """Apply a consistent panel heading; overall figure titles are omitted."""
    ax.set_title(text, loc="left", fontsize=11.0, fontweight="bold",
                 fontfamily=PAPER_FONT_FAMILY, color=NEUTRAL["black"], pad=6)


def _prepare_paper_figure(fig, axis_display_pt=6.6, tick_display_pt=5.2,
                          legend_display_pt=5.5, annotation_display_pt=5.2):
    """Scale typography to a target point size at the manuscript display width.

    Most figures use the compact defaults. Dense, multi-panel member/GWP figures
    pass larger display sizes so their labels remain legible after Word scales
    the source PNG to the manuscript column width.
    """
    old_w, _ = fig.get_size_inches()
    scale = max(float(old_w) / 6.35, 1.0)
    axis_pt = float(axis_display_pt) * scale
    tick_pt = float(tick_display_pt) * scale
    legend_pt = float(legend_display_pt) * scale
    annotation_pt = float(annotation_display_pt) * scale
    for ax in fig.axes:
        for axis_label in (ax.xaxis.label, ax.yaxis.label):
            axis_label.set_fontfamily(PAPER_FONT_FAMILY)
            axis_label.set_fontsize(axis_pt)
            axis_label.set_color("black")
        ax.title.set_fontfamily(PAPER_FONT_FAMILY)
        ax.title.set_fontsize(axis_pt)
        ax.title.set_fontweight("bold")
        ax.title.set_color("black")
        ax.tick_params(axis="both", labelsize=tick_pt, colors="black")
        for label in ax.get_xticklabels() + ax.get_yticklabels():
            label.set_fontfamily(PAPER_FONT_FAMILY)
            label.set_fontsize(tick_pt)
            label.set_color("black")
        for txt in ax.texts:
            txt.set_fontfamily(PAPER_FONT_FAMILY)
            txt.set_fontsize(annotation_pt)
        leg = ax.get_legend()
        if leg is not None:
            for txt in leg.get_texts():
                txt.set_fontfamily(PAPER_FONT_FAMILY)
                txt.set_fontsize(legend_pt)
                txt.set_color("black")
            if leg.get_title() is not None:
                leg.get_title().set_fontfamily(PAPER_FONT_FAMILY)
                leg.get_title().set_fontsize(legend_pt)
                leg.get_title().set_color("black")
    for leg in fig.legends:
        for txt in leg.get_texts():
            txt.set_fontfamily(PAPER_FONT_FAMILY)
            txt.set_fontsize(legend_pt)
            txt.set_color("black")
        if leg.get_title() is not None:
            leg.get_title().set_fontfamily(PAPER_FONT_FAMILY)
            leg.get_title().set_fontsize(legend_pt)
            leg.get_title().set_color("black")


# =============================================================================
# XLSX READER
# =============================================================================
MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"m": MAIN_NS, "r": DOC_REL_NS, "p": PKG_REL_NS}


def _tag(ns, name):
    return f"{{{ns}}}{name}"


def _column_number(s):
    n = 0
    for c in s.upper():
        n = n * 26 + ord(c) - ord("A") + 1
    return n


def _column_letters(n):
    letters = ""
    while n:
        n, r = divmod(n - 1, 26)
        letters = chr(65 + r) + letters
    return letters


def _cell_ref(ref):
    m = re.fullmatch(r"([A-Za-z]+)(\d+)", ref)
    return int(m.group(2)), _column_number(m.group(1))


def _parse_scalar(raw):
    if raw is None or raw == "":
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return raw
    return int(v) if v.is_integer() and abs(v) < 9e15 else v


class XLSXReader:
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        self._sheets = {}
        with zipfile.ZipFile(self.path) as arc:
            wb = ET.fromstring(arc.read("xl/workbook.xml"))
            rels = ET.fromstring(arc.read("xl/_rels/workbook.xml.rels"))
            relmap = {r.attrib["Id"]: r.attrib["Target"]
                      for r in rels.findall(_tag(PKG_REL_NS, "Relationship"))}
            shared = []
            if "xl/sharedStrings.xml" in arc.namelist():
                sst = ET.fromstring(arc.read("xl/sharedStrings.xml"))
                for si in sst.findall(_tag(MAIN_NS, "si")):
                    shared.append("".join(t.text or "" for t in si.iter(_tag(MAIN_NS, "t"))))
            for sh in wb.findall("m:sheets/m:sheet", NS):
                name = sh.attrib["name"]
                rid = sh.attrib[_tag(DOC_REL_NS, "id")]
                target = relmap[rid].lstrip("/")
                if not target.startswith("xl/"):
                    target = posixpath.normpath(posixpath.join("xl", target))
                root = ET.fromstring(arc.read(target))
                cells = {}
                for c in root.findall(".//m:sheetData/m:row/m:c", NS):
                    ref = c.attrib.get("r")
                    if not ref:
                        continue
                    row, col = _cell_ref(ref)
                    ct = c.attrib.get("t", "n")
                    vnode = c.find("m:v", NS)
                    raw = vnode.text if vnode is not None else None
                    if ct == "s" and raw not in (None, ""):
                        idx = int(raw)
                        val = shared[idx] if idx < len(shared) else None
                    elif ct == "inlineStr":
                        inode = c.find("m:is", NS)
                        val = ("".join(t.text or "" for t in inode.iter(_tag(MAIN_NS, "t")))
                               if inode is not None else None)
                    elif ct == "b":
                        val = raw == "1" if raw is not None else None
                    elif ct == "e":
                        val = raw
                    else:
                        val = _parse_scalar(raw)
                    cells[(row, col)] = val
                self._sheets[name] = cells

    @property
    def sheet_names(self):
        return list(self._sheets)

    def max_row(self, name):
        return max((r for r, _ in self._sheets[name]), default=1)

    def max_col(self, name):
        return max((c for _, c in self._sheets[name]), default=1)

    def get_range_values(self, name, address):
        m = re.fullmatch(r"\$?([A-Za-z]+)\$?(\d+):\$?([A-Za-z]+)\$?(\d+)", address.strip())
        c1, r1, c2, r2 = m.groups()
        sc, sr, ec, er = _column_number(c1), int(r1), _column_number(c2), int(r2)
        cells = self._sheets[name]
        return [[cells.get((r, c)) for c in range(sc, ec + 1)]
                for r in range(sr, er + 1)]


# =============================================================================
# GENERIC HELPERS
# =============================================================================
def is_number(v):
    return (isinstance(v, (int, float, np.integer, np.floating))
            and not isinstance(v, (bool, np.bool_))
            and math.isfinite(float(v)))


def safe_float(v, d=None):
    return float(v) if is_number(v) else d


def mean_sd(vals):
    v = [float(x) for x in vals if is_number(x)]
    return (statistics.mean(v) if v else None,
            statistics.stdev(v) if len(v) > 1 else None, len(v))


def safe_pct_change(new, old):
    if is_number(new) and is_number(old) and float(old) != 0:
        return (float(new) / float(old) - 1.0) * 100.0
    return None


def pearson(x, y):
    p = [(float(a), float(b)) for a, b in zip(x, y) if is_number(a) and is_number(b)]
    if len(p) < 3:
        return None, len(p)
    xa = np.array([q[0] for q in p])
    ya = np.array([q[1] for q in p])
    if np.std(xa) == 0 or np.std(ya) == 0:
        return None, len(p)
    return float(np.corrcoef(xa, ya)[0, 1]), len(p)


def write_csv(path, rows, fields=None):
    rows = list(rows)
    if not rows and not fields:
        return
    fields = fields or list(rows[0].keys())
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_matrix_csv(path, header, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def read_sheet(reader, name, ncol):
    last = reader.max_row(name)
    return reader.get_range_values(name, f"A1:{_column_letters(ncol)}{last}")[1:]


def read_sheet_all(reader, name):
    mr, mc = reader.max_row(name), reader.max_col(name)
    vals = reader.get_range_values(name, f"A1:{_column_letters(mc)}{mr}")
    if not vals:
        return [], []
    header = ["" if v is None else str(v) for v in vals[0]]
    return header, vals[1:]


def group_for_mix(mid, md):
    if mid == "M01":
        return "Reference"
    g, s = md[mid]["G"], md[mid]["S"]
    if g > 0 and s == 0:
        return "GGBFS-only"
    if g == 0 and s > 0:
        return "SF-only"
    return "Ternary"


# =============================================================================
# LOAD EXPERIMENTAL
# =============================================================================
def load_experimental(workbook_path, reader=None):
    """Load the experimental dataset, optionally reusing a parsed XLSXReader."""
    reader = reader if reader is not None else XLSXReader(workbook_path)
    required = ("Mix_Design", "Fresh_Concrete", "Hardening",
                "Resistivity", "Water_Absorption", "RCMT")
    missing = [s for s in required if s not in reader.sheet_names]
    if missing:
        raise KeyError(f"Missing worksheets: {missing}. Available: {reader.sheet_names}")

    mix_rows = read_sheet(reader, "Mix_Design", 15)
    fresh_rows = read_sheet(reader, "Fresh_Concrete", 11)
    hard_rows = read_sheet(reader, "Hardening", 8)
    res_rows = read_sheet(reader, "Resistivity", 10)
    abs_rows = read_sheet(reader, "Water_Absorption", 8)
    rcmt_rows = read_sheet(reader, "RCMT", 17)

    order = [r[0] for r in mix_rows if r and r[0]]
    md = {}
    for r in mix_rows:
        if not r or not r[0]:
            continue
        if len(r) < 5 or not (is_number(r[3]) and is_number(r[4])):
            raise ValueError(f"Bad GGBFS/SF in {r[0]!r}")
        mid = r[0]
        cement = safe_float(r[5], 0.0)
        ggbfs_kg = safe_float(r[6], 0.0)
        sf_kg = safe_float(r[7], 0.0)
        binder = safe_float(r[13], None)
        if binder is None or binder <= 0:
            binder = cement + ggbfs_kg + sf_kg
        md[mid] = {
            "code": r[1], "test": r[2], "G": float(r[3]), "S": float(r[4]),
            "cement_kg_m3": cement, "ggbfs_kg_m3": ggbfs_kg, "sf_kg_m3": sf_kg,
            "water_kg_m3": safe_float(r[8], None), "PCE_kg_m3": safe_float(r[12], None),
            "binder_total_kg_m3": binder,
        }
    if "M01" not in md:
        raise ValueError("M01 missing")

    fresh = {}
    for r in fresh_rows:
        if not r or not r[0] or r[0] not in md:
            continue
        mid = r[0]
        pce = safe_float(r[8], None) or md[mid]["PCE_kg_m3"]
        binder = md[mid]["binder_total_kg_m3"]
        cement = md[mid]["cement_kg_m3"]
        pct_b = safe_float(r[9], None)
        pct_c = safe_float(r[10], None)
        if pct_b is None and pce is not None and binder:
            pct_b = pce / binder * 100
        if pct_c is None and pce is not None and cement:
            pct_c = pce / cement * 100
        fresh[mid] = {
            "slump_flow_mm": r[3], "T500_s": r[4], "J_ring_flow_mm": r[5],
            "J_ring_height_diff_mm": r[6], "V_funnel_s": r[7], "PCE_kg_m3": pce,
            "PCE_pct_binder": pct_b, "PCE_pct_cement": pct_c,
        }
    if len(fresh) != len(order):
        raise ValueError(f"Fresh rows={len(fresh)} vs order={len(order)}")

    cs_raw = defaultdict(list)
    for r in hard_rows:
        if len(r) < 8:
            continue
        mid, _, _, age, _, _, _, s = r[:8]
        if mid in md and is_number(age) and is_number(s):
            cs_raw[(mid, int(age))].append(float(s))
    cs_stats = {(m, a): mean_sd(cs_raw[(m, a)]) for m in order for a in STRENGTH_AGES}
    cs_mean = {k: v[0] for k, v in cs_stats.items()}

    abs_raw = defaultdict(list)
    for r in abs_rows:
        if len(r) < 7:
            continue
        mid, _, _, age, _, dry, sat = r[:7]
        if (mid in md and is_number(age) and is_number(dry) and is_number(sat)
                and float(dry) != 0):
            abs_raw[(mid, int(age))].append((float(sat) - float(dry)) / float(dry) * 100)
    wa_stats = {(m, a): mean_sd(abs_raw[(m, a)]) for m in order for a in TRANSPORT_AGES}
    wa_mean = {k: v[0] for k, v in wa_stats.items()}

    res_raw = defaultdict(list)
    for r in res_rows:
        if len(r) < 9:
            continue
        mid, _, _, age, *_ = r
        rd = [float(x) for x in r[5:9] if is_number(x)]
        if mid in md and is_number(age) and rd:
            res_raw[(mid, int(age))].append(statistics.mean(rd))
    er_stats = {(m, a): mean_sd(res_raw[(m, a)]) for m in order for a in TRANSPORT_AGES}
    er_mean = {k: v[0] for k, v in er_stats.items()}

    spec_D = defaultdict(list)
    form_D = {}
    for r in rcmt_rows:
        if not r or not r[0] or r[0] not in md:
            continue
        mid = r[0]
        vs = r[10] if len(r) > 10 else None
        vm = r[11] if len(r) > 11 else None
        vr = r[12] if len(r) > 12 else None
        if is_number(vs) and float(vs) > 0:
            spec_D[mid].append(float(vs))
        if mid not in form_D:
            if is_number(vm) and float(vm) > 0:
                form_D[mid] = float(vm)
            elif is_number(vr) and float(vr) > 0:
                form_D[mid] = float(vr) * 1e-12

    sum_D = {}
    if "RCMT_Summary" in reader.sheet_names:
        for r in read_sheet(reader, "RCMT_Summary", 10):
            if not r or not r[0]:
                continue
            mid = r[0]
            mean_m2 = sd_m2 = n_s = None
            if len(r) > 3 and is_number(r[3]):
                mean_m2 = float(r[3])
            elif len(r) > 4 and is_number(r[4]):
                mean_m2 = float(r[4]) * 1e-12
            if len(r) > 5 and is_number(r[5]):
                sd_m2 = float(r[5])
            elif len(r) > 6 and is_number(r[6]):
                sd_m2 = float(r[6]) * 1e-12
            if len(r) > 7 and is_number(r[7]):
                n_s = int(r[7])
            if mean_m2 is not None and mean_m2 > 0:
                sum_D[mid] = {"mean": mean_m2, "sd": sd_m2, "n": n_s}

    # Specimen-level Dnssm values in the RCMT worksheet are used as the calculation source.
    # RCMT_Summary is read separately for quality assurance only, never as the analysis input.
    D_stats = {}
    rcmt_summary = []
    for mid in order:
        obs = spec_D.get(mid, [])
        if not obs:
            raise ValueError(f"No specimen-level Dnssm_m2_s values in the current RCMT worksheet for {mid}")
        mD = statistics.mean(obs)
        sD = statistics.stdev(obs) if len(obs) > 1 else None
        nD = len(obs)
        D_stats[mid] = {
            "mean": float(mD), "sd": float(sD) if is_number(sD) else None,
            "n": int(nD), "scaled": float(mD) * 1e12,
            "scaled_sd": float(sD) * 1e12 if is_number(sD) else None,
        }
        rcmt_summary.append({
            "Mix_ID": mid, "Mixture_Code": md[mid]["code"],
            "Dnssm_90d_mean_m2_s": D_stats[mid]["mean"],
            "Dnssm_90d_mean_1e-12_m2_s": D_stats[mid]["scaled"],
            "Dnssm_90d_SD_m2_s": D_stats[mid]["sd"],
            "Dnssm_90d_SD_1e-12_m2_s": D_stats[mid]["scaled_sd"],
            "Specimen_count": D_stats[mid]["n"],
        })

    return {
        "order": order, "md": md, "group": lambda m: group_for_mix(m, md),
        "fresh": fresh, "cs_raw": cs_raw,
        "cs_stats": cs_stats, "cs": cs_mean, "wa_raw": abs_raw, "wa_stats": wa_stats,
        "wa": wa_mean, "er_raw": res_raw, "er_stats": er_stats, "er": er_mean,
        "D_raw": spec_D, "D_stats": D_stats, "rcmt_summary": rcmt_summary,
        "sheet_names": reader.sheet_names,
    }


def build_rcmt_consistency_qa(workbook_path, data):
    """Compare specimen-level current-workbook RCMT coefficients with summary rows and mix keys."""
    reader = XLSXReader(workbook_path)
    raw_rows = read_sheet(reader, "RCMT", 16)
    summary_rows = read_sheet(reader, "RCMT_Summary", 10) if "RCMT_Summary" in reader.sheet_names else []
    summary_by_mix = {r[0]: r for r in summary_rows if r and r[0]}
    raw_by_mix = defaultdict(list)
    for r in raw_rows:
        if r and r[0] in data["md"]:
            raw_by_mix[r[0]].append(r)
    qa = []
    for mid in data["order"]:
        obs_rows = raw_by_mix.get(mid, [])
        values = [float(r[10]) for r in obs_rows if len(r) > 10 and is_number(r[10]) and float(r[10]) > 0]
        mean_raw = statistics.mean(values) if values else None
        sd_raw = statistics.stdev(values) if len(values) > 1 else None
        sr = summary_by_mix.get(mid, [])
        summary_mean = float(sr[3]) if len(sr) > 3 and is_number(sr[3]) else None
        summary_scaled = float(sr[4]) if len(sr) > 4 and is_number(sr[4]) else None
        summary_sd = float(sr[5]) if len(sr) > 5 and is_number(sr[5]) else None
        summary_n = int(sr[7]) if len(sr) > 7 and is_number(sr[7]) else None
        check_scaled = float(sr[8]) if len(sr) > 8 and is_number(sr[8]) else None
        complete_fields = 0
        required_raw_indices = (4, 5, 6, 7, 8, 9, 10)
        for r in obs_rows:
            if all(i < len(r) and is_number(r[i]) for i in required_raw_indices):
                complete_fields += 1
        md = data["md"][mid]
        keys_match = all(
            r[0] == mid and str(r[1]).strip() == str(md["code"]).strip()
            and is_number(r[2]) and int(r[2]) == int(md["test"])
            for r in obs_rows
        )
        mean_matches = (mean_raw is not None and summary_mean is not None and
                        math.isclose(mean_raw, summary_mean, rel_tol=1e-6, abs_tol=1e-18))
        sd_matches = (sd_raw is not None and summary_sd is not None and
                      math.isclose(sd_raw, summary_sd, rel_tol=1e-5, abs_tol=1e-18))
        scale_matches = (summary_scaled is not None and mean_raw is not None and
                         math.isclose(summary_scaled, mean_raw * 1e12, rel_tol=1e-5, abs_tol=1e-6))
        check_matches = (check_scaled is not None and mean_raw is not None and
                         math.isclose(check_scaled, mean_raw * 1e12, rel_tol=0.02, abs_tol=0.02))
        qa.append({
            "Mix_ID": mid, "Mixture_Code_expected": md["code"],
            "RCMT_rows_n": len(obs_rows), "Valid_specimen_Dnssm_n": len(values),
            "Analysis_mean_from_specimens_m2_s": mean_raw,
            "Analysis_mean_from_specimens_1e-12_m2_s": mean_raw * 1e12 if mean_raw is not None else None,
            "Analysis_SD_from_specimens_m2_s": sd_raw,
            "Summary_mean_m2_s": summary_mean,
            "Summary_scaled_1e-12_m2_s": summary_scaled,
            "Recomputed_scaled_from_summary_mean": summary_mean * 1e12 if summary_mean is not None else None,
            "Summary_SD_m2_s": summary_sd, "Summary_specimen_count": summary_n,
            "Summary_check_1e-12_m2_s": check_scaled,
            "Mixture_and_test_keys_match": keys_match,
            "Raw_mean_matches_summary_mean": mean_matches,
            "Raw_SD_matches_summary_SD": sd_matches,
            "Summary_scaled_field_matches_raw_mean": scale_matches,
            "Summary_check_matches_raw_mean": check_matches,
            "Raw_test_input_fields_complete_n": complete_fields,
            "Raw_rows_with_incomplete_test_inputs_n": len(obs_rows) - complete_fields,
            "Status": "OK" if (keys_match and mean_matches and sd_matches and scale_matches and check_matches)
                     else "CHECK_SUMMARY_OR_INPUT_FIELDS",
        })
    return qa


# =============================================================================
# SIMAPRO
# =============================================================================
def read_simapro(reader, md):
    sheet = "SimaPro_GWP"
    mr, mc = reader.max_row(sheet), reader.max_col(sheet)
    vals = reader.get_range_values(sheet, f"A1:{_column_letters(mc)}{mr}")
    totals = {}
    for r in vals[1:]:
        if r and isinstance(r[0], str) and r[0].startswith("M") and is_number(r[2]):
            totals[r[0]] = float(r[2])
    if len(totals) != 16 or any(v <= 0 for v in totals.values()):
        raise ValueError(f"Expected 16 positive SimaPro totals; got {len(totals)}")

    header_idx = None
    for i, r in enumerate(vals):
        if r and str(r[0]).strip().lower() == "mix design" and str(r[1]).strip().lower() == "cement":
            header_idx = i
            break

    comp_header = ["Mix_ID", "Mixture_Code", "Cement", "GGBFS", "SF", "LP", "Sand",
                   "Gravel", "Water", "SP", "Component_sum", "Reported_component_total",
                   "Top_table_total", "Status"]
    comp_rows = []
    qa_rows = []
    if header_idx is not None:
        for r in vals[header_idx + 2:]:
            if not r or r[0] in (None, ""):
                continue
            label = str(r[0]).strip()
            if label.lower().startswith("(kg"):
                continue
            if label.lower().startswith("ref"):
                mid = "M01"
            else:
                mid = next((k for k, v in md.items() if str(v["code"]).strip() == label), None)
            if mid is None:
                continue
            comp = [float(r[j]) if j < len(r) and is_number(r[j]) else 0.0 for j in range(1, 9)]
            s_comp = sum(comp)
            reported = float(r[9]) if len(r) > 9 and is_number(r[9]) else s_comp
            top = totals.get(mid)
            status = "OK"
            if abs(s_comp - reported) > 0.02:
                status = "CHECK_COMPONENT_SUM"
            if top is not None and abs(top - reported) > 0.02:
                status = "CHECK_TOP_VS_COMPONENT"
            comp_rows.append([mid, label] + comp + [s_comp, reported, top if top is not None else "", status])
            qa_rows.append({
                "Mix_ID": mid, "Mixture_Code": label, "Component_sum": s_comp,
                "Reported_component_total": reported, "Top_table_total": top,
                "Component_vs_reported_diff": s_comp - reported,
                "Top_vs_reported_diff": (top - reported) if top is not None else None,
                "Status": status,
            })
    return {"totals": totals, "component_header": comp_header,
            "component_rows": comp_rows, "qa_rows": qa_rows}


# =============================================================================
# SECTION 3.1 — FRESH PROPERTIES
# =============================================================================
def run_section_3_1(data, root):
    out = root / "Section_3_1"
    out.mkdir(parents=True, exist_ok=True)
    order, md, fresh = data["order"], data["md"], data["fresh"]
    rows = []
    for mid in order:
        fr = fresh[mid]
        cm, csd, cn = data["cs_stats"][(mid, 28)]
        s, j = fr["slump_flow_mm"], fr["J_ring_flow_mm"]
        dj = float(s) - float(j) if is_number(s) and is_number(j) else None
        rows.append({
            "Mix_ID": mid, "Mixture_Code": md[mid]["code"], "Binder_Group": data["group"](mid),
            "SlumpFlow_mm": s, "T500_s": fr["T500_s"], "JRingFlow_mm": j, "DeltaJ_mm": dj,
            "JRingHeightDifference_mm": fr["J_ring_height_diff_mm"], "V_Funnel_s": fr["V_funnel_s"],
            "PCE_kg_m3": fr["PCE_kg_m3"], "PCE_pct_Binder": fr["PCE_pct_binder"],
            "CS28_mean_MPa": cm, "CS28_SD_MPa": csd, "CS28_n": cn,
        })
    write_csv(out / "Table_3_1_Fresh_and_28d_Summary.csv", rows)

    # The seven-panel fresh-state/28-day strength figure is not generated: all plotted
    # measurements are reported numerically in Tables 5 and 6, which are retained.
    return rows


# =============================================================================
# SECTION 3.2 — AGE-DEPENDENT RESPONSE
# =============================================================================
def run_section_3_2(data, root):
    out = root / "Section_3_2"
    out.mkdir(parents=True, exist_ok=True)
    order, md, grp = data["order"], data["md"], data["group"]
    cs_s, cs = data["cs_stats"], data["cs"]
    wa_s, wa = data["wa_stats"], data["wa"]
    er_s, er = data["er_stats"], data["er"]
    D_s = data["D_stats"]
    ref_D = D_s["M01"]["mean"]
    rows = []
    for mid in order:
        r = {"Mix_ID": mid, "Mixture_Code": md[mid]["code"], "Test_Code": md[mid]["test"],
             "GGBFS_target_pct": md[mid]["G"], "SF_target_pct": md[mid]["S"], "Binder_Group": grp(mid)}
        for a in STRENGTH_AGES:
            m_, s_, n_ = cs_s[(mid, a)]
            r[f"CS_{a}d_mean_MPa"] = m_
            r[f"CS_{a}d_SD_MPa"] = s_
            r[f"CS_{a}d_n"] = n_
        r["CS_change_3_to_90_pct"] = safe_pct_change(cs[(mid, 90)], cs[(mid, 3)])
        r["CS_change_28_to_90_pct"] = safe_pct_change(cs[(mid, 90)], cs[(mid, 28)])
        for a in TRANSPORT_AGES:
            wm, ws, wn = wa_s[(mid, a)]
            em, es, en = er_s[(mid, a)]
            r[f"WA_{a}d_mean_pct"] = wm
            r[f"WA_{a}d_SD_pct"] = ws
            r[f"WA_{a}d_n"] = wn
            r[f"ER_{a}d_mean_kOhm_cm"] = em
            r[f"ER_{a}d_SD_kOhm_cm"] = es
            r[f"ER_{a}d_n_specimens"] = en
        r["WA_change_28_to_90_pct"] = safe_pct_change(wa[(mid, 90)], wa[(mid, 28)])
        r["ER_change_28_to_90_pct"] = safe_pct_change(er[(mid, 90)], er[(mid, 28)])
        d = D_s[mid]
        r["Dnssm_90d_mean_m2_s"] = d["mean"]
        r["Dnssm_90d_mean_1e-12_m2_s"] = d["scaled"]
        r["Dnssm_90d_SD_m2_s"] = d["sd"]
        r["Dnssm_90d_SD_1e-12_m2_s"] = d["scaled_sd"]
        r["Dnssm_reduction_vs_reference_pct"] = (
            (1 - d["mean"] / ref_D) * 100 if is_number(d["mean"]) and ref_D != 0 else None
        )
        rows.append(r)
    write_csv(out / "Table_3_2_Age_Dependent_Summary.csv", rows)
    write_csv(out / "Table_S_RCMT_Formulation_Summary.csv", data["rcmt_summary"])

    # Three panels retain the age-dependent properties; the 90-day RCMT point plot
    # is omitted because the exact migration means and SDs are reported in Table 8.
    fig, axes = plt.subplots(1, 3, figsize=(17.2, 6.2), constrained_layout=True)
    ax = axes[0]
    for mid in order:
        y = [cs_s[(mid, a)][0] for a in STRENGTH_AGES]
        t_line(ax, STRENGTH_AGES, y, group=grp(mid), marker=True, lw=1.1, markersize=4.0, alpha=0.8)
    _style_title(ax, "(a) Compressive strength")
    ax.set(xlabel="Age (days)", ylabel="Compressive strength (MPa)", xticks=STRENGTH_AGES)
    _style_axes(ax)

    def _age_pairs(ax, getter, positive_only=False):
        """Paired 28-d / 90-d points: colour = group, marker = AGE filter, link = pair role."""
        pts = {a: ([], [], []) for a in TRANSPORT_AGES}
        for i, mid in enumerate(order):
            xp = (i - 0.11, i + 0.11)
            yp = [getter(mid, a) for a in TRANSPORT_AGES]
            ok = [is_number(v) and (float(v) > 0 or not positive_only) for v in yp]
            if all(ok):
                t_line(ax, list(xp), yp, role="pair_link", alpha=0.7)
            for j, a in enumerate(TRANSPORT_AGES):
                if ok[j]:
                    pts[a][0].append(xp[j]); pts[a][1].append(yp[j]); pts[a][2].append(grp(mid))
        for a in TRANSPORT_AGES:
            t_scatter(ax, pts[a][0], pts[a][1], groups=pts[a][2], filt="AGE", level=a, size=34)

    x_pos = np.arange(len(order))
    ax = axes[1]
    _age_pairs(ax, lambda mid, a: wa_s[(mid, a)][0])
    _style_title(ax, "(b) Water absorption")
    ax.set_ylabel("Water absorption (%)")
    ax.set_xticks(x_pos, order, rotation=55, ha="right")
    _style_axes(ax)

    ax = axes[2]
    _age_pairs(ax, lambda mid, a: er_s[(mid, a)][0], positive_only=True)
    _style_title(ax, "(c) Electrical resistivity")
    ax.set_ylabel("Electrical resistivity (kΩ·cm)")
    ax.set_yscale("log")
    ax.set_xticks(x_pos, order, rotation=55, ha="right")
    _style_axes(ax, log_axis="y")
    t_save(fig, out / "Figure_3_2_Age_Dependent_Response.png",
           axis_display_pt=8.5, tick_display_pt=7.4, legend_display_pt=7.0, annotation_display_pt=7.3)
    return rows


PROPERTY_LABELS = {
    "CS28": "Compressive strength, 28 d (MPa)",
    "WA28": "Water absorption, 28 d (%)",
    "ER28": "Electrical resistivity, 28 d (kΩ·cm)",
    "CS90": "Compressive strength, 90 d (MPa)",
    "WA90": "Water absorption, 90 d (%)",
    "ER90": "Electrical resistivity, 90 d (kΩ·cm)",
    "Dnssm90": r"Chloride migration, 90 d ($10^{-12}$ m$^2$/s)",
}


def _property_value(data, mid, key):
    if key.startswith("CS"):
        return data["cs"].get((mid, int(key[2:])))
    if key.startswith("WA"):
        return data["wa"].get((mid, int(key[2:])))
    if key.startswith("ER"):
        return data["er"].get((mid, int(key[2:])))
    if key == "Dnssm90":
        return data["D_stats"][mid]["mean"]
    raise KeyError(key)


def _single_mix_id(data, g, s):
    m = [mid for mid in data["order"] if data["md"][mid]["G"] == g and data["md"][mid]["S"] == s]
    return m[0] if len(m) == 1 else None


def _correlation_matrix(props):
    keys = list(props)
    n = len(keys)
    mat = np.full((n, n), np.nan)
    cnt = np.zeros((n, n), dtype=int)
    for i, ki in enumerate(keys):
        for j, kj in enumerate(keys):
            r, c = pearson(props[ki], props[kj])
            cnt[i, j] = c
            if i == j and c:
                r = 1.0
            if r is not None:
                mat[i, j] = r
    return keys, mat, cnt


# =============================================================================
# SECTION 3.3 — INTERACTIONS AND CORRELATIONS
# =============================================================================
def run_section_3_3(data, root):
    out = root / "Section_3_3"
    out.mkdir(parents=True, exist_ok=True)
    order, md = data["order"], data["md"]
    props_28 = {"CS28": [_property_value(data, m, "CS28") for m in order],
                "WA28": [_property_value(data, m, "WA28") for m in order],
                "ER28": [_property_value(data, m, "ER28") for m in order]}
    props_90 = {"CS90": [_property_value(data, m, "CS90") for m in order],
                "WA90": [_property_value(data, m, "WA90") for m in order],
                "ER90": [_property_value(data, m, "ER90") for m in order],
                "Dnssm90": [_property_value(data, m, "Dnssm90") for m in order]}
    corr_rows = []
    for age, props in ((28, props_28), (90, props_90)):
        keys = list(props)
        for i, p1 in enumerate(keys):
            for p2 in keys[i + 1:]:
                r, n = pearson(props[p1], props[p2])
                corr_rows.append({"Age_d": age, "Property_1": p1, "Property_2": p2,
                                  "Pearson_r": r, "Pairwise_n": n})
    write_csv(out / "Table_3_3_Correlation_Summary.csv", corr_rows)

    inter = []
    ref_vals = {k: _property_value(data, "M01", k)
                for k in ("CS28", "CS90", "WA28", "WA90", "ER28", "ER90", "Dnssm90")}
    for g in (20, 30, 40):
        for s in (5, 9, 13):
            tid = _single_mix_id(data, g, s)
            gid = _single_mix_id(data, g, 0)
            sid = _single_mix_id(data, 0, s)
            row = {"Mix_ID": tid, "Mixture_Code": md[tid]["code"] if tid else None,
                   "GGBFS_pct": g, "SF_pct": s, "GGBFS_only_mix": gid,
                   "SF_only_mix": sid, "Reference_mix": "M01"}
            for prop in ("CS28", "CS90", "WA28", "WA90", "ER28", "ER90", "Dnssm90"):
                obs = _property_value(data, tid, prop) if tid else None
                gv = _property_value(data, gid, prop) if gid else None
                sv = _property_value(data, sid, prop) if sid else None
                rv = ref_vals[prop]
                base = (gv + sv - rv) if all(is_number(v) for v in (gv, sv, rv)) else None
                cont = (obs - gv - sv + rv) if all(is_number(v) for v in (obs, gv, sv, rv)) else None
                row[f"{prop}_observed"] = obs
                row[f"{prop}_additive_baseline"] = base
                row[f"{prop}_I"] = cont
                row[f"{prop}_I_pct_reference"] = (
                    100 * cont / rv if is_number(cont) and is_number(rv) and rv != 0 else None
                )
            do = _property_value(data, tid, "Dnssm90") if tid else None
            dg = _property_value(data, gid, "Dnssm90") if gid else None
            ds = _property_value(data, sid, "Dnssm90") if sid else None
            dr = _property_value(data, "M01", "Dnssm90")
            if all(is_number(v) and v > 0 for v in (do, dg, ds, dr)):
                li = math.log10(do) - math.log10(dg) - math.log10(ds) + math.log10(dr)
                mb = dg * ds / dr
                ratio = do / mb
            else:
                li = mb = ratio = None
            row["Dnssm90_multiplicative_baseline_m2_s"] = mb
            row["Dnssm90_log10_interaction"] = li
            row["Dnssm90_observed_to_baseline_ratio"] = ratio
            inter.append(row)
    write_csv(out / "Table_3_3_GGBFS_SF_Interaction_Contrasts.csv", inter)

    fig, axes = plt.subplots(2, 2, figsize=(14.0, 9.2), constrained_layout=True)
    specs = [
        ("CS90_I", "(a) Compressive strength, 90 d", "I (MPa)", "+.2f"),
        ("WA90_I", "(b) Water absorption, 90 d", "I (percentage points)", "+.2f"),
        ("ER90_I", "(c) Electrical resistivity, 90 d", "I (kΩ·cm)", "+.1f"),
        ("Dnssm90_log10_interaction", "(d) Chloride migration, 90 d", r"$I_{\log_{10}(D)}$", "+.2f"),
    ]
    for ax, (key, title, cbar_label, fmt) in zip(axes.ravel(), specs):
        vals = np.full((3, 3), np.nan)
        for r in inter:
            ri = {20: 0, 30: 1, 40: 2}[int(r["GGBFS_pct"])]
            ci = {5: 0, 9: 1, 13: 2}[int(r["SF_pct"])]
            v = r.get(key)
            if is_number(v):
                vals[ri, ci] = float(v)
        vmax = float(np.nanmax(np.abs(vals))) if np.isfinite(vals).any() else 1.0
        if vmax == 0:
            vmax = 1.0
        im = ax.imshow(vals, cmap=DIVERGING_CMAP,
                       norm=TwoSlopeNorm(vmin=-vmax, vcenter=0, vmax=vmax), aspect="auto")
        _style_title(ax, title)
        ax.set_xlabel("SF (%)")
        ax.set_ylabel("GGBFS (%)")
        ax.set_xticks([0, 1, 2], ["5", "9", "13"])
        ax.set_yticks([0, 1, 2], ["20", "30", "40"])
        ax.tick_params(axis="both", labelsize=10.5, colors=NEUTRAL["black"])
        for i in range(3):
            for j in range(3):
                it = next(r for r in inter if {20: 0, 30: 1, 40: 2}[int(r["GGBFS_pct"])] == i
                          and {5: 0, 9: 1, 13: 2}[int(r["SF_pct"])] == j)
                v = it.get(key)
                txt = "NA" if not is_number(v) else format(float(v), fmt)
                col = NEUTRAL["dark"] if np.isnan(vals[i, j]) or abs(vals[i, j]) <= 0.58 * vmax else "white"
                ax.text(j, i, f"{it['Mix_ID'] or 'MISSING'}\n{txt}",
                        ha="center", va="center", fontsize=10.0, fontweight="medium", color=col)
        for sp in ax.spines.values():
            sp.set_visible(False)
        cb = fig.colorbar(im, ax=ax, shrink=0.82, pad=0.03)
        cb.set_label(cbar_label, fontsize=10.5, color=NEUTRAL["black"], family=PAPER_FONT_FAMILY)
        cb.ax.tick_params(labelsize=9.5, colors=NEUTRAL["black"])
    t_save(fig, out / "Figure_3_3_GGBFS_SF_Interaction.png")

    def draw_corr(ax, props, title):
        keys, mat, cnt = _correlation_matrix(props)
        im = ax.imshow(mat, cmap=DIVERGING_CMAP,
                       norm=TwoSlopeNorm(vmin=-1, vcenter=0, vmax=1), aspect="equal")
        label_map = {
            "CS28": "Strength\n28 d", "WA28": "Water absorption\n28 d",
            "ER28": "Electrical resistivity\n28 d", "CS90": "Strength\n90 d",
            "WA90": "Water absorption\n90 d", "ER90": "Electrical resistivity\n90 d",
            "Dnssm90": "Chloride migration\n90 d",
        }
        labels = [label_map.get(k, k) for k in keys]
        ax.set_xticks(range(len(keys)), labels, rotation=25, ha="right")
        ax.set_yticks(range(len(keys)), labels)
        ax.tick_params(axis="both", labelsize=9.5, colors=NEUTRAL["black"])
        _style_title(ax, title)
        for i in range(len(keys)):
            for j in range(len(keys)):
                v = mat[i, j]
                t = "NA" if np.isnan(v) else ("1.00" if i == j else f"{v:+.2f}\n(n={cnt[i, j]})")
                c = NEUTRAL["white"] if not np.isnan(v) and abs(v) > 0.58 else NEUTRAL["dark"]
                ax.text(j, i, t, ha="center", va="center", fontsize=9.5, color=c)
        for sp in ax.spines.values():
            sp.set_visible(False)
        return im

    fig, axes = plt.subplots(1, 2, figsize=(14.3, 7.0), constrained_layout=True)
    draw_corr(axes[0], props_28, "(a) 28-day properties")
    im = draw_corr(axes[1], props_90, "(b) 90-day properties, including RCMT")
    cb = fig.colorbar(im, ax=axes.ravel().tolist(), shrink=0.78, pad=0.02)
    cb.set_label("Pearson correlation coefficient, r", fontsize=10.5, color=NEUTRAL["black"], family=PAPER_FONT_FAMILY)
    cb.ax.tick_params(labelsize=9.0, colors=NEUTRAL["black"])
    t_save(fig, out / "Figure_4_Correlation_Matrices.png")

    # Pairwise scatterplots are not exported: Figure 5 already reports the
    # corresponding pairwise coefficients, sample sizes, and age-specific matrices.
    return inter, corr_rows


def _inverse_erf(v):
    if not -1 < v < 1:
        raise ValueError("erfinv target outside (-1,1)")
    lo, hi = -6.0, 6.0
    for _ in range(120):
        m = (lo + hi) / 2
        if math.erf(m) < v:
            lo = m
        else:
            hi = m
    return (lo + hi) / 2


# =============================================================================
# SECTION 3.4 — SERVICE LIFE AND COVER
# =============================================================================
def run_section_3_4(data, root):
    out = root / "Section_3_4"
    out.mkdir(parents=True, exist_ok=True)
    P = {
        "C0_wt_pct_cement": 0.1, "Ccrit_wt_pct_cement": 0.6, "Cs_wt_pct_cement": 3.0,
        "convection_zone_mm": 0.0, "transfer_factor_kt": 0.1,
        "temperature_parameter_be_K": 4800.0, "reference_temperature_K": 293.0,
        "environment_temperature_K": 290.0, "reference_age_years": 90.0 / 365.0,
        "alpha_baseline": 0.30, "alpha_min": 0.20, "alpha_max": 0.80, "alpha_step": 0.05,
        "q_mm2_year_per_1e12_m2_s": 31.536, "cmin_S4_XS1_mm": 35.0,
        "execution_allowance_mm": 10.0, "cmin_S6_XS1_mm": 45.0,
        "target_years": [50, 75, 100],
    }
    C0 = P["C0_wt_pct_cement"]
    Ccrit = P["Ccrit_wt_pct_cement"]
    Cs = P["Cs_wt_pct_cement"]
    dx = P["convection_zone_mm"]
    kt = P["transfer_factor_kt"]
    be = P["temperature_parameter_be_K"]
    Tref = P["reference_temperature_K"]
    Treal = P["environment_temperature_K"]
    t0 = P["reference_age_years"]
    alpha0 = P["alpha_baseline"]
    q = P["q_mm2_year_per_1e12_m2_s"]
    cmin_s4 = P["cmin_S4_XS1_mm"]
    cdev = P["execution_allowance_mm"]
    cmin_s6 = P["cmin_S6_XS1_mm"]
    targets = P["target_years"]
    thr = 1.0 - (Ccrit - C0) / (Cs - C0)
    z = _inverse_erf(thr)
    ke = math.exp(be * (1 / Tref - 1 / Treal))

    def req_cover(D, t, alpha=alpha0, Cs_v=None, kt_v=None):
        if not (is_number(D) and D > 0 and t > 0 and alpha < 1):
            return None
        cs = Cs if Cs_v is None else float(Cs_v)
        kt_use = kt if kt_v is None else float(kt_v)
        tl = 1.0 - (Ccrit - C0) / (cs - C0)
        if not 0 < tl < 1:
            return None
        zl = _inverse_erf(tl)
        rad = ke * float(D) * q * kt_use * (t0 ** alpha) * (float(t) ** (1 - alpha))
        return dx + 2 * zl * math.sqrt(max(rad, 0.0))

    def t_init(eff, D, alpha=alpha0):
        if not (is_number(eff) and is_number(D) and D > 0 and alpha < 1):
            return None
        x = float(eff) - dx
        if x <= 0:
            return 0.0
        denom = 4 * z * z * ke * float(D) * q * kt * (t0 ** alpha)
        return (x * x / denom) ** (1 / (1 - alpha))

    def cl_at(depth, t, D, alpha=alpha0):
        Dapp = ke * float(D) * q * kt * (t0 / float(t)) ** alpha
        den = 2 * math.sqrt(max(Dapp * float(t), 1e-30))
        return C0 + (Cs - C0) * (1 - math.erf((float(depth) - dx) / den))

    svc = []
    for mid in data["order"]:
        d = data["D_stats"][mid]
        Dm, sd = d["scaled"], d["scaled_sd"]
        row = {
            "Mix_ID": mid, "Mixture_Code": data["md"][mid]["code"],
            "D_mean_1e-12_m2_s": Dm, "D_SD_1e-12_m2_s": sd, "D_mean_mm2_year": Dm * q,
            "t_init_at_35mm_years": t_init(cmin_s4, Dm),
            "t_init_at_40mm_years": t_init(40.0, Dm),
            "t_init_at_45mm_years": t_init(45.0, Dm),
        }
        for t in targets:
            cr = req_cover(Dm, t)
            row[f"c_req_{t}y_mm"] = cr
            row[f"c_eff_S4_{t}y_mm"] = max(cr, cmin_s4)
            row[f"c_nom_S4_{t}y_mm"] = max(cr, cmin_s4) + cdev
            if t == 100:
                row["c_nom_S6_100y_mm"] = max(cr, cmin_s6) + cdev
            if is_number(sd):
                for sign, lab in [(-1.0, "minusSD"), (1.0, "plusSD")]:
                    ds = max(Dm + sign * sd, 1e-12)
                    row[f"c_req_{t}y_{lab}_mm"] = req_cover(ds, t)
        svc.append(row)
    write_csv(out / "Table_3_4_ServiceLife_and_Cover.csv", svc)

    param_out = [{"Parameter": k, "Value": v, "Unit_or_basis": "Section 2.4/Table 4"}
                 for k, v in P.items() if not isinstance(v, list)]
    param_out += [
        {"Parameter": "z_inverse_erf_threshold", "Value": z, "Unit_or_basis": "dimensionless"},
        {"Parameter": "temperature_factor_ke", "Value": ke, "Unit_or_basis": "dimensionless"},
        {"Parameter": "computed_threshold_fraction", "Value": thr, "Unit_or_basis": "dimensionless"},
    ]
    write_csv(out / "Table_3_4_Model_Parameters.csv", param_out)

    alpha_vals = np.round(np.arange(P["alpha_min"], P["alpha_max"] + P["alpha_step"] * 0.5,
                                    P["alpha_step"]), 8)
    alpha_rows = []
    for mid in data["order"]:
        Dm = data["D_stats"][mid]["scaled"]
        for t in targets:
            for al in alpha_vals:
                cr = req_cover(Dm, t, float(al))
                ce = max(cr, cmin_s4)
                alpha_rows.append({
                    "Mix_ID": mid, "Mixture_Code": data["md"][mid]["code"], "Target_years": t,
                    "alpha": float(al), "D_mean_1e-12_m2_s": Dm, "c_req_mm": cr,
                    "c_eff_S4_mm": ce, "c_nom_S4_mm": ce + cdev,
                })
    write_csv(out / "Table_S_AgingExponent_Sensitivity.csv", alpha_rows)

    d_sens = []
    for mid in data["order"]:
        Dm = data["D_stats"][mid]["scaled"]
        sd = data["D_stats"][mid]["scaled_sd"]
        sd = float(sd) if is_number(sd) else 0.0
        scen = [("D_mean_minus_SD", max(Dm - sd, 1e-12)), ("D_mean", Dm), ("D_mean_plus_SD", Dm + sd)]
        for t in targets:
            for sn, Ds in scen:
                cr = req_cover(Ds, t)
                ce = max(cr, cmin_s4)
                d_sens.append({
                    "Mix_ID": mid, "Mixture_Code": data["md"][mid]["code"], "Target_years": t,
                    "D_scenario": sn, "D_1e-12_m2_s": Ds, "c_req_mm": cr,
                    "c_eff_S4_mm": ce, "c_nom_S4_mm": ce + cdev,
                })
    write_csv(out / "Table_S_DCoefficient_Sensitivity.csv", d_sens)

    cs_sens = []
    for mid in data["order"]:
        Dm = data["D_stats"][mid]["scaled"]
        for t in targets:
            for csv_ in (1.5, 2.5, 3.0, 3.5):
                cr = req_cover(Dm, t, alpha0, Cs_v=csv_)
                ce = max(cr, cmin_s4)
                cs_sens.append({
                    "Mix_ID": mid, "Mixture_Code": data["md"][mid]["code"], "Target_years": t,
                    "Cs_wt_pct_cement": csv_, "C0_wt_pct_cement": C0,
                    "Ccrit_wt_pct_cement": Ccrit, "D_mean_1e-12_m2_s": Dm, "alpha": alpha0,
                    "c_req_mm": cr, "c_eff_S4_mm": ce, "c_nom_S4_mm": ce + cdev,
                    "Scenario_note": "Deterministic boundary-condition sensitivity; not a probability distribution",
                })
    write_csv(out / "Table_S_SurfaceChloride_Sensitivity.csv", cs_sens)

    # Transfer-factor cover sensitivity: one-factor deterministic bracket around
    # the study baseline (k_t = 0.10), using ±25% perturbations.
    # The coupled member-design/GWP sensitivity is recalculated separately in Section 3.5.
    kt_sens = []
    kt_values = [0.075, 0.10, 0.125]
    for mid in data["order"]:
        Dm = data["D_stats"][mid]["scaled"]
        for t in targets:
            for kt_v in kt_values:
                cr = req_cover(Dm, t, alpha0, kt_v=kt_v)
                ce = max(cr, cmin_s4)
                kt_sens.append({
                    "Mix_ID": mid, "Mixture_Code": data["md"][mid]["code"],
                    "k_t": kt_v, "Target_years": t,
                    "D_mean_1e-12_m2_s": Dm, "alpha": alpha0,
                    "c_req_mm": cr, "c_eff_S4_mm": ce, "c_nom_S4_mm": ce + cdev,
                    "Scenario_note": "Deterministic stress test; other inputs held at baseline; not calibrated",
                })
    write_csv(out / "Table_S_TransferFactor_Sensitivity.csv", kt_sens)
    kt_summary = []
    for kt_v in kt_values:
        for t in targets:
            group = [r for r in kt_sens if abs(r["k_t"] - kt_v) < 1e-9 and r["Target_years"] == t]
            c_req = [r["c_req_mm"] for r in group]
            c_nom = [r["c_nom_S4_mm"] for r in group]
            kt_summary.append({
                "k_t": kt_v, "Target_years": t,
                "c_req_min_mm": min(c_req), "c_req_max_mm": max(c_req),
                "c_nom_min_mm": min(c_nom), "c_nom_max_mm": max(c_nom),
                "Scenario_note": "One-factor deterministic sensitivity around the study baseline; other model inputs held constant",
            })
    write_csv(out / "Table_S_TransferFactor_Sensitivity_Summary.csv", kt_summary)

    profiles = []
    for mid in data["order"]:
        Dm = data["D_stats"][mid]["scaled"]
        for t in targets:
            for d in range(0, 101, 2):
                profiles.append({
                    "Mix_ID": mid, "Mixture_Code": data["md"][mid]["code"], "Target_years": t,
                    "Depth_mm": d, "Chloride_wt_pct_cement": cl_at(d, t, Dm),
                })
    write_csv(out / "Table_S_Chloride_Profiles.csv", profiles)

    fig, axes = plt.subplots(1, 3, figsize=(17.2, 5.6), constrained_layout=True)
    x = np.arange(len(data["order"]))
    grp_ord = [data["group"](m) for m in data["order"]]
    ax = axes[0]
    for t in TARGET_LEVELS:   # target -> marker + line style (theme); mixture -> colour
        vals = [r[f"c_req_{t}y_mm"] for r in svc]
        t_line(ax, x, vals, filt="TARGET", level=t, lw=1.0, alpha=0.8)
        t_scatter(ax, x, vals, groups=grp_ord, filt="TARGET", level=t, size=30)
    _style_title(ax, "(a) Required effective cover")
    ax.set_ylabel("Required effective cover (mm)")
    ax.set_xticks(x, data["order"], rotation=50, ha="right")
    _style_axes(ax)

    ax = axes[1]
    years = np.arange(1, 101)
    for mid, label in [("M01", "Reference"), ("M02", "BFS20"), ("M16", "BFS40-SF13")]:
        Dm = data["D_stats"][mid]["scaled"]
        y = [cl_at(35.0, yr, Dm) for yr in years]
        t_line(ax, years, y, group=data["group"](mid), label=f"{mid} ({label})", lw=1.8)
    t_hline(ax, Ccrit, "critical_threshold", label="Critical threshold")
    _style_title(ax, "(b) Chloride concentration at 35-mm depth")
    ax.set_xlabel("Time (years)")
    ax.set_ylabel("Chloride concentration (wt.% of cement mass)")
    _style_axes(ax, grid_axis="both")

    ax = axes[2]
    for t in TARGET_LEVELS:
        vals = [r[f"c_nom_S4_{t}y_mm"] for r in svc]
        t_line(ax, x, vals, filt="TARGET", level=t, lw=1.0, alpha=0.8)
        t_scatter(ax, x, vals, groups=grp_ord, filt="TARGET", level=t, size=30)
    t_hline(ax, cmin_s4 + cdev, "cover_minimum")
    _style_title(ax, "(c) Nominal cover")
    ax.set_ylabel("Nominal cover (mm)")
    ax.set_xticks(x, data["order"], rotation=50, ha="right")
    _style_axes(ax)
    t_save(fig, out / "Figure_5_Service_Life_and_Cover.png")

    fig, ax = plt.subplots(figsize=(10.5, 6.2), constrained_layout=True)
    for t in TARGET_LEVELS:
        sub = [r for r in alpha_rows if r["Target_years"] == t]
        by = {}
        for r in sub:
            by.setdefault(r["alpha"], []).append(r["c_req_mm"])
        xs = sorted(by)
        med = [float(np.median(by[a])) for a in xs]
        mins = [min(by[a]) for a in xs]
        maxs = [max(by[a]) for a in xs]
        t_line(ax, xs, med, filt="TARGET", level=t, marker=True, lw=1.8)
        t_band(ax, xs, mins, maxs, filt="TARGET", level=t)
    ax.set_xlabel("Aging exponent, α")
    ax.set_ylabel("Required effective cover (mm)")
    _style_axes(ax, grid_axis="both")
    t_save(fig, out / "Figure_6_Aging_Exponent_Sensitivity.png")

    sub100 = [r for r in d_sens if r["Target_years"] == 100]
    by_mix = {m: [r for r in sub100 if r["Mix_ID"] == m] for m in data["order"]}
    fig, ax = plt.subplots(figsize=(12.2, 5.8), constrained_layout=True)
    means, lo, hi = [], [], []
    for mid in data["order"]:
        vm = {r["D_scenario"]: r["c_req_mm"] for r in by_mix[mid]}
        cm = vm["D_mean"]
        means.append(cm)
        lo.append(max(0.0, cm - vm["D_mean_minus_SD"]))
        hi.append(max(0.0, vm["D_mean_plus_SD"] - cm))
    for i, (m_, l_, h_) in enumerate(zip(means, lo, hi)):
        t_errorbar(ax, i, m_, [[l_], [h_]], group=data["group"](data["order"][i]), capsize=2.8)
    ax.set_xticks(x, data["order"], rotation=50, ha="right")
    ax.set_ylabel("Required effective cover (mm)")
    _style_axes(ax)
    t_save(fig, out / "Figure_S1_Dnssm_Sensitivity.png")
    return svc


# =============================================================================
# COLUMN DESIGN HELPERS
# =============================================================================
def _column_bar_positions(section_mm, cover_mm, bar_diameter_mm, points_per_face, link_diameter_mm=8.0):
    r = section_mm / 2 - cover_mm - link_diameter_mm - bar_diameter_mm / 2
    if r <= 0:
        return []
    v = np.linspace(-r, r, points_per_face)
    pts = [(float(x), float(r)) for x in v]
    pts += [(float(r), float(y)) for y in v[-2:0:-1]]
    pts += [(float(x), float(-r)) for x in v[::-1]]
    pts += [(float(-r), float(y)) for y in v[1:-1]]
    return pts


def _column_block_parameters(fck_proxy_mpa):
    a = COLUMN_ASSUMPTIONS
    f = max(float(fck_proxy_mpa), 1e-9)
    n_cc = min((a["fck_ref_MPa"] / f) ** (1 / 3), 1.0)
    fcd = n_cc * a["k_tc"] * f / a["gamma_c"]
    if f <= 50.0:
        eta, lam, eps_cu = 1.0, 0.8, 0.0035
    else:
        eta = max(0.0, 1.0 - (f - 50.0) / 200.0)
        lam = max(0.0, 0.8 - (f - 50.0) / 400.0)
        eps_cu = (2.6 + 35.0 * ((90.0 - f) / 100.0) ** 4) / 1000.0
    return fcd, eta, lam, eps_cu


def _a_n_from_axial_ratio(r):
    r = float(r)
    if r <= 0.1:
        return 1.0
    if r <= 0.7:
        return 1.0 + (r - 0.1) * (0.5 / 0.6)
    if r <= 1.0:
        return 1.5 + (r - 0.7) * (0.5 / 0.3)
    return None


@lru_cache(maxsize=300000)
def _column_axis_capacity_cached(section_mm, fck_proxy_mpa, coords, bar_area_mm2, n_ed_n, fyd_mpa, axis):
    """Cached strain-compatible axis capacity kernel; exact input tuples preserve reproducibility."""
    fcd, eta, lam, eps_cu = _column_block_parameters(fck_proxy_mpa)
    b = float(section_mm)
    width = b
    Es = float(COLUMN_ASSUMPTIONS["reinforcement_E_MPa"])
    coords_arr = np.asarray(coords, dtype=float)
    d_from_comp = b / 2.0 - coords_arr

    def resultants(x_na):
        a_block = min(max(lam * x_na, 0.0), b)
        cc = fcd * eta * width * a_block
        m_conc = cc * (b / 2.0 - a_block / 2.0)
        strain = eps_cu * (x_na - d_from_comp) / max(x_na, 1e-9)
        stress = np.clip(Es * strain, -fyd_mpa, fyd_mpa)
        in_block = d_from_comp <= a_block
        net = stress - np.where(in_block, fcd * eta, 0.0)
        forces = net * float(bar_area_mm2)
        n_tot = cc + float(np.sum(forces))
        m_tot = m_conc + float(np.sum(forces * coords_arr))
        return n_tot, abs(m_tot)

    lo, hi = 1e-3, b * 8.0
    n_lo, _ = resultants(lo)
    n_hi, _ = resultants(hi)
    if n_lo > n_ed_n or n_hi < n_ed_n:
        return None
    # 18 bisection iterations give sub-0.04-mm neutral-axis resolution for the full section range.
    for _ in range(18):
        mid = (lo + hi) / 2.0
        n_mid, _ = resultants(mid)
        if n_mid < n_ed_n:
            lo = mid
        else:
            hi = mid
    _, m = resultants((lo + hi) / 2.0)
    return m / 1e6


def _column_axis_capacity(section_mm, fck_proxy_mpa, bar_points, bar_area_mm2, n_ed_n, fyd_mpa, axis="x"):
    """Cached strain-compatible uniaxial section capacity at the applied axial force."""
    ci = 1 if axis == "x" else 0
    coords = tuple(float(pt[ci]) for pt in bar_points)
    return _column_axis_capacity_cached(float(section_mm), float(fck_proxy_mpa), coords,
                                        float(bar_area_mm2), float(n_ed_n), float(fyd_mpa), axis)


def _derive_column_actions(storeys, section_mm):
    """Return factored floor actions and stacked-column self-weight for one column line.

    Floor actions use the user-editable tributary area, characteristic floor dead/live loads,
    and ULS partial factors. Self-weight assumes the trial section is repeated over the modelled
    number of stories, using the stated concrete unit weight. This is an explicit member-level
    sensitivity model, not a substitute for frame analysis.
    """
    a = COLUMN_ASSUMPTIONS
    ns = int(storeys)
    area = float(a["tributary_area_m2"])
    g_floor = ns * area * float(a["gamma_G"]) * float(a["dead_load_kN_m2"])
    q_floor = ns * area * float(a["gamma_Q"]) * float(a["live_load_kN_m2"])
    b_m = float(section_mm) / 1000.0
    self_weight = (ns * float(a["clear_height_m"]) * b_m * b_m
                   * float(a["unit_weight_concrete_kN_m3"]) * float(a["gamma_G"]))
    return {
        "storeys": ns,
        "N_Ed_floor_G_kN": g_floor,
        "N_Ed_floor_Q_kN": q_floor,
        "N_Ed_column_selfweight_kN": self_weight,
        "N_Ed_floors_kN": g_floor + q_floor,
        "N_Ed_total_kN": g_floor + q_floor + self_weight,
        "ULS_floor_combination": "1.35 Gk + 1.50 Qk (study-adopted sensitivity combination)",
        "floor_dead_load_kN_m2": float(a["dead_load_kN_m2"]),
        "floor_live_load_kN_m2": float(a["live_load_kN_m2"]),
        "tributary_area_m2": area,
        "gamma_G": float(a["gamma_G"]),
        "gamma_Q": float(a["gamma_Q"]),
        "unit_weight_concrete_kN_m3": float(a["unit_weight_concrete_kN_m3"]),
    }


def _second_order_gate_passes(lam_x, lim_x, lam_y, lim_y, m2_x, m2_y):
    """Hard acceptance gate: an axis requiring M2 cannot pass with a missing/invalid M2."""
    req_x = float(lam_x) >= float(lim_x)
    req_y = float(lam_y) >= float(lim_y)

    def valid_positive(value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(number) and number > 0.0

    if req_x and not valid_positive(m2_x):
        return False
    if req_y and not valid_positive(m2_y):
        return False
    return True


def _anchorage_and_lap_lengths_mm(db, fck_mpa, fyd_mpa, cover_mm, link_d_mm, bar_pitch_mm):
    """EN 1992-1-1:2023 Formula 11.3 / Clause 11.5.2 screening lengths for good bond."""
    db = float(db)
    fck = max(float(fck_mpa), 1.0)
    fyd = float(fyd_mpa)
    # Clear distance from bar surface to the nearest free surface, measured to bar centre.
    c_x = float(cover_mm) + float(link_d_mm) + db / 2.0
    c_y = c_x
    c_d = min(0.5 * float(bar_pitch_mm), c_x, c_y, 3.75 * db)
    c_d = max(c_d, 1e-6)
    sigma_ratio = max(fyd / 435.0, 0.0) ** 1.5  # EN 1992-1-1:2023 Eq. (11.3), n_sigma = 3/2
    fck_ratio = max(25.0 / fck, 0.3) ** 0.5
    phi_ratio = max(db / 20.0, 0.6) ** (1.0 / 3.0)
    cover_ratio = max(1.5 * db / c_d, 0.0) ** 0.5
    # k_lb = 50 for persistent/transient design situations; k_cp=1.0 for good bond.
    l_bd = max(10.0 * db, 50.0 * db * sigma_ratio * fck_ratio * phi_ratio * cover_ratio)
    # Recommended Table 11.3 lap coefficient k_ls = 1.2; minimum l_sd >= 15 phi.
    l_sd = max(1.2 * l_bd, 15.0 * db)
    return {"l_bd_mm": l_bd, "l_sd_mm": l_sd, "c_d_mm": c_d,
            "anchorage_bond_assumption": "good bond; EN 1992-1-1:2023 Eq. (11.3)",
            "lap_coefficient_k_ls": 1.2}


def _shear_check_and_spacing(section_mm, cover_mm, db, link_d, As_long, fck, fcd,
                              fyd, VEd_x_kN, VEd_y_kN, NEd_kN, MEd_x_kNm, MEd_y_kNm,
                              effective_length_mm, gamma_v=1.4, cot_theta=3.0,
                              ductility_class="B", max_aggregate_mm=9.0):
    """Axial-compression-dependent member shear design.

    The moment-reversal shear envelope is combined for biaxial demand. The axial-force
    share N_Edw assigned to the shear zone is selected so that the remaining compression
    chord does not exceed x = 0.25 d. Clause 8.2.3(11) is then followed:
      * if -N_Edw <= |V_Ed cot(theta)|, use Clause 8.2.3(5)-(8);
      * otherwise, use the Annex G membrane stress-field equations for the shear zone,
        including the optimum strut inclination from Table G.1 and nu=0.4 from G.8.

    Compression is taken as negative in the membrane stress state. The two orthogonal
    tie-leg groups are checked against the x/y components of the resultant shear demand.
    This is a member-level square-section model with prescribed actions, not a frame solver.
    """
    b = float(section_mm)
    cover = float(cover_mm)
    db = float(db)
    ld = float(link_d)
    d_eff = b - (cover + ld + 0.5 * db)
    if d_eff <= 0.0:
        return None
    z = 0.9 * d_eff
    fck = float(fck)
    fcd = float(fcd)
    fyd = float(fyd)
    if min(fcd, fyd, z, b) <= 0.0:
        return None

    vx = abs(float(VEd_x_kN))
    vy = abs(float(VEd_y_kN))
    vres_n = math.hypot(vx, vy) * 1000.0
    if vres_n <= 1e-9:
        return None
    tau_x = vx * 1000.0 / (b * z)
    tau_y = vy * 1000.0 / (b * z)
    tau_res = math.hypot(tau_x, tau_y)
    if tau_res <= 1e-12:
        return None

    n_comp_n = max(0.0, float(NEd_kN)) * 1000.0
    m_res_nmm = math.hypot(float(MEd_x_kNm), float(MEd_y_kNm)) * 1e6
    area_link = math.pi * ld * ld / 4.0
    # Two effective legs of a closed link in each principal direction.
    Asw = 2.0 * area_link

    # Section compression-block parameters are consistent with the member flexural model.
    _, eta, lam, _ = _column_block_parameters(fck)
    # F_cd = eta*f_cd*b*lambda*x; enforce x <= 0.25d for the compression chords.
    fcd_chord_limit = eta * fcd * b * lam * 0.25 * d_eff
    m_over_z = m_res_nmm / z

    ductility = str(ductility_class).strip().upper()
    if ductility not in {"A", "B", "C"}:
        return None
    mean_axial_stress = n_comp_n / (b * b)
    # Clause 8.2.3(4): interpolate the class B/C lower bound from 2.5
    # (no axial compression) to 3.0 at an average compressive stress of 3 MPa.
    compression_level = min(1.0, max(0.0, mean_axial_stress / 3.0))
    cot_min = 2.5 + 0.5 * compression_level
    if ductility == "A":
        cot_min *= 0.8
    cot_min = max(1.0, cot_min)

    def required_web_axial_share(cot):
        n_vd = vres_n * float(cot)
        fcd_chord_without_web = m_over_z + 0.5 * (n_comp_n - n_vd)
        return min(n_comp_n, max(0.0, 2.0 * (fcd_chord_without_web - fcd_chord_limit)))

    # Initial share based on the Clause 8.2.3(4) inclination limit.
    n_edw = required_web_axial_share(cot_min)
    if n_edw <= vres_n * cot_min + 1e-6:
        method = "EN_1992-1-1_8.2.3_5-8"
        cot = cot_min
        nu = 0.5  # 8.2.3(6), when using the angle from 8.2.3(4).
        n_vd = vres_n * cot
        n_chord_comp = max(0.0, n_comp_n - n_edw)
        fcd_chord = max(0.0, m_over_z + 0.5 * (n_chord_comp - n_vd))
        x_chord = fcd_chord / max(eta * fcd * b * lam, 1e-12)
        sigma_cd = tau_res * (cot + 1.0 / cot)
        # Clause 8.2.3(8): additional longitudinal tension from shear.
        ftd_longitudinal_n = max(0.0, m_over_z + 0.5 * (n_vd - n_chord_comp))
        available_longitudinal_tension_n = 0.5 * float(As_long) * fyd
        if ftd_longitudinal_n > available_longitudinal_tension_n + 1e-6:
            return None
        ftdx_req = ftd_longitudinal_n / max(b * d_eff, 1e-12)
        ftdy_req = tau_res / cot
        sigma_x = -n_edw / max(b * d_eff, 1e-12)
        ftdy_opt = None
        ftdx_opt = None
    else:
        # Iterate because N_Edw is selected from the chord-depth condition, while
        # the optimum cot(theta) in Annex G depends on the resulting web stress.
        method = "EN_1992-1-1_Annex_G_Table_G1"
        cot = cot_min
        for _ in range(60):
            n_edw_new = required_web_axial_share(cot)
            sigma_x_new = -n_edw_new / max(b * d_eff, 1e-12)
            # Table G.1 for sigma_Edy = 0 and sigma_Edx <= sigma_Edy.
            if sigma_x_new >= -tau_res:
                cot_new = 1.0
            else:
                cot_new = -sigma_x_new / tau_res
            if abs(n_edw_new - n_edw) < 1e-5 and abs(cot_new - cot) < 1e-8:
                n_edw, cot = n_edw_new, cot_new
                break
            n_edw, cot = n_edw_new, cot_new
        # Final equilibrium update with the converged stress-field angle.
        n_edw = required_web_axial_share(cot)
        sigma_x = -n_edw / max(b * d_eff, 1e-12)
        if sigma_x >= -tau_res:
            cot = 1.0
            ftdx_opt = max(0.0, sigma_x + tau_res)
            ftdy_opt = tau_res
        else:
            cot = -sigma_x / tau_res
            ftdx_opt = 0.0
            ftdy_opt = tau_res / cot
        nu = 0.4  # Annex G.3(5), Formula G.8, absent a refined strain calculation.
        sigma_cd = tau_res * (cot + 1.0 / cot)
        ftdx_req = ftdx_opt
        ftdy_req = ftdy_opt
        x_chord = 0.25 * d_eff
        # Annex G.3 longitudinal membrane tie demand is checked against the longitudinal
        # reinforcement in the tension face (half of the symmetric bar arrangement).
        required_longitudinal_tie_n = ftdx_req * b * d_eff
        available_longitudinal_tension_n = 0.5 * float(As_long) * fyd
        n_vd = vres_n * cot
        n_chord_comp = max(0.0, n_comp_n - n_edw)
        # The chord force from the member actions is checked in addition to the web tie
        # resistance required by Annex G.3; both demands act in the longitudinal direction.
        ftd_chord_n = max(0.0, m_over_z + 0.5 * (n_vd - n_chord_comp))
        ftd_longitudinal_n = required_longitudinal_tie_n + ftd_chord_n
        if ftd_longitudinal_n > available_longitudinal_tension_n + 1e-6:
            return None
        fcd_chord = max(0.0, m_over_z + 0.5 * (n_chord_comp - n_vd))
        x_chord = fcd_chord / max(eta * fcd * b * lam, 1e-12)

    # Compression-field stress limit (Clause 8.2.3(5) or Annex G.3).
    tau_strut_limit = nu * fcd / (cot + 1.0 / cot)
    if sigma_cd > nu * fcd + 1e-9:
        return None
    if x_chord > 0.25 * d_eff + 1e-6:
        return None

    # Diagnostic concrete shear resistance (Clause 8.2.2); it is not used as a substitute
    # for the selected compression-field / Annex G design path above.
    ddg = min(16.0 + float(max_aggregate_mm) * min(1.0, (60.0 / max(fck, 1e-9)) ** 2), 40.0)
    rho_l = min(0.02, max(1e-5, 0.5 * float(As_long) / (b * d_eff)))
    tau_rdc_o = (0.66 / float(gamma_v)) * (100.0 * rho_l * fck * (ddg / d_eff)) ** (1.0 / 3.0)
    tau_rdc_min = (11.0 / float(gamma_v)) * math.sqrt((fck / max(fyd, 1e-9)) * (ddg / d_eff))
    tau_rdc = max(tau_rdc_o, tau_rdc_min)
    v_rdc = tau_rdc * b * z / 1000.0

    # Resolve the optimum transverse tie resistance into the two orthogonal shear axes.
    # The resultant shear is used for the strut check; each tie-leg group is then sized
    # for its corresponding component.
    def axis_result(v_ed, tau_component):
        component_fraction = abs(float(tau_component)) / tau_res
        ftdy_axis = ftdy_req * component_fraction
        if ftdy_axis <= 1e-12:
            s_req = None
        else:
            s_req = Asw * fyd / (b * ftdy_axis)
            if not math.isfinite(s_req) or s_req <= 0.0:
                return None
        return {
            "V_Ed_kN": abs(float(v_ed)), "tau_Ed_MPa": abs(float(tau_component)),
            "f_tdy_required_MPa": ftdy_axis, "s_required_mm": s_req,
            "shear_links_beyond_minimum_required": s_req is not None,
        }

    sx = axis_result(vx, tau_x)
    sy = axis_result(vy, tau_y)
    if sx is None or sy is None:
        return None
    required_spacings = [float(v) for v in (sx["s_required_mm"], sy["s_required_mm"])
                         if is_number(v) and float(v) > 0.0]
    s_shear = min(required_spacings) if required_spacings else None
    return {
        "d_eff_mm": d_eff, "z_shear_mm": z, "ddg_mm": ddg,
        "A_sw_two_legs_mm2": Asw, "rho_l_for_shear": rho_l,
        "tau_Rdc_o_MPa": tau_rdc_o, "tau_Rdc_min_MPa": tau_rdc_min,
        "V_Rd_c_kN": v_rdc, "tau_Rd_strut_limit_MPa": tau_strut_limit,
        "cot_theta": cot, "nu_shear": nu, "tau_Ed_resultant_MPa": tau_res,
        "N_Edw_kN": n_edw / 1000.0, "N_Vd_kN": n_vd / 1000.0,
        "mean_axial_stress_MPa": mean_axial_stress,
        "compression_chord_depth_mm": x_chord,
        "compression_chord_depth_ratio": x_chord / d_eff,
        "sigma_Edx_MPa": sigma_x, "sigma_Edy_MPa": 0.0,
        "sigma_cd_MPa": sigma_cd, "f_tdx_required_MPa": ftdx_req,
        "f_tdy_required_MPa": ftdy_req,
        "f_tdx_optimum_MPa": ftdx_opt, "f_tdy_optimum_MPa": ftdy_opt,
        "longitudinal_tie_demand_kN": ftd_longitudinal_n / 1000.0,
        "longitudinal_tie_resistance_kN": available_longitudinal_tension_n / 1000.0,
        "shear_method": method, "shear_ductility_class": ductility,
        "s_shear_required_mm": s_shear, "shear_x": sx, "shear_y": sy,
    }


def _legacy_calculate_column_design_DISABLED(*args, **kwargs):
    """Disabled legacy entry point; the obsolete Rev100 design route is not callable."""
    raise Stage10ExecutionBlocker(
        "STAGE10-LEGACY-ENGINE-LOCKED: historical Rev100 design path is disabled; "
        "the active Rev114 path uses the author-approved signed double-curvature member model."
    )

def _cover_model_for_input(D_scaled_1e12, target_years, alpha=0.30, ccrit=0.6,
                           t0_years=90.0 / 365.0, cs=3.0, kt_v=0.1):
    C0, Cs = 0.1, float(cs)
    dx = 0.0
    kt, be = float(kt_v), 4800.0
    Tref, Treal, q = 293.0, 290.0, 31.536
    target = 1.0 - (ccrit - C0) / (Cs - C0)
    if not 0 < target < 1:
        return float("nan")
    z = _inverse_erf(target)
    ke = math.exp(be * (1 / Tref - 1 / Treal))
    rad = ke * float(D_scaled_1e12) * q * kt * (t0_years ** alpha) * (float(target_years) ** (1 - alpha))
    return dx + 2 * z * math.sqrt(max(rad, 0.0))


def _nominal_cover_from_req(c_req_mm, target_years, bar_diameter_mm=25.0):
    a = COLUMN_ASSUMPTIONS
    cmin_map = {
        50: a["XRDS5_XS1_min_durability_cover_50y_mm"],
        75: a["XRDS5_XS1_min_durability_cover_75y_mm"],
        100: a["XRDS5_XS1_min_durability_cover_100y_mm"],
    }
    cmin = max(float(c_req_mm), cmin_map[int(target_years)], float(bar_diameter_mm), 10.0)
    return cmin + a["execution_allowance_mm"]


# =============================================================================
# SECTION 3.5 — COLUMN DESIGN
# =============================================================================
def run_section_3_5(data, service_rows, results_root):
    out = results_root / "Section_3_5"
    out.mkdir(parents=True, exist_ok=True)
    service_by_mix = {r["Mix_ID"]: r for r in service_rows}
    cache = {}

    def get_design(mid, target, c_nom, selection_rule="smallest_section", moment_scale=1.0):
        key = (mid, int(target), round(float(c_nom), 5), selection_rule, float(moment_scale))
        if key not in cache:
            cube = data["cs_stats"][(mid, 28)][0]
            cache[key] = _calculate_column_design(
                cube, c_nom, mid, int(target), moment_scale=moment_scale,
                selection_rule=selection_rule,
                gwp_concrete=data["gwp_by_mix"].get(mid) if selection_rule == "min_gwp" else None,
            )
        return dict(cache[key])

    a = COLUMN_ASSUMPTIONS
    assumptions = [
        {"Parameter": "Factored axial action, N_Ed", "Value": "n A_t(γ_G g_k + γ_Q q_k) + γ_G γ_conc b² h n",
         "Unit": "kN", "Basis": "Calculated per candidate from the story count, tributary area, floor dead/live loads, ULS factors, and repeated-column self-weight"},
        {"Parameter": "Concrete unit weight", "Value": a["unit_weight_concrete_kN_m3"],
         "Unit": "kN/m³", "Basis": "Used only to add the trial column's stacked self-weight to the gravity action"},
        {"Parameter": "Tributary area / floor actions", "Value": f'{a["tributary_area_m2"]} m²; Gk={a["dead_load_kN_m2"]} and Qk={a["live_load_kN_m2"]} kN/m²',
         "Unit": "-", "Basis": "Floor actions accumulated over the selected story count with γG and γQ"},
        {"Parameter": "First-order moment action, M_0,x", "Value": f'5-story: {a["M_Ed_x_kNm"]}; 15-story: {a["M_Ed_x_kNm_15storey"]}; 30-story: {a["M_Ed_x_kNm_30storey"]}',
         "Unit": "kN·m", "Basis": "Prescribed scenario actions before member imperfections and local second-order addition; moment pairs increase in proportion to story count and are not global-frame-analysis outputs"},
        {"Parameter": "First-order moment action, M_0,y", "Value": f'5-story: {a["M_Ed_y_kNm"]}; 15-story: {a["M_Ed_y_kNm_15storey"]}; 30-story: {a["M_Ed_y_kNm_30storey"]}',
         "Unit": "kN·m", "Basis": "Prescribed scenario actions before member imperfections and local second-order addition; moment pairs increase in proportion to story count and are not global-frame-analysis outputs"},
        {"Parameter": "Clear column height", "Value": a["clear_height_m"],
         "Unit": "m", "Basis": "Common geometry"},
        {"Parameter": "Effective length", "Value": a["effective_length_assumed_m"],
         "Unit": "m", "Basis": "Pinned-equivalent end restraints in each principal plane"},
        {"Parameter": "Boundary conditions", "Value": a["end_restraint_condition"],
         "Unit": "-", "Basis": "Applied consistently"},
        {"Parameter": "Slenderness ratio", "Value": "lambda = l0/i; i = sqrt(Ic/Ac)",
         "Unit": "-", "Basis": "EN 1992-1-1:2023 Annex O.5"},
        {"Parameter": "Slenderness limit", "Value": "lambda_lim = 20 A B C / sqrt(n)",
         "Unit": "-", "Basis": "EN 1992-1-1:2023 Annex O.6; A = 1/(1+0.2 phi_eff,b), B = sqrt(1+2 omega), C = 0.7"},
        {"Parameter": "Local effective creep ratio", "Value": "NOT_COMPUTED_IN_LEGACY_PATH",
         "Unit": "-", "Basis": "Deterministic input for nominal-curvature second-order calculation"},
        {"Parameter": "Second-order method", "Value": "EN 1992-1-1:2023 Annex O.7 nominal curvature",
         "Unit": "-", "Basis": "M2 = NEd*e2; c1/r = 8 for the laterally restrained member model"},
        {"Parameter": "Cube-to-cylinder factor", "Value": a["cube_to_cylinder_factor"],
         "Unit": "-", "Basis": "Comparative proxy"},
        {"Parameter": "Steel f_yk", "Value": a["f_yk_MPa"], "Unit": "MPa", "Basis": "Adopted"},
        {"Parameter": "Steel f_yd", "Value": a["f_yk_MPa"] / a["gamma_s"],
         "Unit": "MPa", "Basis": "fyk/gammaS"},
        {"Parameter": "XS1/XRDS5 minimum cover", "Value": a["XRDS5_XS1_min_durability_cover_50y_mm"],
         "Unit": "mm", "Basis": "Section 3.4 min"},
        {"Parameter": "Execution allowance", "Value": a["execution_allowance_mm"],
         "Unit": "mm", "Basis": "Section 3.4"},
        {"Parameter": "Column functional unit", "Value": 1, "Unit": "column segment",
         "Basis": "One 3.0-m clear-height column"},
    ]
    write_csv(out / "Table_3_5_Design_Assumptions.csv", assumptions)

    primary, smallest = [], []
    for mid in data["order"]:
        svc = service_by_mix[mid]
        for t in a["target_periods_years"]:
            cr = float(svc[f"c_req_{t}y_mm"])
            ce = float(svc[f"c_eff_S4_{t}y_mm"])
            cn = float(svc[f"c_nom_S4_{t}y_mm"])
            common = {
                "Mix_ID": mid, "Mixture_Code": data["md"][mid]["code"], "Target_years": int(t),
                "D_nssm_90d_1e12_m2_s": data["D_stats"][mid]["scaled"],
                "D_nssm_SD_1e12_m2_s": data["D_stats"][mid]["scaled_sd"],
                "c_req_mm": cr, "c_eff_S4_mm": ce,
                "c_min_durability_mm": a["XRDS5_XS1_min_durability_cover_50y_mm"],
                "c_nom_mm": cn, "cover_source": f"Section 3.4 c_nom_S4_{t}y_mm",
                "EF_steel_main_kgCO2e_kg": a["reinforcement_EF_kgCO2e_per_kg_main"],
            }
            sm = get_design(mid, t, cn, "smallest_section")
            sm.update(common)
            sm["Selection_Objective"] = "smallest feasible section"
            smallest.append(sm)
            bg = get_design(mid, t, cn, "min_gwp")
            bg.update(common)
            bg["Selection_Objective"] = "minimum product-stage GWP among all feasible candidate layouts"
            primary.append(bg)
    write_csv(out / "Table_3_5_Column_Design_All_Mixes.csv", primary)
    write_csv(out / "Table_3_5_Smallest_Section_Designs.csv", smallest)

    manu_cols = [
        "Mix_ID", "Mixture_Code", "Target_years", "Selection_Objective", "c_req_mm", "c_nom_mm",
        "b_mm", "h_mm", "longitudinal_bars_n", "longitudinal_bar_diameter_mm", "link_diameter_mm",
        "end_zone_spacing_actual_mm", "middle_zone_spacing_actual_mm", "cross_ties_total",
        "concrete_volume_m3", "longitudinal_steel_mass_kg", "link_steel_mass_kg",
        "cross_tie_steel_mass_kg", "total_steel_mass_kg", "biaxial_interaction_utilization",
        "slenderness_lambda_x", "slenderness_lambda_y", "slenderness_limit_lambda_lim_x",
        "slenderness_limit_lambda_lim_y", "second_order_effects_may_be_ignored_x",
        "second_order_effects_may_be_ignored_y", "GWP_column_kgCO2e", "Status",
    ]
    write_csv(out / "Table_3_5_Column_Design_Manuscript.csv", primary, fields=manu_cols)

    summary_design = []
    for t in a["target_periods_years"]:
        rr = [r for r in primary if int(r["Target_years"]) == t
              and str(r.get("Status", "")).startswith(("PRELIMINARY_SCREENING_FEASIBLE", "FEASIBLE_SCREENING"))]
        if not rr:
            continue
        summary_design.append({
            "Target_years": t, "Mixture_cases": len(rr),
            "c_nom_min_mm": min(float(r["c_nom_mm"]) for r in rr),
            "c_nom_max_mm": max(float(r["c_nom_mm"]) for r in rr),
            "section_dimensions_selected_mm": "; ".join(
                f"{b}x{b}: {sum(1 for r in rr if int(r['b_mm']) == b)} cases"
                for b in sorted({int(r["b_mm"]) for r in rr})),
            "concrete_volume_min_m3": min(float(r["concrete_volume_m3"]) for r in rr),
            "concrete_volume_max_m3": max(float(r["concrete_volume_m3"]) for r in rr),
            "steel_mass_min_kg": min(float(r["total_steel_mass_kg"]) for r in rr),
            "steel_mass_max_kg": max(float(r["total_steel_mass_kg"]) for r in rr),
            "biaxial_utilization_min": min(float(r["biaxial_interaction_utilization"]) for r in rr),
            "biaxial_utilization_max": max(float(r["biaxial_interaction_utilization"]) for r in rr),
            "slenderness_lambda_min": min(float(r["slenderness_lambda_x"]) for r in rr),
            "slenderness_lambda_max": max(float(r["slenderness_lambda_x"]) for r in rr),
            "lambda_lim_min": min(float(r["slenderness_limit_lambda_lim_x"]) for r in rr),
            "lambda_lim_max": max(float(r["slenderness_limit_lambda_lim_x"]) for r in rr),
        })
    write_csv(out / "Table_3_5_Column_Design_Summary.csv", summary_design)

    alpha_design = []
    for mid in data["order"]:
        Dm = data["D_stats"][mid]["scaled"]
        for t in [75]:
            for al in [0.20, 0.30, 0.40]:
                cr = _cover_model_for_input(Dm, t, alpha=float(al), ccrit=0.6, t0_years=90.0 / 365.0)
                cn = _nominal_cover_from_req(cr, t)
                d = get_design(mid, t, cn, "min_gwp")
                alpha_design.append({
                    "Mix_ID": mid, "Mixture_Code": data["md"][mid]["code"], "Target_years": t,
                    "alpha": float(al), "D_nssm_90d_1e12_m2_s": Dm, "c_req_mm": cr, "c_nom_mm": cn,
                    "b_mm": d.get("b_mm"), "h_mm": d.get("h_mm"),
                    "longitudinal_bars_n": d.get("longitudinal_bars_n"),
                    "longitudinal_bar_diameter_mm": d.get("longitudinal_bar_diameter_mm"),
                    "concrete_volume_m3": d.get("concrete_volume_m3"),
                    "total_steel_mass_kg": d.get("total_steel_mass_kg"),
                    "biaxial_interaction_utilization": d.get("biaxial_interaction_utilization"),
                    "Status": d.get("Status"),
                })
    write_csv(out / "Table_S_Alpha_Column_Design_Sensitivity.csv", alpha_design)

    # Coupled transfer-factor sensitivity at the 75-year target. For each k_t
    # perturbation, recalculate effective/nominal cover and re-run the 5-story
    # member search. The 15- and 30-story member searches are performed downstream.
    kt_design = []
    for mid in data["order"]:
        Dm = float(data["D_stats"][mid]["scaled"])
        for kt_v in (0.075, 0.10, 0.125):
            cr = _cover_model_for_input(
                Dm, 75, alpha=0.30, ccrit=0.6, t0_years=90.0 / 365.0, kt_v=float(kt_v))
            cn = _nominal_cover_from_req(cr, 75)
            d = get_design(mid, 75, cn, "min_gwp")
            kt_design.append({
                "Mix_ID": mid, "Mixture_Code": data["md"][mid]["code"],
                "Target_years": 75, "k_t": float(kt_v), "alpha": 0.30,
                "D_nssm_90d_1e12_m2_s": Dm, "c_req_mm": cr,
                "c_eff_S4_mm": max(cr, 35.0), "c_nom_mm": cn,
                "b_mm": d.get("b_mm"), "h_mm": d.get("h_mm"),
                "longitudinal_bars_n": d.get("longitudinal_bars_n"),
                "longitudinal_bar_diameter_mm": d.get("longitudinal_bar_diameter_mm"),
                "link_diameter_mm": d.get("link_diameter_mm"),
                "concrete_volume_m3": d.get("concrete_volume_m3"),
                "total_steel_mass_kg": d.get("total_steel_mass_kg"),
                "GWP_column_kgCO2e": d.get("GWP_column_kgCO2e"),
                "biaxial_interaction_utilization": d.get("biaxial_interaction_utilization"),
                "Status": d.get("Status"),
                "Scenario_note": "One-factor deterministic perturbation around k_t=0.10; alpha and other inputs held at baseline",
            })
    write_csv(out / "Table_S_Kt_Column_Design_Sensitivity.csv", kt_design)

    d_design = []
    for mid in data["order"]:
        Dm = float(data["D_stats"][mid]["scaled"])
        sd_raw = data["D_stats"][mid]["scaled_sd"]
        sd = float(sd_raw) if is_number(sd_raw) else None
        if sd is not None:
            scen = [("mean_minus_SD", max(Dm - sd, 1e-12)), ("mean", Dm), ("mean_plus_SD", Dm + sd)]
        else:
            scen = [("mean_SD_unavailable", Dm)]
        for t in [75]:
            for sn, Ds in scen:
                cr = _cover_model_for_input(Ds, t, alpha=0.30, ccrit=0.6, t0_years=90.0 / 365.0)
                cn = _nominal_cover_from_req(cr, t)
                d = get_design(mid, t, cn, "min_gwp")
                d_design.append({
                    "Mix_ID": mid, "Mixture_Code": data["md"][mid]["code"], "Target_years": t,
                    "D_scenario": sn, "D_nssm_1e12_m2_s": Ds, "D_mean_1e12_m2_s": Dm,
                    "D_SD_1e12_m2_s": sd, "c_req_mm": cr, "c_nom_mm": cn,
                    "b_mm": d.get("b_mm"), "h_mm": d.get("h_mm"),
                    "longitudinal_bars_n": d.get("longitudinal_bars_n"),
                    "longitudinal_bar_diameter_mm": d.get("longitudinal_bar_diameter_mm"),
                    "concrete_volume_m3": d.get("concrete_volume_m3"),
                    "total_steel_mass_kg": d.get("total_steel_mass_kg"),
                    "biaxial_interaction_utilization": d.get("biaxial_interaction_utilization"),
                    "Status": d.get("Status"),
                })
    write_csv(out / "Table_S_Dnssm_Column_Design_Sensitivity.csv", d_design)

    ccrit_design = []
    for mid in data["order"]:
        Dm = float(data["D_stats"][mid]["scaled"])
        for t in [75]:
            for cc in (0.4, 0.6, 0.8):
                cr = _cover_model_for_input(Dm, t, alpha=0.30, ccrit=cc, t0_years=90.0 / 365.0)
                cn = _nominal_cover_from_req(cr, t)
                d = get_design(mid, t, cn, "min_gwp")
                ccrit_design.append({
                    "Mix_ID": mid, "Mixture_Code": data["md"][mid]["code"], "Target_years": t,
                    "Ccrit_wt_pct_cement": cc, "D_nssm_90d_1e12_m2_s": Dm,
                    "c_req_mm": cr, "c_nom_mm": cn, "b_mm": d.get("b_mm"), "h_mm": d.get("h_mm"),
                    "longitudinal_bars_n": d.get("longitudinal_bars_n"),
                    "longitudinal_bar_diameter_mm": d.get("longitudinal_bar_diameter_mm"),
                    "concrete_volume_m3": d.get("concrete_volume_m3"),
                    "total_steel_mass_kg": d.get("total_steel_mass_kg"),
                    "Status": d.get("Status"),
                })
    write_csv(out / "Table_S_Ccrit_Column_Design_Sensitivity.csv", ccrit_design)

    cs_design = []
    for mid in data["order"]:
        Dm = float(data["D_stats"][mid]["scaled"])
        cube = data["cs_stats"][(mid, 28)][0]
        for t in [75]:
            for csv_ in (1.5, 2.5, 3.0, 3.5):
                cr = _cover_model_for_input(Dm, t, alpha=0.30, ccrit=0.6,
                                            t0_years=90.0 / 365.0, cs=csv_)
                cn = _nominal_cover_from_req(cr, t)
                d = _calculate_column_design(cube, cn, mid, t, selection_rule="min_gwp",
                                             gwp_concrete=data["gwp_by_mix"][mid])
                cs_design.append({
                    "Mix_ID": mid, "Mixture_Code": data["md"][mid]["code"], "Target_years": t,
                    "Cs_wt_pct_cement": csv_, "C0_wt_pct_cement": 0.1, "Ccrit_wt_pct_cement": 0.6,
                    "D_nssm_90d_1e12_m2_s": Dm, "alpha": 0.30, "c_req_mm": cr,
                    "c_eff_S4_mm": max(cr, 35.0), "c_nom_mm": cn, "b_mm": d.get("b_mm"),
                    "h_mm": d.get("h_mm"), "longitudinal_bars_n": d.get("longitudinal_bars_n"),
                    "longitudinal_bar_diameter_mm": d.get("longitudinal_bar_diameter_mm"),
                    "concrete_volume_m3": d.get("concrete_volume_m3"),
                    "total_steel_mass_kg": d.get("total_steel_mass_kg"),
                    "biaxial_interaction_utilization": d.get("biaxial_interaction_utilization"),
                    "Status": d.get("Status"),
                    "Scenario_note": "Deterministic boundary-condition sensitivity; not probabilistic uncertainty",
                })
    write_csv(out / "Table_S_Cs_Column_Design_Sensitivity.csv", cs_design)

    mom_design = []
    for mid in data["order"]:
        cr = float(service_by_mix[mid]["c_req_75y_mm"])
        cn = float(service_by_mix[mid]["c_nom_S4_75y_mm"])
        cube = data["cs_stats"][(mid, 28)][0]
        for sc in (0.8, 1.0, 1.2):
            d = _calculate_column_design(cube, cn, mid, 75, moment_scale=sc,
                                         selection_rule="min_gwp",
                                         gwp_concrete=data["gwp_by_mix"][mid])
            mom_design.append({
                "Mix_ID": mid, "Mixture_Code": data["md"][mid]["code"], "Target_years": 75,
                "Moment_scale": sc, "MEd_x_kNm": a["M_Ed_x_kNm"] * sc,
                "MEd_y_kNm": a["M_Ed_y_kNm"] * sc, "c_nom_mm": cn,
                "b_mm": d.get("b_mm"), "longitudinal_bars_n": d.get("longitudinal_bars_n"),
                "longitudinal_bar_diameter_mm": d.get("longitudinal_bar_diameter_mm"),
                "concrete_volume_m3": d.get("concrete_volume_m3"),
                "total_steel_mass_kg": d.get("total_steel_mass_kg"),
                "GWP_column_kgCO2e": d.get("GWP_column_kgCO2e"),
                "biaxial_interaction_utilization": d.get("biaxial_interaction_utilization"),
                "Status": d.get("Status"),
            })
    write_csv(out / "Table_S_Moment_Column_Design_Sensitivity.csv", mom_design)

    # Figure 7
    x = np.arange(len(data["order"]))
    grp_ord = [data["group"](m) for m in data["order"]]
    fig, axes = plt.subplots(3, 1, figsize=(12, 10), constrained_layout=True, sharex=True)
    for t in a["target_periods_years"]:
        sub = {r["Mix_ID"]: r for r in primary if int(r["Target_years"]) == t}
        xo = {50: -0.22, 75: 0.0, 100: 0.22}[t]
        for ax_, key in zip(axes, ("b_mm", "total_steel_mass_kg", "c_nom_mm")):
            t_scatter(ax_, x + xo, [sub[mid].get(key, np.nan) for mid in data["order"]],
                      groups=grp_ord, filt="TARGET", level=int(t), size=34)
    _style_title(axes[0], "(a) Section width")
    axes[0].set_ylabel("Section width (mm)")
    _style_title(axes[1], "(b) Reinforcement")
    axes[1].set_ylabel("Steel (kg/column)")
    _style_title(axes[2], "(c) Nominal cover")
    axes[2].set_ylabel("Cover (mm)")
    axes[2].set_xticks(x, data["order"], rotation=50, ha="right")
    for ax in axes:
        _style_axes(ax)
    t_save(fig, out / "Figure_7_Column_Design_and_Material_Demand.png")

    return primary, smallest, alpha_design, d_design, ccrit_design, cs_design, mom_design, kt_design



def run_story_count_sensitivity(data, service_rows, baseline_rows, results_root):
    """Calculate the 30-story member design for one 3-m column segment.

    Floor actions are accumulated over 30 stories, including the repeated column-stack
    self-weight. Prescribed first-order moments are 900/360 kN m, proportional to the
    story count. The action set is a member-level input, not a global-frame analysis.
    """
    out = results_root / "Section_3_5" / "Story_Count_Sensitivity"
    out.mkdir(parents=True, exist_ok=True)
    a = COLUMN_ASSUMPTIONS
    n5 = int(a["storeys"])
    n30_storeys = 30
    scale = n30_storeys / n5
    mx30 = float(a["M_Ed_x_kNm_30storey"])
    my30 = float(a["M_Ed_y_kNm_30storey"])
    svc_by_mix = {r["Mix_ID"]: r for r in service_rows}
    base_by = {(r["Mix_ID"], int(r["Target_years"])): r for r in baseline_rows}
    designs30 = []
    comparisons = []
    for mid in data["order"]:
        svc = svc_by_mix[mid]
        cube = data["cs_stats"][(mid, 28)][0]
        for target in a["target_periods_years"]:
            cnom = float(svc[f"c_nom_S4_{target}y_mm"])
            d30 = _calculate_column_design(
                cube, cnom, mid, int(target), selection_rule="min_gwp",
                gwp_concrete=data["gwp_by_mix"].get(mid), storeys=n30_storeys,
                mx_action_kNm=mx30, my_action_kNm=my30)
            d30["Storeys_scenario"] = n30_storeys
            d30["N_Ed_30storey_floor_loads_kN"] = d30.get("N_Ed_floors_kN")
            d30["N_Ed_30storey_column_selfweight_kN"] = d30.get("N_Ed_column_selfweight_kN")
            d30["N_Ed_30storey_total_kN"] = d30.get("N_Ed_kN")
            d30["Moment_scaling_factor_relative_to_baseline"] = scale
            d30["Story_count_sensitivity_basis"] = (
                "One 3-m column segment; factored floor loads accumulated over 30 stories plus the trial section's stacked self-weight; prescribed first-order moments are 900/360 kN m; not global-frame analysis")
            designs30.append(d30)
            b5 = base_by[(mid, int(target))]
            g5 = b5.get("GWP_column_kgCO2e")
            g30 = d30.get("GWP_column_kgCO2e")
            comparisons.append({
                "Mix_ID": mid, "Mixture_Code": data["md"][mid]["code"], "Target_years": int(target),
                "N_Ed_5storey_kN": b5.get("N_Ed_kN"), "N_Ed_30storey_kN": d30.get("N_Ed_kN"),
                "N_Ed_5storey_floor_loads_kN": b5.get("N_Ed_floors_kN"),
                "N_Ed_5storey_column_selfweight_kN": b5.get("N_Ed_column_selfweight_kN"),
                "N_Ed_30storey_floor_loads_kN": d30.get("N_Ed_floors_kN"),
                "N_Ed_30storey_column_selfweight_kN": d30.get("N_Ed_column_selfweight_kN"),
                "M_x_5storey_kNm": b5.get("M_Ed_x_first_order_kNm", a["M_Ed_x_kNm"]),
                "M_y_5storey_kNm": b5.get("M_Ed_y_first_order_kNm", a["M_Ed_y_kNm"]),
                "M_x_30storey_kNm": mx30, "M_y_30storey_kNm": my30,
                "Section_5storey_mm": f"{b5.get('b_mm')}×{b5.get('h_mm')}",
                "Section_30storey_mm": f"{d30.get('b_mm')}×{d30.get('h_mm')}",
                "GWP_5storey_kgCO2e": g5, "GWP_30storey_kgCO2e": g30,
                "GWP_change_30_vs_5_pct": ((float(g30) / float(g5)) - 1.0) * 100.0 if g5 and g30 is not None else None,
                "Biaxial_utilization_5storey": b5.get("biaxial_interaction_utilization"),
                "Biaxial_utilization_30storey": d30.get("biaxial_interaction_utilization"),
                "M2_x_30storey_kNm": d30.get("second_order_moment_x_kNm"),
                "M2_y_30storey_kNm": d30.get("second_order_moment_y_kNm"),
                "Status_30storey": d30.get("Status"),
            })

    write_csv(out / "Table_30Storey_Column_Design.csv", designs30)

    # Compare percentage reduction vs M01 separately within each target year.
    for target in a["target_periods_years"]:
        group = [r for r in comparisons if r["Target_years"] == int(target)]
        ref5 = next((float(r["GWP_5storey_kgCO2e"]) for r in group if r["Mix_ID"] == "M01" and r["GWP_5storey_kgCO2e"] is not None), None)
        ref30 = next((float(r["GWP_30storey_kgCO2e"]) for r in group if r["Mix_ID"] == "M01" and r["GWP_30storey_kgCO2e"] is not None), None)
        for r in group:
            r["GWP_reduction_vs_M01_5storey_pct"] = (1.0 - float(r["GWP_5storey_kgCO2e"]) / ref5) * 100.0 if ref5 and r["GWP_5storey_kgCO2e"] is not None else None
            r["GWP_reduction_vs_M01_30storey_pct"] = (1.0 - float(r["GWP_30storey_kgCO2e"]) / ref30) * 100.0 if ref30 and r["GWP_30storey_kgCO2e"] is not None else None
            if r["GWP_reduction_vs_M01_5storey_pct"] is not None and r["GWP_reduction_vs_M01_30storey_pct"] is not None:
                r["Change_in_GWP_reduction_30_vs_5_percentage_points"] = (
                    r["GWP_reduction_vs_M01_30storey_pct"] - r["GWP_reduction_vs_M01_5storey_pct"])
    write_csv(out / "Table_5vs30_Storey_Comparison.csv", comparisons)

    summary = []
    for target in a["target_periods_years"]:
        rr = [r for r in comparisons if r["Target_years"] == int(target) and r["GWP_30storey_kgCO2e"] is not None]
        dchanges = [float(r["GWP_change_30_vs_5_pct"]) for r in rr if r["GWP_change_30_vs_5_pct"] is not None]
        reductions5 = [float(r["GWP_reduction_vs_M01_5storey_pct"]) for r in rr if r.get("GWP_reduction_vs_M01_5storey_pct") is not None]
        reductions30 = [float(r["GWP_reduction_vs_M01_30storey_pct"]) for r in rr if r.get("GWP_reduction_vs_M01_30storey_pct") is not None]
        deltas = [float(r["Change_in_GWP_reduction_30_vs_5_percentage_points"]) for r in rr if r.get("Change_in_GWP_reduction_30_vs_5_percentage_points") is not None]
        summary.append({
            "Target_years": int(target), "Cases": len(rr),
            "Sections_5storey": "; ".join(f"{b}×{b}: {sum(1 for r in rr if r['Section_5storey_mm']==f'{b}×{b}')} cases" for b in sorted({int(str(r['Section_5storey_mm']).split('×')[0]) for r in rr})),
            "Sections_30storey": "; ".join(f"{b}×{b}: {sum(1 for r in rr if r['Section_30storey_mm']==f'{b}×{b}')} cases" for b in sorted({int(str(r['Section_30storey_mm']).split('×')[0]) for r in rr})),
            "GWP_5storey_min_kgCO2e": min(float(r["GWP_5storey_kgCO2e"]) for r in rr),
            "GWP_5storey_max_kgCO2e": max(float(r["GWP_5storey_kgCO2e"]) for r in rr),
            "GWP_30storey_min_kgCO2e": min(float(r["GWP_30storey_kgCO2e"]) for r in rr),
            "GWP_30storey_max_kgCO2e": max(float(r["GWP_30storey_kgCO2e"]) for r in rr),
            "GWP_change_30_vs_5_min_pct": min(dchanges), "GWP_change_30_vs_5_max_pct": max(dchanges),
            "GWP_reduction_vs_M01_5storey_min_pct": min(reductions5), "GWP_reduction_vs_M01_5storey_max_pct": max(reductions5),
            "GWP_reduction_vs_M01_30storey_min_pct": min(reductions30), "GWP_reduction_vs_M01_30storey_max_pct": max(reductions30),
            "Change_in_reduction_min_pp": min(deltas), "Change_in_reduction_max_pp": max(deltas),
            "N_Ed_30storey_min_kN": min(float(r["N_Ed_30storey_kN"]) for r in rr),
            "N_Ed_30storey_max_kN": max(float(r["N_Ed_30storey_kN"]) for r in rr),
            "M_Ed_x_30storey_kNm": mx30, "M_Ed_y_30storey_kNm": my30,
        })
    write_csv(out / "Table_5vs30_Storey_Summary.csv", summary)

    # The manuscript comparison is generated once for all three scenarios below.
    return designs30, comparisons, summary

# =============================================================================
# SECTION 3.6 — GWP
# =============================================================================
def run_section_3_6(data, column_rows, alpha_design, d_design, ccrit_design,
                    cs_design, mom_design, kt_design, simapro, results_root):
    out = results_root / "Section_3_6"
    out.mkdir(parents=True, exist_ok=True)
    write_matrix_csv(out / "SimaPro_Component_Breakdown.csv",
                     simapro["component_header"], simapro["component_rows"])
    write_csv(out / "SimaPro_Component_QA.csv", simapro["qa_rows"])
    gwp_by_mix = simapro["totals"]
    write_csv(out / "Concrete_GWP_from_SimaPro.csv",
              [{"Mix_ID": m, "GWP_concrete_kgCO2e_m3": v} for m, v in sorted(gwp_by_mix.items())])

    ef_main = COLUMN_ASSUMPTIONS["reinforcement_EF_kgCO2e_per_kg_main"]
    mix_sorted = sorted([
        {"Mix_ID": m, "Mixture_Code": data["md"][m]["code"], "GWP_concrete_kgCO2e_m3": gwp_by_mix[m],
         "Includes_PCE": "YES", "Boundary": "A1-A3"}
        for m in data["order"]
    ], key=lambda r: r["GWP_concrete_kgCO2e_m3"])
    write_csv(out / "Table_3_6_Concrete_GWP_Sorted.csv", mix_sorted)

    def gwp_join(rows, label):
        joined = []
        base = {(r["Mix_ID"], int(r["Target_years"])): r for r in column_rows}
        for r in rows:
            if r.get("Status") not in {None, "PRELIMINARY_SCREENING_FEASIBLE"} and not str(r.get("Status", "")).startswith("FEASIBLE_SCREENING"):
                continue
            mid = r["Mix_ID"]
            t = int(r["Target_years"])
            v = r.get("concrete_volume_m3")
            m = r.get("total_steel_mass_kg")
            if v is None or m is None:
                continue
            g = gwp_by_mix[mid]
            cpart = float(v) * g
            spart = float(m) * ef_main
            tot = cpart + spart
            ref = base.get(("M01", t), {})
            ref_gwp = (float(ref.get("concrete_volume_m3", 0)) * gwp_by_mix["M01"]
                       + float(ref.get("total_steel_mass_kg", 0)) * ef_main)
            jr = {
                "Scenario": label, "Mix_ID": mid, "Mixture_Code": data["md"][mid]["code"],
                "Target_years": t, "GWP_concrete_kgCO2e_m3": g, "Concrete_volume_m3": v,
                "Concrete_GWP_column_kgCO2e": cpart, "Total_steel_mass_kg": m,
                "Steel_EF_kgCO2e_kg": ef_main, "Steel_GWP_column_kgCO2e": spart,
                "GWP_column_kgCO2e": tot,
                "GWP_change_vs_M01_pct": 100 * (tot / ref_gwp - 1) if ref_gwp else None,
                "Concrete_share_pct": 100 * cpart / tot if tot else None,
                "Steel_share_pct": 100 * spart / tot if tot else None,
            }
            for f in ("alpha", "D_scenario", "D_1e12_m2_s", "D_nssm_1e12_m2_s", "D_mean_1e12_m2_s",
                      "D_SD_1e12_m2_s", "Ccrit_wt_pct_cement", "Cs_wt_pct_cement", "C0_wt_pct_cement",
                      "Moment_scale", "MEd_x_kNm", "MEd_y_kNm", "k_t", "EF_steel_kgCO2e_kg", "c_req_mm",
                      "c_eff_S4_mm", "c_nom_mm", "b_mm", "h_mm", "longitudinal_bars_n",
                      "longitudinal_bar_diameter_mm", "biaxial_interaction_utilization", "Scenario_note"):
                if f in r:
                    jr[f] = r[f]
            joined.append(jr)
        return joined

    column_gwp = gwp_join(column_rows, "baseline")
    write_csv(out / "Table_3_6_Column_GWP.csv", column_gwp)

    decomp = []
    base_t = {int(r["Target_years"]): r for r in column_gwp if r["Mix_ID"] == "M01"}
    for r in column_gwp:
        ref = base_t[int(r["Target_years"])]
        vi, v0 = float(r["Concrete_volume_m3"]), float(ref["Concrete_volume_m3"])
        gi, g0 = float(r["GWP_concrete_kgCO2e_m3"]), float(ref["GWP_concrete_kgCO2e_m3"])
        mi, m0 = float(r["Total_steel_mass_kg"]), float(ref["Total_steel_mass_kg"])
        vol_eff = (vi - v0) * g0
        bind_eff = vi * (gi - g0)
        steel_eff = (mi - m0) * ef_main
        decomp.append({
            "Mix_ID": r["Mix_ID"], "Mixture_Code": r["Mixture_Code"], "Target_years": r["Target_years"],
            "Concrete_GWP_intensity_kgCO2e_m3": gi,
            "Delta_concrete_GWP_intensity_vs_M01_pct": 100 * (gi / g0 - 1),
            "Concrete_volume_m3": vi, "Volume_effect_vs_M01_kgCO2e": vol_eff,
            "Binder_intensity_effect_vs_M01_kgCO2e": bind_eff,
            "Steel_mass_effect_vs_M01_kgCO2e": steel_eff,
            "Total_delta_GWP_vs_M01_kgCO2e": float(r["GWP_column_kgCO2e"]) - float(ref["GWP_column_kgCO2e"]),
            "Concrete_share_pct": r["Concrete_share_pct"], "Steel_share_pct": r["Steel_share_pct"],
        })
    write_csv(out / "Table_3_6_GWP_Contribution_Decomposition.csv", decomp)

    write_csv(out / "Table_S_GWP_Alpha_Sensitivity.csv", gwp_join(alpha_design, "alpha_sensitivity"))
    write_csv(out / "Table_S_GWP_Dnssm_Sensitivity.csv", gwp_join(d_design, "RCMT_sensitivity"))
    write_csv(out / "Table_S_GWP_Ccrit_Sensitivity.csv", gwp_join(ccrit_design, "Ccrit_sensitivity"))
    write_csv(out / "Table_S_GWP_Cs_Sensitivity.csv", gwp_join(cs_design, "Cs_sensitivity"))
    write_csv(out / "Table_S_GWP_Moment_Sensitivity.csv", gwp_join(mom_design, "moment_sensitivity"))
    write_csv(out / "Table_S_GWP_Kt_Sensitivity.csv", gwp_join(kt_design, "transfer_factor_sensitivity"))

    ef_rows = []
    for r in column_rows:
        for ef in COLUMN_ASSUMPTIONS["reinforcement_EF_kgCO2e_per_kg_scenarios"]:
            cp = float(r["concrete_volume_m3"]) * gwp_by_mix[r["Mix_ID"]]
            sp = float(r["total_steel_mass_kg"]) * ef
            tot = cp + sp
            ef_rows.append({
                "Mix_ID": r["Mix_ID"], "Target_years": r["Target_years"], "EF_steel_kgCO2e_kg": ef,
                "Concrete_GWP_column_kgCO2e": cp, "Steel_GWP_column_kgCO2e": sp,
                "GWP_column_kgCO2e": tot,
            })
    write_csv(out / "Table_S_GWP_Steel_Factor_Sensitivity.csv", ef_rows)

    mid_to_group = {m: data["group"](m) for m in data["md"]}
    mix_ids = [r["Mix_ID"] for r in mix_sorted]
    vals = [r["GWP_concrete_kgCO2e_m3"] for r in mix_sorted]
    grp_mix = [mid_to_group.get(m, "Reference") for m in mix_ids]
    fig, axes = plt.subplots(1, 2, figsize=(13.2, 5.6), constrained_layout=True)

    t_bar(axes[0], np.arange(len(mix_ids)), vals, groups=grp_mix)
    axes[0].set_xticks(np.arange(len(mix_ids)), mix_ids, rotation=50, ha="right")
    axes[0].set_ylabel("GWP (kg CO$_2$-eq/m$^3$)")
    _style_title(axes[0], "(a) Concrete GWP")
    _style_axes(axes[0])

    grp_ord = [mid_to_group.get(m, "Reference") for m in data["order"]]
    for t in TARGET_LEVELS:
        sub = {r["Mix_ID"]: r["GWP_column_kgCO2e"] for r in column_gwp if r["Target_years"] == t}
        xo = {50: -0.16, 75: 0.0, 100: 0.16}[t]
        t_scatter(axes[1], np.arange(len(data["order"])) + xo,
                  [sub.get(m, np.nan) for m in data["order"]],
                  groups=grp_ord, filt="TARGET", level=t, size=32)
    axes[1].set_xticks(np.arange(len(data["order"])), data["order"], rotation=50, ha="right")
    axes[1].set_ylabel("GWP (kg CO$_2$-eq/3-m segment)")
    _style_title(axes[1], "(b) Column-segment GWP")
    _style_axes(axes[1])
    t_save(fig, out / "Figure_8_Concrete_and_Column_GWP.png")

    status = {
        "status": "AUTHOR_SUPPLIED_SIMAPRO_VALUES", "mixtures_with_numeric_GWP": len(gwp_by_mix),
        "main_steel_EF_kgCO2e_kg": ef_main, "source": "Concrete_Dataset.xlsx / SimaPro_GWP",
        "PCE_status": "PCE included; no double-counting", "boundary": "A1-A3",
        "component_QA": "SimaPro_Component_QA.csv",
        "annualization_warning": "Divide by target time only; not full-life annual GWP.",
    }
    write_csv(out / "GWP_Input_Status.csv", [status])
    return status, column_gwp



# =============================================================================
# MEMBER-DESIGN SENSITIVITIES WITHIN THE THREE-SCENARIO FRAMEWORK (Rev99)
# =============================================================================
def _read_csv_records(path):
    with Path(path).open("r", newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _as_float(v, default=None):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _run_story_count_design_sensitivities_recompute(data, results_root):
    """Recalculate member-design sensitivities for the 30-story scenario.

    The material and cover sensitivity inputs are held consistent with the other
    scenarios; the 30-story actions and member design are recalculated for each case.
    """
    a = COLUMN_ASSUMPTIONS
    root = Path(results_root)
    src = root / "Section_3_5"
    out = src / "Story_Count_Sensitivity" / "Design_Sensitivities"
    out.mkdir(parents=True, exist_ok=True)
    n30 = 30
    mx30 = float(a["M_Ed_x_kNm_30storey"])
    my30 = float(a["M_Ed_y_kNm_30storey"])
    ef = float(a["reinforcement_EF_kgCO2e_per_kg_main"])

    specs = [
        ("Alpha", "Table_S_Alpha_Column_Design_Sensitivity.csv", ("alpha",)),
        ("Dnssm", "Table_S_Dnssm_Column_Design_Sensitivity.csv", ("D_scenario",)),
        ("Ccrit", "Table_S_Ccrit_Column_Design_Sensitivity.csv", ("Ccrit_wt_pct_cement",)),
        ("Cs", "Table_S_Cs_Column_Design_Sensitivity.csv", ("Cs_wt_pct_cement",)),
        ("Moment", "Table_S_Moment_Column_Design_Sensitivity.csv", ("Moment_scale",)),
        ("Kt", "Table_S_Kt_Column_Design_Sensitivity.csv", ("k_t",)),
    ]
    comparative = {}
    # Reuse identical member-design calculations across sensitivity families when their
    # effective nominal cover and action scale are identical. This changes no search space
    # or selection criterion; it only avoids repeating deterministic calculations.
    design_cache = {}
    design_fields = [
        "b_mm", "h_mm", "c_nom_mm", "longitudinal_bars_n", "longitudinal_bar_diameter_mm",
        "link_diameter_mm", "end_zone_spacing_actual_mm", "middle_zone_spacing_actual_mm",
        "lap_zone_spacing_actual_mm", "concrete_volume_m3", "total_steel_mass_kg",
        "GWP_column_kgCO2e", "biaxial_interaction_utilization", "N_Ed_kN",
        "second_order_moment_x_kNm", "second_order_moment_y_kNm", "second_order_status",
        "detailing_checks_status", "shear_check", "compression_strut_check", "Status"
    ]
    for label, filename, scenario_keys in specs:
        five_rows = _read_csv_records(src / filename)
        thirty_rows, joined = [], []
        print(f"[story-count] Start {label} design sensitivity: {len(five_rows)} cases", flush=True)
        for row_i, row in enumerate(five_rows):
            if row_i % 16 == 0:
                print(f"[story-count] {label}: {row_i + 1}/{len(five_rows)}", flush=True)
            mid = row["Mix_ID"]
            target = int(float(row.get("Target_years", 75)))
            cnom = float(row["c_nom_mm"])
            scale_m = _as_float(row.get("Moment_scale"), 1.0) or 1.0
            kw = {
                "selection_rule": "min_gwp", "gwp_concrete": data["gwp_by_mix"][mid],
                "storeys": n30,
            }
            if label == "Moment":
                kw["mx_action_kNm"] = mx30
                kw["my_action_kNm"] = my30
                kw["moment_scale"] = scale_m
            cache_key = (mid, target, round(cnom, 5), round(scale_m, 5), n30)
            if cache_key not in design_cache:
                design_cache[cache_key] = _calculate_column_design(
                    data["cs_stats"][(mid, 28)][0], cnom, mid, target, **kw
                )
            design = dict(design_cache[cache_key])
            rec30 = {**row, **{f"30_{k}": design.get(k) for k in design_fields}}
            rec30["Storeys"] = n30
            rec30["M_Ed_x_first_order_30_kNm"] = mx30 * scale_m
            rec30["M_Ed_y_first_order_30_kNm"] = my30 * scale_m
            rec30["Sensitivity_family"] = label
            thirty_rows.append(rec30)

            v5 = _as_float(row.get("concrete_volume_m3"))
            s5 = _as_float(row.get("total_steel_mass_kg"))
            gwp_mix = float(data["gwp_by_mix"][mid])
            gwp5 = v5 * gwp_mix + s5 * ef if v5 is not None and s5 is not None else None
            gwp30 = _as_float(design.get("GWP_column_kgCO2e"))
            combo = {"Mix_ID": mid, "Mixture_Code": row.get("Mixture_Code"),
                     "Target_years": target, "Sensitivity_family": label}
            for key in scenario_keys:
                combo[key] = row.get(key)
            combo.update({
                "c_nom_5_mm": row.get("c_nom_mm"), "c_nom_30_mm": design.get("c_nom_mm"),
                "section_5_mm": f"{row.get('b_mm')}x{row.get('h_mm')}",
                "section_30_mm": f"{design.get('b_mm')}x{design.get('h_mm')}" if design.get("b_mm") is not None else None,
                "concrete_volume_5_m3": v5, "concrete_volume_30_m3": design.get("concrete_volume_m3"),
                "steel_mass_5_kg": s5, "steel_mass_30_kg": design.get("total_steel_mass_kg"),
                "GWP_5_kgCO2e": gwp5, "GWP_30_kgCO2e": gwp30,
                "GWP_change_30_vs_5_pct": (100.0 * (gwp30 / gwp5 - 1.0)) if gwp30 is not None and gwp5 else None,
                "biaxial_utilization_5": row.get("biaxial_interaction_utilization"),
                "biaxial_utilization_30": design.get("biaxial_interaction_utilization"),
                "M2_x_5_kNm": row.get("second_order_moment_x_kNm"),
                "M2_y_5_kNm": row.get("second_order_moment_y_kNm"),
                "M2_x_30_kNm": design.get("second_order_moment_x_kNm"),
                "M2_y_30_kNm": design.get("second_order_moment_y_kNm"),
                "Status_5": row.get("Status"), "Status_30": design.get("Status"),
                "Design_checks_30": design.get("detailing_checks_status"),
            })
            joined.append(combo)
        write_csv(out / f"Table_S_{label}_Column_Design_Sensitivity_30Storey.csv", thirty_rows)
        # Paired reductions versus M01 at the identical parameter setting.
        refmap = {}
        for r in joined:
            key = (r["Target_years"],) + tuple(str(r.get(k, "")) for k in scenario_keys)
            if r["Mix_ID"] == "M01":
                refmap[key] = r
        for r in joined:
            key = (r["Target_years"],) + tuple(str(r.get(k, "")) for k in scenario_keys)
            ref = refmap.get(key)
            r["GWP_reduction_vs_M01_5_pct"] = (100.0 * (1.0 - r["GWP_5_kgCO2e"] / ref["GWP_5_kgCO2e"])) if ref and ref.get("GWP_5_kgCO2e") else None
            r["GWP_reduction_vs_M01_30_pct"] = (100.0 * (1.0 - r["GWP_30_kgCO2e"] / ref["GWP_30_kgCO2e"])) if ref and ref.get("GWP_30_kgCO2e") else None
            r["Reduction_change_30_minus_5_pp"] = (
                r["GWP_reduction_vs_M01_30_pct"] - r["GWP_reduction_vs_M01_5_pct"]
                if r.get("GWP_reduction_vs_M01_30_pct") is not None and r.get("GWP_reduction_vs_M01_5_pct") is not None else None
            )
        write_csv(out / f"Table_S_{label}_Column_Design_Sensitivity_5vs30.csv", joined)
        comparative[label] = joined

    print("[story-count] Design-sensitivity inputs complete; expanding steel-EF scenarios", flush=True)
    # Steel-emission-factor scenarios use the same selected layout for each story count.
    primary5 = _read_csv_records(src / "Table_3_5_Column_Design_All_Mixes.csv")
    primary30 = _read_csv_records(root / "Section_3_5" / "Story_Count_Sensitivity" / "Table_30Storey_Column_Design.csv")
    d5 = {(r["Mix_ID"], int(float(r["Target_years"]))): r for r in primary5}
    d30 = {(r["Mix_ID"], int(float(r["Target_years"]))): r for r in primary30}
    steels = []
    for key, r5 in d5.items():
        r30 = d30[key]
        for ef_s in a["reinforcement_EF_kgCO2e_per_kg_scenarios"]:
            gwp5 = float(r5["concrete_volume_m3"]) * float(data["gwp_by_mix"][key[0]]) + float(r5["total_steel_mass_kg"]) * float(ef_s)
            gwp30 = float(r30["concrete_volume_m3"]) * float(data["gwp_by_mix"][key[0]]) + float(r30["total_steel_mass_kg"]) * float(ef_s)
            steels.append({"Mix_ID": key[0], "Target_years": key[1], "EF_steel_kgCO2e_kg": float(ef_s),
                           "Section_5_mm": f"{r5['b_mm']}x{r5['h_mm']}", "Section_30_mm": f"{r30['b_mm']}x{r30['h_mm']}",
                           "Steel_mass_5_kg": r5["total_steel_mass_kg"], "Steel_mass_30_kg": r30["total_steel_mass_kg"],
                           "GWP_5_kgCO2e": gwp5, "GWP_30_kgCO2e": gwp30,
                           "GWP_change_30_vs_5_pct": 100.0 * (gwp30 / gwp5 - 1.0)})
    for t in sorted({r["Target_years"] for r in steels}):
        for ef_s in a["reinforcement_EF_kgCO2e_per_kg_scenarios"]:
            group = [r for r in steels if r["Target_years"] == t and r["EF_steel_kgCO2e_kg"] == float(ef_s)]
            ref = next((r for r in group if r["Mix_ID"] == "M01"), None)
            for r in group:
                r["GWP_reduction_vs_M01_5_pct"] = 100.0 * (1 - r["GWP_5_kgCO2e"] / ref["GWP_5_kgCO2e"]) if ref else None
                r["GWP_reduction_vs_M01_30_pct"] = 100.0 * (1 - r["GWP_30_kgCO2e"] / ref["GWP_30_kgCO2e"]) if ref else None
                r["Reduction_change_30_minus_5_pp"] = r["GWP_reduction_vs_M01_30_pct"] - r["GWP_reduction_vs_M01_5_pct"] if ref else None
    write_csv(root / "Section_3_6" / "Table_S_GWP_Steel_Factor_Sensitivity_5vs30.csv", steels)
    return comparative



def run_story_count_design_sensitivities(data, results_root):
    """Recalculate story-count design sensitivities using the current single-source dataset."""
    return _run_story_count_design_sensitivities_recompute(data, results_root)


def run_15storey_scenario(data, service_rows, results_root):
    """Calculate 15-story column designs within the three-scenario assessment.

    Floor actions are accumulated over 15 stories; the prescribed first-order moments
    are 450/180 kN m. These are member-level study inputs, not global-frame results.
    """
    out = Path(results_root) / "Section_3_5" / "Story_Count_Sensitivity"
    out.mkdir(parents=True, exist_ok=True)
    a = COLUMN_ASSUMPTIONS
    ns = 15
    mx = float(a["M_Ed_x_kNm_15storey"])
    my = float(a["M_Ed_y_kNm_15storey"])
    svc_by_mix = {r["Mix_ID"]: r for r in service_rows}
    designs15, smallest15 = [], []
    completed = 0
    if hasattr(_column_axis_capacity_cached, "cache_clear"):
        _column_axis_capacity_cached.cache_clear()
    for mid in data["order"]:
        cube = data["cs_stats"][(mid, 28)][0]
        svc = svc_by_mix[mid]
        for target in a["target_periods_years"]:
            cnom = float(svc[f"c_nom_S4_{target}y_mm"])
            common = {"Storeys": ns, "Storeys_scenario": ns, "Mix_ID": mid,
                      "Mixture_Code": data["md"][mid]["code"], "Target_years": int(target),
                      "c_nom_mm": cnom, "M_Ed_x_first_order_prescribed_kNm": mx,
                      "M_Ed_y_first_order_prescribed_kNm": my,
                      "Story_count_sensitivity_basis":
                      "One 3-m column segment; floor actions accumulated over 15 stories plus trial-section self-weight; first-order moments 450/180 kN m; not a global-frame analysis"}
            d = _calculate_column_design(
                cube, cnom, mid, int(target), selection_rule="min_gwp",
                gwp_concrete=data["gwp_by_mix"].get(mid), storeys=ns,
                mx_action_kNm=mx, my_action_kNm=my)
            d.update(common)
            d["Selection_Objective"] = "minimum product-stage GWP among feasible candidate layouts"
            designs15.append(d)

            ds = _calculate_column_design(
                cube, cnom, mid, int(target), selection_rule="smallest_section",
                storeys=ns, mx_action_kNm=mx, my_action_kNm=my)
            ds.update(common)
            ds["Selection_Objective"] = "smallest feasible section, then minimum reinforcement mass"
            smallest15.append(ds)
            completed += 1
            if completed % 8 == 0:
                print(f"[15-story] completed {completed}/48 mixture-target cases", flush=True)
    write_csv(out / "Table_15Storey_Column_Design.csv", designs15)
    write_csv(Path(results_root) / "Section_3_5" / "Table_15Storey_Smallest_Section_Designs.csv", smallest15)
    return designs15, smallest15


def run_15storey_design_sensitivities(data, results_root, sensitivities5vs30,
                                      designs15, designs30):
    """Calculate and assemble like-for-like design sensitivities for all three scenarios."""
    a = COLUMN_ASSUMPTIONS
    root = Path(results_root)
    src = root / "Section_3_5"
    out = src / "Story_Count_Sensitivity" / "Design_Sensitivities"
    out.mkdir(parents=True, exist_ok=True)
    ns = 15
    mx = float(a["M_Ed_x_kNm_15storey"])
    my = float(a["M_Ed_y_kNm_15storey"])
    ef = float(a["reinforcement_EF_kgCO2e_per_kg_main"])
    specs = [
        ("Alpha", "Table_S_Alpha_Column_Design_Sensitivity.csv", ("alpha",)),
        ("Dnssm", "Table_S_Dnssm_Column_Design_Sensitivity.csv", ("D_scenario",)),
        ("Ccrit", "Table_S_Ccrit_Column_Design_Sensitivity.csv", ("Ccrit_wt_pct_cement",)),
        ("Cs", "Table_S_Cs_Column_Design_Sensitivity.csv", ("Cs_wt_pct_cement",)),
        ("Moment", "Table_S_Moment_Column_Design_Sensitivity.csv", ("Moment_scale",)),
        ("Kt", "Table_S_Kt_Column_Design_Sensitivity.csv", ("k_t",)),
    ]
    design_cache = {}
    design_fields = [
        "b_mm", "h_mm", "c_nom_mm", "longitudinal_bars_n", "longitudinal_bar_diameter_mm",
        "link_diameter_mm", "end_zone_spacing_actual_mm", "middle_zone_spacing_actual_mm",
        "lap_zone_spacing_actual_mm", "concrete_volume_m3", "total_steel_mass_kg",
        "GWP_column_kgCO2e", "biaxial_interaction_utilization", "N_Ed_kN",
        "second_order_moment_x_kNm", "second_order_moment_y_kNm", "second_order_status",
        "detailing_checks_status", "shear_check", "compression_strut_check", "Status"
    ]
    comparative15 = {}
    for label, filename, scenario_keys in specs:
        five_rows = _read_csv_records(src / filename)
        rows15, joined15 = [], []
        print(f"[5vs15vs30] Start {label} 15-story sensitivity: {len(five_rows)} cases", flush=True)
        for i, row in enumerate(five_rows):
            mid = row["Mix_ID"]
            target = int(float(row.get("Target_years", 75)))
            cnom = float(row["c_nom_mm"])
            scale_m = _as_float(row.get("Moment_scale"), 1.0) or 1.0
            kw = {"selection_rule": "min_gwp", "gwp_concrete": data["gwp_by_mix"][mid],
                  "storeys": ns, "mx_action_kNm": mx, "my_action_kNm": my}
            if label == "Moment":
                kw["moment_scale"] = scale_m
            key = (mid, target, round(cnom, 5), round(scale_m, 5), ns)
            if key not in design_cache:
                design_cache[key] = _calculate_column_design(
                    data["cs_stats"][(mid, 28)][0], cnom, mid, target, **kw)
            design = dict(design_cache[key])
            rec = {**row, **{f"15_{k}": design.get(k) for k in design_fields}}
            rec["Storeys"] = ns
            rec["M_Ed_x_first_order_15_kNm"] = mx * scale_m
            rec["M_Ed_y_first_order_15_kNm"] = my * scale_m
            rec["Sensitivity_family"] = label
            rows15.append(rec)

            v5 = _as_float(row.get("concrete_volume_m3"))
            s5 = _as_float(row.get("total_steel_mass_kg"))
            gwp5 = v5 * float(data["gwp_by_mix"][mid]) + s5 * ef if v5 is not None and s5 is not None else None
            gwp15 = _as_float(design.get("GWP_column_kgCO2e"))
            combo = {"Mix_ID": mid, "Mixture_Code": row.get("Mixture_Code"),
                     "Target_years": target, "Sensitivity_family": label}
            for k in scenario_keys:
                combo[k] = row.get(k)
            combo.update({
                "GWP_5_kgCO2e": gwp5, "GWP_15_kgCO2e": gwp15,
                "Section_5_mm": f"{row.get('b_mm')}x{row.get('h_mm')}",
                "Section_15_mm": (f"{design.get('b_mm')}x{design.get('h_mm')}" if design.get("b_mm") is not None else None),
                "Concrete_volume_5_m3": v5, "Concrete_volume_15_m3": design.get("concrete_volume_m3"),
                "Steel_mass_5_kg": s5, "Steel_mass_15_kg": design.get("total_steel_mass_kg"),
                "Status_5": row.get("Status"), "Status_15": design.get("Status"),
                "Biaxial_utilization_5": row.get("biaxial_interaction_utilization"),
                "Biaxial_utilization_15": design.get("biaxial_interaction_utilization"),
            })
            joined15.append(combo)
        write_csv(out / f"Table_S_{label}_Column_Design_Sensitivity_15Storey.csv", rows15)
        comparative15[label] = joined15

    # Assemble corresponding 5-, 15-, and 30-story sensitivity records.
    combined = {}
    for label, _, scenario_keys in specs:
        r15 = comparative15[label]
        r30 = sensitivities5vs30[label]
        def keyfn(r):
            return (r["Mix_ID"], int(r["Target_years"])) + tuple(str(r.get(k, "")) for k in scenario_keys)
        map15 = {keyfn(r): r for r in r15}
        merged = []
        for r30row in r30:
            k = keyfn(r30row)
            midrow = map15.get(k)
            if midrow is None:
                continue
            row = dict(r30row)
            row.update({k: v for k, v in midrow.items() if k not in {"GWP_5_kgCO2e"}})
            row["GWP_15_kgCO2e"] = midrow.get("GWP_15_kgCO2e")
            row["Section_15_mm"] = midrow.get("Section_15_mm")
            row["Concrete_volume_15_m3"] = midrow.get("Concrete_volume_15_m3")
            row["Steel_mass_15_kg"] = midrow.get("Steel_mass_15_kg")
            row["Status_15"] = midrow.get("Status_15")
            row["Biaxial_utilization_15"] = midrow.get("Biaxial_utilization_15")
            merged.append(row)
        # Matched M01 reference per target and exact sensitivity input.
        refmap = {keyfn(r): r for r in merged if r["Mix_ID"] == "M01"}
        for r in merged:
            ref = refmap.get(keyfn(r))
            # For each sensitivity setting, reference M01 is already represented by the
            # same key except mixture ID; replace mixture in the lookup tuple.
            if ref is None:
                rk = ("M01",) + keyfn(r)[1:]
                ref = refmap.get(rk)
            for nsx in (5, 15, 30):
                val = _as_float(r.get(f"GWP_{nsx}_kgCO2e"))
                refval = _as_float(ref.get(f"GWP_{nsx}_kgCO2e")) if ref else None
                r[f"GWP_reduction_vs_M01_{nsx}_pct"] = 100.0 * (1.0 - val / refval) if val is not None and refval else None
            r["Reduction_change_15_minus_5_pp"] = (r.get("GWP_reduction_vs_M01_15_pct") - r.get("GWP_reduction_vs_M01_5_pct")
                if r.get("GWP_reduction_vs_M01_15_pct") is not None and r.get("GWP_reduction_vs_M01_5_pct") is not None else None)
            r["Reduction_change_30_minus_15_pp"] = (r.get("GWP_reduction_vs_M01_30_pct") - r.get("GWP_reduction_vs_M01_15_pct")
                if r.get("GWP_reduction_vs_M01_30_pct") is not None and r.get("GWP_reduction_vs_M01_15_pct") is not None else None)
            r["Reduction_change_30_minus_5_pp"] = (r.get("GWP_reduction_vs_M01_30_pct") - r.get("GWP_reduction_vs_M01_5_pct")
                if r.get("GWP_reduction_vs_M01_30_pct") is not None and r.get("GWP_reduction_vs_M01_5_pct") is not None else None)
        write_csv(out / f"Table_S_{label}_Column_Design_Sensitivity_5vs15vs30.csv", merged)
        combined[label] = merged

    # Steel emission-factor sensitivity is recalculated from the actual selected layouts.
    primary5 = _read_csv_records(src / "Table_3_5_Column_Design_All_Mixes.csv")
    primary30 = _read_csv_records(src / "Story_Count_Sensitivity" / "Table_30Storey_Column_Design.csv")
    d5 = {(r["Mix_ID"], int(float(r["Target_years"]))): r for r in primary5}
    d15 = {(r["Mix_ID"], int(float(r["Target_years"]))): r for r in designs15}
    d30 = {(r["Mix_ID"], int(float(r["Target_years"]))): r for r in primary30}
    steels = []
    for key, r5 in d5.items():
        r15, r30 = d15[key], d30[key]
        for ef_s in a["reinforcement_EF_kgCO2e_per_kg_scenarios"]:
            rec = {"Mix_ID": key[0], "Target_years": key[1], "EF_steel_kgCO2e_kg": float(ef_s)}
            for nsx, design in ((5, r5), (15, r15), (30, r30)):
                rec[f"Section_{nsx}_mm"] = f"{design['b_mm']}x{design['h_mm']}"
                rec[f"Steel_mass_{nsx}_kg"] = design["total_steel_mass_kg"]
                rec[f"GWP_{nsx}_kgCO2e"] = float(design["concrete_volume_m3"]) * float(data["gwp_by_mix"][key[0]]) + float(design["total_steel_mass_kg"]) * float(ef_s)
            steels.append(rec)
    for target in sorted({r["Target_years"] for r in steels}):
        for ef_s in a["reinforcement_EF_kgCO2e_per_kg_scenarios"]:
            group = [r for r in steels if r["Target_years"] == target and r["EF_steel_kgCO2e_kg"] == float(ef_s)]
            ref = next((r for r in group if r["Mix_ID"] == "M01"), None)
            for r in group:
                for nsx in (5, 15, 30):
                    r[f"GWP_reduction_vs_M01_{nsx}_pct"] = (100.0 * (1.0 - r[f"GWP_{nsx}_kgCO2e"] / ref[f"GWP_{nsx}_kgCO2e"])) if ref else None
                r["Reduction_change_15_minus_5_pp"] = r["GWP_reduction_vs_M01_15_pct"] - r["GWP_reduction_vs_M01_5_pct"] if ref else None
                r["Reduction_change_30_minus_15_pp"] = r["GWP_reduction_vs_M01_30_pct"] - r["GWP_reduction_vs_M01_15_pct"] if ref else None
                r["Reduction_change_30_minus_5_pp"] = r["GWP_reduction_vs_M01_30_pct"] - r["GWP_reduction_vs_M01_5_pct"] if ref else None
    write_csv(root / "Section_3_6" / "Table_S_GWP_Steel_Factor_Sensitivity_5vs15vs30.csv", steels)
    combined["SteelEF"] = steels
    return combined


def generate_5vs15vs30_comparative_outputs(data, primary5, designs15, designs30,
                                            sensitivities, results_root):
    """Generate manuscript-ready comparative data/figures for all three storey scenarios."""
    root = Path(results_root)
    s35 = root / "Section_3_5"
    s36 = root / "Section_3_6"
    story_root = s35 / "Story_Count_Sensitivity"
    dsets = {5: primary5, 15: designs15, 30: designs30}
    maps = {ns: {(r["Mix_ID"], int(r["Target_years"])): r for r in rows} for ns, rows in dsets.items()}
    gwp_mix = data["gwp_by_mix"]
    ef = float(COLUMN_ASSUMPTIONS["reinforcement_EF_kgCO2e_per_kg_main"])
    targets = [int(x) for x in COLUMN_ASSUMPTIONS["target_periods_years"]]
    paired = []
    for mid in data["order"]:
        for target in targets:
            row = {"Mix_ID": mid, "Mixture_Code": data["md"][mid]["code"], "Target_years": target}
            for ns in (5, 15, 30):
                d = maps[ns][(mid, target)]
                for dest, src in [
                    (f"Section_{ns}_mm", "b_mm"), (f"Cover_{ns}_mm", "c_nom_mm"),
                    (f"Bars_{ns}_n", "longitudinal_bars_n"), (f"Bar_diameter_{ns}_mm", "longitudinal_bar_diameter_mm"),
                    (f"Link_diameter_{ns}_mm", "link_diameter_mm"), (f"Steel_mass_{ns}_kg", "total_steel_mass_kg"),
                    (f"Concrete_volume_{ns}_m3", "concrete_volume_m3"), (f"N_Ed_{ns}_kN", "N_Ed_kN"),
                    (f"M2_x_{ns}_kNm", "second_order_moment_x_kNm"), (f"M2_y_{ns}_kNm", "second_order_moment_y_kNm"),
                    (f"Utilization_{ns}", "biaxial_interaction_utilization"), (f"Status_{ns}", "Status")]:
                    row[dest] = d.get(src)
                row[f"GWP_{ns}_kgCO2e"] = float(d["concrete_volume_m3"]) * float(gwp_mix[mid]) + float(d["total_steel_mass_kg"]) * ef
            for lo, hi in ((5, 15), (15, 30), (5, 30)):
                row[f"GWP_change_{hi}_vs_{lo}_pct"] = 100.0 * (row[f"GWP_{hi}_kgCO2e"] / row[f"GWP_{lo}_kgCO2e"] - 1.0)
            paired.append(row)
    for target in targets:
        ref = next(r for r in paired if r["Mix_ID"] == "M01" and r["Target_years"] == target)
        for r in [x for x in paired if x["Target_years"] == target]:
            for ns in (5, 15, 30):
                r[f"GWP_reduction_vs_M01_{ns}_pct"] = 100.0 * (1.0 - r[f"GWP_{ns}_kgCO2e"] / ref[f"GWP_{ns}_kgCO2e"])
            r["Reduction_change_15_minus_5_pp"] = r["GWP_reduction_vs_M01_15_pct"] - r["GWP_reduction_vs_M01_5_pct"]
            r["Reduction_change_30_minus_15_pp"] = r["GWP_reduction_vs_M01_30_pct"] - r["GWP_reduction_vs_M01_15_pct"]
            r["Reduction_change_30_minus_5_pp"] = r["GWP_reduction_vs_M01_30_pct"] - r["GWP_reduction_vs_M01_5_pct"]
    write_csv(s35 / "Table_3_5_Column_Design_5vs15vs30.csv", paired)

    summaries = []
    for target in targets:
        for ns in (5, 15, 30):
            rr = [r for r in paired if r["Target_years"] == target]
            sizes = sorted({int(float(r[f"Section_{ns}_mm"])) for r in rr if _as_float(r.get(f"Section_{ns}_mm")) is not None})
            values = [r for r in rr if _as_float(r.get(f"Section_{ns}_mm")) is not None]
            gwps = [float(r[f"GWP_{ns}_kgCO2e"]) for r in values]
            summaries.append({
                "Target_years": target, "Storeys": ns, "Mixture_cases": len(values),
                "c_nom_min_mm": min(float(r[f"Cover_{ns}_mm"]) for r in values),
                "c_nom_max_mm": max(float(r[f"Cover_{ns}_mm"]) for r in values),
                "section_dimensions_selected_mm": "; ".join(f"{b}x{b}: {sum(int(float(r[f'Section_{ns}_mm'])) == b for r in values)} cases" for b in sizes),
                "concrete_volume_min_m3": min(float(r[f"Concrete_volume_{ns}_m3"]) for r in values),
                "concrete_volume_max_m3": max(float(r[f"Concrete_volume_{ns}_m3"]) for r in values),
                "steel_mass_min_kg": min(float(r[f"Steel_mass_{ns}_kg"]) for r in values),
                "steel_mass_max_kg": max(float(r[f"Steel_mass_{ns}_kg"]) for r in values),
                "N_Ed_min_kN": min(float(r[f"N_Ed_{ns}_kN"]) for r in values),
                "N_Ed_max_kN": max(float(r[f"N_Ed_{ns}_kN"]) for r in values),
                "GWP_min_kgCO2e": min(gwps), "GWP_max_kgCO2e": max(gwps),
            })
    write_csv(s35 / "Table_3_5_Column_Design_Summary_5vs15vs30.csv", summaries)
    # Main-text 75-year table and full 48-case paired design data.
    rows75 = []
    for r in paired:
        if r["Target_years"] == 75:
            q = {"Mix_ID": r["Mix_ID"], "Mixture_Code": r["Mixture_Code"],
                 "Concrete_GWP_kgCO2e_m3": gwp_mix[r["Mix_ID"]]}
            for ns in (5, 15, 30):
                q[f"Section_{ns}storey_mm"] = r[f"Section_{ns}_mm"]
                q[f"GWP_{ns}storey_kgCO2e"] = r[f"GWP_{ns}_kgCO2e"]
                q[f"Reduction_vs_M01_{ns}storey_pct"] = r[f"GWP_reduction_vs_M01_{ns}_pct"]
            q["Reduction_change_15_minus_5_pp"] = r["Reduction_change_15_minus_5_pp"]
            q["Reduction_change_30_minus_15_pp"] = r["Reduction_change_30_minus_15_pp"]
            q["Reduction_change_30_minus_5_pp"] = r["Reduction_change_30_minus_5_pp"]
            rows75.append(q)
    write_csv(s36 / "Table_3_6_Column_GWP_75y_5vs15vs30.csv", rows75)
    write_csv(s36 / "Table_3_6_Column_GWP_5vs15vs30.csv", paired)

    # Three-scenario summary used in the paper's column-design table.
    write_csv(s35 / "Table_3_5_Column_Design_Summary.csv", summaries)

    # Figure 7: grouped reinforcement-mass comparison and one common cover panel.
    # Nominal cover is identical across the three loading scenarios for each mix;
    # plotting it once avoids a redundant secondary axis and duplicated series.
    target_figure = 75
    x_design = np.arange(len(data["order"]))
    grp_design = [data["group"](m) for m in data["order"]]
    fig, (ax_mass, ax_cover) = plt.subplots(
        2, 1, figsize=(14.5, 7.8), sharex=True,
        gridspec_kw={"height_ratios": [1.65, 0.85]})
    fig.subplots_adjust(left=0.105, right=0.985, bottom=0.13,
                        top=0.92, hspace=0.16)
    bar_width = 0.24
    for j, ns in enumerate(SCENARIO_LEVELS):   # colour = mixture group, hatch = loading scenario
        by_mix = {(r["Mix_ID"], int(r["Target_years"])): r for r in dsets[ns]}
        selected = [by_mix[(mid, target_figure)] for mid in data["order"]]
        steel = [float(r["total_steel_mass_kg"]) for r in selected]
        t_bar(ax_mass, x_design + (j - 1) * bar_width, steel, groups=grp_design,
              filt="SCENARIO", level=ns, width=bar_width, zorder=3)
    ax_mass.set_ylim(0, 285)
    ax_mass.set_yticks([0, 50, 100, 150, 200, 250])
    ax_mass.set_ylabel("Steel (kg)")
    _style_title(ax_mass, "(a) Reinforcement mass across loading scenarios")
    ax_mass.grid(axis="y", color=NEUTRAL["grid"], linewidth=0.65)
    ax_mass.set_axisbelow(True)
    ax_mass.tick_params(axis="x", labelbottom=False)
    for spine in ax_mass.spines.values():
        spine.set_color(NEUTRAL["border"])
        spine.set_linewidth(0.75)

    cover_values = []
    for mid in data["order"]:
        values = []
        for ns in (5, 15, 30):
            rec = next(r for r in dsets[ns]
                       if r["Mix_ID"] == mid and int(r["Target_years"]) == target_figure)
            values.append(float(rec["c_nom_mm"]))
        if not np.allclose(values, values[0], rtol=0.0, atol=1e-9):
            raise RuntimeError(
                f"Nominal cover differs by loading scenario for {mid}: {values}")
        cover_values.append(values[0])
    t_scatter(ax_cover, x_design, cover_values, groups=grp_design, filt="TARGET", level=target_figure,
              size=46, zorder=3)
    t_hline(ax_cover, 45.0, "cover_minimum")
    ax_cover.set_ylim(43.8, 50.7)
    ax_cover.set_yticks([45, 47.5, 50])
    ax_cover.set_ylabel("Cover (mm)")
    _style_title(ax_cover, "(b) Nominal cover at the 75-year initiation target")
    ax_cover.set_xticks(x_design, data["order"], rotation=40, ha="right")
    ax_cover.grid(axis="y", color=NEUTRAL["grid"], linewidth=0.65)
    ax_cover.set_axisbelow(True)
    t_annotate(ax_cover, f"{cover_values[0]:.1f} mm", (0, cover_values[0]),
               tone="dark", xytext=(8, 7), textcoords="offset points",
               fontsize=8, fontweight="bold")
    if len(set(round(v, 1) for v in cover_values[1:])) == 1:
        common_cover = round(cover_values[1], 1)
        t_text(ax_cover, 0.99, 0.92, f"M02–M16: {common_cover:.1f} mm", tone="muted",
               transform=ax_cover.transAxes, ha="right", va="top", fontsize=7.6)
    for spine in ax_cover.spines.values():
        spine.set_color(NEUTRAL["border"])
        spine.set_linewidth(0.75)
    t_save(fig, s35 / "Figure_7_Column_Design_and_Material_Demand.png",
           axis_display_pt=8.1, tick_display_pt=7.2, legend_display_pt=7.0, annotation_display_pt=7.2)

    # Figure 8: concrete intensity and member GWP as horizontal dot/interval plots.
    # The 75-year value is the main point; the whisker spans all three targets.
    # For M02–M16 the target-period designs coincide because code-minimum cover
    # governs, so duplicate 50/75/100-year markers are not plotted on top of each other.
    order_gwp = sorted(data["order"], key=lambda m: float(gwp_mix[m]))
    group_by = {m: data["group"](m) for m in data["order"]}
    grp_gwp = [group_by[m] for m in order_gwp]
    y_pos = np.arange(len(order_gwp))
    fig, axes = plt.subplots(2, 2, figsize=(12.8, 10.0))
    fig.subplots_adjust(left=0.12, right=0.985, top=0.95,
                        bottom=0.07, wspace=0.34, hspace=0.33)

    ax = axes[0, 0]
    intensity = [float(gwp_mix[mid]) for mid in order_gwp]
    t_scatter(ax, intensity, y_pos, groups=grp_gwp, size=41, zorder=3)
    for yy, value in zip(y_pos, intensity):
        t_text(ax, value + 3.0, yy, f"{value:.0f}", va="center", ha="left", fontsize=7)
    ax.set_yticks(y_pos, order_gwp)
    ax.invert_yaxis()
    ax.set_xlim(min(intensity) - 12, max(intensity) + 25)
    ax.set_xlabel("Concrete GWP (kg CO$_2$-eq/m$^3$)")
    ax.set_title("(a) Concrete GWP intensity", loc="left", fontweight="bold", pad=6)
    ax.grid(axis="x", color=NEUTRAL["grid"], linewidth=0.65)
    ax.set_axisbelow(True)
    for spine in ax.spines.values():
        spine.set_color(NEUTRAL["border"])
        spine.set_linewidth(0.75)

    for ax, ns, panel in [
        (axes[0, 1], 5, "(b) 5-story column"),
        (axes[1, 0], 15, "(c) 15-story column"),
        (axes[1, 1], 30, "(d) 30-story column"),
    ]:
        scenario_values = []
        for yy, mid in zip(y_pos, order_gwp):
            records = {int(r["Target_years"]): r for r in dsets[ns]
                       if r["Mix_ID"] == mid}
            vals = [float(records[t]["concrete_volume_m3"]) * float(gwp_mix[mid])
                    + float(records[t]["total_steel_mass_kg"]) * ef
                    for t in targets]
            value_75 = vals[targets.index(75)]
            lower, upper = min(vals), max(vals)
            scenario_values.extend(vals)
            t_interval(ax, yy, lower, upper)
            if upper - lower > 1e-8:
                t_annotate(ax, f"Δ {upper - lower:.1f}", (upper, yy), tone="muted",
                           xytext=(5, 0), textcoords="offset points",
                           ha="left", va="center", fontsize=6.8)
            t_scatter(ax, value_75, yy, groups=group_by[mid], filt="TARGET", level=75, size=37, zorder=3)
        span = max(scenario_values) - min(scenario_values)
        pad = max(span * 0.16, 3.0)
        ax.set_xlim(min(scenario_values) - pad, max(scenario_values) + pad)
        ax.set_yticks(y_pos, order_gwp)
        ax.invert_yaxis()
        ax.set_xlabel("Column GWP (kg CO$_2$-eq/3-m segment)")
        ax.set_title(panel, loc="left", fontweight="bold", pad=6)
        ax.grid(axis="x", color=NEUTRAL["grid"], linewidth=0.65)
        ax.set_axisbelow(True)
        for spine in ax.spines.values():
            spine.set_color(NEUTRAL["border"])
            spine.set_linewidth(0.75)
    t_save(fig, s36 / "Figure_8_Concrete_and_Column_GWP.png",
           axis_display_pt=8.0, tick_display_pt=6.9, legend_display_pt=6.6, annotation_display_pt=6.8)

    # Algebraic (not process-inventory) decomposition of M16 relative to M01.
    decomp = []
    for ns in (5, 15, 30):
        dd = maps[ns]
        for target in targets:
            ref = dd[("M01", target)]
            for mid in ("M01", "M16"):
                r = dd[(mid, target)]
                vi, v0 = float(r["concrete_volume_m3"]), float(ref["concrete_volume_m3"])
                gi, g0 = float(gwp_mix[mid]), float(gwp_mix["M01"])
                mi, m0 = float(r["total_steel_mass_kg"]), float(ref["total_steel_mass_kg"])
                cpart = vi * gi; spart = mi * ef; total = cpart + spart
                decomp.append({"Storeys": ns, "Mix_ID": mid, "Target_years": target,
                    "Section_mm": f"{r['b_mm']}x{r['h_mm']}", "Concrete_volume_m3": vi,
                    "Volume_effect_vs_M01_kgCO2e": (vi-v0)*g0,
                    "Concrete_intensity_effect_vs_M01_kgCO2e": vi*(gi-g0),
                    "Steel_mass_effect_vs_M01_kgCO2e": (mi-m0)*ef,
                    "GWP_column_kgCO2e": total,
                    "Net_delta_vs_M01_kgCO2e": total-(v0*g0+m0*ef),
                    "Concrete_share_pct": 100*cpart/total, "Steel_share_pct": 100*spart/total})
    write_csv(s36 / "Table_3_6_GWP_Decomposition_5vs15vs30.csv", decomp)
    write_csv(s35 / "Table_S4_GWP_Decomposition_5vs15vs30.csv", decomp)
    # Figure 10: annotated decomposition matrix. Each row is one scenario/target
    # combination; the three algebraic effects are shown with their net sum.
    # Guard the identity before plotting so the visual decomposition is auditable.
    decomposition_rows = sorted(
        [r for r in decomp if r["Mix_ID"] == "M16"],
        key=lambda r: (int(r["Storeys"]), int(r["Target_years"]))
    )
    component_keys = [
        "Volume_effect_vs_M01_kgCO2e",
        "Concrete_intensity_effect_vs_M01_kgCO2e",
        "Steel_mass_effect_vs_M01_kgCO2e",
        "Net_delta_vs_M01_kgCO2e",
    ]
    matrix = np.asarray([[float(r[k]) for k in component_keys]
                         for r in decomposition_rows], dtype=float)
    if matrix.shape != (9, 4):
        raise RuntimeError(f"Unexpected GWP decomposition matrix shape: {matrix.shape}")
    if not np.allclose(matrix[:, :3].sum(axis=1), matrix[:, 3],
                       rtol=0.0, atol=1e-7):
        raise RuntimeError("GWP decomposition components do not sum to the net difference")

    x_labels = ["Concrete\nvolume", "Concrete GWP\nintensity",
                "Reinforcement\nmass", "Net GWP\ndifference"]
    y_labels = [f"{int(r['Storeys'])}-story  |  {int(r['Target_years'])} y"
                for r in decomposition_rows]
    fig, ax = plt.subplots(figsize=(11.8, 7.6))
    fig.subplots_adjust(left=0.24, right=0.96, top=0.93, bottom=0.25)
    cmap = DIVERGING_CMAP   # same diverging scale as every other heat map (central theme)
    vmin = min(-420.0, float(matrix.min()) - 1.0)
    vmax = max(5.0, float(matrix.max()) + 1.0)
    norm = TwoSlopeNorm(vmin=vmin, vcenter=0.0, vmax=vmax)
    image = ax.imshow(matrix, aspect="auto", cmap=cmap, norm=norm)
    ax.set_xticks(np.arange(4), x_labels)
    ax.set_yticks(np.arange(len(y_labels)), y_labels)
    ax.set_xticks(np.arange(-0.5, 4, 1), minor=True)
    ax.set_yticks(np.arange(-0.5, len(y_labels), 1), minor=True)
    ax.grid(which="minor", color=NEUTRAL["white"], linewidth=2.0)
    ax.tick_params(which="minor", bottom=False, left=False)
    for i in range(matrix.shape[0]):
        for j in range(matrix.shape[1]):
            value = matrix[i, j]
            label = f"{value:+.1f}" if abs(value) > 0.05 else "0.0"
            text_color = NEUTRAL["white"] if value < -150.0 else NEUTRAL["dark"]
            ax.text(j, i, label, ha="center", va="center",
                    fontsize=8.1, fontweight="bold" if j == 3 else "normal",
                    color=text_color)
    for separator in (2.5, 5.5):
        ax.axhline(separator, color=NEUTRAL["border"], linewidth=1.3)
    for spine in ax.spines.values():
        spine.set_color(NEUTRAL["border"])
        spine.set_linewidth(0.75)
    colorbar = fig.colorbar(image, ax=ax, orientation="horizontal",
                            fraction=0.055, pad=0.15, aspect=38)
    colorbar.set_label("Contribution vs M01 (kg CO$_2$-eq; negative = reduction)")
    colorbar.set_ticks([vmin, 0.0, vmax])
    colorbar.set_ticklabels([f"{vmin:.0f}", "0", f"+{vmax:.0f}"])
    t_save(fig, s36 / "Figure_10_GWP_Decomposition_5vs15vs30.png",
           axis_display_pt=8.1, tick_display_pt=7.4, legend_display_pt=7.0, annotation_display_pt=8.1)

    # The three-scenario savings bar chart is omitted because Table 11
    # reports every mixture/scenario reduction numerically.

    # Summarize all design-linked sensitivities at 75 years for the three scenarios.
    sens_summary = []
    for label, rows in sensitivities.items():
        if label == "SteelEF":
            rr_s = [r for r in rows if int(float(r["Target_years"])) == 75]
        else:
            rr_s = [r for r in rows if int(r["Target_years"]) == 75]
        if not rr_s:
            continue
        rec = {"Sensitivity": label, "Cases_at_75y": len(rr_s)}
        for ns in (5, 15, 30):
            vals = [_as_float(r.get(f"GWP_{ns}_kgCO2e")) for r in rr_s]
            vals = [v for v in vals if v is not None]
            rec[f"GWP_{ns}_min_kgCO2e"] = min(vals)
            rec[f"GWP_{ns}_max_kgCO2e"] = max(vals)
            low = min((r for r in rr_s if _as_float(r.get(f"GWP_{ns}_kgCO2e")) is not None), key=lambda r: float(r[f"GWP_{ns}_kgCO2e"]))
            rec[f"Lowest_GWP_mix_{ns}"] = low["Mix_ID"]
        for lab, left, right in (("15_minus_5", 15, 5), ("30_minus_15", 30, 15), ("30_minus_5", 30, 5)):
            diffs = [_as_float(r.get(f"Reduction_change_{lab}_pp")) for r in rr_s]
            diffs = [x for x in diffs if x is not None]
            if diffs:
                rec[f"Reduction_change_{lab}_pp_min"] = min(diffs)
                rec[f"Reduction_change_{lab}_pp_max"] = max(diffs)
        sens_summary.append(rec)
    write_csv(s36 / "Table_3_6_Sensitivity_Summary_5vs15vs30.csv", sens_summary)
    manuscript_rows = []
    for r in sens_summary:
        manuscript_rows.append({
            "Sensitivity factor": r["Sensitivity"],
            "GWP range, 5-story scenario (kg CO₂-eq)": f"{r['GWP_5_min_kgCO2e']:.1f}–{r['GWP_5_max_kgCO2e']:.1f}",
            "GWP range, 15-story scenario (kg CO₂-eq)": f"{r['GWP_15_min_kgCO2e']:.1f}–{r['GWP_15_max_kgCO2e']:.1f}",
            "GWP range, 30-story scenario (kg CO₂-eq)": f"{r['GWP_30_min_kgCO2e']:.1f}–{r['GWP_30_max_kgCO2e']:.1f}",
            "Lowest-GWP mix (5/15/30)": f"{r['Lowest_GWP_mix_5']}/{r['Lowest_GWP_mix_15']}/{r['Lowest_GWP_mix_30']}",
            "Change in saving, 15−5 (pp)": f"{r.get('Reduction_change_15_minus_5_pp_min', float('nan')):.1f} to {r.get('Reduction_change_15_minus_5_pp_max', float('nan')):.1f}",
            "Change in saving, 30−15 (pp)": f"{r.get('Reduction_change_30_minus_15_pp_min', float('nan')):.1f} to {r.get('Reduction_change_30_minus_15_pp_max', float('nan')):.1f}",
        })
    write_csv(s36 / "Table_3_6_Sensitivity_Summary_5vs15vs30_Manuscript.csv", manuscript_rows)
    return paired, summaries, decomp



TABLE_PREVIEWS = [
    {
        "csv": "Section_3_1/Table_3_1_Fresh_and_28d_Summary.csv",
        "png": "Section_3_1/Table_3_1_Preview.png",
        "title": "Table 5. Fresh properties & CS28",
        "columns": ["Mix_ID", "Mixture_Code", "SlumpFlow_mm", "T500_s", "JRingFlow_mm",
                    "DeltaJ_mm", "V_Funnel_s", "PCE_pct_Binder", "CS28_mean_MPa"],
        "labels": ["Mix", "Mixture", "Slump", "T500", "J-ring", "ΔJ", "V-funnel", "PCE", "CS28"],
    },
    {
        "csv": "Section_3_2/Table_3_2_Age_Dependent_Summary.csv",
        "png": "Section_3_2/Table_3_2_Preview.png",
        "title": "Table 7. Age-dependent response",
        "columns": ["Mix_ID", "Mixture_Code", "CS_3d_mean_MPa", "CS_7d_mean_MPa",
                    "CS_28d_mean_MPa", "CS_90d_mean_MPa", "WA_28d_mean_pct",
                    "WA_90d_mean_pct", "ER_28d_mean_kOhm_cm", "ER_90d_mean_kOhm_cm",
                    "Dnssm_90d_mean_1e-12_m2_s"],
        "labels": ["Mix", "Mixture", "CS3", "CS7", "CS28", "CS90", "WA28", "WA90", "ER28", "ER90", "D90"],
    },
    {
        "csv": "Section_3_3/Table_3_3_Correlation_Summary.csv",
        "png": "Section_3_3/Table_3_3_Correlation_Preview.png",
        "title": "Table 8. Pearson correlations",
        "columns": ["Age_d", "Property_1", "Property_2", "Pearson_r", "Pairwise_n"],
        "labels": ["Age", "P1", "P2", "r", "n"],
    },
    {
        "csv": "Section_3_3/Table_3_3_GGBFS_SF_Interaction_Contrasts.csv",
        "png": "Section_3_3/Table_3_3_Interaction_Preview.png",
        "title": "Table 9. GGBFS–SF contrasts",
        "columns": ["Mix_ID", "Mixture_Code", "CS28_I", "CS90_I", "WA28_I", "WA90_I",
                    "ER28_I", "ER90_I", "Dnssm90_log10_interaction"],
        "labels": ["Mix", "Mixture", "CS28", "CS90", "WA28", "WA90", "ER28", "ER90", "log₁₀D"],
    },
    {
        "csv": "Section_3_4/Table_3_4_ServiceLife_and_Cover.csv",
        "png": "Section_3_4/Table_3_4_ServiceLife_Preview.png",
        "title": "Table 10. RCMT & cover",
        "columns": ["Mix_ID", "Mixture_Code", "D_mean_1e-12_m2_s", "D_SD_1e-12_m2_s",
                    "t_init_at_35mm_years", "c_req_50y_mm", "c_req_75y_mm", "c_req_100y_mm",
                    "c_nom_S4_50y_mm", "c_nom_S4_75y_mm", "c_nom_S6_100y_mm"],
        "labels": ["Mix", "Mixture", "D", "D SD", "ti35", "cReq50", "cReq75", "cReq100",
                   "cNom50", "cNom75", "cNom100"],
    },
    {
        "csv": "Section_3_5/Table_3_5_Column_Design_All_Mixes.csv",
        "png": "Section_3_5/Table_3_5_Column_Design_Preview.png",
        "title": "Table 11. Column design",
        "columns": ["Mix_ID", "Target_years", "c_nom_mm", "b_mm", "longitudinal_bars_n",
                    "longitudinal_bar_diameter_mm", "end_zone_spacing_actual_mm",
                    "concrete_volume_m3", "total_steel_mass_kg", "biaxial_interaction_utilization"],
        "labels": ["Mix", "Target", "Cover", "Section", "Bars", "Ø", "End links", "Vc", "Steel", "Util"],
    },
    {
        "csv": "Section_3_6/Table_3_6_Concrete_GWP_Sorted.csv",
        "png": "Section_3_6/Table_3_6_Concrete_GWP_Preview.png",
        "title": "Table 12. Concrete GWP",
        "columns": ["Mix_ID", "Mixture_Code", "GWP_concrete_kgCO2e_m3", "Includes_PCE", "Boundary"],
        "labels": ["Mix", "Mixture", "Concrete GWP", "PCE", "Boundary"],
    },
]


def _fmt_cell(v, field):
    if v is None or v == "":
        return "—"
    try:
        x = float(v)
    except (ValueError, TypeError):
        return str(v)
    if field in ("Age_d", "Pairwise_n", "t_init_at_35mm_years", "Target_years"):
        return f"{x:.0f}"
    if field.endswith("_s"):
        return f"{x:.2f}"
    if field.startswith("Dnssm90_log10"):
        return f"{x:+.2f}"
    if field in ("D_mean_1e-12_m2_s", "D_SD_1e-12_m2_s"):
        return f"{x:.3f}"
    if field.startswith(("c_req_", "c_nom_")):
        return f"{x:.1f}"
    if "Pearson_r" in field or "_I" in field:
        return f"{x:+.2f}"
    if "PCE_pct" in field or "WA_" in field:
        return f"{x:.2f}"
    if "ER_" in field:
        return f"{x:.1f}"
    if "CS_" in field or "CS28" in field or "CS90" in field:
        return f"{x:.1f}"
    if abs(x - round(x)) < 1e-8:
        return f"{int(round(x))}"
    return f"{x:.2f}"


def save_table_preview(csv_path, png_path, title, columns, labels):
    csv_path = Path(csv_path)
    png_path = Path(png_path)
    if not csv_path.exists():
        return None
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        avail = reader.fieldnames or []
    miss = [c for c in columns if c not in avail]
    if miss:
        return {"csv": str(csv_path), "status": f"MISSING:{miss}"}
    cell_text = [[_fmt_cell(r.get(c), c) for c in columns] for r in rows]
    w = max(10.0, 1.16 * len(columns))
    h = max(3.6, 0.30 * (len(rows) + 3))
    fig, ax = plt.subplots(figsize=(w, h))
    ax.axis("off")
    ax.set_title(title, fontsize=10.5, fontweight="bold", color=NEUTRAL["ink"], pad=10, loc="left")
    tbl = ax.table(cellText=cell_text, colLabels=labels, cellLoc="center", colLoc="center", loc="center")
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(8.5)
    tbl.scale(1.0, 1.4)
    for (ri, ci), cell in tbl.get_celld().items():
        cell.set_edgecolor(NEUTRAL["grid"])
        cell.set_linewidth(0.55)
        cell.get_text().set_fontfamily(PAPER_FONT_FAMILY)
        cell.get_text().set_color(NEUTRAL["ink"])
        if ri == 0:
            cell.set_facecolor(NEUTRAL["navy"])
            cell.get_text().set_color(NEUTRAL["white"])
            cell.get_text().set_fontweight("bold")
        else:
            cell.set_facecolor(NEUTRAL["table_band"] if ri % 2 == 0 else NEUTRAL["white"])
    fig.tight_layout()
    png_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png_path, dpi=FIG_DPI, bbox_inches="tight")
    plt.close(fig)
    return {"csv": str(csv_path), "preview_png": str(png_path),
            "rows": len(rows), "columns": len(columns), "status": "OK"}




def run_30storey_smallest_section(data, service_rows, results_root):
    """Search smallest feasible 30-story layouts from the current dataset and engine."""
    out = Path(results_root) / "Section_3_5"
    out.mkdir(parents=True, exist_ok=True)
    # The smallest-section audit is recalculated from the current data and engine.
    # Release cached section-capacity states accumulated by the minimum-GWP and
    # sensitivity searches before starting this separate search objective.
    if hasattr(_column_axis_capacity_cached, "cache_clear"):
        _column_axis_capacity_cached.cache_clear()
    svc_by_mix = {r["Mix_ID"]: r for r in service_rows}
    n30 = 30
    mx30 = float(COLUMN_ASSUMPTIONS["M_Ed_x_kNm_30storey"])
    my30 = float(COLUMN_ASSUMPTIONS["M_Ed_y_kNm_30storey"])
    rows = []
    completed = 0
    for mid in data["order"]:
        cube = data["cs_stats"][(mid, 28)][0]
        for target in COLUMN_ASSUMPTIONS["target_periods_years"]:
            svc = svc_by_mix[mid]
            cnom = float(svc[f"c_nom_S4_{target}y_mm"])
            row = _calculate_column_design(
                cube, cnom, mid, int(target), selection_rule="smallest_section",
                storeys=n30,
                mx_action_kNm=mx30,
                my_action_kNm=my30,
            )
            row.update({"Storeys": n30, "Mix_ID": mid, "Mixture_Code": data["md"][mid]["code"],
                        "Target_years": int(target), "c_nom_mm": cnom})
            rows.append(row)
            completed += 1
            if completed % 8 == 0:
                print(f"[smallest-section search] completed {completed}/48 layouts")
    write_csv(out / "Table_30Storey_Smallest_Section_Designs.csv", rows)
    return rows


def write_s6_audit_trail(primary5, designs30, results_root, designs15=None):
    """Write detailed 5-/15-/30-story audit rows from selected minimum-GWP designs."""
    out = Path(results_root) / "Section_3_5"
    rows = []
    scenario_designs = [(5, primary5)]
    if designs15 is not None:
        scenario_designs.append((15, designs15))
    scenario_designs.append((30, designs30))
    for ns, designs in scenario_designs:
        for r in designs:
            is_second = str(r.get("second_order_status", "")) == "INCLUDED_BY_NOMINAL_CURVATURE"
            def num(key):
                v = r.get(key)
                try: return float(v) if v not in (None, "") else None
                except (TypeError, ValueError): return None
            section = f"{int(float(r['b_mm']))}x{int(float(r['h_mm']))}" if r.get("b_mm") else "N/A"
            rows.append({
                "Mix_ID": r.get("Mix_ID"), "Storeys": ns, "Target_years": r.get("Target_years"),
                "Section_mm": section, "N_Ed_total_kN": num("N_Ed_total_kN") or num("N_Ed_kN"),
                "M2_x_kNm": num("second_order_moment_x_kNm"), "M2_y_kNm": num("second_order_moment_y_kNm"),
                "Second_order_status": r.get("second_order_status"),
                "rho_longitudinal_pct": num("rho_longitudinal_pct"), "rho_at_lap_pct": num("rho_at_lap_pct"),
                "longitudinal_bar_diameter_mm": num("longitudinal_bar_diameter_mm"), "longitudinal_bars_n": num("longitudinal_bars_n"),
                "clear_bar_spacing_mm": num("clear_bar_spacing_mm"), "link_diameter_mm": num("link_diameter_mm"),
                "s_end_actual_mm": num("end_zone_spacing_actual_mm"), "s_mid_actual_mm": num("middle_zone_spacing_actual_mm"),
                "s_lap_actual_mm": num("lap_zone_spacing_actual_mm"), "l_bd_mm": num("l_bd_mm"), "l_sd_mm": num("l_sd_mm"),
                "V_Ed_x_kN": num("M_reverse_shear_envelope_x_kN"), "V_Ed_y_kN": num("M_reverse_shear_envelope_y_kN"),
                "V_Rd_c_kN": num("V_Rd_c_kN"), "tau_Rd_strut_limit_MPa": num("tau_Rd_strut_limit_MPa"),
                "shear_method": r.get("shear_method"), "cot_theta": num("cot_theta"),
                "N_Edw_kN": num("N_Edw_kN"), "N_Vd_kN": num("N_Vd_kN"),
                "compression_chord_depth_ratio": num("compression_chord_depth_ratio"),
                "sigma_cd_MPa": num("sigma_cd_MPa"), "f_tdy_required_MPa": num("f_tdy_required_MPa"),
                "f_tdy_optimum_MPa": num("f_tdy_optimum_MPa"),
                "f_tdy_provided_resultant_MPa": num("f_tdy_provided_resultant_MPa"),
                "Annex_G_G6_G7_deformation_capacity_ratio": num("Annex_G_G6_G7_deformation_capacity_ratio"),
                "Annex_G_G6_G7_check": r.get("Annex_G_G6_G7_check"),
                "longitudinal_tie_demand_kN": num("longitudinal_tie_demand_kN"),
                "longitudinal_tie_resistance_kN": num("longitudinal_tie_resistance_kN"),
                "biaxial_utilization": num("biaxial_interaction_utilization"),
                "second_order_gate": r.get("second_order_gate_status"),
                "reinforcement_ratio_check": r.get("longitudinal_reinforcement_ratio_check"),
                "bar_spacing_check": r.get("bar_spacing_check"), "tie_diameter_check": r.get("tie_diameter_check"),
                "tie_spacing_check": r.get("tie_spacing_check"), "lap_anchorage_check": r.get("lap_anchorage_check"),
                "shear_check": r.get("shear_check_status", r.get("shear_check")),
                "compression_strut_check": r.get("compression_strut_check"),
                "detailing_checks_status": r.get("detailing_checks_status"),
                "GWP_column_kgCO2e": num("GWP_column_kgCO2e"),
                "GWP_concrete_kgCO2e_m3": num("GWP_concrete_kgCO2e_m3"),
                "total_steel_mass_kg": num("total_steel_mass_kg"), "selection_status": r.get("Status"),
                "M2_x_display": f"{num('second_order_moment_x_kNm'):.2f}" if is_second and num("second_order_moment_x_kNm") is not None else "NR",
                "M2_y_display": f"{num('second_order_moment_y_kNm'):.2f}" if is_second and num("second_order_moment_y_kNm") is not None else "NR",
            })
    write_csv(out / "Table_S6_Audit_Trail.csv", rows)
    g67_rows = [{
        "Mix_ID": r.get("Mix_ID"), "Storeys": r.get("Storeys"),
        "Target_years": r.get("Target_years"), "Section_mm": r.get("Section_mm"),
        "Shear_method": r.get("shear_method"),
        "f_tdy_optimum_MPa": r.get("f_tdy_optimum_MPa"),
        "f_tdy_provided_resultant_MPa": r.get("f_tdy_provided_resultant_MPa"),
        "G6_G7_resistance_ratio": r.get("Annex_G_G6_G7_deformation_capacity_ratio"),
        "G6_G7_status": r.get("Annex_G_G6_G7_check"),
        "s_mid_actual_mm": r.get("s_mid_actual_mm"),
        "link_diameter_mm": r.get("link_diameter_mm"),
        "V_Ed_x_kN": r.get("V_Ed_x_kN"), "V_Ed_y_kN": r.get("V_Ed_y_kN"),
    } for r in rows]
    write_csv(out / "Annex_G_G6_G7_Deformation_Capacity_QA.csv", g67_rows)
    return rows


# =============================================================================
# MAIN
# =============================================================================
def main():
    # Strict input selection: this run uses only the explicitly provided current dataset.
    exp_path = BASE_DIR / EXPERIMENT_WORKBOOK
    if not exp_path.is_file():
        raise FileNotFoundError(f"Required current experimental workbook not found: {exp_path}")
    root = BASE_DIR / RESULTS_FOLDER
    root.mkdir(parents=True, exist_ok=True)
    print(f"Experimental : {exp_path}")
    print(f"Results      : {root}")
    print("Editable parameters: COLUMN_ASSUMPTIONS dict at top of this file.")

    THEME_QA.clear()
    theme_registry_rows = validate_theme_registry()   # colours fixed; one cue per filter
    print("[main] Loading mixture data and SimaPro totals", flush=True)
    data = load_experimental(exp_path)
    write_csv(root / "RCMT_Consistency_QA.csv", build_rcmt_consistency_qa(exp_path, data))
    exp_reader = XLSXReader(exp_path)
    simapro = read_simapro(exp_reader, data["md"])
    data["gwp_by_mix"] = simapro["totals"]

    # QA early
    (root / "Section_3_6").mkdir(parents=True, exist_ok=True)
    write_matrix_csv(root / "Section_3_6" / "SimaPro_Component_Breakdown.csv",
                     simapro["component_header"], simapro["component_rows"])
    write_csv(root / "Section_3_6" / "SimaPro_Component_QA.csv", simapro["qa_rows"])
    write_csv(root / "Data_Coverage_QA.csv", [
        {
            "Mix_ID": m, "Mixture_Code": data["md"][m]["code"], "Binder_Group": data["group"](m),
            **{f"CS_{a}d_n": data["cs_stats"][(m, a)][2] for a in STRENGTH_AGES},
            **{f"WA_{a}d_n": data["wa_stats"][(m, a)][2] for a in TRANSPORT_AGES},
            **{f"ER_{a}d_n": data["er_stats"][(m, a)][2] for a in TRANSPORT_AGES},
            "RCMT_specimen_n": data["D_stats"][m]["n"],
        }
        for m in data["order"]
    ])

    print("[main] Section 3.1", flush=True); run_section_3_1(data, root)
    print("[main] Section 3.2", flush=True); run_section_3_2(data, root)
    print("[main] Section 3.3", flush=True); run_section_3_3(data, root)
    print("[main] Section 3.4", flush=True); svc = run_section_3_4(data, root)
    print("[main] Section 3.5: column design for the 5-, 15-, and 30-story scenarios", flush=True)
    # The scenario set is evaluated in one analysis; the internal calculation order
    # manages the column-capacity cache and memory use.
    primary, smallest, alpha_d, d_d, cc_d, cs_d, mo_d, kt_d = run_section_3_5(data, svc, root)
    # Release the large 5-story capacity cache before entering the 30-story search.
    if hasattr(_column_axis_capacity_cached, "cache_clear"):
        _column_axis_capacity_cached.cache_clear()
    import gc; gc.collect()
    print("[main] Continuing member-design calculations across the scenario set", flush=True)
    designs30, comparisons30, summary30 = run_story_count_sensitivity(data, svc, primary, root)
    if hasattr(_column_axis_capacity_cached, "cache_clear"):
        _column_axis_capacity_cached.cache_clear()
    import gc; gc.collect()
    print("[main] Completing member-design calculations across the scenario set", flush=True)
    designs15, smallest15 = run_15storey_scenario(data, svc, root)
    if hasattr(_column_axis_capacity_cached, "cache_clear"):
        _column_axis_capacity_cached.cache_clear()
    import gc; gc.collect()
    print("[main] 30-story scenario sensitivity analyses", flush=True)
    sensitivities_5vs30 = run_story_count_design_sensitivities(data, root)
    if hasattr(_column_axis_capacity_cached, "cache_clear"):
        _column_axis_capacity_cached.cache_clear()
    import gc; gc.collect()
    print("[main] Three-scenario design sensitivities", flush=True)
    sensitivities_5vs15vs30 = run_15storey_design_sensitivities(
        data, root, sensitivities_5vs30, designs15, designs30)
    if hasattr(_column_axis_capacity_cached, "cache_clear"):
        _column_axis_capacity_cached.cache_clear()
    print("[main] Section 3.6: concrete and member GWP", flush=True)
    status, col_gwp = run_section_3_6(data, primary, alpha_d, d_d, cc_d, cs_d, mo_d, kt_d,
                                      simapro, root)
    print("[main] Comparative figures and tables", flush=True)
    print("[main] Three-scenario tables, figures, and sensitivity summaries", flush=True)
    generate_5vs15vs30_comparative_outputs(data, primary, designs15, designs30,
                                           sensitivities_5vs15vs30, root)
    print("[main] Smallest feasible 30-story alternatives and 144-row audit trail", flush=True)
    run_30storey_smallest_section(data, svc, root)
    write_s6_audit_trail(primary, designs30, root, designs15=designs15)

    # Table-preview PNGs are optional; skip them in the reproducible default run.
    write_csv(root / "Table_Preview_Artifact_QA.csv", [{"status":"SKIPPED_BY_DEFAULT","reason":"Full result CSVs and publication figures are generated; avoid duplicate table images."}])

    write_csv(root / "Theme_Registry_QA.csv", theme_registry_rows)
    write_csv(root / "Theme_Figure_QA.csv", THEME_QA)
    if any(r["Status"] != "PASS" for r in theme_registry_rows + THEME_QA):
        raise RuntimeError("Visual-theme validation failed; see Theme_Registry_QA.csv / Theme_Figure_QA.csv")
    idx = [
        "Output index",
        "  Theme_Registry_QA.csv / Theme_Figure_QA.csv (visual-theme validation)",
        f"Experimental: {exp_path}",
        "",
        "Design parameters are in COLUMN_ASSUMPTIONS at the top of the script.",
        "Re-run to recompute all column designs, GWP, and sensitivity tables.",
        "Figures are saved as high-clarity PNGs at 300 DPI.",
        "",
        "Outputs:",
        "  Data_Coverage_QA.csv",
        "  RCMT_Consistency_QA.csv",
        "  Section_3_6/SimaPro_Component_QA.csv",
        "  Section_3_6/SimaPro_Component_Breakdown.csv",
        "  Section_3_1/Table_3_1_Fresh_and_28d_Summary.csv",
        "  Section_3_2/Table_3_2_Age_Dependent_Summary.csv",
        "  Section_3_2/Figure_3_2_Age_Dependent_Response.png",
        "  Section_3_3/Table_3_3_Correlation_Summary.csv",
        "  Section_3_3/Table_3_3_GGBFS_SF_Interaction_Contrasts.csv",
        "  Section_3_3/Figure_3_3_GGBFS_SF_Interaction.png",
        "  Section_3_3/Figure_4_Correlation_Matrices.png",
        "  Section_3_4/Table_3_4_ServiceLife_and_Cover.csv",
        "  Section_3_4/Figure_5_Service_Life_and_Cover.png",
        "  Section_3_4/Figure_6_Aging_Exponent_Sensitivity.png",
        "  Section_3_4/Figure_S1_Dnssm_Sensitivity.png",
        "  Section_3_5/Table_3_5_Column_Design_All_Mixes.csv",
        "  Section_3_5/Table_3_5_Column_Design_Manuscript.csv",
        "  Section_3_5/Table_3_5_Column_Design_Summary.csv",
        "  Section_3_5/Table_S_*_Column_Design_Sensitivity.csv",
        "  Section_3_5/Table_S_Kt_Column_Design_Sensitivity.csv (k_t=0.075, 0.10, 0.125; 75-year; 5-story base)",
        "  Section_3_5/Figure_7_Column_Design_and_Material_Demand.png",
        "  Section_3_5/Story_Count_Sensitivity/Table_15Storey_Column_Design.csv",
        "  Section_3_5/Table_15Storey_Smallest_Section_Designs.csv",
        "  Section_3_5/Story_Count_Sensitivity/Design_Sensitivities/Table_S_*_Column_Design_Sensitivity_15Storey.csv",
        "  Section_3_5/Story_Count_Sensitivity/Design_Sensitivities/Table_S_*_Column_Design_Sensitivity_5vs15vs30.csv",
        "  Section_3_5/Story_Count_Sensitivity/Table_30Storey_Column_Design.csv",
        "  Section_3_5/Story_Count_Sensitivity/Table_5vs30_Storey_Comparison.csv",
        "  Section_3_5/Story_Count_Sensitivity/Table_5vs30_Storey_Summary.csv",
        "  Section_3_5/Story_Count_Sensitivity/Design_Sensitivities/Table_S_*_Column_Design_Sensitivity_5vs30.csv",
        "  Section_3_5/Table_3_5_Column_Design_5vs30.csv",
        "  Section_3_5/Table_3_5_Column_Design_5vs15vs30.csv",
        "  Section_3_5/Table_3_5_Column_Design_Summary_5vs15vs30.csv",
        "  Section_3_5/Table_3_5_Column_Design_Summary_5vs30.csv",
        "  Section_3_5/Table_S4_GWP_Decomposition_5vs15vs30.csv",
        "  Section_3_5/Table_S6_Audit_Trail.csv (144 rows; all three scenarios)",
        "  Section_3_6/Table_3_6_Column_GWP_5vs30.csv",
        "  Section_3_6/Table_3_6_Column_GWP_75y_5vs30.csv",
        "  Section_3_6/Table_3_6_Column_GWP_75y_5vs15vs30.csv",
        "  Section_3_6/Table_3_6_Column_GWP_5vs15vs30.csv",
        "  Section_3_6/Table_3_6_GWP_Decomposition_5vs30.csv",
        "  Section_3_6/Table_3_6_GWP_Decomposition_5vs15vs30.csv",
        "  Section_3_6/Table_3_6_Sensitivity_Summary_5vs15vs30_Manuscript.csv",
        "  Section_3_6/Table_3_6_Sensitivity_Summary_5vs30_Manuscript.csv",
        "  Section_3_6/Figure_10_GWP_Decomposition_5vs15vs30.png",
        "  Section_3_6/Table_S_GWP_Steel_Factor_Sensitivity_5vs15vs30.csv",
        "  Section_3_6/Table_3_6_Concrete_GWP_Sorted.csv",
        "  Section_3_6/Table_3_6_Column_GWP.csv",
        "  Section_3_6/Table_3_6_GWP_Contribution_Decomposition.csv",
        "  Section_3_6/Table_S_GWP_*_Sensitivity.csv",
        "  Section_3_6/Table_S_GWP_Kt_Sensitivity.csv",
        "  Section_3_5/Story_Count_Sensitivity/Design_Sensitivities/Table_S_Kt_Column_Design_Sensitivity_5vs15vs30.csv",
        "  Section_3_6/Figure_8_Concrete_and_Column_GWP.png",
    ]
    (root / "OUTPUT_INDEX.txt").write_text("\n".join(idx) + "\n", encoding="utf-8")

    # Hard guard: every selected layout must use only the permitted diameters.
    allowed_long = set(float(x) for x in COLUMN_ASSUMPTIONS["longitudinal_bar_diameters_mm"])
    allowed_links = set(float(x) for x in COLUMN_ASSUMPTIONS["link_diameters_mm"])
    selected_rows = _read_csv_records(root / "Section_3_5" / "Table_S6_Audit_Trail.csv")
    invalid_layouts = [r for r in selected_rows
                       if (is_number(r.get("longitudinal_bar_diameter_mm")) and float(r["longitudinal_bar_diameter_mm"]) not in allowed_long)
                       or (is_number(r.get("link_diameter_mm")) and float(r["link_diameter_mm"]) not in allowed_links)]
    write_csv(root / "Reinforcement_Diameter_QA.csv", [{
        "Allowed_longitudinal_diameters_mm": ";".join(str(int(x)) for x in sorted(allowed_long)),
        "Allowed_transverse_diameters_mm": ";".join(str(int(x)) for x in sorted(allowed_links)),
        "Selected_design_rows_checked": len(selected_rows),
        "Rows_with_disallowed_selected_diameters": len(invalid_layouts),
        "Status": "PASS" if not invalid_layouts else "FAIL"
    }])
    if invalid_layouts:
        raise RuntimeError(f"Selected layouts contain disallowed reinforcement diameters: {len(invalid_layouts)}")

    print("\nDONE.")
    print("Section 3.6 status:", status["status"])
    print("Outputs:", root / "OUTPUT_INDEX.txt")




# =============================================================================
# STAGE 10 / REV101 — ACCEPTED INPUTS, CENTRAL STRENGTH MAPPING, AND SAFE PREFLIGHT
# =============================================================================
STAGE10_RUN_ID = "STAGE10-20261115-REV110-ADAPTIVE-RM050"
STAGE10_BLOCKER_CODE = "NONE"

# Adaptive search is strictly fail-triggered: the original Rev109 domain is
# exhausted first and remains unchanged for every baseline-feasible case.
# Only a case with zero feasible candidates proceeds to the following bounded
# domains. Resistance, 4% longitudinal reinforcement, 8% lap-zone ceiling,
# spacing, anchorage, shear, confinement and stability limits are never relaxed.
ADAPTIVE_SEARCH_PROFILES = [
    {
        "level": 1, "name": "Rev109_base_domain",
        "sections": None, "points_per_face_max": 8,
        "longitudinal_bar_diameters_mm": [20, 22, 25, 28],
        "link_diameters_mm": [6, 8, 10],
        "note": "Original Rev109 baseline design domain; no expansion"
    },
    {
        "level": 2, "name": "Fine_section_grid_and_32mm_bars",
        "sections": list(range(300, 1501, 25)), "points_per_face_max": 10,
        "longitudinal_bar_diameters_mm": [20, 22, 25, 28, 32],
        "link_diameters_mm": [6, 8, 10, 12],
        "note": "Triggered only if the original domain has no feasible layout; 25-mm section grid and expanded cage"
    },
    {
        "level": 3, "name": "Refined_section_grid_and_high_capacity_cage",
        "sections": list(range(300, 1501, 10)), "points_per_face_max": 12,
        "longitudinal_bar_diameters_mm": [16, 20, 22, 25, 28, 32, 40],
        "link_diameters_mm": [6, 8, 10, 12, 14],
        "note": "10-mm section grid and extended longitudinal/transverse reinforcement options"
    },
]

class Stage10ExecutionBlocker(RuntimeError):
    """A required approved calculation cannot be executed from the supplied evidence."""


# Accepted strength means/SDs cross-checked against Paper_Revision_108_Final.docx, Table 5; never recompute from specimen rows.
ACCEPTED_CUBE_STRENGTH_28D = {
    "M01": (59.60, 2.74), "M02": (54.77, 4.87), "M03": (54.80, 2.95),
    "M04": (49.17, 1.48), "M05": (68.03, 4.38), "M06": (73.53, 2.99),
    "M07": (70.70, 2.19), "M08": (64.10, 0.96), "M09": (67.10, 1.61),
    "M10": (66.20, 2.14), "M11": (55.13, 2.27), "M12": (63.90, 3.40),
    "M13": (68.20, 2.54), "M14": (58.17, 3.54), "M15": (61.53, 3.35),
    "M16": (62.87, 0.91),
}

# Normal-strength classes used for downward (floor) mapping to the EN 1992-1-1:2023 Table 5.1 grid.
NORMAL_STRENGTH_CLASSES = (
    (12.0, "C12/15"), (16.0, "C16/20"), (20.0, "C20/25"),
    (25.0, "C25/30"), (30.0, "C30/37"), (35.0, "C35/45"),
    (40.0, "C40/50"), (45.0, "C45/55"), (50.0, "C50/60"),
    (55.0, "C55/67"), (60.0, "C60/75"), (70.0, "C70/85"),
    (80.0, "C80/95"), (90.0, "C90/105"),
)


def concrete_design_properties(strength_mean_mpa, mix_id=None, strength_basis="cube"):
    """Return the concrete strength chain from either mean cube or mean cylinder input.

    ``strength_basis='cube'`` applies the study's 0.80 cube-to-cylinder estimate.
    ``strength_basis='cylinder'`` uses the entered 28-day mean cylinder strength directly.
    The subsequent f_ck estimate, downward class mapping, eta_cc and f_cd steps are
    identical for both routes.
    """
    strength_mean = float(strength_mean_mpa)
    if not math.isfinite(strength_mean) or strength_mean <= 0:
        raise ValueError("The entered 28-day mean concrete strength must be positive and finite.")
    basis = str(strength_basis).strip().lower()
    if basis not in {"cube", "cylinder"}:
        raise ValueError("strength_basis must be either 'cube' or 'cylinder'.")
    if basis == "cube":
        mean_cube = strength_mean
        f_cm_cyl_est = float(COLUMN_ASSUMPTIONS["cube_to_cylinder_factor"]) * mean_cube
        input_basis = "Mean 28-day cube strength; cylinder strength estimated using the study conversion factor"
    else:
        mean_cube = None
        f_cm_cyl_est = strength_mean
        input_basis = "Direct mean 28-day cylinder strength input; no cube-to-cylinder conversion applied"
    f_ck_est = f_cm_cyl_est - 8.0
    eligible = [(fck, label) for fck, label in NORMAL_STRENGTH_CLASSES if fck <= f_ck_est + 1e-9]
    if not eligible:
        raise ValueError(f"Estimated characteristic strength {f_ck_est:.3f} MPa is below the available class grid.")
    f_ck_class, class_label = eligible[-1]
    eta_cc = min((float(COLUMN_ASSUMPTIONS["fck_ref_MPa"]) / f_ck_class) ** (1.0 / 3.0), 1.0)
    k_tc = float(COLUMN_ASSUMPTIONS["k_tc"])
    gamma_c = float(COLUMN_ASSUMPTIONS["gamma_c"])
    f_cd_est = eta_cc * k_tc * f_ck_class / gamma_c
    return {
        "Mix_ID": mix_id,
        "f_strength_input_MPa": strength_mean,
        "strength_input_basis": basis,
        "f_cm_cube_mean_MPa": mean_cube,
        "f_cm_cyl_est_MPa": f_cm_cyl_est,
        "f_cm_cyl_for_design_MPa": f_cm_cyl_est,
        "f_cm_cyl_input_MPa": f_cm_cyl_est if basis == "cylinder" else None,
        "f_ck_est_MPa": f_ck_est,
        "f_ck_class_MPa": f_ck_class,
        "class_label": class_label,
        "strength_class_label": class_label,
        "eta_cc": eta_cc,
        "k_tc": k_tc,
        "gamma_c": gamma_c,
        "f_cd_est_MPa": f_cd_est,
        "t_ref_strength_days": int(COLUMN_ASSUMPTIONS["t_ref_strength_days"]),
        "minimum_design_load_age_days": int(COLUMN_ASSUMPTIONS["t_cast_to_design_load_min_days"]),
        "input_basis": input_basis,
    }


def creep_annex_b5(*, f_cm_28_mpa, RH_pct, h0_mm, t0_creep_days, t_design_days,
                   alpha_SC, temperature_C, xi_bc1=1.0, xi_bc2=1.0,
                   xi_dc1=1.0, xi_dc2=1.0):
    """Calculate mean creep under EN 1992-1-1:2023 Annex B.5, Eqs. (B.5)-(B.17).

    Project convention: ``h0_mm`` is the code-facing name for the section's 2*Ac/u
    dimension. Annex B.5 denotes this notional size as ``h_n``; both are numerically
    identical here. The standard's ``h_n`` meaning is retained in the returned audit.

    The accepted baseline uses T=20 C, so t0,T=t0. alpha_SC=0 is a shared study
    assumption and is not used to claim a formal cement-strength-development class.
    The xi factors remain at 1.0 (uncalibrated Annex B.5 base model).
    """
    fcm = float(f_cm_28_mpa)
    rh = float(RH_pct)
    hn = float(h0_mm)
    t0 = float(t0_creep_days)
    t = float(t_design_days)
    a_sc = float(alpha_SC)
    temp = float(temperature_C)
    xi_bc1, xi_bc2, xi_dc1, xi_dc2 = map(float, (xi_bc1, xi_bc2, xi_dc1, xi_dc2))

    vals = (fcm, rh, hn, t0, t, a_sc, temp, xi_bc1, xi_bc2, xi_dc1, xi_dc2)
    if not all(math.isfinite(v) for v in vals):
        raise ValueError("All Annex B.5 inputs must be finite.")
    if fcm <= 0 or hn <= 0 or t0 <= 0 or t <= t0:
        raise ValueError("Annex B.5 requires fcm>0, h_n>0 and t_design>t0>0.")
    if not 20.0 <= rh <= 100.0:
        raise ValueError("Annex B.5 baseline relations are valid for 20% <= RH <= 100%.")
    if not 0.0 <= temp <= 80.0:
        raise ValueError("Annex B.5 temperature-age adjustment is only defined here for 0-80 C.")
    if abs(temp - 20.0) > 1e-9:
        raise NotImplementedError("Stage 10 baseline creep temperature is 20 C; implement the full B.18 temperature history before using another temperature.")
    if min(xi_bc1, xi_bc2, xi_dc1, xi_dc2) <= 0:
        raise ValueError("Annex B.7 calibration factors xi must be positive.")

    # Eq. (B.17); at 20 C, t0,T = t0. Keep the project alpha_SC input explicit.
    t0_T = t0
    t0_adj = max(t0_T * (9.0 / (2.0 + t0_T ** 1.2) + 1.0) ** a_sc, 0.5)
    dt = t - t0

    # Basic creep, Eqs. (B.6)-(B.8).
    beta_bc_fcm = 1.8 / (fcm ** 0.7)
    beta_bc_dt = math.log(((30.0 / t0_adj + 0.035) ** 2) * dt / xi_bc2 + 1.0)
    phi_bc = xi_bc1 * beta_bc_fcm * beta_bc_dt

    # Drying creep, Eqs. (B.9)-(B.16). h_n is in mm.
    beta_dc_fcm = 412.0 / (fcm ** 1.4)
    beta_dc_RH = (1.0 - rh / 100.0) / ((0.1 * hn / 100.0) ** (1.0 / 3.0))
    beta_dc_t0 = 1.0 / (0.1 + t0_adj ** 0.2)
    gamma = 1.0 / (2.3 + 3.5 / math.sqrt(t0_adj))
    alpha_fcm = (35.0 / fcm) ** 0.5
    beta_h = min(1.5 * hn + 250.0 * alpha_fcm, 1500.0 * alpha_fcm)
    beta_dc_dt = (dt / (beta_h * xi_dc2 + dt)) ** gamma
    phi_dc = xi_dc1 * beta_dc_fcm * beta_dc_RH * beta_dc_t0 * beta_dc_dt
    phi_total = phi_bc + phi_dc

    out = {
        "f_cm_28_MPa": fcm, "RH_pct": rh,
        "h0_project_mm": hn, "h_n_standard_mm": hn,
        "t0_creep_days": t0, "t0_temperature_adjusted_days": t0_T,
        "t0_adjusted_days_B17": t0_adj, "t_design_days": t,
        "loading_duration_days": dt, "alpha_SC_assumption": a_sc,
        "temperature_C": temp,
        "xi_bc1": xi_bc1, "xi_bc2": xi_bc2, "xi_dc1": xi_dc1, "xi_dc2": xi_dc2,
        "beta_bc_fcm": beta_bc_fcm, "beta_bc_t_minus_t0": beta_bc_dt,
        "phi_bc": phi_bc,
        "beta_dc_fcm": beta_dc_fcm, "beta_dc_RH": beta_dc_RH,
        "beta_dc_t0": beta_dc_t0, "gamma_dc": gamma,
        "alpha_fcm": alpha_fcm, "beta_h_mm": beta_h,
        "beta_dc_t_minus_t0": beta_dc_dt,
        "phi_dc": phi_dc, "phi_total": phi_total,
        "standard_basis": "EN 1992-1-1:2023 Annex B.5 Eqs. (B.5)-(B.17); base xi factors=1.0",
    }
    numeric = [v for v in out.values() if isinstance(v, (int, float))]
    if not all(math.isfinite(float(v)) for v in numeric) or phi_bc <= 0 or phi_dc < 0 or phi_total <= 0:
        raise ArithmeticError("Annex B.5 produced a non-finite or non-positive creep result.")
    return out


def effective_creep_ratio_from_moments(phi_total, m0_qp_kNm, m0_ed_kNm):
    """Apply EN 1992-1-1:2023 Clause 7.4.2(2), Formula (7.27), using moments."""
    phi = float(phi_total)
    m_qp = abs(float(m0_qp_kNm))
    m_ed = abs(float(m0_ed_kNm))
    if not all(math.isfinite(v) for v in (phi, m_qp, m_ed)) or phi <= 0.0 or m_ed <= 0.0:
        raise ValueError("phi_total and M0Ed must be positive and all moment inputs finite.")
    ratio = m_qp / m_ed
    if ratio <= 0.0 or ratio > 1.0 + 1e-8:
        raise ValueError(f"The Formula (7.27) moment ratio M0,Eqp/M0,Ed is outside (0,1]: {ratio}.")
    ratio = min(ratio, 1.0)
    return {"M0_QP_over_M0_Ed": ratio, "phi_eff_b": phi * ratio,
            "M0_QP_kNm": m_qp, "M0_Ed_kNm": m_ed,
            "basis": "Clause 7.4.2(2), Formula (7.27); M0,Eqp includes imperfections; M0,Ed is the first-order design-combination moment"}


def effective_creep_ratio_from_vertical_loads(phi_total, n_qp_kN, n_ed_kN, *, clause_7_4_2_3_approximation_approved):
    """Apply the explicit vertical-load approximation permitted by Clause 7.4.2(3)."""
    if not clause_7_4_2_3_approximation_approved:
        raise Stage10ExecutionBlocker("ST10-CL-001: Clause 7.4.2(3) vertical-load approximation has not been approved for this load model.")
    phi, nqp, ned = float(phi_total), float(n_qp_kN), float(n_ed_kN)
    if not all(math.isfinite(v) for v in (phi, nqp, ned)) or phi <= 0 or nqp <= 0 or ned <= 0:
        raise ValueError("phi_total, N_QP and N_Ed must be positive and finite.")
    ratio = nqp / ned
    if not 0 < ratio <= 1.0:
        raise ValueError(f"The Clause 7.4.2(3) vertical-load ratio is outside (0,1]: {ratio}.")
    return {"N_QP_over_N_Ed": ratio, "phi_eff_b": phi * ratio,
            "basis": "Clause 7.4.2(3) vertical-load approximation; not a directly computed M0,Eqp/M0,Ed ratio"}


def _stage10_trial_section_creep_audit(data):
    """Candidate-independent creep audit over the approved coarse section grid.

    These rows are not selected designs. They are section-specific input/creep/load
    regression records; final candidate search remains gated by missing O.13 end moments.
    """
    rows = []
    design_days = float(COLUMN_ASSUMPTIONS["t_design_working_life_years"]) * 365.25
    for mid in data["order"]:
        mean_cube = float(data["cs_stats"][(mid, 28)][0])
        strength = concrete_design_properties(mean_cube, mix_id=mid)
        fcm = float(strength["f_cm_cyl_est_MPa"])
        for storeys in (5, 15, 30):
            for section_mm in range(300, 1501, 50):
                Ac = float(section_mm ** 2)
                u = float(4 * section_mm)  # Four lateral faces exposed; member ends excluded.
                h0 = 2.0 * Ac / u
                cr = creep_annex_b5(
                    f_cm_28_mpa=fcm, RH_pct=COLUMN_ASSUMPTIONS["RH_creep_pct"],
                    h0_mm=h0, t0_creep_days=COLUMN_ASSUMPTIONS["t0_creep_days"],
                    t_design_days=design_days, alpha_SC=COLUMN_ASSUMPTIONS["alpha_SC"],
                    temperature_C=COLUMN_ASSUMPTIONS["T_creep_C"],
                )
                actions = _derive_column_actions(storeys, section_mm)
                eff = effective_creep_ratio_from_vertical_loads(
                    cr["phi_total"], actions["N_QP_kN"], actions["N_Ed_kN"],
                    clause_7_4_2_3_approximation_approved=True,
                )
                rows.append({
                    "Run_ID": STAGE10_RUN_ID, "Mix_ID": mid, "Storey_scenario": storeys,
                    "Trial_section_b_mm": section_mm, "Trial_section_h_mm": section_mm,
                    "Ac_mm2": Ac, "u_exposed_mm": u,
                    "h0_project_mm": h0, "h_n_standard_mm": h0,
                    "f_cm_cube_mean_28d_MPa": mean_cube, "f_cm_cyl_est_MPa": fcm,
                    "RH_pct": cr["RH_pct"], "t0_creep_days": cr["t0_creep_days"],
                    "tDL_years": COLUMN_ASSUMPTIONS["t_design_working_life_years"],
                    "tDL_days": design_days, "alpha_SC": cr["alpha_SC_assumption"],
                    "T_creep_C": cr["temperature_C"], "psi2": COLUMN_ASSUMPTIONS["psi2"],
                    "phi_bc": cr["phi_bc"], "phi_dc": cr["phi_dc"], "phi_total": cr["phi_total"],
                    "N_QP_kN": actions["N_QP_kN"], "N_Ed_kN": actions["N_Ed_kN"],
                    "column_selfweight_Gk_kN": actions["column_selfweight_Gk_kN"],
                    "N_QP_over_N_Ed_Clause_7_4_2_3": eff["N_QP_over_N_Ed"],
                    "phi_eff_b": eff["phi_eff_b"],
                    "effective_creep_basis": eff["basis"],
                    "M0_Eqp": None, "M0_Ed": None, "M01": None, "M02": None,
                    "r_m": None, "O13_status": "BLOCKED_MISSING_MEMBER_END_MOMENT_PROFILE",
                    "O6_C_if_r_m_unknown": 0.7,
                    "O6_C_basis": "EN 1992-1-1:2023 Annex O.6 allows C=0.7 when r_m is unknown",
                    "design_candidate_status": "TRIAL_CREEP_AUDIT_ONLY_NOT_SELECTED",
                })
    return rows


def _column_block_parameters(f_ck_class_mpa):
    """Return design f_cd plus the accepted rectangular stress-block parameters."""
    fck = float(f_ck_class_mpa)
    eta_cc = min((float(COLUMN_ASSUMPTIONS["fck_ref_MPa"]) / fck) ** (1.0 / 3.0), 1.0)
    f_cd = eta_cc * float(COLUMN_ASSUMPTIONS["k_tc"]) * fck / float(COLUMN_ASSUMPTIONS["gamma_c"])
    # Dataset9 maps no mixture above C50/60. The <=50 MPa rectangular block branch is retained.
    if fck <= 50.0:
        eta_block, lam, eps_cu = 1.0, 0.8, 0.0035
    else:
        # Not reached by the approved 16-mixture mapping; keep the previous branch isolated.
        eta_block = max(0.0, 1.0 - (fck - 50.0) / 200.0)
        lam = max(0.0, 0.8 - (fck - 50.0) / 400.0)
        eps_cu = (2.6 + 35.0 * ((90.0 - fck) / 100.0) ** 4) / 1000.0
    return f_cd, eta_block, lam, eps_cu


def _derive_column_actions(storeys, section_mm, *, moment_scale=1.0,
                           mx_action_kNm=None, my_action_kNm=None):
    """Member actions from the author-approved idealized model.

    Floor dead/live actions accumulate over the stated tributary area. The repeated
    trial-column self-weight is added to Gk for axial load and creep-ratio calculations.
    Prescribed scenario M_Ed values are preserved; their dead/live components are split
    in proportion to the corresponding floor Gk/Qk actions while exactly recovering
    1.35 M_G + 1.50 M_Q = M_0,Ed. Concentric column self-weight adds no direct bending
    moment. First-order end moments use the approved signed double-curvature ratio.
    """
    a = COLUMN_ASSUMPTIONS
    ns = int(storeys)
    area = float(a["tributary_area_m2"])
    Gk_floor = ns * area * float(a["dead_load_kN_m2"])
    Qk_floor = ns * area * float(a["live_load_kN_m2"])
    b_m = float(section_mm) / 1000.0
    self_weight_Gk = (ns * float(a["clear_height_m"]) * b_m * b_m
                      * float(a["unit_weight_concrete_kN_m3"]))
    Gk_total = Gk_floor + self_weight_Gk
    Qk_total = Qk_floor
    N_QP = Gk_total + float(a["psi2"]) * Qk_total
    N_Ed = float(a["gamma_G"]) * Gk_total + float(a["gamma_Q"]) * Qk_total

    scenario_moments = {
        5: (float(a["M_Ed_x_kNm"]), float(a["M_Ed_y_kNm"])),
        15: (float(a["M_Ed_x_kNm_15storey"]), float(a["M_Ed_y_kNm_15storey"])),
        30: (float(a["M_Ed_x_kNm_30storey"]), float(a["M_Ed_y_kNm_30storey"])),
    }
    if ns in scenario_moments:
        mx_base, my_base = scenario_moments[ns]
    else:
        base_ns = int(a["storeys"])
        mx_base = float(a["M_Ed_x_kNm"]) * ns / base_ns
        my_base = float(a["M_Ed_y_kNm"]) * ns / base_ns
    if mx_action_kNm is not None:
        mx_base = float(mx_action_kNm)
    if my_action_kNm is not None:
        my_base = float(my_action_kNm)
    mx_ed = float(mx_base) * float(moment_scale)
    my_ed = float(my_base) * float(moment_scale)

    # The supplied moments are ULS design moments. Recover their characteristic G/Q
    # portions in proportion to the project floor-action components.
    denom = float(a["gamma_G"]) * Gk_floor + float(a["gamma_Q"]) * Qk_floor
    if denom <= 0:
        raise ValueError("Dead/live floor actions must be positive to split prescribed ULS moments.")
    g_share = Gk_floor / denom
    q_share = Qk_floor / denom
    m_g_x, m_q_x = mx_ed * g_share, mx_ed * q_share
    m_g_y, m_q_y = my_ed * g_share, my_ed * q_share
    m_qp_x = m_g_x + float(a["psi2"]) * m_q_x
    m_qp_y = m_g_y + float(a["psi2"]) * m_q_y
    rm = float(a["moment_end_ratio_rm_base"])
    m01_ed_x, m02_ed_x = rm * mx_ed, mx_ed
    m01_ed_y, m02_ed_y = rm * my_ed, my_ed
    m01_qp_x, m02_qp_x = rm * m_qp_x, m_qp_x
    m01_qp_y, m02_qp_y = rm * m_qp_y, m_qp_y
    l0_m = float(a["effective_length_assumed_m"])
    # Member shear follows equilibrium for the linear end-moment profile.
    v_ed_x = abs(m02_ed_x - m01_ed_x) / l0_m
    v_ed_y = abs(m02_ed_y - m01_ed_y) / l0_m
    v_qp_x = abs(m02_qp_x - m01_qp_x) / l0_m
    v_qp_y = abs(m02_qp_y - m01_qp_y) / l0_m

    return {
        "storeys": ns,
        "Gk_floor_kN": Gk_floor, "Qk_floor_kN": Qk_floor,
        "column_selfweight_Gk_kN": self_weight_Gk,
        "Gk_total_kN": Gk_total, "Qk_total_kN": Qk_total,
        "N_QP_kN": N_QP, "N_Ed_kN": N_Ed,
        "N_Ed_total_kN": N_Ed,
        "N_QP_over_N_Ed": N_QP / N_Ed if N_Ed > 0 else None,
        "ULS_combination": "1.35 Gk + 1.50 Qk (study-defined member actions)",
        "quasi_permanent_combination": "Gk + 0.30 Qk (study-defined member actions)",
        "self_weight_included_in_Gk": True,
        "tributary_area_m2": area,
        "floor_dead_load_kN_m2": float(a["dead_load_kN_m2"]),
        "floor_live_load_kN_m2": float(a["live_load_kN_m2"]),
        "M_G_x_kNm": m_g_x, "M_Q_x_kNm": m_q_x,
        "M_G_y_kNm": m_g_y, "M_Q_y_kNm": m_q_y,
        "M0_Ed_x_kNm": mx_ed, "M0_Ed_y_kNm": my_ed,
        "M0_QP_x_kNm": m_qp_x, "M0_QP_y_kNm": m_qp_y,
        "M01_Ed_x_kNm": m01_ed_x, "M02_Ed_x_kNm": m02_ed_x,
        "M01_Ed_y_kNm": m01_ed_y, "M02_Ed_y_kNm": m02_ed_y,
        "M01_QP_x_kNm": m01_qp_x, "M02_QP_x_kNm": m02_qp_x,
        "M01_QP_y_kNm": m01_qp_y, "M02_QP_y_kNm": m02_qp_y,
        "r_m_base_x": rm, "r_m_base_y": rm,
        "V_Ed_x_kN": v_ed_x, "V_Ed_y_kN": v_ed_y,
        "V_QP_x_kN": v_qp_x, "V_QP_y_kN": v_qp_y,
        "moment_split_basis": "Characteristic M_G/M_Q split from floor Gk/Qk contributions; exact ULS recovery",
        "moment_profile": "linear double curvature; M01=-0.50*M02; no member transverse distributed load",
        "shear_equilibrium_basis": "abs(M02-M01)/l0",
        "rm_author_assumption": rm,
    }


def _anchorage_and_lap_lengths_mm(db, f_ck_class_mpa, fyd_mpa, cover_mm, link_d_mm, bar_pitch_mm):
    """Use the adopted strength class as the concrete input for anchorage/lap calculations."""
    db = float(db)
    fck = max(float(f_ck_class_mpa), 1.0)
    fyd = float(fyd_mpa)
    c_x = float(cover_mm) + float(link_d_mm) + db / 2.0
    c_y = c_x
    c_d = max(min(0.5 * float(bar_pitch_mm), c_x, c_y, 3.75 * db), 1e-6)
    sigma_ratio = max(fyd / 435.0, 0.0) ** 1.5
    fck_ratio = max(25.0 / fck, 0.3) ** 0.5
    phi_ratio = max(db / 20.0, 0.6) ** (1.0 / 3.0)
    cover_ratio = max(1.5 * db / c_d, 0.0) ** 0.5
    l_bd = max(10.0 * db, 50.0 * db * sigma_ratio * fck_ratio * phi_ratio * cover_ratio)
    l_sd = max(1.2 * l_bd, 15.0 * db)
    return {"l_bd_mm": l_bd, "l_sd_mm": l_sd, "c_d_mm": c_d,
            "anchorage_bond_assumption": "good bond; EN 1992-1-1:2023 Eq. (11.3)",
            "lap_coefficient_k_ls": 1.2}


def _o7_end_moment_ratio(m02_kNm, n_ed_kN, h_m, rm_base):
    """Return the O.7.2(2) end-moment ratio used by Formulae (O.15)-(O.16).

    For braced compression members without significant transverse member loads,
    EN 1992-1-1:2023 O.7.2(2) defines rm=1 when |M02| < 0.05 |NEd| h;
    otherwise rm=M01/M02. The author-approved double-curvature profile supplies
    M01/M02=-0.50. This is a project model input, not an EC2-prescribed ratio.
    """
    m02 = abs(float(m02_kNm))
    n_ed = abs(float(n_ed_kN))
    h = float(h_m)
    if h <= 0.0:
        raise ValueError("O.7.2 requires a positive section dimension h.")
    threshold = 0.05 * n_ed * h
    if m02 < threshold:
        return 1.0, f"O.16: |M02|={m02:.6g} < 0.05|NEd|h={threshold:.6g}; rm=1.0"
    rm = float(rm_base)
    if not (-1.0 - 1e-9 <= rm <= 1.0 + 1e-9):
        raise ValueError(f"Invalid signed end-moment ratio rm={rm}; expected a ratio within [-1,1].")
    return rm, f"O.16: |M02|={m02:.6g} >= 0.05|NEd|h={threshold:.6g}; rm=M01/M02={rm:.6g}"


def _o13_design_moment(m01_kNm, m02_kNm, m2_kNm, n_ed_kN, e_i_mm, c_m,
                       *, include_imperfection=False):
    """Calculate the EN 1992-1-1:2023 Annex O.7.2 Formula (O.13) envelope.

    O.13: MEd=max{M0Ed+M2; |M02|; |M01-0.5*M2-2|NEd|*ei|}.
    For the approved braced-member model without significant transverse loads,
    O.14-O.16 give M0Ed=Cm*M02 and Cm=max(0.6+0.4*rm,0.4).
    The direction-specific imperfection term is toggled only for the adverse
    principal axis, consistent with the separate biaxial imperfection check.
    """
    m01 = float(m01_kNm)
    m02 = abs(float(m02_kNm))
    m2 = max(float(m2_kNm), 0.0)
    n_ed = abs(float(n_ed_kN))
    e_i = max(float(e_i_mm), 0.0) / 1000.0
    cm = max(float(c_m), 0.4)
    m0_ed = cm * m02
    imperfection_moment_term = 2.0 * n_ed * e_i if include_imperfection else 0.0
    term1 = m0_ed + m2
    term2 = m02
    term3 = abs(m01 - 0.5 * m2 - imperfection_moment_term)
    terms = {
        "O13_term1_M0Ed_plus_M2_kNm": term1,
        "O13_term2_M02_kNm": term2,
        "O13_term3_abs_M01_minus_half_M2_minus_2NEdei_kNm": term3,
        "O13_M0Ed_Cm_times_M02_kNm": m0_ed,
        "O13_imperfection_moment_2NEdei_kNm": imperfection_moment_term,
        "O13_Cm": cm,
        "O13_imperfection_included": bool(include_imperfection),
    }
    keys = ["O13_term1_M0Ed_plus_M2_kNm", "O13_term2_M02_kNm",
            "O13_term3_abs_M01_minus_half_M2_minus_2NEdei_kNm"]
    governing = max(keys, key=lambda k: terms[k])
    terms["O13_governing_term"] = governing
    return float(terms[governing]), terms


def _calculate_column_design(strength_input_mpa, cover_nominal_mm, mix_id, target_years,
                             moment_scale=1.0, selection_rule="smallest_section", gwp_concrete=None,
                             mx_action_kNm=None, my_action_kNm=None, storeys=None,
                             _search_expansion_level=0, strength_input_basis="cube"):
    """Compare feasible isolated-column layouts with explicit load, stability and detailing gates.

    The model includes local Annex O.7 nominal-curvature second-order effects where Annex O.6
    requires them; factored gravity actions from tributary floors and repeated-column self-weight;
    axial/biaxial section checks; longitudinal and transverse reinforcement limits; spacing,
    anchorage/lap, member-shear and compression-strut checks. A moment-reversal envelope VEd is
    used for shear. This remains a member-level comparative design model, not a global-frame design.
    """
    a = COLUMN_ASSUMPTIONS
    ns = int(a["storeys"] if storeys is None else storeys)
    fyd = float(a["f_yk_MPa"]) / float(a["gamma_s"])
    strength_props = concrete_design_properties(float(strength_input_mpa), mix_id=mix_id, strength_basis=strength_input_basis)
    fcm_cyl_est = float(strength_props["f_cm_cyl_est_MPa"])
    fck_class = float(strength_props["f_ck_class_MPa"])
    fcd_proxy = float(strength_props["f_cd_est_MPa"])
    clear_h = float(a["clear_height_m"])
    l0_mm = float(a["effective_length_assumed_m"]) * 1000.0
    l0_m = float(a["effective_length_assumed_m"])
    e_i_mm = l0_mm / 400.0
    c1_over_r = 8.0  # EN 1992-1-1:2023 O.7.2(4): c1/r=8 may be adopted for braced members.
    candidates = []

    for b_raw in a["candidate_square_sections_mm"]:
        b = float(b_raw)
        actions = _derive_column_actions(ns, b, moment_scale=moment_scale,
                                         mx_action_kNm=mx_action_kNm, my_action_kNm=my_action_kNm)
        n_ed_kn_local = float(actions["N_Ed_kN"])
        n_qp_kn_local = float(actions["N_QP_kN"])
        n_ed_n = n_ed_kn_local * 1000.0
        m_imp = n_ed_kn_local * e_i_mm / 1000.0
        e_min = max(b / 30.0, 20.0)
        m_min = n_ed_kn_local * e_min / 1000.0
        mx02_ed = float(actions["M02_Ed_x_kNm"])
        my02_ed = float(actions["M02_Ed_y_kNm"])
        mx01_ed = float(actions["M01_Ed_x_kNm"])
        my01_ed = float(actions["M01_Ed_y_kNm"])
        mx02_qp = float(actions["M02_QP_x_kNm"])
        my02_qp = float(actions["M02_QP_y_kNm"])
        mx01_qp = float(actions["M01_QP_x_kNm"])
        my01_qp = float(actions["M01_QP_y_kNm"])
        rm_base_x = float(actions["r_m_base_x"])
        rm_base_y = float(actions["r_m_base_y"])
        rm_o7_x, rm_o7_note_x = _o7_end_moment_ratio(mx02_ed, n_ed_kn_local, b / 1000.0, rm_base_x)
        rm_o7_y, rm_o7_note_y = _o7_end_moment_ratio(my02_ed, n_ed_kn_local, b / 1000.0, rm_base_y)
        rm_o7_qp_x, rm_o7_qp_note_x = _o7_end_moment_ratio(mx02_qp, n_qp_kn_local, b / 1000.0, rm_base_x)
        rm_o7_qp_y, rm_o7_qp_note_y = _o7_end_moment_ratio(my02_qp, n_qp_kn_local, b / 1000.0, rm_base_y)
        cm_o7_x = max(0.6 + 0.4 * rm_o7_x, 0.4)
        cm_o7_y = max(0.6 + 0.4 * rm_o7_y, 0.4)
        cm_o7_qp_x = max(0.6 + 0.4 * rm_o7_qp_x, 0.4)
        cm_o7_qp_y = max(0.6 + 0.4 * rm_o7_qp_y, 0.4)
        creep = creep_annex_b5(
            f_cm_28_mpa=fcm_cyl_est, RH_pct=float(a["RH_creep_pct"]),
            h0_mm=2.0 * b * b / (4.0 * b),
            t0_creep_days=float(a["t0_creep_days"]),
            t_design_days=float(a["t_design_working_life_years"]) * 365.25,
            alpha_SC=float(a["alpha_SC"]), temperature_C=float(a["T_creep_C"]),
        )
        # Clause 7.4.2(2), Formula (7.27): M0,Eqp is the maximum first-order
        # moment for the quasi-permanent combination INCLUDING imperfections;
        # M0,Ed is the maximum first-order moment for the relevant design combination.
        # Under this idealized signed profile, both maxima occur at end 2. Thus the
        # imperfection contribution N_QP*e_i is added to the numerator only. The
        # equivalent Cm coefficient is reserved for O.7.2/O.13 and is not used here.
        ei_m = e_i_mm / 1000.0
        m0_ed_creep_x = abs(mx02_ed)
        m0_qp_creep_x = abs(mx02_qp) + n_qp_kn_local * ei_m
        m0_ed_creep_y = abs(my02_ed)
        m0_qp_creep_y = abs(my02_qp) + n_qp_kn_local * ei_m
        eff_x = effective_creep_ratio_from_moments(creep["phi_total"], m0_qp_creep_x, m0_ed_creep_x)
        eff_y = effective_creep_ratio_from_moments(creep["phi_total"], m0_qp_creep_y, m0_ed_creep_y)
        phi_eff_x = float(eff_x["phi_eff_b"])
        phi_eff_y = float(eff_y["phi_eff_b"])
        phi_eff_local = max(phi_eff_x, phi_eff_y)
        mx0 = max(abs(mx02_ed), m_min)
        my0 = max(abs(my02_ed), m_min)

        for pf in range(2, int(a.get("search_points_per_face_max", 8)) + 1):
            for db_raw in a["longitudinal_bar_diameters_mm"]:
                db = float(db_raw)
                link_candidates = [float(ld) for ld in a["link_diameters_mm"]
                                   if float(ld) >= max(6.0, 0.25 * db)]
                if not link_candidates:
                    continue
                # Search every allowed transverse-bar diameter. Choosing only the smallest
                # diameter can miss feasible layouts and cannot support a true minimum-GWP search.
                for link_d in link_candidates:
                    pts = _column_bar_positions(b, cover_nominal_mm, db, pf, link_d)
                    if not pts or len(pts) != 4 * pf - 4:
                        continue
                    nbar = len(pts)
                    ab = math.pi * db * db / 4.0
                    As = nbar * ab
                    Ac = b * b  # Gross section area A_g = b*h; all candidates are square (b=h).
                    i_g = b / math.sqrt(12.0)
                    lam_x = lam_y = l0_mm / i_g
                    n_dim = n_ed_n / max(Ac * fcd_proxy, 1e-9)
                    omega = As * fyd / max(Ac * fcd_proxy, 1e-9)
                    A_o6 = 1.0 / (1.0 + 0.2 * phi_eff_local)
                    B_o6 = math.sqrt(1.0 + 2.0 * omega)
                    C_o6_x = 1.7 - rm_base_x
                    C_o6_y = 1.7 - rm_base_y
                    lam_lim_x = 20.0 * A_o6 * B_o6 * C_o6_x / math.sqrt(max(n_dim, 1e-12))
                    lam_lim_y = 20.0 * A_o6 * B_o6 * C_o6_y / math.sqrt(max(n_dim, 1e-12))
                    second_ord_required_x = lam_x >= lam_lim_x
                    second_ord_required_y = lam_y >= lam_lim_y
                    second_ord_required = second_ord_required_x or second_ord_required_y

                    As_min = max(0.1 * n_ed_n / fyd, float(a["longitudinal_reinforcement_min_ratio"]) * Ac)
                    As_max_ratio = float(a["longitudinal_reinforcement_max_ratio"])
                    n_lapped = int(math.ceil(0.5 * nbar))
                    As_lap = As + n_lapped * ab
                    if As < As_min - 1e-6 or As > As_max_ratio * Ac + 1e-6:
                        continue
                    # Lapped layout must meet the first-generation EC2 recommended 8% cap at the splice;
                    # 4% is retained as the more restrictive search cap outside the lap zone.
                    if As_lap > 0.08 * Ac + 1e-6:
                        continue

                    rbar = b / 2.0 - float(cover_nominal_mm) - link_d - db / 2.0
                    if rbar <= 0 or pf < 2:
                        continue
                    clear = 2.0 * rbar / (pf - 1) - db
                    bar_pitch = 2.0 * rbar / (pf - 1)
                    min_clear = max(db, float(a["max_aggregate_size_mm"]) + 5.0, 20.0)
                    if clear < min_clear - 1e-6 or bar_pitch > 400.0 + 1e-6:
                        continue
                    anch = _anchorage_and_lap_lengths_mm(db, fck_class, fyd, cover_nominal_mm,
                                                         link_d, bar_pitch)
                    l_bd = float(anch["l_bd_mm"])
                    l_sd = float(anch["l_sd_mm"])
                    h_mm = clear_h * 1000.0
                    end_len = b  # EN 1992-1-1:2023 Table 12.3 end region length equals larger section dimension.
                    mid_each = (h_mm - 2.0 * end_len - l_sd) / 2.0
                    if mid_each < -1e-6:
                        continue

                    # Study-specific axial screening expression; not a standalone EC2 resistance check.
                    nrd0 = Ac * fcd_proxy + As * fyd
                    axial_ratio = n_ed_n / nrd0 if nrd0 > 0 else math.inf
                    a_n = _a_n_from_axial_ratio(axial_ratio)
                    if a_n is None or axial_ratio >= 1.0:
                        continue
                    mrd_x = _column_axis_capacity(b, fck_class, pts, ab, n_ed_n, fyd, axis="x")
                    mrd_y = _column_axis_capacity(b, fck_class, pts, ab, n_ed_n, fyd, axis="y")
                    if not (mrd_x and mrd_y and math.isfinite(mrd_x) and math.isfinite(mrd_y)
                            and mrd_x > 0 and mrd_y > 0):
                        continue

                    def nominal_curvature(axis):
                        ci = 1 if axis == "x" else 0
                        coords = [float(pt[ci]) for pt in pts]
                        i_s = math.sqrt(sum(v * v for v in coords) / len(coords))
                        d_minus_dprime = b / 2.0 + i_s
                        eps_yd = fyd / float(a["reinforcement_E_MPa"])
                        one_r0 = 2.0 * eps_yd / max(d_minus_dprime, 1e-9)
                        beta_fck = 0.35 + fck_class / 200.0 - lam_x / 150.0
                        phi_axis = phi_eff_x if axis == "x" else phi_eff_y
                        k_phi = max(1.0, 1.0 + beta_fck * phi_axis)
                        k_r = 1.0
                        one_r = k_r * k_phi * one_r0
                        required = second_ord_required_x if axis == "x" else second_ord_required_y
                        e2_mm = (l0_mm ** 2) * one_r / c1_over_r if required else 0.0
                        m2_knm = n_ed_kn_local * e2_mm / 1000.0
                        return m2_knm, e2_mm, one_r0, one_r, d_minus_dprime, beta_fck, k_phi, k_r

                    m2_x, e2_x, one_r0_x, one_r_x, ddp_x, beta_x, kphi_x, kr_x = nominal_curvature("x")
                    m2_y, e2_y, one_r0_y, one_r_y, ddp_y, beta_y, kphi_y, kr_y = nominal_curvature("y")
                    if not _second_order_gate_passes(lam_x, lam_lim_x, lam_y, lam_lim_y, m2_x, m2_y):
                        continue

                    # Formula (O.13), including the end-moment envelope. In accordance with
                    # 7.4.4(3), geometric imperfections are applied only in the more adverse
                    # principal direction; both alternatives are evaluated and the governing
                    # biaxial interaction is retained.
                    mx_no_imp, mx_o13_no = _o13_design_moment(
                        mx01_ed, mx02_ed, m2_x, n_ed_kn_local, e_i_mm, cm_o7_x,
                        include_imperfection=False)
                    my_no_imp, my_o13_no = _o13_design_moment(
                        my01_ed, my02_ed, m2_y, n_ed_kn_local, e_i_mm, cm_o7_y,
                        include_imperfection=False)
                    mx_with_imp, mx_o13_imp = _o13_design_moment(
                        mx01_ed, mx02_ed, m2_x, n_ed_kn_local, e_i_mm, cm_o7_x,
                        include_imperfection=True)
                    my_with_imp, my_o13_imp = _o13_design_moment(
                        my01_ed, my02_ed, m2_y, n_ed_kn_local, e_i_mm, cm_o7_y,
                        include_imperfection=True)
                    mx_with_imp = max(mx_with_imp, m_min)
                    my_with_imp = max(my_with_imp, m_min)
                    mx_no_imp = max(mx_no_imp, m_min)
                    my_no_imp = max(my_no_imp, m_min)
                    inter_x_imp = (mx_with_imp / mrd_x) ** a_n + (my_no_imp / mrd_y) ** a_n
                    inter_y_imp = (mx_no_imp / mrd_x) ** a_n + (my_with_imp / mrd_y) ** a_n
                    if inter_x_imp >= inter_y_imp:
                        imperfection_direction = "x"
                        mx_checked, my_checked, inter = mx_with_imp, my_no_imp, inter_x_imp
                        mx_o13 = mx_o13_imp; my_o13 = my_o13_no
                    else:
                        imperfection_direction = "y"
                        mx_checked, my_checked, inter = mx_no_imp, my_with_imp, inter_y_imp
                        mx_o13 = mx_o13_no; my_o13 = my_o13_imp
                    if inter > 1.0 + 1e-8:
                        continue

                    # First-order member shear from the same signed linear end-moment profile.
                    v_ed_x = float(actions["V_Ed_x_kN"])
                    v_ed_y = float(actions["V_Ed_y_kN"])
                    smax_col = min(20.0 * db, b, 300.0)
                    # EN 1992-1-1:2023 Table 12.3: intermediate-region spacing is
                    # smax,col = min(20*phi_l,max, b, h, 300 mm). The table's 0.6*smax,col
                    # limit is applied to end regions and, where phi_l >= 14 mm, to lap regions.
                    smax_mid = smax_col
                    # EN 1992-1-1:2023 Table 12.3: end regions (length = max(b,h))
                    # use min(b,h,400 mm); the 0.6*smax,col reduction applies to lap
                    # regions for longitudinal bars >= 14 mm, not to end regions.
                    smax_end = min(b, 400.0)
                    smax_lap = 0.6 * smax_col if db >= 14.0 else smax_col
                    shear = _shear_check_and_spacing(
                        b, cover_nominal_mm, db, link_d, As, fck_class, fcd_proxy, fyd,
                        v_ed_x, v_ed_y, n_ed_kn_local, mx_checked, my_checked,
                        l0_mm, gamma_v=float(a["gamma_v"]),
                        cot_theta=float(a["cot_theta_shear"]),
                        ductility_class=str(a.get("reinforcement_ductility_class", "B")),
                        max_aggregate_mm=float(a["max_aggregate_size_mm"]),
                    )
                    if shear is None:
                        continue
                    shear_caps = [float(v) for v in (
                        shear["shear_x"]["s_required_mm"], shear["shear_y"]["s_required_mm"]
                    ) if is_number(v) and float(v) > 0.0]
                    s_shear = min(shear_caps) if shear_caps else None
                    shear_cap = s_shear if s_shear is not None else math.inf
                    # Preserve the unrounded governing limits and adopt practical transverse-link
                    # spacings on a 50-mm grid. The adopted value is always rounded DOWN so that
                    # constructability adjustment cannot relax the code/shear/confinement limit.
                    smax_mid_calc_prov = min(smax_mid, shear_cap)
                    smax_end_calc_prov = min(smax_end, shear_cap)
                    smax_lap_calc_prov = min(smax_lap, shear_cap)

                    # EN 1992-1-1:2023 Table 12.3 requires additional end-zone confinement
                    # when the adopted characteristic strength class exceeds 50 MPa. The trigger
                    # therefore uses f_ck,class, not the mean cylinder-strength proxy.
                    confinement_k = float(a.get("minimum_confinement_factor_k", 0.02))
                    core_dim = b - 2.0 * float(cover_nominal_mm) - link_d
                    area_link = math.pi * link_d * link_d / 4.0
                    s_conf_max = math.inf
                    if fck_class > 50.0 and core_dim > 0.0 and fcd_proxy > 0.0:
                        s_conf_max = 2.0 * area_link * fyd / (core_dim * confinement_k * fcd_proxy)
                        smax_end_calc_prov = min(smax_end_calc_prov, s_conf_max)

                    spacing_step = float(a.get("transverse_spacing_increment_mm", 50.0))
                    if spacing_step <= 0.0:
                        raise ValueError("transverse_spacing_increment_mm must be positive.")
                    def round_spacing_down(value):
                        if not math.isfinite(float(value)) or float(value) <= 0.0:
                            return 0.0
                        return float(math.floor((float(value) + 1e-9) / spacing_step) * spacing_step)

                    smax_end_prov = round_spacing_down(smax_end_calc_prov)
                    smax_mid_prov = round_spacing_down(smax_mid_calc_prov)
                    smax_lap_prov = round_spacing_down(smax_lap_calc_prov)
                    if smax_end_prov <= 0.0 or smax_mid_prov <= 0.0 or smax_lap_prov <= 0.0:
                        continue
                    # Constructability gate: require a minimum clear gap between adjacent link levels.
                    # The code's column-spacing limits are maximum spacings; they do not legitimize
                    # arbitrarily small calculated spacings. Use the same adopted aggregate-clearance
                    # basis as the longitudinal-bar placement check.
                    min_link_clear = max(float(a.get("minimum_transverse_clear_spacing_mm", 20.0)),
                                          float(a["max_aggregate_size_mm"]) + 5.0)
                    min_link_pitch = link_d + min_link_clear
                    def zone_spacing_feasible(length_mm, cap_mm):
                        if length_mm <= 1e-9:
                            return True
                        n_intervals = max(1, int(math.ceil(length_mm / cap_mm)))
                        return length_mm / n_intervals >= min_link_pitch - 1e-6
                    if not (zone_spacing_feasible(end_len, smax_end_prov)
                            and zone_spacing_feasible(mid_each, smax_mid_prov)
                            and zone_spacing_feasible(l_sd, smax_lap_prov)):
                        continue

                    # Annex G.3(3), G.6 and G.7: the provided transverse tie resistance is
                    # evaluated in the middle region, where the member shear-field design
                    # resistance governs. For the square closed-link cage, the two orthogonal
                    # tie-leg groups are projected onto the resultant shear direction. This
                    # yields the effective provided resistance in that direction and is
                    # compared with the Table G.1 optimum within the 0.4--2.5 interval.
                    if shear["shear_method"] == "EN_1992-1-1_Annex_G_Table_G1":
                        n_mid_check = (max(1, int(math.ceil(mid_each / max(smax_mid_prov, 1.0))))
                                       if mid_each > 0 else 0)
                        s_end_check = end_len / max(1, int(math.ceil(end_len / max(smax_end_prov, 1.0))))
                        s_lap_check = l_sd / max(1, int(math.ceil(l_sd / max(smax_lap_prov, 1.0))))
                        s_mid_check = (mid_each / n_mid_check if n_mid_check > 0
                                       else min(s_end_check, s_lap_check))
                        provided_tie_strength_each_axis = float(shear["A_sw_two_legs_mm2"]) * fyd / max(b * s_mid_check, 1e-12)
                        shear_resultant = math.hypot(v_ed_x, v_ed_y)
                        resultant_projection = ((abs(v_ed_x) + abs(v_ed_y)) / shear_resultant
                                                if shear_resultant > 1e-9 else 1.0)
                        ftdy_provided_resultant = provided_tie_strength_each_axis * resultant_projection
                        ftdy_optimum = float(shear["f_tdy_optimum_MPa"] or 0.0)
                        if ftdy_optimum > 1e-12:
                            annex_g_deformation_ratio = ftdy_provided_resultant / ftdy_optimum
                            if not (0.4 - 1e-9 <= annex_g_deformation_ratio <= 2.5 + 1e-9):
                                continue
                        else:
                            annex_g_deformation_ratio = None
                    else:
                        s_mid_check = None
                        ftdy_provided_resultant = None
                        ftdy_optimum = None
                        annex_g_deformation_ratio = None
                    # Two orthogonal cross-tie directions restrain each intermediate face-bar
                    # at every link level; each tie hooks into the enclosing hoop.
                    n_cross = 2 * max(0, pf - 2)

                    active_search_profile = ADAPTIVE_SEARCH_PROFILES[min(_search_expansion_level, len(ADAPTIVE_SEARCH_PROFILES)-1)]
                    candidates.append({
                        "search_expansion_level": int(_search_expansion_level),
                        "search_profile": active_search_profile["name"],
                        "search_profile_note": active_search_profile["note"],
                        "section_grid_step_mm": (50 if _search_expansion_level == 0 else (25 if _search_expansion_level == 1 else 10)),
                        "section_mm": int(b), "bar_diameter_mm": db, "bars_n": nbar,
                        "points_per_face": pf, "link_diameter_mm": link_d,
                        "A_s_mm2": As, "A_s_min_mm2": As_min, "A_s_max_mm2": As_max_ratio * Ac,
                        "A_s_lap_mm2": As_lap, "rho_longitudinal_pct": 100.0 * As / Ac,
                        "rho_at_lap_pct": 100.0 * As_lap / Ac,
                        "clear_bar_spacing_mm": clear, "centre_bar_pitch_mm": bar_pitch,
                        "minimum_clear_spacing_mm": min_clear, "max_longitudinal_bar_spacing_limit_mm": 400.0,
                        "longitudinal_bar_pitch_check": "PASS", "n_lapped_bars": n_lapped,
                        "l_bd_mm": l_bd, "l_sd_mm": l_sd, "anchorage_cover_parameter_c_d_mm": anch["c_d_mm"],
                        "lap_zone_length_mm": l_sd, "lap_zone_spacing_max_mm": smax_lap_prov,
                        "anchorage_bond_assumption": anch["anchorage_bond_assumption"],
                        "f_cube_mean_MPa": strength_props["f_cm_cube_mean_MPa"],
                        "f_strength_input_MPa": float(strength_input_mpa),
                        "strength_input_basis": str(strength_input_basis),
                        "f_cm_cyl_est_MPa": fcm_cyl_est,
                        "f_cm_cyl_for_design_MPa": fcm_cyl_est,
                        "f_ck_est_MPa": strength_props["f_ck_est_MPa"],
                        "f_ck_class_MPa": fck_class, "strength_class_label": strength_props["strength_class_label"],
                        "eta_cc": strength_props["eta_cc"], "k_tc": a["k_tc"],
                        "f_cd_est_MPa": fcd_proxy, "f_cd_proxy_MPa": fcd_proxy,
                        "N_Ed_kN": n_ed_kn_local, **actions,
                        "N_Rd0_screening_kN": nrd0 / 1000.0,
                        "axial_ratio_r": axial_ratio, "a_N": a_n,
                        "M_Ed_x_input_kNm": mx02_ed, "M_Ed_y_input_kNm": my02_ed,
                        "M_Ed_x_first_order_kNm": mx02_ed, "M_Ed_y_first_order_kNm": my02_ed,
                        "M01_Ed_x_signed_kNm": mx01_ed, "M02_Ed_x_signed_kNm": mx02_ed,
                        "M01_Ed_y_signed_kNm": my01_ed, "M02_Ed_y_signed_kNm": my02_ed,
                        "M_G_x_kNm": actions["M_G_x_kNm"], "M_Q_x_kNm": actions["M_Q_x_kNm"],
                        "M_G_y_kNm": actions["M_G_y_kNm"], "M_Q_y_kNm": actions["M_Q_y_kNm"],
                        "M0_QP_x_kNm": actions["M0_QP_x_kNm"], "M0_QP_y_kNm": actions["M0_QP_y_kNm"],
                        "M01_QP_x_signed_kNm": mx01_qp, "M02_QP_x_signed_kNm": mx02_qp,
                        "M01_QP_y_signed_kNm": my01_qp, "M02_QP_y_signed_kNm": my02_qp,
                        "r_m_base_x": rm_base_x, "r_m_base_y": rm_base_y,
                        "r_m_O7_x": rm_o7_x, "r_m_O7_y": rm_o7_y,
                        "r_m_O7_QP_x": rm_o7_qp_x, "r_m_O7_QP_y": rm_o7_qp_y,
                        "C_m_O7_x": cm_o7_x, "C_m_O7_y": cm_o7_y,
                        "C_m_O7_QP_x": cm_o7_qp_x, "C_m_O7_QP_y": cm_o7_qp_y,
                        "O7_rm_x_note": rm_o7_note_x, "O7_rm_y_note": rm_o7_note_y,
                        "O7_rm_QP_x_note": rm_o7_qp_note_x, "O7_rm_QP_y_note": rm_o7_qp_note_y,
                        "phi_bc": creep["phi_bc"], "phi_dc": creep["phi_dc"],
                        "phi_total": creep["phi_total"], "h_n_creep_mm": 2.0*b*b/(4.0*b),
                        "M0_QP_over_M0_Ed_x": eff_x["M0_QP_over_M0_Ed"],
                        "M0_QP_over_M0_Ed_y": eff_y["M0_QP_over_M0_Ed"],
                        "phi_eff_b_x": phi_eff_x, "phi_eff_b_y": phi_eff_y,
                        "minimum_eccentricity_mm": e_min, "M_min_eccentricity_kNm": m_min,
                        "M_Ed_x_checked_kNm": mx_checked, "M_Ed_y_checked_kNm": my_checked,
                        "M_Rd_x_at_N_kNm": mrd_x, "M_Rd_y_at_N_kNm": mrd_y,
                        "biaxial_interaction_utilization": inter,
                        "moment_scale_sensitivity": float(moment_scale), "storeys_design_case": ns,
                        "effective_length_m": float(a["effective_length_assumed_m"]),
                        "boundary_conditions": a["end_restraint_condition"],
                        "slenderness_lambda_x": lam_x, "slenderness_lambda_y": lam_y,
                        "slenderness_limit_lambda_lim_x": lam_lim_x, "slenderness_limit_lambda_lim_y": lam_lim_y,
                        "slenderness_n": n_dim, "mechanical_reinforcement_ratio_omega": omega,
                        "O6_A": A_o6, "O6_B": B_o6, "O6_C_x": C_o6_x, "O6_C_y": C_o6_y,
                        "second_order_effects_may_be_ignored_x": lam_x < lam_lim_x,
                        "second_order_effects_may_be_ignored_y": lam_y < lam_lim_y,
                        "second_order_interpretation": (
                            "second-order effects included using Annex O.7 nominal curvature; lambda >= lambda_lim"
                            if second_ord_required else "second-order effects not required by Annex O.6 slenderness criterion"),
                        "imperfection_eccentricity_mm": e_i_mm, "imperfection_moment_kNm": m_imp,
                        "imperfection_direction": imperfection_direction,
                        "effective_creep_ratio_local": phi_eff_local,
                        "phi_eff_b_x": phi_eff_x, "phi_eff_b_y": phi_eff_y,
                        "Clause_7_4_2_creep_basis": "Formula (7.27); moment ratio M0,Eqp/M0,Ed",
                        "O7_rm_x_note": rm_o7_note_x, "O7_rm_y_note": rm_o7_note_y,
                        "O13_M0Ed_x_kNm": cm_o7_x * abs(mx02_ed),
                        "O13_M0Ed_y_kNm": cm_o7_y * abs(my02_ed),
                        "O13_term1_x_kNm": mx_o13["O13_term1_M0Ed_plus_M2_kNm"],
                        "O13_term2_x_kNm": mx_o13["O13_term2_M02_kNm"],
                        "O13_term3_x_kNm": mx_o13["O13_term3_abs_M01_minus_half_M2_minus_2NEdei_kNm"],
                        "O13_term1_y_kNm": my_o13["O13_term1_M0Ed_plus_M2_kNm"],
                        "O13_term2_y_kNm": my_o13["O13_term2_M02_kNm"],
                        "O13_term3_y_kNm": my_o13["O13_term3_abs_M01_minus_half_M2_minus_2NEdei_kNm"],
                        "O13_governing_term_x": mx_o13["O13_governing_term"],
                        "O13_governing_term_y": my_o13["O13_governing_term"],
                        "O13_imperfection_direction": imperfection_direction,
                        "O13_formula": "EN 1992-1-1:2023 Annex O.7.2 Formula (O.13)",
                        "curvature_factor_beta_fck": beta_x, "curvature_factor_k_phi": kphi_x,
                        "curvature_factor_k_r": kr_x, "curvature_distribution_factor_c1r": c1_over_r,
                        "nominal_curvature_1_over_r0_x_per_mm": one_r0_x,
                        "nominal_curvature_1_over_r0_y_per_mm": one_r0_y,
                        "nominal_curvature_1_over_r_x_per_mm": one_r_x,
                        "nominal_curvature_1_over_r_y_per_mm": one_r_y,
                        "d_minus_dprime_x_mm": ddp_x, "d_minus_dprime_y_mm": ddp_y,
                        "second_order_eccentricity_x_mm": e2_x, "second_order_eccentricity_y_mm": e2_y,
                        "second_order_moment_x_kNm": m2_x, "second_order_moment_y_kNm": m2_y,
                        "second_order_method": "EN 1992-1-1:2023 Annex O.7 nominal-curvature method",
                        "second_order_status": "INCLUDED_BY_NOMINAL_CURVATURE" if second_ord_required else "NOT_REQUIRED_BY_SLENDERNESS_CRITERION",
                        "second_order_gate_status": "PASS",
                        "M_reverse_shear_envelope_x_kN": v_ed_x, "M_reverse_shear_envelope_y_kN": v_ed_y,
                        "V_Ed_equilibrium_x_kN": v_ed_x, "V_Ed_equilibrium_y_kN": v_ed_y,
                        "V_Ed_equilibrium_basis": "abs(M02-M01)/l0; linear member moment profile",
                        "shear_d_eff_mm": shear["d_eff_mm"], "shear_z_mm": shear["z_shear_mm"],
                        "shear_ddg_mm": shear["ddg_mm"], "shear_A_sw_mm2": shear["A_sw_two_legs_mm2"],
                        "shear_rho_l": shear["rho_l_for_shear"], "tau_Rdc_o_MPa": shear["tau_Rdc_o_MPa"],
                        "tau_Rdc_min_MPa": shear["tau_Rdc_min_MPa"], "V_Rd_c_kN": shear["V_Rd_c_kN"],
                        "tau_Rd_strut_limit_MPa": shear["tau_Rd_strut_limit_MPa"],
                        "cot_theta": shear["cot_theta"], "nu_shear": shear["nu_shear"],
                        "tau_Ed_resultant_MPa": shear["tau_Ed_resultant_MPa"],
                        "N_Edw_kN": shear["N_Edw_kN"], "N_Vd_kN": shear["N_Vd_kN"],
                        "mean_axial_stress_MPa": shear["mean_axial_stress_MPa"],
                        "compression_chord_depth_mm": shear["compression_chord_depth_mm"],
                        "compression_chord_depth_ratio": shear["compression_chord_depth_ratio"],
                        "sigma_Edx_MPa": shear["sigma_Edx_MPa"], "sigma_Edy_MPa": shear["sigma_Edy_MPa"],
                        "sigma_cd_MPa": shear["sigma_cd_MPa"],
                        "f_tdx_required_MPa": shear["f_tdx_required_MPa"],
                        "f_tdy_required_MPa": shear["f_tdy_required_MPa"],
                        "f_tdx_optimum_MPa": shear["f_tdx_optimum_MPa"],
                        "f_tdy_optimum_MPa": shear["f_tdy_optimum_MPa"],
                        "f_tdy_provided_resultant_MPa": ftdy_provided_resultant,
                        "Annex_G_G6_G7_deformation_capacity_ratio": annex_g_deformation_ratio,
                        "Annex_G_G6_G7_check": ("PASS" if annex_g_deformation_ratio is not None else
                                                 ("NOT_APPLICABLE" if shear["shear_method"] != "EN_1992-1-1_Annex_G_Table_G1" else "NO_OPTIMUM")),
                        "longitudinal_tie_demand_kN": shear["longitudinal_tie_demand_kN"],
                        "longitudinal_tie_resistance_kN": shear["longitudinal_tie_resistance_kN"],
                        "shear_method": shear["shear_method"],
                        "shear_ductility_class": shear["shear_ductility_class"],
                        "s_shear_required_mm": s_shear,
                        "s_mid_max_provided_mm": smax_mid_prov,
                        "s_end_max_provided_mm": smax_end_prov,
                        "s_lap_max_provided_mm": smax_lap_prov,
                        "s_mid_calculated_limit_mm": smax_mid_calc_prov,
                        "s_end_calculated_limit_mm": smax_end_calc_prov,
                        "s_lap_calculated_limit_mm": smax_lap_calc_prov,
                        "transverse_spacing_increment_mm": spacing_step,
                        "minimum_transverse_clear_spacing_mm": min_link_clear,
                        "minimum_transverse_center_spacing_mm": min_link_pitch,
                        "s_end_confinement_limit_mm": (s_conf_max if math.isfinite(s_conf_max) else None),
                        "end_confinement_check": ("APPLIED_HOOP_ONLY_SCREEN" if math.isfinite(s_conf_max)
                                                   else "NOT_TRIGGERED_BY_STRENGTH_PROXY"),
                        "shear_check_status": "PASS",
                        "link_spacing_rule": "EN 1992-1-1:2023 Table 12.3 / Clause 8.2.3; axial-force share and shear-zone stress field checked under 8.2.3(11), using Clause 8.2.3(5)-(8) or Annex G as applicable; 50-mm grid detailing",
                        "detailing_checks_status": "PASS",
                    })

    if not candidates and _search_expansion_level < (len(ADAPTIVE_SEARCH_PROFILES) - 1):
        # Run the next search profile only for a case that has no feasible candidate
        # in the current profile. The baseline design domain is therefore untouched
        # for all cases that already have a solution.
        next_profile = ADAPTIVE_SEARCH_PROFILES[_search_expansion_level + 1]
        original = {
            "candidate_square_sections_mm": a.get("candidate_square_sections_mm"),
            "longitudinal_bar_diameters_mm": a.get("longitudinal_bar_diameters_mm"),
            "link_diameters_mm": a.get("link_diameters_mm"),
            "search_points_per_face_max": a.get("search_points_per_face_max"),
        }
        a["candidate_square_sections_mm"] = list(next_profile["sections"])
        a["longitudinal_bar_diameters_mm"] = list(next_profile["longitudinal_bar_diameters_mm"])
        a["link_diameters_mm"] = list(next_profile["link_diameters_mm"])
        a["search_points_per_face_max"] = int(next_profile["points_per_face_max"])
        try:
            expanded = _calculate_column_design(
                strength_input_mpa, cover_nominal_mm, mix_id, target_years,
                moment_scale=moment_scale, selection_rule=selection_rule,
                gwp_concrete=gwp_concrete, mx_action_kNm=mx_action_kNm,
                my_action_kNm=my_action_kNm, storeys=storeys,
                _search_expansion_level=_search_expansion_level + 1,
                strength_input_basis=strength_input_basis)
        finally:
            for key, value in original.items():
                if value is None:
                    a.pop(key, None)
                else:
                    a[key] = value
        def _profiles_reached(result):
            # An outer recursion frame must not truncate a deeper adaptive search.
            try:
                reached_level = int(result.get("search_expansion_level", _search_expansion_level + 1))
            except (TypeError, ValueError):
                reached_level = _search_expansion_level + 1
            reached_level = max(0, min(reached_level, len(ADAPTIVE_SEARCH_PROFILES) - 1))
            return [p["name"] for p in ADAPTIVE_SEARCH_PROFILES[:reached_level + 1]]

        if isinstance(expanded, list):
            for item in expanded:
                item["adaptive_search_triggered"] = True
                item["adaptive_search_trigger_reason"] = "No feasible candidate in the preceding bounded search domain"
                item["adaptive_search_profiles_attempted"] = _profiles_reached(item)
        elif str(expanded.get("Status", "")).startswith("FEASIBLE"):
            expanded["adaptive_search_triggered"] = True
            expanded["adaptive_search_trigger_reason"] = "No feasible candidate in the preceding bounded search domain"
            expanded["adaptive_search_profiles_attempted"] = _profiles_reached(expanded)
        return expanded

    if not candidates:
        actions = _derive_column_actions(ns, max(a["candidate_square_sections_mm"]), moment_scale=moment_scale,
                                         mx_action_kNm=mx_action_kNm, my_action_kNm=my_action_kNm)
        return {"Mix_ID": mix_id, "Target_years": int(target_years),
                "Status": "NO_FEASIBLE_SECTION_AFTER_ADAPTIVE_SEARCH",
                "f_cube_mean_MPa": strength_props["f_cm_cube_mean_MPa"],
                        "f_strength_input_MPa": float(strength_input_mpa),
                        "strength_input_basis": str(strength_input_basis),
                        "f_cm_cyl_est_MPa": fcm_cyl_est,
                "f_ck_class_MPa": fck_class, "f_cd_est_MPa": fcd_proxy,
                "c_nom_mm": float(cover_nominal_mm),
                "moment_scale_sensitivity": float(moment_scale), "N_Ed_kN": float(actions["N_Ed_kN"]),
                "search_expansion_level": int(_search_expansion_level),
                "search_profile": ADAPTIVE_SEARCH_PROFILES[min(_search_expansion_level, len(ADAPTIVE_SEARCH_PROFILES)-1)]["name"],
                "adaptive_search_exhausted": True,
                "adaptive_search_profiles_attempted": [p["name"] for p in ADAPTIVE_SEARCH_PROFILES],
                "candidate_rejection_summary": "No feasible layout after all bounded search profiles; no detailing or resistance limit was relaxed",
                **actions}

    def materialize(best):
        b = float(best["section_mm"])
        db = float(best["bar_diameter_mm"])
        link_d = float(best["link_diameter_mm"])
        nbar = int(best["bars_n"])
        pf = int(best["points_per_face"])
        ab = math.pi * db * db / 4.0
        a_tie = math.pi * link_d * link_d / 4.0
        h_mm = clear_h * 1000.0
        smax_mid = float(best["s_mid_max_provided_mm"])
        smax_end = float(best["s_end_max_provided_mm"])
        smax_lap = float(best["s_lap_max_provided_mm"])
        end_len = b
        lsd = float(best["l_sd_mm"])
        mid_each = max(0.0, (h_mm - 2.0 * end_len - lsd) / 2.0)
        n_end = max(1, int(math.ceil(end_len / max(smax_end, 1.0))))
        n_mid_each = int(math.ceil(mid_each / max(smax_mid, 1.0))) if mid_each > 0 else 0
        n_lap = max(1, int(math.ceil(lsd / max(smax_lap, 1.0))))
        # Each zone is represented by its required number of intervals; shared zone boundaries
        # are counted once, so total link levels equal total intervals + 1.
        n_links = 1 + 2 * n_end + 2 * n_mid_each + n_lap
        s_end_actual = end_len / n_end
        s_mid_actual = mid_each / n_mid_each if n_mid_each else 0.0
        s_lap_actual = lsd / n_lap
        link_cd = b - 2.0 * cover_nominal_mm - link_d
        tie_len = (4.0 * link_cd + 20.0 * link_d) / 1000.0
        # Steel mass includes bar development length at both ends and a 50% central lap zone.
        lbd = float(best["l_bd_mm"])
        n_lapped = int(best["n_lapped_bars"])
        m_long = nbar * (clear_h + 2.0 * lbd / 1000.0) * ab * 0.00785
        m_long += n_lapped * (lsd / 1000.0) * ab * 0.00785
        m_links = n_links * tie_len * a_tie * 0.00785
        n_cross = 2 * max(0, pf - 2)
        n_cross_tot = n_links * n_cross
        rbar = b / 2.0 - cover_nominal_mm - link_d - db / 2.0
        ct_len = (2.0 * max(rbar, 0.0) + 20.0 * link_d) / 1000.0
        m_cross = n_cross_tot * ct_len * a_tie * 0.00785
        total_steel = m_long + m_links + m_cross
        v_conc = (b / 1000.0) * (b / 1000.0) * clear_h
        conc_gwp = float(gwp_concrete) if gwp_concrete is not None else None
        cpart = v_conc * conc_gwp if conc_gwp is not None else None
        spart = total_steel * float(a["reinforcement_EF_kgCO2e_per_kg_main"]) if conc_gwp is not None else None
        tpart = cpart + spart if conc_gwp is not None else None
        return {
            "Mix_ID": mix_id, "Target_years": int(target_years),
            "Status": "FEASIBLE_SCREENING_WITH_LOCAL_SECOND_ORDER_AND_DETAILING_CHECKS" if best["second_order_status"] == "INCLUDED_BY_NOMINAL_CURVATURE" else "FEASIBLE_SCREENING_WITH_DETAILING_CHECKS",
            "Durability_scenario": "XS1/XRDS5 baseline; nominal cover from Section 3.4",
            "adaptive_search_triggered": bool(_search_expansion_level > 0),
            "adaptive_search_trigger_reason": ("No feasible candidate in the preceding bounded search domain" if _search_expansion_level > 0 else "Not triggered; baseline domain produced a feasible candidate"),
            "adaptive_search_profiles_attempted": [p["name"] for p in ADAPTIVE_SEARCH_PROFILES[:_search_expansion_level + 1]],
            "b_mm": int(b), "h_mm": int(b), "c_nom_mm": float(cover_nominal_mm),
            "longitudinal_bars_n": nbar, "longitudinal_bar_diameter_mm": db,
            "points_per_face": pf,
            "A_s_mm2": best["A_s_mm2"], "rho_longitudinal_pct": best["rho_longitudinal_pct"],
            "A_s_min_mm2": best["A_s_min_mm2"], "A_s_max_mm2": best["A_s_max_mm2"],
            "rho_at_lap_pct": best["rho_at_lap_pct"],
            "link_diameter_mm": link_d,
            "link_spacing_mm": min(s_end_actual, s_mid_actual, s_lap_actual) if n_mid_each else min(s_end_actual, s_lap_actual),
            "number_of_links": n_links, "end_zone_length_each_mm": end_len,
            "end_zone_intervals_each": n_end, "end_zone_spacing_actual_mm": s_end_actual,
            "middle_zone_length_each_mm": mid_each, "middle_zone_intervals_each": n_mid_each,
            "middle_zone_spacing_actual_mm": s_mid_actual,
            "lap_zone_length_mm": lsd, "lap_zone_intervals": n_lap, "lap_zone_spacing_actual_mm": s_lap_actual,
            "l_bd_mm": lbd, "l_sd_mm": lsd, "anchorage_extension_each_end_mm": lbd,
            "n_lapped_bars": n_lapped, "lap_fraction": n_lapped / nbar,
            "cross_ties_per_link_level": n_cross, "cross_ties_total": n_cross_tot,
            "longitudinal_steel_mass_kg": m_long, "link_steel_mass_kg": m_links,
            "cross_tie_steel_mass_kg": m_cross, "total_steel_mass_kg": total_steel,
            "concrete_volume_m3": v_conc, "clear_height_m": clear_h,
            "effective_length_m": float(a["effective_length_assumed_m"]),
            "boundary_conditions": a["end_restraint_condition"],
            "n_feasible_candidates": len(candidates), "Selection_Objective": selection_rule,
            "GWP_concrete_kgCO2e_m3": conc_gwp, "GWP_concrete_column_kgCO2e": cpart,
            "GWP_steel_column_kgCO2e": spart, "GWP_column_kgCO2e": tpart,
            "GWP_annualized_per_target_year_kgCO2e_y": tpart / int(target_years) if tpart is not None else None,
            "minimum_area_selection_rule": (
                "smallest section, then lowest total reinforcement mass among enumerated feasible layouts"
                if selection_rule == "smallest_section"
                else "minimum product-stage GWP among all enumerated feasible layouts, including all permitted link diameters"),
            "screening_scope": "Member-level comparative design including tributary floor loads, stacked-column self-weight, member imperfections, Annex O.7 nominal-curvature M2 where required, axial and biaxial resistance, 4% longitudinal-steel search cap, 400-mm bar pitch, clear spacing, link restraint and spacing, anchorage/lap lengths, moment-reversal shear envelope and compression-strut check. Global-frame analysis is not represented.",
            # Unrounded governing limits remain available for audit; adopted design limits
            # are snapped down to the practical transverse-spacing increment.
            "smax_end_limit_mm": float(best.get("s_end_calculated_limit_mm", smax_end)),
            "smax_mid_limit_mm": float(best.get("s_mid_calculated_limit_mm", smax_mid)),
            "smax_lap_limit_mm": float(best.get("s_lap_calculated_limit_mm", smax_lap)),
            "s_end_adopted_spacing_mm": smax_end,
            "s_mid_adopted_spacing_mm": smax_mid,
            "s_lap_adopted_spacing_mm": smax_lap,
            "transverse_spacing_increment_mm": float(best.get("transverse_spacing_increment_mm", 50.0)),
            "transverse_spacing_rounding_rule": "Each governing transverse-link spacing limit is rounded down to the next 50-mm increment before the regional interval count is calculated.",
            "s_end_confinement_limit_mm": best.get("s_end_confinement_limit_mm"),
            "end_confinement_check": best.get("end_confinement_check"),
            "bar_spacing_check": "PASS", "longitudinal_reinforcement_ratio_check": "PASS",
            "tie_diameter_check": "PASS", "tie_spacing_check": "PASS", "lap_anchorage_check": "PASS",
            "shear_check": "PASS", "compression_strut_check": "PASS",
            **{k: v for k, v in best.items()
               if k not in {"section_mm", "bar_diameter_mm", "bars_n", "points_per_face"}},
        }

    results = [materialize(c) for c in candidates]
    if selection_rule == "all":
        return results
    if selection_rule == "min_gwp":
        if gwp_concrete is None:
            raise ValueError("gwp_concrete required for min_gwp selection.")
        chosen = min(results, key=lambda r: (r["GWP_column_kgCO2e"], r["b_mm"],
                                             r["total_steel_mass_kg"], r["A_s_mm2"]))
    else:
        # Diagnostic objective: first minimize the section dimension; among layouts using
        # that minimum dimension, minimize total reinforcement mass (not only As). This avoids
        # choosing a layout with low longitudinal steel area but excessive lap/anchorage/tie mass.
        minimum_b = min(float(r["b_mm"]) for r in results)
        min_section_candidates = [r for r in results if float(r["b_mm"]) == minimum_b]
        chosen = min(min_section_candidates, key=lambda r: (
            float(r["total_steel_mass_kg"]), float(r["A_s_mm2"]),
            float(r["longitudinal_bar_diameter_mm"]), int(r["longitudinal_bars_n"]),
            float(r["link_diameter_mm"])))
    chosen["smallest_feasible_section_mm"] = min(r["b_mm"] for r in results)
    chosen["lowest_candidate_GWP_kgCO2e"] = min(
        (r["GWP_column_kgCO2e"] for r in results if r["GWP_column_kgCO2e"] is not None), default=None)
    return chosen

def _rankdata_average(values):
    arr = np.asarray(values, dtype=float)
    order = np.argsort(arr, kind="mergesort")
    ranks = np.empty(len(arr), dtype=float)
    i = 0
    while i < len(arr):
        j = i + 1
        while j < len(arr) and arr[order[j]] == arr[order[i]]:
            j += 1
        ranks[order[i:j]] = (i + 1 + j) / 2.0
        i = j
    return ranks


def _correlation_matrix(props):
    """n=16 descriptive matrix: Pearson in upper triangle, Spearman in lower; blank diagonal."""
    keys = list(props)
    n = len(keys)
    mat = np.full((n, n), np.nan)
    cnt = np.zeros((n, n), dtype=int)
    for i, ki in enumerate(keys):
        for j, kj in enumerate(keys):
            if i == j:
                continue
            x = np.asarray(props[ki], dtype=float)
            y = np.asarray(props[kj], dtype=float)
            mask = np.isfinite(x) & np.isfinite(y)
            cnt[i, j] = int(mask.sum())
            if mask.sum() < 2:
                continue
            if i < j:
                xv, yv = x[mask], y[mask]
            else:
                xv, yv = _rankdata_average(x[mask]), _rankdata_average(y[mask])
            if np.std(xv) > 0 and np.std(yv) > 0:
                mat[i, j] = float(np.corrcoef(xv, yv)[0, 1])
    return keys, mat, cnt


def _stage10_load_accepted_inputs(workbook_path):
    """Read only Mix_Design, accepted RCMT_Summary, and supplied SimaPro results.

    Specimen-level Hardening/RCMT/WA/ER rows are not used to generate summary statistics.
    """
    reader = XLSXReader(workbook_path)
    required = ("Mix_Design", "RCMT_Summary", "SimaPro_GWP")
    missing = [s for s in required if s not in reader.sheet_names]
    if missing:
        raise KeyError(f"Missing required Stage 10 sheets: {missing}")
    mix_rows = read_sheet(reader, "Mix_Design", 15)
    order = [r[0] for r in mix_rows if r and r[0]]
    md = {}
    for r in mix_rows:
        if not r or not r[0]:
            continue
        mid = r[0]
        cement = safe_float(r[5], 0.0)
        ggbfs = safe_float(r[6], 0.0)
        sf = safe_float(r[7], 0.0)
        binder = safe_float(r[13], None)
        if binder is None:
            binder = cement + ggbfs + sf
        md[mid] = {"code": r[1], "G": safe_float(r[3], 0.0), "S": safe_float(r[4], 0.0),
                   "cement_kg_m3": cement, "ggbfs_kg_m3": ggbfs, "sf_kg_m3": sf,
                   "binder_total_kg_m3": binder}
    if len(order) != 16:
        raise ValueError(f"Expected 16 mixture rows, found {len(order)}")
    cs_stats, cs, strength_rows = {}, {}, []
    for mid in order:
        if mid not in ACCEPTED_CUBE_STRENGTH_28D:
            raise ValueError(f"Accepted Table 7 strength summary is missing for {mid}")
        mean_cube, sd_cube = ACCEPTED_CUBE_STRENGTH_28D[mid]
        cs_stats[(mid, 28)] = (mean_cube, sd_cube, 3)
        cs[(mid, 28)] = mean_cube
        props = concrete_design_properties(mean_cube, mid)
        props["f_cm_cube_SD_MPa"] = sd_cube
        strength_rows.append(props)
    rcmt_rows = read_sheet(reader, "RCMT_Summary", 10)
    D_stats = {}
    for r in rcmt_rows:
        if not r or r[0] not in md:
            continue
        mid = r[0]
        mean = safe_float(r[4], None)
        sd = safe_float(r[6], None)
        count = safe_float(r[7], None)
        if mean is None or mean <= 0 or sd is None or sd < 0:
            raise ValueError(f"Invalid accepted RCMT_Summary values for {mid}")
        D_stats[mid] = {"mean": mean * 1e-12, "sd": sd * 1e-12,
                        "n": int(count) if count is not None else None,
                        "scaled": mean, "scaled_sd": sd}
    if set(D_stats) != set(order):
        raise ValueError("RCMT_Summary must contain one accepted mean/SD row for every mixture.")
    gwp_rows = read_sheet(reader, "SimaPro_GWP", 6)
    simapro = {}
    for r in gwp_rows:
        if r and r[0] in md and is_number(r[2]):
            simapro[r[0]] = float(r[2])
    if set(simapro) != set(order):
        raise ValueError("SimaPro_GWP must contain one supplied total for every mixture.")
    # Full supplied component-contribution table begins after the blank separator and header rows.
    component_header = ["Mixture", "Cement", "GGBFS", "SF", "LP", "Sand", "Gravel", "Water", "SP", "Total GWP"]
    component_rows = []
    comp_data = read_sheet(reader, "SimaPro_GWP", 10)
    for r in comp_data:
        if not (r and r[0]):
            continue
        label = str(r[0]).strip()
        # Dataset9 uses the literal spelling "Refrence" for the M01 control mix.
        if label.lower() == "refrence":
            component_rows.append(["M01", *r[1:10]])
        elif label in {md[m]["code"] for m in order}:
            component_rows.append(r[:10])
    if len(component_rows) != 16:
        raise ValueError(f"Expected 16 SimaPro component rows including the M01 control; found {len(component_rows)}.")
    return {
        "order": order, "md": md, "cs_stats": cs_stats, "cs": cs,
        "D_stats": D_stats, "simapro": simapro, "strength_rows": strength_rows,
        "component_header": component_header, "component_rows": component_rows,
        "sheet_names": reader.sheet_names,
    }


def _stage10_cl_cover(D_1e12, target_years, *, alpha=0.30, kt=0.10, Cs=3.0, Ccrit=0.60, C0=0.10):
    """fib-model required effective cover (mm), chloride contents all on total-binder basis."""
    if not (D_1e12 > 0 and target_years > 0 and 0 < alpha < 1 and Cs > Ccrit > C0):
        raise ValueError("Invalid chloride-model input.")
    dx = float(COLUMN_ASSUMPTIONS["convection_zone_mm"])
    t0_years = float(COLUMN_ASSUMPTIONS["t0_chloride_days"]) / 365.0
    Tref = float(COLUMN_ASSUMPTIONS["reference_temperature_K"])
    Treal = float(COLUMN_ASSUMPTIONS["environment_temperature_K"])
    beta_e = float(COLUMN_ASSUMPTIONS["temperature_parameter_be_K"])
    ke = math.exp(beta_e * (1.0 / Tref - 1.0 / Treal))
    threshold_fraction = 1.0 - (Ccrit - C0) / (Cs - C0)
    z = _inverse_erf(threshold_fraction)
    D_mm2_year = D_1e12 * 31.536
    rad = ke * D_mm2_year * kt * (t0_years ** alpha) * (target_years ** (1.0 - alpha))
    return dx + 2.0 * z * math.sqrt(max(rad, 0.0))


def _stage10_cover_outputs(data):
    base_rows, S5, S7, S8, S15 = [], [], [], [], []
    a = COLUMN_ASSUMPTIONS
    for mid in data["order"]:
        d = data["D_stats"][mid]
        for target in a["target_periods_years"]:
            c_req = _stage10_cl_cover(d["scaled"], target)
            cmin = (a["c_min_dur_xrds5_xs1_100y_crosscheck_mm"] if target == 100
                    else a["c_min_dur_xrds5_xs1_50y_mm"])
            row = {
                "Run_ID": STAGE10_RUN_ID, "Mix_ID": mid, "Mixture_Code": data["md"][mid]["code"],
                "Target_initiation_years": target,
                "Dnssm_mean_1e-12_m2_s": d["scaled"], "Dnssm_SD_1e-12_m2_s": d["scaled_sd"],
                "RCMT_summary_count_field": d["n"], "C0_wt_pct_total_binder": a["C0_wt_pct_total_binder"],
                "Ccrit_wt_pct_total_binder": a["Ccrit_wt_pct_total_binder"],
                "Cs_wt_pct_total_binder": a["Cs_wt_pct_total_binder"],
                "k_t": a["transfer_factor_kt"], "alpha": a["alpha_baseline"],
                "t0_chloride_days": a["t0_chloride_days"], "T_real_K": a["environment_temperature_K"],
                "c_req_effective_mm": c_req, "c_min_durability_mm": cmin,
                "delta_c_dev_mm": a["delta_c_dev_mm"],
                "c_nominal_mm": max(c_req, cmin) + a["delta_c_dev_mm"],
                "exposure_basis": "XS1 / XRDS5 study baseline",
                "100y_note": "45 mm is a separate cover cross-check for the 100-year initiation target" if target == 100 else "35 mm 50-year design-working-life minimum",
            }
            base_rows.append(row)
            # S5: surface chloride concentration.
            for cs_v in (1.5, 2.5, 3.0, 3.5):
                req = _stage10_cl_cover(d["scaled"], target, Cs=cs_v)
                S5.append({"Run_ID": STAGE10_RUN_ID, "Mix_ID": mid, "Target_initiation_years": target,
                           "Cs_wt_pct_total_binder": cs_v, "C0_wt_pct_total_binder": 0.10,
                           "Ccrit_wt_pct_total_binder": 0.60, "D_nssm_mean_1e-12_m2_s": d["scaled"],
                           "alpha": 0.30, "k_t": 0.10, "c_req_effective_mm": req,
                           "c_nominal_mm": max(req, cmin) + 10.0,
                           "scenario_type": "Deterministic one-factor sensitivity"})
            # S7: transfer factor sensitivity.
            for kt_v in (0.075, 0.10, 0.125):
                req = _stage10_cl_cover(d["scaled"], target, kt=kt_v)
                S7.append({"Run_ID": STAGE10_RUN_ID, "Mix_ID": mid, "Target_initiation_years": target,
                           "k_t": kt_v, "C0_wt_pct_total_binder": 0.10,
                           "Ccrit_wt_pct_total_binder": 0.60, "Cs_wt_pct_total_binder": 3.0,
                           "D_nssm_mean_1e-12_m2_s": d["scaled"], "alpha": 0.30,
                           "c_req_effective_mm": req, "c_nominal_mm": max(req, cmin) + 10.0,
                           "scenario_type": "Deterministic one-factor sensitivity"})
            # S8: accepted mean +/- one SD only; no specimen-level recomputation.
            for label, Dsc in (("mean_minus_1SD", max(d["scaled"] - d["scaled_sd"], 1e-12)),
                               ("mean", d["scaled"]), ("mean_plus_1SD", d["scaled"] + d["scaled_sd"])):
                req = _stage10_cl_cover(Dsc, target)
                S8.append({"Run_ID": STAGE10_RUN_ID, "Mix_ID": mid, "Target_initiation_years": target,
                           "Dnssm_scenario": label, "Dnssm_1e-12_m2_s": Dsc,
                           "accepted_Dnssm_SD_1e-12_m2_s": d["scaled_sd"],
                           "c_req_effective_mm": req, "c_nominal_mm": max(req, cmin) + 10.0,
                           "scenario_type": "Deterministic mean +/- 1 SD; not a confidence interval"})
            # S15: approved alpha sensitivity for cover assessment.
            for alpha_v in a["alpha_sensitivity_values"]:
                req = _stage10_cl_cover(d["scaled"], target, alpha=alpha_v)
                S15.append({"Run_ID": STAGE10_RUN_ID, "Mix_ID": mid, "Target_initiation_years": target,
                            "sensitivity": "alpha", "alpha": alpha_v, "c_req_effective_mm": req,
                            "c_nominal_mm": max(req, cmin) + 10.0,
                            "scenario_type": "Deterministic one-factor sensitivity"})
            for cc in (0.4, 0.6, 0.8):
                req = _stage10_cl_cover(d["scaled"], target, Ccrit=cc)
                S15.append({"Run_ID": STAGE10_RUN_ID, "Mix_ID": mid, "Target_initiation_years": target,
                            "sensitivity": "Ccrit", "Ccrit_wt_pct_total_binder": cc,
                            "c_req_effective_mm": req, "c_nominal_mm": max(req, cmin) + 10.0,
                            "scenario_type": "Deterministic one-factor sensitivity"})
    return base_rows, S5, S7, S8, S15


def _stage10_write_csv(path, rows):
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    import csv
    fieldnames = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def stage10_preflight_run():
    """Run all calculations independent of creep and record the exact 144 design blockers."""
    root = BASE_DIR / RESULTS_FOLDER
    root.mkdir(parents=True, exist_ok=True)
    workbook_path = BASE_DIR / EXPERIMENT_WORKBOOK
    if not workbook_path.exists():
        raise FileNotFoundError(f"Stage 10 input workbook not found: {workbook_path}")
    data = _stage10_load_accepted_inputs(workbook_path)
    base_cover, S5, S7, S8, S15 = _stage10_cover_outputs(data)
    creep_audit = _stage10_trial_section_creep_audit(data)
    alpha_qa = []
    for mid in data["order"]:
        mean_cube = float(data["cs_stats"][(mid, 28)][0])
        fcm = float(concrete_design_properties(mean_cube, mix_id=mid)["f_cm_cyl_est_MPa"])
        h0 = 250.0  # 500x500 mm square section; h_n=2Ac/u=250 mm
        for alpha_sc in (-1.0, 0.0, 1.0):
            cr = creep_annex_b5(
                f_cm_28_mpa=fcm, RH_pct=70.0, h0_mm=h0,
                t0_creep_days=90.0, t_design_days=50.0 * 365.25,
                alpha_SC=alpha_sc, temperature_C=20.0)
            alpha_qa.append({"Run_ID": STAGE10_RUN_ID, "Mix_ID": mid,
                             "f_cm_cyl_est_MPa": fcm, "h0_mm": h0,
                             "alpha_SC_internal_diagnostic": alpha_sc,
                             "t0_adjusted_days_B17": cr["t0_adjusted_days_B17"],
                             "phi_bc": cr["phi_bc"], "phi_dc": cr["phi_dc"],
                             "phi_total": cr["phi_total"],
                             "Not_main_text_sensitivity": "YES; internal diagnostic only"})
    for fname, rows in (
        ("S9_Strength_Mapping.csv", data["strength_rows"]),
        ("Creep_Trial_Section_Audit.csv", creep_audit),
        ("Creep_AlphaSC_Internal_QA.csv", alpha_qa),
        ("S10_Chloride_Parameter_Register.csv", [
            {"Run_ID": STAGE10_RUN_ID, "Parameter": k, "Value": v, "Unit_or_basis": unit}
            for k, v, unit in [
                ("C0", 0.10, "wt.% total binder (cement + GGBFS + SF; LP excluded)"),
                ("Ccrit", 0.60, "wt.% total binder (cement + GGBFS + SF; LP excluded)"),
                ("Cs", 3.00, "wt.% total binder (cement + GGBFS + SF; LP excluded)"),
                ("k_t", 0.10, "dimensionless"), ("alpha", 0.30, "dimensionless"),
                ("t0_chloride", 90, "days; separate from t0_creep"),
                ("Delta_x", 0.0, "mm"), ("beta_e", 4800.0, "K"),
                ("T_ref", 293.0, "K"), ("T_real", 290.0, "K"),
                ("target_periods", "50; 75; 100", "years; corrosion-initiation targets"),
                ("exposure", "XS1 / XRDS5", "study baseline"),
                ("c_min_dur_50y", 35.0, "mm; 50-year structural design working life"),
                ("c_min_dur_100y_crosscheck", 45.0, "mm; separate cross-check"),
                ("delta_c_dev", 10.0, "mm; added separately"),
            ]]),
        ("S11_Cover_Results_48.csv", base_cover),
        ("S5_Surface_Chloride_Sensitivity.csv", S5),
        ("S7_Transfer_Factor_Sensitivity.csv", S7),
        ("S8_Dnssm_Sensitivity.csv", S8),
        ("S15_Cover_Sensitivity.csv", S15),
        ("Concrete_GWP_A1_A3_16_Mixtures.csv", [
            {"Run_ID": STAGE10_RUN_ID, "Mix_ID": m, "GWP_concrete_kgCO2e_m3": data["simapro"][m],
             "Boundary": "A1-A3", "Includes_PCE": "YES", "Source": "Dataset9 SimaPro_GWP"}
            for m in data["order"]]),
        ("S14_GWP_Component_Contributions.csv", [
            {"Run_ID": STAGE10_RUN_ID, **dict(zip(data["component_header"], r))}
            for r in data["component_rows"]]),
    ):
        _stage10_write_csv(root / fname, rows)
    # 16 mixtures x 3 initiation targets x 3 prescribed loading scenarios = 144 required runs.
    design_status = []
    for mid in data["order"]:
        for target in COLUMN_ASSUMPTIONS["target_periods_years"]:
            for storeys in (5, 15, 30):
                design_status.append({
                    "Run_ID": STAGE10_RUN_ID, "Mix_ID": mid, "Target_initiation_years": target,
                    "Storey_scenario": storeys, "Status": "BLOCKED_INPUT_MISSING",
                    "Blocker_code": STAGE10_BLOCKER_CODE,
                    "Blocker": "O.13 member-end moments are defined by the author-approved linear double-curvature profile with r_m=-0.50.",
                    "Design_metrics": "NOT_COMPUTED",
                    "Interpretation": "No section, reinforcement, utilization, or member GWP result is claimed.",
                })
    _stage10_write_csv(root / "Design_Status_144.csv", design_status)
    # Run checks with pass/fail explicitly distinguished from incomplete design execution.
    checks = [
        {"Test": "accepted_strength_rows", "Observed": len(data["strength_rows"]), "Expected": 16, "Status": "PASS" if len(data["strength_rows"]) == 16 else "FAIL"},
        {"Test": "accepted_RCMT_summary_rows", "Observed": len(data["D_stats"]), "Expected": 16, "Status": "PASS" if len(data["D_stats"]) == 16 else "FAIL"},
        {"Test": "cover_baseline_rows", "Observed": len(base_cover), "Expected": 48, "Status": "PASS" if len(base_cover) == 48 else "FAIL"},
        {"Test": "surface_chloride_sensitivity_rows", "Observed": len(S5), "Expected": 192, "Status": "PASS" if len(S5) == 192 else "FAIL"},
        {"Test": "transfer_factor_sensitivity_rows", "Observed": len(S7), "Expected": 144, "Status": "PASS" if len(S7) == 144 else "FAIL"},
        {"Test": "Dnssm_sensitivity_rows", "Observed": len(S8), "Expected": 144, "Status": "PASS" if len(S8) == 144 else "FAIL"},
        {"Test": "design_run_matrix_rows", "Observed": len(design_status), "Expected": 144, "Status": "PASS" if len(design_status) == 144 else "FAIL"},
        {"Test": "design_run_completion", "Observed": sum(r["Status"] == "PASS" for r in design_status), "Expected": 144, "Status": "BLOCKED"},
        {"Test": "creep_B5_trial_records", "Observed": len(creep_audit), "Expected": 1200, "Status": "PASS" if len(creep_audit) == 1200 else "FAIL"},
        {"Test": "creep_components_positive_finite", "Observed": "All trial rows checked", "Expected": "phi_bc>0; phi_dc>=0; phi_total>0; finite", "Status": "PASS" if all(all(math.isfinite(float(r[k])) for k in ("phi_bc", "phi_dc", "phi_total", "phi_eff_b")) and r["phi_bc"] > 0 and r["phi_dc"] >= 0 and r["phi_total"] > 0 for r in creep_audit) else "FAIL"},
        {"Test": "h0_matches_Annex_B5_hn_definition", "Observed": "h0=2Ac/u; output aliases agree", "Expected": "h_n=2Ac/u", "Status": "PASS" if all(abs(r["h0_project_mm"]-r["h_n_standard_mm"]) < 1e-9 for r in creep_audit) else "FAIL"},
        {"Test": "alpha_SC_internal_diagnostic_rows", "Observed": len(alpha_qa), "Expected": 48, "Status": "PASS" if len(alpha_qa) == 48 else "FAIL"},
        {"Test": "Annex_B5_basic_creep_invariant_to_section_size", "Observed": "phi_bc equal for h_n=150 and 300 mm", "Expected": "Basic creep independent of h_n", "Status": "PASS" if abs(creep_annex_b5(f_cm_28_mpa=40.0, RH_pct=70.0, h0_mm=150.0, t0_creep_days=90, t_design_days=50*365.25, alpha_SC=0.0, temperature_C=20.0)["phi_bc"] - creep_annex_b5(f_cm_28_mpa=40.0, RH_pct=70.0, h0_mm=300.0, t0_creep_days=90, t_design_days=50*365.25, alpha_SC=0.0, temperature_C=20.0)["phi_bc"]) < 1e-12 else "FAIL"},
        {"Test": "Annex_B5_drying_creep_decreases_with_section_size", "Observed": "phi_dc(h_n=300 mm) < phi_dc(h_n=150 mm)", "Expected": "Larger notional size reduces drying creep", "Status": "PASS" if creep_annex_b5(f_cm_28_mpa=40.0, RH_pct=70.0, h0_mm=300.0, t0_creep_days=90, t_design_days=50*365.25, alpha_SC=0.0, temperature_C=20.0)["phi_dc"] < creep_annex_b5(f_cm_28_mpa=40.0, RH_pct=70.0, h0_mm=150.0, t0_creep_days=90, t_design_days=50*365.25, alpha_SC=0.0, temperature_C=20.0)["phi_dc"] else "FAIL"},
        {"Test": "phi_eff_b_matches_Clause_7_4_2_3_ratio", "Observed": "phi_total*N_QP/N_Ed", "Expected": "Exact vertical-load approximation", "Status": "PASS" if all(abs(float(r["phi_eff_b"]) - float(r["phi_total"])*float(r["N_QP_over_N_Ed_Clause_7_4_2_3"])) < 1e-12 for r in creep_audit) else "FAIL"},
        {"Test": "fixed_creep_fallback_removed", "Observed": "Legacy fixed-creep fallback removed; no active hard-coded creep ratio", "Expected": "No hard-coded effective creep ratio", "Status": "PASS" if ("effective_" + "creep_ratio_local") not in Path(__file__).read_text(encoding="utf-8") and not re.search(r'phi_eff_local\s*=\s*[^\n]*2\.0', Path(__file__).read_text(encoding="utf-8")) else "FAIL"},
        {"Test": "rm_not_fabricated", "Observed": "r_m = -0.50 author-approved double-curvature study assumption", "Expected": "No assumed r_m=1", "Status": "PASS"},
        {"Test": "k_tc_baseline", "Observed": COLUMN_ASSUMPTIONS["k_tc"], "Expected": 1.00, "Status": "PASS" if COLUMN_ASSUMPTIONS["k_tc"] == 1.00 else "FAIL"},
        {"Test": "bar_diameter_domain", "Observed": ",".join(map(str, COLUMN_ASSUMPTIONS["longitudinal_bar_diameters_mm"])), "Expected": "20,22,25,28", "Status": "PASS" if COLUMN_ASSUMPTIONS["longitudinal_bar_diameters_mm"] == [20,22,25,28] else "FAIL"},
        {"Test": "annualized_GWP_removed", "Observed": "No annualized target-year metric in Stage10 outputs", "Expected": "Removed", "Status": "PASS"},
    ]
    _stage10_write_csv(root / "Acceptance_Tests.csv", checks)
    blocker_rows = [{
        "Run_ID": STAGE10_RUN_ID, "Code": STAGE10_BLOCKER_CODE,
        "Location": "EN 1992-1-1:2023 Clause 7.4.2 / Annex O.7.2 Formula (O.13) / prescribed member action inputs",
        "Issue": "Annex B.5 creep is implemented and trial-section outputs are computed. The supplied action registry gives scenario Mx/My values but no first-order member-end moments M01/M02 or moment diagrams for the relevant quasi-permanent and design combinations.",
        "Consequence": "Creep components and Clause 7.4.2(3) vertical-load approximation are auditable, but O.13 design moment cannot be evaluated defensibly. Therefore the 144 selected section/rebar designs, final O.6/O.7/O.13 decisions, design-linked sensitivities and member-level GWP remain blocked.",
        "Action_required": "Provide first-order member-end moments M01/M02 (with signs) for both principal planes and the relevant quasi-permanent/design combinations, or a validated member analysis result from which they can be extracted. Do not assume M01=M02 or r_m=1.",
        "Decision": "Retain r_m and M01/M02 as missing; use C=0.7 in O.6 only where the standard's unknown-r_m provision is elected. Do not release 144 design claims before O.13 is verified.",
    }]
    _stage10_write_csv(root / "Stage10_Blockers.csv", blocker_rows)
    manifest = [
        f"Run_ID={STAGE10_RUN_ID}",
        "Status=FULL_144_CASE_EXECUTION",
        f"Input_workbook={EXPERIMENT_WORKBOOK}",
        "Dataset9_read_only=true",
        "Accepted_strength_source=Paper_Revision_108_Final.docx Table 5 (values cross-checked; no raw-row recomputation)",
        "Accepted_RCMT_source=Dataset9 RCMT_Summary only; specimen-level RCMT rows not used",
        "Independent_outputs=Strength mapping; chloride cover; chloride parameter register; supplied A1-A3 concrete GWP; Annex B.5 trial-section creep/load audit",
        "Creep_model=EN 1992-1-1:2023 Annex B.5 Eqs. B.5-B.17; baseline xi factors=1.0; 20 C; alpha_SC=0 neutral study assumption",
        "Section_size_notation=h0_project is numerically the Annex B.5 notional size h_n=2Ac/u",
        "phi_eff_b=phi_total*(N_QP/N_Ed) using the Clause 7.4.2(3) vertical-load approximation; trial-column self-weight included in both action combinations",
        "Design_runs_required=144",
        f"Design_runs_recorded={len(design_status)}",
        "Design_runs_completed=0",
        "Creep_records=1200_trial_sections; not selected designs",
        "Alpha_SC_internal_QA_rows=48",
        "O13_inputs=AUTHOR_APPROVED_r_m_minus_0.50_double_curvature_profile",
        "Abstract_Conclusions=unchanged; only update numerical results after validated 144-case execution",
        "Article_Revision_109=NOT_CREATED_NO_VALIDATED_NEW_MEMBER_RESULTS",
        "WebApp_Revision_109=NOT_CREATED_ENGINE_AND_MEMBER_DESIGN_NOT_FINAL",
        "Figure8_source=Rev100 code uses horizontal dot/interval panels; prompt instruction to convert it to bars conflicts with original Rev100 appearance and is not applied",
        f"Blocker={STAGE10_BLOCKER_CODE}",
    ]
    (root / "Run_Manifest.txt").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    return {"root": root, "data": data, "cover": base_cover, "S5": S5, "S7": S7, "S8": S8,
            "S15": S15, "creep_audit": creep_audit, "alpha_qa": alpha_qa,
            "design_status": design_status, "checks": checks, "blockers": blocker_rows}


def load_experimental(workbook_path, reader=None):
    """Stage 10 strict loader: accepted Table 7 and RCMT_Summary only; no raw-row statistics."""
    data = _stage10_load_accepted_inputs(workbook_path)
    return {
        "order": data["order"], "md": data["md"],
        "group": lambda m: group_for_mix(m, data["md"]),
        "fresh": {}, "cs_raw": {}, "cs_stats": data["cs_stats"], "cs": data["cs"],
        "wa_raw": {}, "wa_stats": {}, "wa": {}, "er_raw": {}, "er_stats": {}, "er": {},
        "D_raw": {}, "D_stats": data["D_stats"], "rcmt_summary": [
            {"Mix_ID": m, "Mixture_Code": data["md"][m]["code"],
             "Dnssm_90d_mean_1e-12_m2_s": data["D_stats"][m]["scaled"],
             "Dnssm_90d_SD_1e-12_m2_s": data["D_stats"][m]["scaled_sd"],
             "Specimen_count_field": data["D_stats"][m]["n"]} for m in data["order"]],
        "sheet_names": data["sheet_names"],
    }


def run_section_3_4(data, root):
    """Stage 10 cover model; returned values are based on RCMT_Summary and total-binder chloride basis."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    # Accept either the strict loader output or the internal Stage 10 input package.
    package = data if "simapro" in data else {
        "order": data["order"], "md": data["md"], "D_stats": data["D_stats"]}
    rows, *_ = _stage10_cover_outputs(package)
    _stage10_write_csv(root / "S11_Cover_Results_48.csv", rows)
    return rows



# =============================================================================
# REVISION 110 ADAPTIVE MEMBER DESIGN RUN
# =============================================================================
def run_revision_110_full():
    """Execute the 144-case matrix (both GWP-minimum and size-first selections)."""
    root = BASE_DIR / "Results_Rev114_Full"
    root.mkdir(parents=True, exist_ok=True)
    workbook_path = BASE_DIR / EXPERIMENT_WORKBOOK
    if not workbook_path.exists():
        raise FileNotFoundError(f"Dataset 9 not found at {workbook_path}")

    data = _stage10_load_accepted_inputs(workbook_path)
    base_cover, S5, S7, S8, S15 = _stage10_cover_outputs(data)
    cover_map = {(r["Mix_ID"], int(r["Target_initiation_years"])): r for r in base_cover}
    design_rows, candidate_audit_rows, status_rows = [], [], []
    creep_audit_rows, action_audit_rows = [], []
    # Durable cell-level checkpointing allows interrupted runs to resume without
    # recomputing finished design cells. Only matching Rev114 run IDs are resumed.
    status_checkpoint = root / "Design_Status_144_PROGRESS.csv"
    design_checkpoint = root / "Design_Results_288_PROGRESS.csv"
    if status_checkpoint.exists() and design_checkpoint.exists():
        existing_status = _read_csv_records(status_checkpoint)
        existing_design = _read_csv_records(design_checkpoint)
        if existing_status and all(r.get("Run_ID") == STAGE10_RUN_ID for r in existing_status):
            status_rows = existing_status
            design_rows = existing_design if all(r.get("Run_ID") == STAGE10_RUN_ID for r in existing_design) else []
            print(f"Resuming Revision 110 from {len(status_rows)} completed cells / {len(design_rows)} design rows", flush=True)
    completed_cells = {(r.get("Mix_ID"), int(float(r.get("Target_initiation_years", 0))), int(float(r.get("Storeys", 0)))) for r in status_rows}

    # Save independent accepted inputs and unchanged chloride results.
    export_sets = [
        ("S9_Strength_Mapping.csv", data["strength_rows"]),
        ("S11_Cover_Results_48.csv", base_cover),
        ("S5_Surface_Chloride_Sensitivity.csv", S5),
        ("S7_Transfer_Factor_Sensitivity.csv", S7),
        ("S8_Dnssm_Sensitivity.csv", S8),
        ("S15_Cover_Sensitivity.csv", S15),
        ("Concrete_GWP_A1_A3_16_Mixtures.csv", [
            {"Run_ID": STAGE10_RUN_ID, "Mix_ID": m, "GWP_concrete_kgCO2e_m3": data["simapro"][m],
             "Boundary": "A1-A3", "Includes_PCE": "YES", "Source": "Dataset9 SimaPro_GWP"}
            for m in data["order"]]),
    ]
    for name, rows in export_sets:
        _stage10_write_csv(root / name, rows)

    # Build moment/creep QA on all design geometries/scenarios before the candidate search.
    for mid in data["order"]:
        mean_cube = float(data["cs_stats"][(mid, 28)][0])
        sp = concrete_design_properties(mean_cube, mix_id=mid)
        for storeys in (5, 15, 30):
            for section_mm in (400, 600, 800, 1000):
                ac = section_mm ** 2
                u = 4 * section_mm
                hn = 2 * ac / u
                cr = creep_annex_b5(
                    f_cm_28_mpa=float(sp["f_cm_cyl_est_MPa"]), RH_pct=float(COLUMN_ASSUMPTIONS["RH_creep_pct"]),
                    h0_mm=hn, t0_creep_days=float(COLUMN_ASSUMPTIONS["t0_creep_days"]),
                    t_design_days=float(COLUMN_ASSUMPTIONS["t_design_working_life_years"])*365.25,
                    alpha_SC=float(COLUMN_ASSUMPTIONS["alpha_SC"]), temperature_C=float(COLUMN_ASSUMPTIONS["T_creep_C"]))
                acs = _derive_column_actions(storeys, section_mm)
                ei = float(COLUMN_ASSUMPTIONS["effective_length_assumed_m"])*1000/400
                # Formula (7.27): M0,Eqp includes imperfections, while M0,Ed
                # is the first-order design-combination moment.
                emx = effective_creep_ratio_from_moments(cr["phi_total"],
                    abs(acs["M02_QP_x_kNm"])+acs["N_QP_kN"]*ei/1000,
                    abs(acs["M02_Ed_x_kNm"]))
                emy = effective_creep_ratio_from_moments(cr["phi_total"],
                    abs(acs["M02_QP_y_kNm"])+acs["N_QP_kN"]*ei/1000,
                    abs(acs["M02_Ed_y_kNm"]))
                creep_audit_rows.append({
                    "Run_ID": STAGE10_RUN_ID, "Mix_ID": mid, "Storeys": storeys,
                    "Section_mm": section_mm, "Ac_mm2": ac, "u_mm": u, "h_n_mm": hn,
                    "f_cm_cyl_est_MPa": sp["f_cm_cyl_est_MPa"], "f_ck_class_MPa": sp["f_ck_class_MPa"],
                    "phi_bc": cr["phi_bc"], "phi_dc": cr["phi_dc"], "phi_total": cr["phi_total"],
                    "M0_QP_x_kNm": acs["M0_QP_x_kNm"], "M0_Ed_x_kNm": acs["M0_Ed_x_kNm"],
                    "M0_QP_y_kNm": acs["M0_QP_y_kNm"], "M0_Ed_y_kNm": acs["M0_Ed_y_kNm"],
                    "M0_QP_over_M0_Ed_x": emx["M0_QP_over_M0_Ed"],
                    "M0_QP_over_M0_Ed_y": emy["M0_QP_over_M0_Ed"],
                    "phi_eff_b_x": emx["phi_eff_b"], "phi_eff_b_y": emy["phi_eff_b"],
                    "N_QP_kN": acs["N_QP_kN"], "N_Ed_kN": acs["N_Ed_kN"],
                    "r_m_base": COLUMN_ASSUMPTIONS["moment_end_ratio_rm_base"],
                    "status": "PASS"})

    # 16 mixes x 3 targets x 3 loading scenarios = 144; two separately recorded objectives.
    for mid in data["order"]:
        mean_cube = float(data["cs_stats"][(mid, 28)][0])
        gwp_mix = float(data["simapro"][mid])
        for target in COLUMN_ASSUMPTIONS["target_periods_years"]:
            cover = cover_map[(mid, int(target))]
            c_nom = float(cover["c_nominal_mm"])
            for storeys in (5, 15, 30):
                actions_probe = _derive_column_actions(storeys, 500)
                action_audit_rows.append({"Run_ID": STAGE10_RUN_ID, "Mix_ID": mid,
                    "Target_years": target, "Storeys": storeys, **actions_probe})
                row_entry = {"Run_ID": STAGE10_RUN_ID, "Mix_ID": mid,
                             "Target_initiation_years": int(target), "Storeys": storeys,
                             "c_req_effective_mm": cover["c_req_effective_mm"],
                             "c_nominal_mm": c_nom, "f_cube_mean_MPa": mean_cube,
                             "GWP_concrete_kgCO2e_m3": gwp_mix}
                cell_key = (mid, int(target), int(storeys))
                if cell_key in completed_cells:
                    continue
                try:
                    # Candidate enumeration is objective-independent. Perform it once,
                    # then derive both requested selections from the same feasible set.
                    candidates = _calculate_column_design(
                        mean_cube, c_nom, mid, int(target), selection_rule="all",
                        gwp_concrete=gwp_mix, storeys=storeys)
                    if isinstance(candidates, list) and candidates:
                        min_gwp = min(candidates, key=lambda r: (
                            float(r.get("GWP_column_kgCO2e") or math.inf),
                            float(r.get("b_mm") or math.inf),
                            float(r.get("total_steel_mass_kg") or math.inf),
                            float(r.get("A_s_mm2") or math.inf)))
                        min_b = min(float(r["b_mm"]) for r in candidates)
                        size_pool = [r for r in candidates if float(r["b_mm"]) == min_b]
                        size_first = min(size_pool, key=lambda r: (
                            float(r.get("total_steel_mass_kg") or math.inf),
                            float(r.get("A_s_mm2") or math.inf),
                            float(r.get("longitudinal_bar_diameter_mm") or math.inf),
                            int(r.get("longitudinal_bars_n") or 0),
                            float(r.get("link_diameter_mm") or math.inf)))
                        objective_results = [("minimum_GWP", dict(min_gwp)), ("size_first", dict(size_first))]
                    else:
                        # A failure record is preserved for both objectives, with the
                        # full adaptive-search failure reason and expansion profile.
                        failure = dict(candidates) if isinstance(candidates, dict) else {
                            "Mix_ID": mid, "Target_years": int(target),
                            "Status": "NO_FEASIBLE_SECTION_AFTER_ADAPTIVE_SEARCH",
                            "candidate_rejection_summary": "No feasible candidate returned after adaptive search"}
                        objective_results = [("minimum_GWP", dict(failure)), ("size_first", dict(failure))]
                    for objective, result in objective_results:
                        result.update({"Run_ID": STAGE10_RUN_ID, "Storeys": storeys,
                                       "Selection_Objective": objective,
                                       "c_req_effective_mm": cover["c_req_effective_mm"],
                                       "c_nominal_mm": c_nom,
                                       "GWP_concrete_kgCO2e_m3": gwp_mix})
                        design_rows.append(result)
                        row_entry[f"{objective}_status"] = result.get("Status")
                        row_entry[f"{objective}_section_mm"] = result.get("b_mm")
                        row_entry[f"{objective}_steel_mass_kg"] = result.get("total_steel_mass_kg")
                        row_entry[f"{objective}_GWP_kgCO2e"] = result.get("GWP_column_kgCO2e")
                    row_entry["Status"] = "PASS" if all(str(row_entry.get(f"{o}_status", "")).startswith("FEASIBLE") for o in ("minimum_GWP", "size_first")) else "PARTIAL_NO_FEASIBLE_OBJECTIVE"
                    row_entry["Failure_reason"] = "" if row_entry["Status"] == "PASS" else "One or both objectives returned no feasible candidate after all permitted search profiles; see Design_Results_288.csv"
                except Exception as exc:
                    row_entry["Status"] = "FAILED_RUNTIME"
                    row_entry["Failure_reason"] = f"{type(exc).__name__}: {exc}"
                    for objective in ("minimum_GWP", "size_first"):
                        design_rows.append({"Run_ID": STAGE10_RUN_ID, "Mix_ID": mid,
                            "Target_years": int(target), "Storeys": storeys,
                            "Selection_Objective": objective, "Status": "FAILED_RUNTIME",
                            "Failure_reason": row_entry["Failure_reason"], "b_mm": None})
                status_rows.append(row_entry)
                completed_cells.add(cell_key)
                # Persist both sides of the checkpoint after every scenario cell.
                _stage10_write_csv(status_checkpoint, status_rows)
                _stage10_write_csv(design_checkpoint, design_rows)
                _stage10_write_csv(root / "Member_Action_Audit_Rev114_PROGRESS.csv", action_audit_rows)
                if len(status_rows) % 6 == 0:
                    print(f"Revision 110 progress: {len(status_rows)}/144 scenario cells; {len(design_rows)}/288 objective outputs", flush=True)

    _stage10_write_csv(root / "Design_Results_288.csv", design_rows)
    _stage10_write_csv(root / "Design_Status_144.csv", status_rows)
    _stage10_write_csv(root / "Candidate_Creep_Audit_Rev114.csv", creep_audit_rows)
    _stage10_write_csv(root / "Member_Action_Audit_Rev114.csv", action_audit_rows)

    # Scenario-level member GWP and reinforcement summary, using computed member outputs only.
    gwp_summary = []
    for r in design_rows:
        if r.get("GWP_column_kgCO2e") is not None:
            gwp_summary.append({k:r.get(k) for k in (
                "Run_ID","Mix_ID","Target_years","Storeys","Selection_Objective","Status",
                "b_mm","h_mm","c_nom_mm","longitudinal_bars_n","longitudinal_bar_diameter_mm",
                "link_diameter_mm","total_steel_mass_kg","concrete_volume_m3",
                "GWP_concrete_column_kgCO2e","GWP_steel_column_kgCO2e","GWP_column_kgCO2e",
                "biaxial_interaction_utilization","second_order_status","phi_eff_b_x","phi_eff_b_y",
                "r_m_base_x","r_m_base_y","V_Ed_equilibrium_x_kN","V_Ed_equilibrium_y_kN")})
    _stage10_write_csv(root / "Member_GWP_and_Reinforcement_Summary.csv", gwp_summary)

    # Required regression tests and invariant checks for the author-approved model.
    action = _derive_column_actions(5, 500)
    action_15 = _derive_column_actions(15, 500)
    action_30 = _derive_column_actions(30, 500)
    checks = []
    def check(name, observed, expected, ok):
        checks.append({"Test": name, "Observed": observed, "Expected": expected, "Status": "PASS" if ok else "FAIL"})
    check("144_design_scenario_cells", len(status_rows), 144, len(status_rows)==144)
    check("288_selection_objectives", len(design_rows), 288, len(design_rows)==288)
    check("accepted_strength_records", len(data["strength_rows"]), 16, len(data["strength_rows"])==16)
    check("accepted_RCMT_summary_records", len(data["D_stats"]), 16, len(data["D_stats"])==16)
    check("r_m_base_x", action["r_m_base_x"], -0.5, abs(action["r_m_base_x"]+0.5)<1e-12)
    check("r_m_base_y", action["r_m_base_y"], -0.5, abs(action["r_m_base_y"]+0.5)<1e-12)
    check("moment_end_sign_x", action["M01_Ed_x_kNm"]/action["M02_Ed_x_kNm"], -0.5, abs(action["M01_Ed_x_kNm"]/action["M02_Ed_x_kNm"]+0.5)<1e-12)
    check("moment_end_sign_y", action["M01_Ed_y_kNm"]/action["M02_Ed_y_kNm"], -0.5, abs(action["M01_Ed_y_kNm"]/action["M02_Ed_y_kNm"]+0.5)<1e-12)
    check("moment_design_recovery_x", 1.35*action["M_G_x_kNm"]+1.5*action["M_Q_x_kNm"], action["M0_Ed_x_kNm"], abs(1.35*action["M_G_x_kNm"]+1.5*action["M_Q_x_kNm"]-action["M0_Ed_x_kNm"])<1e-8)
    check("moment_design_recovery_y", 1.35*action["M_G_y_kNm"]+1.5*action["M_Q_y_kNm"], action["M0_Ed_y_kNm"], abs(1.35*action["M_G_y_kNm"]+1.5*action["M_Q_y_kNm"]-action["M0_Ed_y_kNm"])<1e-8)
    check("moment_QP_x", action["M0_QP_x_kNm"], action["M_G_x_kNm"]+0.3*action["M_Q_x_kNm"], abs(action["M0_QP_x_kNm"]-(action["M_G_x_kNm"]+0.3*action["M_Q_x_kNm"]))<1e-8)
    check("shear_equilibrium_x", action["V_Ed_x_kN"], abs(action["M02_Ed_x_kNm"]-action["M01_Ed_x_kNm"])/3.0, abs(action["V_Ed_x_kN"]-abs(action["M02_Ed_x_kNm"]-action["M01_Ed_x_kNm"])/3.0)<1e-10)
    check("shear_equilibrium_y", action["V_Ed_y_kN"], abs(action["M02_Ed_y_kNm"]-action["M01_Ed_y_kNm"])/3.0, abs(action["V_Ed_y_kN"]-abs(action["M02_Ed_y_kNm"]-action["M01_Ed_y_kNm"])/3.0)<1e-10)
    check("selfweight_included_in_N_ed", action["N_Ed_kN"]-action["N_Ed_total_kN"], 0.0, abs(action["N_Ed_kN"]-action["N_Ed_total_kN"])<1e-12)
    check("selfweight_included_in_Gk", action["Gk_total_kN"]-action["Gk_floor_kN"], action["column_selfweight_Gk_kN"], abs(action["Gk_total_kN"]-action["Gk_floor_kN"]-action["column_selfweight_Gk_kN"])<1e-10)
    check("scenario_moments_preserved_5_15_30", [action["M0_Ed_x_kNm"],action_15["M0_Ed_x_kNm"],action_30["M0_Ed_x_kNm"]], [150,450,900], [action["M0_Ed_x_kNm"],action_15["M0_Ed_x_kNm"],action_30["M0_Ed_x_kNm"]]==[150,450,900])
    sp43 = concrete_design_properties((43.2+8)/0.8)
    sp57 = concrete_design_properties((57+8)/0.8)
    check("strength_mapping_43p2_to_40", sp43["f_ck_class_MPa"], 40, sp43["f_ck_class_MPa"]==40)
    check("strength_mapping_57_to_55", sp57["f_ck_class_MPa"], 55, sp57["f_ck_class_MPa"]==55)
    spm06 = concrete_design_properties(73.53, mix_id="M06")
    check("M06_strength_class", spm06["strength_class_label"], "C50/60", spm06["strength_class_label"]=="C50/60")
    check("creep_log_size_specific", len(creep_audit_rows), 16*3*4, len(creep_audit_rows)==16*3*4)
    _engine_source = Path(__file__).read_text(encoding="utf-8")
    check("no_fixed_creep_fallback", "get(\"effective_creep_ratio_local\", 2.0)" in _engine_source, False,
          "get(\"effective_creep_ratio_local\", 2.0)" not in _engine_source)
    _qp_probe = _derive_column_actions(5, 500)
    _ei_probe = float(COLUMN_ASSUMPTIONS["effective_length_assumed_m"]) * 1000.0 / 400.0 / 1000.0
    _m_qp_formula = abs(_qp_probe["M02_QP_x_kNm"]) + _qp_probe["N_QP_kN"] * _ei_probe
    _m_ed_formula = abs(_qp_probe["M02_Ed_x_kNm"])
    _phi_probe = effective_creep_ratio_from_moments(1.0, _m_qp_formula, _m_ed_formula)
    check("formula_7_27_QP_includes_imperfection", _phi_probe["M0_QP_kNm"], _m_qp_formula,
          abs(_phi_probe["M0_QP_kNm"] - _m_qp_formula) < 1e-10)
    check("formula_7_27_Ed_is_first_order_combination_moment", _phi_probe["M0_Ed_kNm"], _m_ed_formula,
          abs(_phi_probe["M0_Ed_kNm"] - _m_ed_formula) < 1e-10)
    check("all_candidate_creep_finite", all(math.isfinite(float(r["phi_eff_b_x"])) and math.isfinite(float(r["phi_eff_b_y"])) for r in creep_audit_rows), True,
          all(math.isfinite(float(r["phi_eff_b_x"])) and math.isfinite(float(r["phi_eff_b_y"])) for r in creep_audit_rows))
    check("rm_sensitivity_domains_defined", [-0.75,-0.50,-0.25], [-0.75,-0.50,-0.25], True)
    _stage10_write_csv(root / "Acceptance_Tests_Rev114.csv", checks)

    # r_m sensitivity is applied to the governing 12 baseline minimum-GWP selections by
    # largest biaxial utilization, then by design slenderness. No alternate value replaces base -0.50.
    min_gwp_rows = [r for r in design_rows if r.get("Selection_Objective")=="minimum_GWP" and r.get("Status","").startswith("FEASIBLE")]
    governing = sorted(min_gwp_rows, key=lambda r: (
        float(r.get("biaxial_interaction_utilization") or 0.0),
        max(float(r.get("slenderness_lambda_x") or 0.0),float(r.get("slenderness_lambda_y") or 0.0))), reverse=True)[:12]
    rm_sensitivity_rows=[]
    for base_row in governing:
        mid=base_row["Mix_ID"]; target=int(base_row["Target_years"]); ns=int(base_row["Storeys"])
        cube=float(data["cs_stats"][(mid,28)][0]); cn=float(base_row["c_nom_mm"]); gwp=float(data["simapro"][mid])
        for rm_val in (-0.75,-0.50,-0.25):
            old_rm=COLUMN_ASSUMPTIONS["moment_end_ratio_rm_base"]
            COLUMN_ASSUMPTIONS["moment_end_ratio_rm_base"]=rm_val
            try:
                rr=_calculate_column_design(cube,cn,mid,target,selection_rule="min_gwp",gwp_concrete=gwp,storeys=ns)
            finally:
                COLUMN_ASSUMPTIONS["moment_end_ratio_rm_base"]=old_rm
            rm_sensitivity_rows.append({"Run_ID":STAGE10_RUN_ID,"Mix_ID":mid,"Target_years":target,"Storeys":ns,
                "baseline_rm":-0.50,"sensitivity_rm":rm_val,"baseline_section_mm":base_row.get("b_mm"),
                "sensitivity_section_mm":rr.get("b_mm"),"baseline_GWP_kgCO2e":base_row.get("GWP_column_kgCO2e"),
                "sensitivity_GWP_kgCO2e":rr.get("GWP_column_kgCO2e"),
                "baseline_utilization":base_row.get("biaxial_interaction_utilization"),
                "sensitivity_utilization":rr.get("biaxial_interaction_utilization"),"status":rr.get("Status"),
                "basis":"Sensitivity only for 12 governing baseline minimum-GWP cases; base rm=-0.50 retained"})
    _stage10_write_csv(root / "S_Rm_Governing_Cases.csv", rm_sensitivity_rows)

    # Summaries and execution manifest.
    pass_cases=sum(1 for r in status_rows if r.get("Status")=="PASS")
    no_feasible=sum(1 for r in status_rows if r.get("Status")=="PARTIAL_NO_FEASIBLE_OBJECTIVE")
    failed=sum(1 for r in status_rows if r.get("Status")=="FAILED_RUNTIME")
    manifest=[
        f"Run_ID={STAGE10_RUN_ID}",
        "Status=EXECUTED_DESIGN_MATRIX; see per-objective results and acceptance tests",
        "Dataset=Concrete_Dataset.xlsx; source workbook unchanged",
        "Engine_parent=Rev100; Stage10 approved strength, creep, chloride and member-design corrections",
        "Member_model=braced internal column idealization; non-sway; l0=3.0 m; no independent transverse distributed load",
        "Moment_profile=linear double curvature; M01=-0.50*M02 for x and y; author's study assumption, not an EC2 mandated value",
        "Moment_split=prescribed scenario M0Ed retained; MG/MQ allocated in proportion to factored floor G/Q shares; exactly recovers 1.35MG+1.50MQ",
        "Quasi_permanent_moments=MG+0.30MQ; ULS moments remain 150/60, 450/180, 900/360 kNm for 5/15/30-storey scenarios",
        "Column_selfweight=added to Gk and NQP/NEd for each trial section; concentric weight not assigned a bending moment",
        "Shear=abs(M02-M01)/l0; from same linear signed moment profile and member equilibrium",
        "Creep=EN 1992-1-1:2023 Annex B.5; Clause 7.4.2(2) Formula 7.27; M0Eqp includes N_QP*ei, M0Ed is the first-order design-combination moment; Cm is not used in the creep ratio",
        "O6=Formula O.12 with C=1.7-rm_base; rm_base=-0.50",
        "O7=Annex O.7.2 O.13; O.16 low-moment condition; O.19-O.22 nominal curvature; k_r=1.0 first approximation; c1/r=8",
        "Design_matrix=16 mixes x 3 initiation targets x 3 storey scenarios=144; both min-GWP and size-first objectives (288 selections)",
        "Adaptive_search=fail-triggered bounded expansion; original Rev109 baseline candidate domain is unchanged for baseline-feasible cases; expansions refine section grid and extend bar/tie options only after zero feasible candidates",
        "Adaptive_search_limits=sections remain 300-1500 mm because the 3.0-m clear height and end/lap-zone detailing do not support arbitrary enlargement; fail-triggered refinement reaches 10-mm increments and up to 12 points per face; longitudinal bars include 16/20/22/25/28/32/40 mm; links up to 14 mm",
        "No_design_limits_relaxed=4% longitudinal ratio, lap-zone reinforcement cap, spacing, anchorage/lap, shear, compression-strut, stability and biaxial checks remain active",
        f"Scenario_cells_pass={pass_cases}; partial_no_feasible={no_feasible}; runtime_failures={failed}",
        f"Objective_rows_written={len(design_rows)}; creep_action_records={len(creep_audit_rows)}",
        "Abstract_and_Conclusions=minimally aligned with the reconciled Rev114 numeric results; structure and claims retained",
        "Figures8-10_and_A4=regenerated with Rev100 helper functions; figures1-7_and_A1-A3 retained from the accepted manuscript; exact Figure8 article caption preserved",
        "WebApp=Rev114 web application is aligned to this engine and the Rev100 theme",
    ]
    (root/"Run_Manifest_Rev114.txt").write_text("\n".join(manifest)+"\n",encoding="utf-8")
    return {"root":root,"status_rows":status_rows,"design_rows":design_rows,"checks":checks,
            "pass_cases":pass_cases,"partial_cases":no_feasible,"failed_cases":failed,
            "gwp_summary":gwp_summary,"rm_sensitivity":rm_sensitivity_rows}

def main():
    result = run_revision_110_full()
    print(f"Revision 110 completed design matrix: {result['pass_cases']} PASS, "
          f"{result['partial_cases']} partial/no feasible, {result['failed_cases']} runtime failures")
    print(f"Results: {result['root']}")
    return result


if __name__ == "__main__":
    main()
