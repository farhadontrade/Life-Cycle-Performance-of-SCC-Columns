#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Interactive SCC reinforced-concrete column design and A1-A3 GWP application.

Run:
    streamlit run SCC_Column_Design_WebApp_Rev99.py
"""
from __future__ import annotations

import copy
import csv
import importlib.util
import io
import hashlib
import json
import math
import sys
import threading
import zipfile
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, Patch, Rectangle
import numpy as np
import streamlit as st

# Must be the first Streamlit command in this script.
st.set_page_config(page_title="Column Design & GWP Assessment", page_icon="🏗️", layout="wide", initial_sidebar_state="collapsed")

APP_DIR = Path(__file__).resolve().parent

APP_VERSION = "Rev99"
REQUIRED_ENGINE_VERSION = "Rev99"
APP_PALETTE = {
    "navy": "#24415E", "ink": "#263445", "grid": "#DCE3EB",
    "blue": "#3568A8", "red": "#C44E52", "green": "#3A8D68",
    "muted": "#64748B", "light": "#F3F6FA", "white": "#FFFFFF",
}
BINDER_GROUP_COLORS = {
    "Reference": "#555555", "GGBFS-only": "#3568A8",
    "SF-only": "#C44E52", "Ternary": "#3A8D68",
}

# One display-only formatter keeps formula notation consistent across widget labels,
# captions, messages, metric labels, and dataframe headers without changing engine keys.
_DISPLAY_FORMULAE = {
    "N_Ed": "Nₑd", "N_Rd": "Nᵣd", "M_Ed,x": "Mₑd,x", "M_Ed,y": "Mₑd,y",
    "M_Rd,x": "Mᵣd,x", "M_Rd,y": "Mᵣd,y", "VEd,x/y": "Vₑd,x/y",
    "lbd": "lᵦd", "lsd": "lₛd", "tauEd": "τₑd",
    "c_nom": "cₙₒₘ", "c_req": "cᵣₑq", "c_min": "cₘᵢₙ",
    "D_nssm,90": "Dₙₛₛₘ,90", "Dnssm,90": "Dₙₛₛₘ,90",
    "s_max": "sₘₐₓ", "smax": "sₘₐₓ", "s_adopt": "sₐdₒₚₜ", "s_actual": "sₐctᵤₐₗ",
    "f_cube,mean": "f꜀ᵤᵦₑ,mean", "f_cube,28": "f꜀ᵤᵦₑ,28", "f_cube": "f꜀ᵤᵦₑ",
    "f_ck": "f꜀ₖ", "f_yk": "fᵧₖ", "fyk": "fᵧₖ", "GWP_column": "GWP₍column₎",
    "γ_G": "γ₍G₎", "γ_Q": "γ₍Q₎", "γ_s": "γ₍s₎", "γ_c": "γ₍c₎", "γ_v": "γ₍v₎",
    "C_crit": "C₍crit₎", "C_s": "Cₛ", "k_t": "kₜ", "f_tdy": "fₜdy",
    "φ_eff": "φₑff", "k_tc": "kₜ꜀", "k_r": "kᵣ", "ρ_l": "ρₗ",
    "l_0": "l₀", "l_bd": "lᵦd", "l_sd": "lₛd", "E_F,s": "E₍F,s₎",
    "NEd/NRd": "Nₑd/Nᵣd", "MEd,x/MRd,x": "Mₑd,x/Mᵣd,x", "MEd,y/MRd,y": "Mₑd,y/Mᵣd,y",
    "10^-12": "10⁻¹²", "10^-1": "10⁻¹",
}

def _format_formula_text(value):
    if not isinstance(value, str):
        return value
    for source, target in sorted(_DISPLAY_FORMULAE.items(), key=lambda item: len(item[0]), reverse=True):
        value = value.replace(source, target)
    return value

def _format_table_payload(payload):
    if isinstance(payload, list):
        return [_format_table_payload(item) for item in payload]
    if isinstance(payload, tuple):
        return tuple(_format_table_payload(item) for item in payload)
    if isinstance(payload, dict):
        return {_format_formula_text(key) if isinstance(key, str) else key: _format_table_payload(value)
                for key, value in payload.items()}
    # Support pandas-like frames without requiring pandas in the UI layer.
    if hasattr(payload, "copy") and hasattr(payload, "columns") and hasattr(payload, "rename"):
        frame = payload.copy()
        frame.columns = [_format_formula_text(str(column)) for column in frame.columns]
        try:
            for column in frame.select_dtypes(include=["object"]).columns:
                frame[column] = frame[column].map(_format_formula_text)
        except Exception:
            pass
        return frame
    return _format_formula_text(payload)

PERSISTENT_WIDGET_PREFIX = "scc_input__"
PERSISTENT_INPUT_METHODS = {"number_input", "selectbox", "multiselect", "radio", "text_input", "checkbox"}


def _serializable_widget_value(value):
    """Convert Streamlit widget values to plain JSON-compatible values."""
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, tuple):
        return [_serializable_widget_value(v) for v in value]
    if isinstance(value, list):
        return [_serializable_widget_value(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _stable_input_key(method_name: str, label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    slug = slug[:44] or "input"
    digest = hashlib.sha1(f"{method_name}|{label}".encode("utf-8")).hexdigest()[:7]
    return f"{PERSISTENT_WIDGET_PREFIX}{method_name}__{slug}_{digest}"


def _wrap_display_method(method, *, table=False, first_label=False, widget_name=None):
    def wrapped(*args, **kwargs):
        args = list(args)
        persistent_key = None
        original_label = None
        if first_label and args and isinstance(args[0], str):
            original_label = args[0]
        elif first_label and isinstance(kwargs.get("label"), str):
            original_label = kwargs["label"]

        if widget_name in PERSISTENT_INPUT_METHODS and original_label:
            supplied_key = kwargs.get("key")
            if supplied_key:
                persistent_key = (supplied_key if str(supplied_key).startswith(PERSISTENT_WIDGET_PREFIX)
                                  else PERSISTENT_WIDGET_PREFIX + str(supplied_key))
            else:
                persistent_key = _stable_input_key(widget_name, original_label)
            kwargs["key"] = persistent_key

            # Capture the current default and label so the input summary can
            # distinguish study defaults from values edited by the user.
            try:
                if widget_name in ("selectbox", "radio"):
                    options = args[1] if len(args) > 1 else kwargs.get("options", [])
                    index = kwargs.get("index", 0)
                    default_value = options[index] if options and 0 <= int(index) < len(options) else None
                elif widget_name == "multiselect":
                    default_value = kwargs.get("default", [])
                elif widget_name in ("number_input", "text_input"):
                    default_value = kwargs.get("value", None)
                elif widget_name == "checkbox":
                    default_value = kwargs.get("value", False)
                else:
                    default_value = None
                defaults = dict(st.session_state.get("_scc_input_default_values", {}))
                defaults[persistent_key] = _serializable_widget_value(default_value)
                st.session_state["_scc_input_default_values"] = defaults
                labels = dict(st.session_state.get("_scc_input_widget_labels", {}))
                labels[persistent_key] = {"method": widget_name, "label": original_label}
                st.session_state["_scc_input_widget_labels"] = labels
            except Exception:
                pass

            # Restore a value from the saved preset cache before the widget is
            # instantiated. This also restores values from currently hidden branches.
            try:
                cache = st.session_state.get("_scc_input_preset_values", {})
                if persistent_key not in st.session_state and persistent_key in cache:
                    st.session_state[persistent_key] = cache[persistent_key]
            except Exception:
                pass

        if table and args:
            args[0] = _format_table_payload(args[0])
        elif first_label and args and isinstance(args[0], str):
            args[0] = _format_formula_text(args[0])
        elif not table and not first_label:
            args = [_format_formula_text(value) if isinstance(value, str) else value for value in args]
        if "label" in kwargs and isinstance(kwargs["label"], str):
            kwargs["label"] = _format_formula_text(kwargs["label"])

        result = method(*args, **kwargs)
        if persistent_key:
            try:
                cache = dict(st.session_state.get("_scc_input_preset_values", {}))
                current_value = st.session_state.get(persistent_key, result)
                cache[persistent_key] = _serializable_widget_value(current_value)
                st.session_state["_scc_input_preset_values"] = cache
            except Exception:
                pass
        return result
    return wrapped

# Apply at the UI boundary only; calculation keys and engine data remain unchanged.
for _method_name in ("markdown", "caption", "write", "info", "success", "warning", "error", "exception"):
    if hasattr(st, _method_name):
        setattr(st, _method_name, _wrap_display_method(getattr(st, _method_name)))
for _method_name in ("metric", "number_input", "selectbox", "multiselect", "radio", "text_input", "checkbox"):
    if hasattr(st, _method_name):
        setattr(st, _method_name, _wrap_display_method(
            getattr(st, _method_name), first_label=True, widget_name=_method_name
        ))
if hasattr(st, "dataframe"):
    st.dataframe = _wrap_display_method(st.dataframe, table=True)

if "_scc_input_preset_values" not in st.session_state:
    st.session_state["_scc_input_preset_values"] = {}
if "_scc_input_default_values" not in st.session_state:
    st.session_state["_scc_input_default_values"] = {}
if "_scc_input_widget_labels" not in st.session_state:
    st.session_state["_scc_input_widget_labels"] = {}


def _same_json_value(a: Any, b: Any) -> bool:
    try:
        return json.dumps(_serializable_widget_value(a), sort_keys=True) == json.dumps(_serializable_widget_value(b), sort_keys=True)
    except Exception:
        return a == b


def _origin_for_widget_labels(labels: Sequence[str]) -> str:
    label_map = st.session_state.get("_scc_input_widget_labels", {})
    defaults = st.session_state.get("_scc_input_default_values", {})
    matched = [key for key, meta in label_map.items() if meta.get("label") in set(labels)]
    if not matched:
        return "User input / study default"
    for key in matched:
        current = st.session_state.get(key, defaults.get(key))
        if not _same_json_value(current, defaults.get(key)):
            return "User-edited"
    return "Study default"


def _input_summary_table_rows(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    """Build an auditable summary with value provenance, not only raw values."""
    rows = []
    study_mixture = payload.get("mixture_source") == "Study mixtures"
    for label, value in _input_summary_pairs(payload):
        origin = "Study default"
        if label == "Mixture":
            origin = "Study mixture" if study_mixture else "User-defined mixture"
        elif label == "Mean 28-day cube strength":
            if study_mixture:
                source_mix = MIX_DB.get(str(payload.get("mixture_id")), {})
                source_strength = source_mix.get("f_cube_28_MPa")
                origin = ("Study mixture value" if source_strength is not None and
                          abs(_f(payload.get("f_cube_28")) - _f(source_strength)) < 1e-9 else "User-edited")
            else:
                origin = _origin_for_widget_labels(["Mean 28-day cube strength f꜀ᵤᵦₑ,mean (MPa)"])
        elif label == "Concrete GWP intensity":
            origin = "Study-reported A1–A3 result" if study_mixture else "Calculated from constituent factors"
        elif label == "Mixture proportions":
            origin = "Study mixture proportions" if study_mixture and _origin_for_widget_labels(list(COMPONENT_KEYS.keys())) == "Study default" else _origin_for_widget_labels(list(COMPONENT_KEYS.keys()))
        elif label == "Component emission factors":
            origin = _origin_for_widget_labels([f"{name} EF (kg CO₂-eq/kg)" for name in COMPONENT_KEYS])
        elif label == "Axial-load definition":
            origin = "User-entered axial force" if payload.get("load_mode") == "Manual N_Ed" else "Nₑd derived from entered floor loads and column self-weight"
        elif label == "Migration coefficient":
            origin = "Study test result" if study_mixture else "User-entered measurement"
        elif label == "Required effective cover":
            origin = "Calculated by the application"
        elif label == "Nominal cover":
            origin = "Calculated by the application" if payload.get("cover_mode") == "Estimate from chloride migration" else "User-entered value"
        elif label == "Section definition":
            origin = _origin_for_widget_labels(["Section definition"])
        elif label == "Section search":
            origin = _origin_for_widget_labels(["Minimum section width (mm)", "Maximum section width (mm)", "Section increment (mm)", "Fixed square-section width (mm)"])
        elif label == "Design objective":
            origin = _origin_for_widget_labels(["Design objective"])
        elif label == "Longitudinal-bar candidates":
            origin = _origin_for_widget_labels(["Longitudinal-bar diameter candidates Øₗ (mm)", "Additional exploratory longitudinal-bar diameters (mm; comma-separated)"])
        elif label == "Link candidates":
            origin = _origin_for_widget_labels(["Link diameter candidates Øᵥ (mm)", "Additional exploratory link diameters (mm; comma-separated)"])
        elif label == "Steel emission factor":
            origin = _origin_for_widget_labels(["Steel emission factor (E₍F,s₎)", "Custom steel EF (kg CO₂-eq/kg)"])
        elif label == "Advanced design parameters":
            origin = _origin_for_widget_labels([
                "Maximum aggregate size (mm)", "Maximum longitudinal ratio outside laps (%)",
                "Steel partial factor γ₍s₎", "Concrete partial factor γ₍c₎",
                "Reinforcement yield strength fᵧₖ (MPa)", "Concrete stress-block factor kₜ꜀",
                "Shear factor γv", "Shear-field cot θ", "Minimum confinement factor k",
                "Effective creep ratio φ_eff",
            ])
        else:
            label_widgets = {
                "Axial-load mode": ["Axial-load input"],
                "Story count": ["Number of stories n"],
                "Geometry": ["Clear height h_s (m)", "Effective length l₀ (m)"],
                "Concrete unit weight": ["Concrete unit weight (kN/m³)"],
                "First-order moments": ["First-order moment Mₑd,x (kN·m)", "First-order moment Mₑd,y (kN·m)"],
                "ULS load factors": ["ULS factor γ₍G₎", "ULS factor γ₍Q₎"],
                "Cover mode / basis": ["Cover input", "Minimum durability-cover basis"],
                "Initiation target": ["Corrosion-initiation target (years)"],
                "Longitudinal bar-count constraint": ["Longitudinal bar count"],
                "Aging exponent / chloride thresholds / transfer factor": ["Aging exponent α", "Critical chloride concentration C₍crit₎ (wt.% of cement mass)", "Surface chloride concentration Cₛ (wt.% of cement mass)", "Transfer factor kₜ"],
            }
            if label in label_widgets:
                origin = _origin_for_widget_labels(label_widgets[label])
        rows.append({"Input": label, "Current value": value, "Origin": origin})
    return rows


_INPUT_GROUPS = {
    "Materials": ("mixture_source", "mixture_id", "mixture_recipe", "f_cube_28", "concrete_gwp", "component_gwp", "component_efs"),
    "Structural actions and geometry": ("load_mode", "storeys", "clear_height_m", "effective_length_m", "tributary_area", "dead_load", "live_load", "manual_n_ed", "gamma_G", "gamma_Q", "mx_kNm", "my_kNm", "unit_weight"),
    "Durability and cover": ("cover_mode", "cover_basis", "minimum_cover_override", "target_years", "dnssm", "alpha", "ccrit", "cs", "kt", "execution_allowance", "cover_nominal", "cover_required_effective"),
    "Section and reinforcement search": ("section_mode", "fixed_section", "min_section", "max_section", "section_step", "objective", "objective_label", "allowed_long", "allowed_links", "required_bar_count", "max_aggregate", "max_reinf_ratio", "gamma_s", "gamma_c", "fyk", "gamma_v", "k_tc", "cot_theta_shear", "minimum_confinement_factor_k", "effective_creep_ratio_local"),
}


def _changed_input_groups(previous: Optional[Dict[str, Any]], current: Dict[str, Any]) -> List[str]:
    if not isinstance(previous, dict):
        return []
    changed = []
    for group, keys in _INPUT_GROUPS.items():
        if any(previous.get(key) != current.get(key) for key in keys):
            changed.append(group)
    return changed


def _benchmark_signature(payload: Dict[str, Any]) -> str:
    """Signature of inputs shared by all study mixtures; excludes active-mix-specific values."""
    common = dict(payload)
    for key in ("mixture_id", "mixture_recipe", "f_cube_28", "concrete_gwp", "component_gwp", "dnssm"):
        common.pop(key, None)
    if common.get("mixture_source") == "Study mixtures":
        common.pop("component_efs", None)
    if common.get("cover_mode") == "Estimate from chloride migration":
        # Each study mixture has its own measured migration coefficient and therefore
        # its own derived effective/nominal cover. These are outcomes of the common
        # durability assumptions, not changes to the comparison scenario.
        common.pop("cover_nominal", None)
        common.pop("cover_required_effective", None)
    return hashlib.sha256(json.dumps(common, sort_keys=True, default=str).encode("utf-8")).hexdigest()

APP_CSS = """<style>
/* ======================================================================
   SHARED DESIGN SYSTEM
   The three tabs retain distinct hero colours. Cards, section headers,
   labels, inputs, choice controls, metrics and tables share one treatment.
   ====================================================================== */
:root {
  --navy:#203B56;
  --ink:#263445;
  --muted:#586B7D;
  --grid:#D8E1EA;
  --surface:#FFFFFF;
  --surface-soft:#F3F6F9;
  --blue:#3568A8;
  --green:#347D60;
  --orange:#B96A2B;
  --red:#B94D52;
  --focus:#6A9BCB;
}

.stApp { background:#F5F7FA; color:var(--ink); }
/* Hide Streamlit top header and toolbar */
[data-testid="stHeader"],
[data-testid="stToolbar"],
[data-testid="stDecoration"] {
    display: none !important;
}
.block-container { max-width:1540px; padding:1.05rem 1.45rem 3rem; }

/* Navigation: tab identity is expressed only by the active-tab accent. */
.stTabs [data-baseweb="tab-list"] {
  gap:.35rem; background:#E9EEF3; border:1px solid #D7E0E8;
  border-radius:12px; padding:.32rem; margin-bottom:.9rem;
}
.stTabs [data-baseweb="tab"] {
  min-height:2.7rem; padding:0 1rem; border-radius:8px;
  color:#344B61!important; opacity:1!important; font-weight:700;
  border:1px solid transparent;
}
.stTabs [data-baseweb="tab"] p, .stTabs [data-baseweb="tab"] span {
  color:#344B61!important; opacity:1!important;
}
.stTabs [data-baseweb="tab"][aria-selected="true"] {
  background:#FFFFFF!important; color:var(--navy)!important;
  border-color:#C8D5E1!important; box-shadow:0 2px 7px rgba(32,59,86,.08);
}
.stTabs [data-baseweb="tab"]:nth-child(1)[aria-selected="true"] { border-bottom:3px solid var(--blue)!important; }
.stTabs [data-baseweb="tab"]:nth-child(2)[aria-selected="true"] { border-bottom:3px solid var(--green)!important; }
.stTabs [data-baseweb="tab"]:nth-child(3)[aria-selected="true"] { border-bottom:3px solid var(--orange)!important; }
.stTabs [data-baseweb="tab-highlight"] { display:none; }

/* Tab heroes: separate identities, shared typography and geometry. */
.tab-hero {
  position:relative; overflow:hidden; color:#FFFFFF; padding:1.35rem 1.65rem 1.3rem;
  margin:.1rem 0 1rem; border:1px solid rgba(255,255,255,.16);
  border-radius:15px; box-shadow:0 5px 16px rgba(32,59,86,.09);
}
.tab-hero.overview { background:linear-gradient(110deg,#203B56 0%,#2D5B82 100%); }
.tab-hero.design { background:linear-gradient(110deg,#245B4A 0%,#347D60 100%); }
.tab-hero.guide { background:linear-gradient(110deg,#88502B 0%,#B96A2B 100%); }
.tab-hero .eyebrow {
  color:#EDF3F8; font-size:.69rem; font-weight:750; letter-spacing:.13em;
  text-transform:uppercase; margin-bottom:.42rem;
}
.tab-hero h1 { color:#FFFFFF!important; font-size:1.9rem; line-height:1.2; margin:.15rem 0 .4rem; }
.tab-hero p { color:#F4F7FA; font-size:.94rem; line-height:1.5; margin:0; max-width:1080px; }
.tab-hero .hero-tags { display:flex; flex-wrap:wrap; gap:.45rem; margin-top:.85rem; }
.tab-hero .hero-tags span {
  color:#FFFFFF; background:rgba(255,255,255,.12); border:1px solid rgba(255,255,255,.26);
  border-radius:999px; padding:.23rem .64rem; font-size:.73rem; font-weight:650;
}

/* All bordered Streamlit containers are equivalent cards. */
[data-testid="stVerticalBlockBorderWrapper"] {
  background:var(--surface)!important; border:1px solid var(--grid)!important;
  border-radius:12px!important; padding:.95rem 1rem .85rem!important;
  box-shadow:0 2px 8px rgba(32,59,86,.035)!important; margin-bottom:.68rem;
}
[data-testid="stVerticalBlockBorderWrapper"] > div { gap:.62rem; }

/* One section-header style across every tab and every section. */
.section-ribbon, .section-ribbon.blue, .section-ribbon.red,
.section-ribbon.green, .section-ribbon.orange {
  display:flex; align-items:center; gap:.65rem; min-height:2.8rem;
  padding:.58rem .76rem; margin:.04rem 0 .78rem;
  color:var(--navy); background:#F1F5F8!important;
  border:1px solid #DCE4EB!important; border-left:4px solid var(--blue)!important;
  border-radius:9px; font-size:1rem; font-weight:780; line-height:1.3;
}
.section-ribbon .step, .section-ribbon.blue .step, .section-ribbon.red .step,
.section-ribbon.green .step, .section-ribbon.orange .step {
  display:inline-flex; align-items:center; justify-content:center;
  min-width:2.1rem; padding:.22rem .42rem; border-radius:6px;
  color:#FFFFFF!important; background:var(--navy)!important;
  font-size:.70rem; font-weight:750; letter-spacing:.03em;
}
.panel-heading, .panel-heading.blue, .panel-heading.green, .panel-heading.orange {
  display:flex; align-items:center; min-height:2.55rem;
  padding:.48rem .72rem; margin:.02rem 0 .58rem;
  color:var(--navy)!important; background:#F1F5F8;
  border:1px solid #DCE4EB; border-left:4px solid var(--blue);
  border-radius:8px; font-size:.98rem; font-weight:780; line-height:1.35;
}
.guide-step-head {
  display:flex; align-items:center; gap:.58rem; margin:.04rem 0 .62rem;
  color:var(--navy); font-size:1.02rem; font-weight:780; line-height:1.35;
}
.guide-step-head .number {
  display:inline-flex; align-items:center; justify-content:center;
  flex:0 0 2.1rem; width:2.1rem; height:2.1rem; border-radius:7px;
  color:#FFFFFF; background:var(--navy); font-size:.73rem; font-weight:750;
}

/* Labels: every widget label uses the same size, weight, colour and spacing. */
div[data-testid="stWidgetLabel"] { margin-bottom:.30rem!important; }
div[data-testid="stWidgetLabel"] p,
div[data-testid="stWidgetLabel"] label,
[data-testid="stSelectbox"] label,
[data-testid="stNumberInput"] label,
[data-testid="stTextInput"] label,
[data-testid="stMultiSelect"] label,
[data-testid="stRadio"] > label,
[data-testid="stCheckbox"] label {
  color:#40566B!important; font-size:.86rem!important; font-weight:650!important;
  line-height:1.35!important;
}
[data-testid="stCaptionContainer"] { color:var(--muted); }
[data-testid="stMarkdownContainer"] p { line-height:1.55; }

/* Input surfaces: select, numeric, text and multiselect controls share height,
   white fill, border, radius and focus treatment. */
[data-testid="stSelectbox"] [data-baseweb="select"] > div,
[data-testid="stMultiSelect"] [data-baseweb="select"] > div,
[data-testid="stNumberInput"] input,
[data-testid="stTextInput"] input,
[data-testid="stTextArea"] textarea {
  min-height:2.55rem; color:var(--ink)!important; background:#FFFFFF!important;
  border:1px solid #C9D5E0!important; border-radius:8px!important;
  box-shadow:none!important; font-size:.9rem!important;
}
[data-testid="stSelectbox"] [data-baseweb="select"] > div,
[data-testid="stMultiSelect"] [data-baseweb="select"] > div { min-height:2.55rem; }
[data-testid="stNumberInput"] button {
  min-height:2.55rem; background:#F2F5F8!important; color:#40566B!important;
  border-color:#D4DEE7!important;
}
[data-testid="stMultiSelect"] [data-baseweb="tag"] {
  background:#EAF1F7!important; color:#24415E!important; border-radius:6px!important;
}
[data-testid="stSelectbox"] [data-baseweb="select"] > div:focus-within,
[data-testid="stMultiSelect"] [data-baseweb="select"] > div:focus-within,
[data-testid="stNumberInput"] input:focus,
[data-testid="stTextInput"] input:focus,
[data-testid="stTextArea"] textarea:focus {
  border-color:var(--focus)!important; box-shadow:0 0 0 2px rgba(106,155,203,.16)!important;
}

/* Radio groups are consistently rendered as segmented choice cards.
   This applies equally to mixture source, axial-load mode, cover modes and section mode. */
[data-testid="stRadio"] [role="radiogroup"] {
  display:flex!important; flex-direction:row!important; flex-wrap:wrap!important;
  align-items:stretch; gap:.42rem!important; width:100%;
}
[data-testid="stRadio"] [role="radiogroup"] > label {
  display:flex!important; align-items:center; gap:.42rem!important;
  min-height:2.45rem; margin:0!important; padding:.42rem .68rem!important;
  background:#FFFFFF!important; border:1px solid #CFD9E3!important;
  border-radius:8px!important; color:#334B61!important;
  font-size:.84rem!important; font-weight:600!important; line-height:1.3!important;
}
[data-testid="stRadio"] [role="radiogroup"] > label:has(input:checked) {
  background:#EEF4F9!important; border-color:#789CBD!important;
  box-shadow:inset 0 0 0 1px rgba(53,104,168,.12);
}
[data-testid="stRadio"] [role="radiogroup"] > label:hover {
  border-color:#9EB5C9!important; background:#F7FAFC!important;
}
[data-testid="stRadio"] [role="radiogroup"] input { accent-color:var(--blue); }
[data-testid="stCheckbox"] label { padding:.12rem 0; }

/* Metrics and tables use a single card grammar. */
[data-testid="stMetric"] {
  min-width:0; height:100%; padding:.72rem .82rem!important;
  background:#FFFFFF!important; border:1px solid var(--grid)!important;
  border-top:3px solid var(--blue)!important; border-radius:10px!important;
  box-shadow:none!important;
}
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] p,
[data-testid="stMetric"] label {
  color:#52677B!important; font-size:.81rem!important; font-weight:650!important;
  white-space:normal!important; overflow-wrap:anywhere; line-height:1.25!important;
}
[data-testid="stMetricValue"], [data-testid="stMetricValue"] div,
[data-testid="stMetricValue"] p {
  min-width:0; color:var(--navy)!important; font-weight:780!important;
  font-size:clamp(1.03rem,1.45vw,1.45rem)!important; line-height:1.2!important;
  white-space:normal!important; overflow-wrap:anywhere!important; word-break:normal!important;
}
[data-testid="stMetricDelta"] { white-space:normal!important; overflow-wrap:anywhere; }
[data-testid="stDataFrame"] {
  width:100%; background:#FFFFFF!important; border:1px solid var(--grid)!important;
  border-radius:9px!important; overflow:hidden!important;
}
[data-testid="stDataFrame"] [role="columnheader"] {
  background:#EAF0F5!important; color:var(--navy)!important; font-weight:750!important;
}

/* Buttons, downloads, expanders, alerts and dividers. */
.stButton > button, .stDownloadButton > button {
  min-height:2.45rem; border-radius:8px!important; font-weight:700!important;
  box-shadow:none!important; white-space:normal; line-height:1.25;
}
.stButton > button { border:1px solid #C8D5E1!important; }
.stButton > button[kind="primary"] {
  color:#FFFFFF!important; background:var(--green)!important; border-color:var(--green)!important;
}
.stButton > button[kind="primary"]:hover { background:#28644F!important; border-color:#28644F!important; }
.stDownloadButton > button {
  color:var(--navy)!important; background:#F2F6F9!important; border:1px solid #C8D7E3!important;
}
.stDownloadButton > button:hover { background:#EAF1F6!important; border-color:#8DAAC2!important; }
[data-testid="stExpander"] {
  background:#FFFFFF!important; border:1px solid var(--grid)!important;
  border-radius:9px!important; box-shadow:none!important;
}
[data-testid="stExpander"] summary { color:var(--navy)!important; font-weight:680!important; }
[data-testid="stAlert"] { border-radius:9px!important; }
hr { border-color:#DCE4EB!important; }
:focus-visible { outline:2px solid var(--focus)!important; outline-offset:2px!important; }

/* Command bar and selected result remain distinct functional regions, not new card styles. */
.st-key-design-commandbar {
  position:sticky; top:.45rem; z-index:100; background:#FFFFFF;
  border:1px solid #C8D8E4; border-left:4px solid var(--green);
  border-radius:11px; padding:.48rem .65rem; margin:.05rem 0 .9rem;
  box-shadow:0 3px 12px rgba(32,59,86,.07);
}
.st-key-design-commandbar [data-testid="stHorizontalBlock"] { align-items:center; gap:.62rem; }
.st-key-design-commandbar .stButton > button { min-height:2.65rem; }
.st-key-selected-design-preview { position:sticky; top:5rem; align-self:flex-start; }
.st-key-selected-design-preview [data-testid="stVerticalBlockBorderWrapper"] {
  border-top:3px solid var(--green)!important;
}

/* Guide images are deliberately full-width within their step card. */
.guide-intro {
  padding:.82rem 1rem; background:#FBF5EF; border:1px solid #EAD6C5;
  border-left:4px solid var(--orange); border-radius:9px; line-height:1.55;
}
.guide-shot {
  width:100%; padding:.35rem; background:#F8FAFC;
  border:1px solid #D2DDE7; border-radius:9px;
}
.guide-shot img { width:100%; border-radius:6px; }
[class*="st-key-guide-shot-"] { width:100%; }
[class*="st-key-guide-shot-"] [data-testid="stImage"] { width:100%; }
.guide-meta { color:#52677B; font-size:.88rem; line-height:1.55; }
.workflow-strip { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:.62rem; margin:.8rem 0 1rem; }
.workflow-card { min-width:0; padding:.78rem .85rem; background:#FFFFFF; border:1px solid var(--grid); border-top:3px solid var(--blue); border-radius:10px; }
.workflow-card b { display:block; color:var(--navy); font-size:.87rem; margin-bottom:.22rem; }
.workflow-card span { color:var(--muted); font-size:.78rem; line-height:1.4; }
.author-list { display:flex; flex-direction:column; gap:.12rem; margin:.45rem 0 .15rem; }
.author-name { color:var(--navy); font-weight:760; font-size:1rem; line-height:1.4; }
.affiliation-line { color:#586B7D; font-size:.79rem; line-height:1.45; margin:0 0 .35rem .15rem; }
.corresponding-tag { display:inline-block; color:#28644F; background:#EEF6F0; border:1px solid #CFE2D4; border-radius:999px; font-size:.68rem; font-weight:700; padding:.08rem .42rem; vertical-align:middle; }

/* Responsive layout: wrap columns and prevent narrow metric overflow. */
@media(max-width:1100px) {
  .block-container { padding-left:1rem; padding-right:1rem; }
  .st-key-selected-design-preview, .st-key-design-commandbar { position:static; }
}
@media(max-width:850px) {
  .workflow-strip { grid-template-columns:repeat(2,minmax(0,1fr)); }
  [data-testid="stHorizontalBlock"] { flex-wrap:wrap; }
  [data-testid="stHorizontalBlock"] > [data-testid="column"] { min-width:min(100%,245px); }
}
@media(max-width:650px) {
  .block-container { padding:.8rem .72rem 2rem; }
  .tab-hero { padding:1rem 1.05rem; }
  .tab-hero h1 { font-size:1.42rem; }
  .tab-hero p { font-size:.86rem; }
  .workflow-strip { grid-template-columns:1fr; }
  [data-testid="stMetricValue"], [data-testid="stMetricValue"] div,
  [data-testid="stMetricValue"] p { font-size:1.08rem!important; }
}
</style>"""
st.markdown(APP_CSS, unsafe_allow_html=True)


def _tab_hero(theme: str, eyebrow: str, title: str, description: str, tags: Sequence[str]) -> str:
    """Return the consistent tab-specific application banner."""
    safe_tags = "".join(f"<span>{tag}</span>" for tag in tags)
    return (f'<div class="tab-hero {theme}"><div class="eyebrow">{eyebrow}</div>'
            f'<h1>{title}</h1><p>{description}</p><div class="hero-tags">{safe_tags}</div></div>')

ENGINE_BASENAMES = (
    "SCC_Column_Design_Engine_Rev99.py",
)
WORKBOOK_BASENAMES = (
    "Concrete_Dataset.xlsx",
)
DEFAULT_WORKBOOK = APP_DIR / "Concrete_Dataset.xlsx"


def discover_engine_file() -> Optional[Path]:
    """Locate the paired Rev99 engine without repeatedly reading large source files."""
    base_dirs = [
        APP_DIR, APP_DIR / "engine", APP_DIR / "calculation", APP_DIR / "src", APP_DIR / "data",
        Path.cwd(), Path.cwd() / "engine", Path.cwd() / "calculation",
    ]
    parent = APP_DIR
    for _ in range(2):
        parent = parent.parent
        base_dirs.extend([parent, parent / "engine", parent / "calculation"])

    seen = set()
    ordered_dirs = []
    for folder in base_dirs:
        try:
            folder = folder.resolve()
        except Exception:
            continue
        if folder in seen or not folder.is_dir():
            continue
        seen.add(folder)
        ordered_dirs.append(folder)

    # The versioned filename is authoritative; do not silently load a different engine.
    versioned_name = ENGINE_BASENAMES[0]
    for folder in ordered_dirs:
        candidate = folder / versioned_name
        if candidate.is_file():
            return candidate
    return None

def discover_workbook_file() -> Optional[Path]:
    """Find the current study workbook beside the app or in a conventional data folder."""
    base_dirs = [APP_DIR, APP_DIR / "data", APP_DIR / "resources", APP_DIR / "dataset",
                 Path.cwd(), Path.cwd() / "data", Path.cwd() / "resources"]
    seen = set()
    for folder in base_dirs:
        try:
            folder = folder.resolve()
        except Exception:
            continue
        if folder in seen or not folder.is_dir():
            continue
        seen.add(folder)
        for basename in WORKBOOK_BASENAMES:
            candidate = folder / basename
            if candidate.is_file():
                return candidate
    return None


def _resource_fingerprint(path: Optional[Path]) -> str:
    """Use file metadata in the Streamlit cache key so replacing study resources reloads them."""
    if path is None or not path.is_file():
        return "missing"
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


# ---------------------------------------------------------------------------
# Component coefficients reconstructed in the manuscript from the SimaPro
# component contributions. They are editable in the interface.
DEFAULT_EFS = {
    "Cement": 0.92335,
    "GGBFS": 0.30251,
    "Silica fume": 0.10504,
    "Limestone powder": 0.07937,
    "Fine aggregate": 0.03224,
    "Coarse aggregate": 0.03856,
    "Water": 0.00054,
    "PCE admixture": 1.37916,
}
COMPONENT_KEYS = {
    "Cement": "cement_kg_m3",
    "GGBFS": "ggbfs_kg_m3",
    "Silica fume": "sf_kg_m3",
    "Limestone powder": "lp_kg_m3",
    "Fine aggregate": "fine_aggregate_kg_m3",
    "Coarse aggregate": "coarse_aggregate_kg_m3",
    "Water": "water_kg_m3",
    "PCE admixture": "PCE_kg_m3",
}

# Standard discrete reinforcement sizes offered by the app. The user may add
# further integer nominal diameters; the engine enumerates these candidates.
# Rev99 study search space. Additional diameters may be entered as exploratory cases.
STANDARD_LONGITUDINAL_DIAMETERS_MM = [20, 25]
STANDARD_LINK_DIAMETERS_MM = [6, 8, 10]


def parse_diameter_candidates(selected: Sequence[int], custom_text: str, *,
                              label: str, min_mm: int, max_mm: int,
                              max_candidates: int) -> List[int]:
    """Return validated, unique integer nominal bar diameters in mm.

    Reinforcement diameters are discrete product sizes, not continuous design
    variables. Multiple selected/custom values are all passed to the search.
    """
    values = {int(v) for v in selected}
    raw = (custom_text or "").replace(";", ",").replace("،", ",")
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            number = float(token)
        except ValueError as exc:
            raise ValueError(f"{label}: '{token}' is not a valid diameter. Use comma-separated integer mm values.") from exc
        if not math.isfinite(number) or not number.is_integer():
            raise ValueError(f"{label}: '{token}' must be an integer nominal diameter in mm.")
        values.add(int(number))
    if not values:
        raise ValueError(f"Select or enter at least one {label.lower()} candidate.")
    invalid = sorted(v for v in values if v < min_mm or v > max_mm)
    if invalid:
        raise ValueError(f"{label}: allowed diameters are {min_mm}–{max_mm} mm; invalid: {invalid}.")
    result = sorted(values)
    if len(result) > max_candidates:
        raise ValueError(f"{label}: select at most {max_candidates} candidate sizes per search to keep the discrete design search manageable.")
    return result


# ---------------------------------------------------------------------------
# Input preset management, validation, and user-facing summaries
# ---------------------------------------------------------------------------
PRESET_SCHEMA_VERSION = 1


def _collect_current_preset_values() -> Dict[str, Any]:
    cache = dict(st.session_state.get("_scc_input_preset_values", {}))
    for key, value in list(st.session_state.items()):
        if isinstance(key, str) and key.startswith(PERSISTENT_WIDGET_PREFIX):
            cache[key] = _serializable_widget_value(value)
    return {str(k): v for k, v in cache.items() if str(k).startswith(PERSISTENT_WIDGET_PREFIX)}


def _make_preset_json(input_values: Optional[Dict[str, Any]] = None) -> bytes:
    payload = {
        "schema_version": PRESET_SCHEMA_VERSION,
        "application": "SCC Column Design & GWP Assessment",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "inputs": (_collect_current_preset_values() if input_values is None else dict(input_values)),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")


def _read_preset_json(raw: bytes) -> Dict[str, Any]:
    if len(raw) > 1_000_000:
        raise ValueError("The preset file is larger than the permitted 1 MB limit.")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:
        raise ValueError("The selected file is not a valid UTF-8 JSON preset.") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != PRESET_SCHEMA_VERSION:
        raise ValueError("Unsupported preset format. Please use a preset exported by this application.")
    inputs = payload.get("inputs")
    if not isinstance(inputs, dict) or len(inputs) > 150:
        raise ValueError("The preset does not contain a valid input collection.")
    cleaned: Dict[str, Any] = {}
    for key, value in inputs.items():
        if not isinstance(key, str) or not key.startswith(PERSISTENT_WIDGET_PREFIX):
            continue
        if isinstance(value, (dict,)):
            continue
        if isinstance(value, list):
            if len(value) > 50 or not all(isinstance(x, (str, int, float, bool)) or x is None for x in value):
                continue
            if any(isinstance(x, float) and not math.isfinite(x) for x in value):
                continue
        elif isinstance(value, float) and not math.isfinite(value):
            continue
        elif not (isinstance(value, (str, int, float, bool)) or value is None):
            continue
        if isinstance(value, str) and len(value) > 1000:
            continue
        cleaned[key] = value
    if not cleaned:
        raise ValueError("No compatible application inputs were found in this preset.")
    return cleaned


def _clear_selected_design_result() -> None:
    for key in (
        "last_design_result", "last_design_alternatives", "last_design_mix_gwp",
        "last_design_components", "last_design_cover_req", "last_design_target_years",
        "last_design_mix_label", "last_design_storeys", "last_design_input_signature",
        "last_design_input_summary", "last_design_widget_inputs", "last_design_app_version",
        "last_design_resource_signature",
        "_scc_results_bundle_zip", "_scc_report_pdf_cache", "_scc_figure_assets",
    ):
        st.session_state.pop(key, None)


def _clear_design_results() -> None:
    _clear_selected_design_result()
    for key in ("last_benchmark_rows", "last_benchmark_input_signature", "last_benchmark_app_version",
                "last_benchmark_resource_signature",
                "_scc_results_bundle_signature", "_scc_figure_assets", "_scc_report_pdf_cache"):
        st.session_state.pop(key, None)


def _validate_design_inputs(payload: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Field-specific validation before a design or comparison run."""
    errors: List[str] = []
    warnings: List[str] = []

    def numeric(name: str, value: Any, *, minimum: Optional[float] = None,
                maximum: Optional[float] = None, strict_minimum: bool = False) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            errors.append(f"{name}: enter a numeric value.")
            return None
        if not math.isfinite(number):
            errors.append(f"{name}: the value must be finite.")
            return None
        if minimum is not None and (number <= minimum if strict_minimum else number < minimum):
            op = ">" if strict_minimum else "≥"
            errors.append(f"{name}: the value must be {op} {minimum:g}.")
        if maximum is not None and number > maximum:
            errors.append(f"{name}: the value must be ≤ {maximum:g}.")
        return number

    numeric("Mean 28-day cube strength (MPa)", payload.get("f_cube_28"), minimum=1.0, maximum=150.0)
    numeric("Concrete GWP intensity (kg CO₂-eq/m³)", payload.get("concrete_gwp"), minimum=0.0, strict_minimum=True)
    storeys = numeric("Number of stories", payload.get("storeys"), minimum=1, maximum=100)
    numeric("Clear height (m)", payload.get("clear_height_m"), minimum=1.0, maximum=10.0)
    numeric("Effective length (m)", payload.get("effective_length_m"), minimum=0.5, maximum=20.0)
    numeric("Concrete unit weight (kN/m³)", payload.get("unit_weight"), minimum=10.0, maximum=35.0)
    numeric("First-order moment MEd,x (kN·m)", payload.get("mx_kNm"), minimum=0.0, maximum=100000.0)
    numeric("First-order moment MEd,y (kN·m)", payload.get("my_kNm"), minimum=0.0, maximum=100000.0)

    efs = payload.get("component_efs", {})
    if not isinstance(efs, dict):
        errors.append("Component emission factors: the factor record is invalid.")
    else:
        for component in COMPONENT_KEYS:
            if component not in efs:
                errors.append(f"{component} emission factor: a value is required.")
            else:
                numeric(f"{component} emission factor (kg CO₂-eq/kg)", efs.get(component), minimum=0.0, maximum=20.0)

    recipe = payload.get("mixture_recipe", {})
    if not isinstance(recipe, dict):
        errors.append("Mixture proportions: the constituent record is invalid.")
    else:
        component_total = 0.0
        binder_total = 0.0
        for component, key in COMPONENT_KEYS.items():
            mass = numeric(f"{component} content (kg/m³)", recipe.get(key), minimum=0.0, maximum=3000.0)
            if mass is not None:
                component_total += mass
                if key in ("cement_kg_m3", "ggbfs_kg_m3", "sf_kg_m3"):
                    binder_total += mass
        if component_total <= 0:
            errors.append("Mixture proportions: at least one constituent must have a positive mass.")
        if binder_total <= 0:
            errors.append("Binder content: cement + GGBFS + silica fume must be greater than zero.")
        if binder_total > 1000:
            warnings.append("Binder content exceeds 1000 kg/m³; confirm that the entered mixture proportions are intentional.")

    if payload.get("load_mode") == "Manual N_Ed":
        numeric("Manual design axial force NEd (kN)", payload.get("manual_n_ed"), minimum=0.0, strict_minimum=True, maximum=200000.0)
    else:
        numeric("Tributary area (m²)", payload.get("tributary_area"), minimum=0.0, strict_minimum=True, maximum=500.0)
        numeric("Characteristic floor dead load Gk (kN/m²)", payload.get("dead_load"), minimum=0.0, maximum=50.0)
        numeric("Characteristic floor live load Qk (kN/m²)", payload.get("live_load"), minimum=0.0, maximum=50.0)
        if _f(payload.get("dead_load")) == 0 and _f(payload.get("live_load")) == 0:
            warnings.append("Both floor dead and live loads are zero; the axial force will mainly reflect the modelled column self-weight.")
    numeric("ULS factor gamma_G", payload.get("gamma_G"), minimum=1.0, maximum=2.0)
    numeric("ULS factor gamma_Q", payload.get("gamma_Q"), minimum=1.0, maximum=2.0)

    if payload.get("section_mode") == "Search section range":
        low = numeric("Minimum section width (mm)", payload.get("min_section"), minimum=300, maximum=1500)
        high = numeric("Maximum section width (mm)", payload.get("max_section"), minimum=300, maximum=1500)
        step = numeric("Section increment (mm)", payload.get("section_step"), minimum=0.0, strict_minimum=True, maximum=1000)
        if low is not None and high is not None and low > high:
            errors.append("Section search: minimum section width cannot exceed maximum section width.")
        if low is not None and high is not None and step is not None and step > 0 and high >= low:
            if not any(abs(step - x) < 1e-9 for x in (25, 50, 100)):
                warnings.append("Section increment differs from the standard 25-, 50-, or 100-mm options.")
    else:
        numeric("Fixed square-section width (mm)", payload.get("fixed_section"), minimum=300, maximum=1500)

    if payload.get("cover_mode") == "Estimate from chloride migration":
        numeric("90-day chloride migration coefficient (×10⁻¹² m²/s)", payload.get("dnssm"), minimum=0.0, strict_minimum=True, maximum=100.0)
        numeric("Aging exponent alpha", payload.get("alpha"), minimum=0.05, maximum=1.0)
        ccrit = numeric("Critical chloride concentration Ccrit", payload.get("ccrit"), minimum=0.1, maximum=2.0)
        cs = numeric("Surface chloride concentration Cs", payload.get("cs"), minimum=0.2, maximum=10.0)
        numeric("Transfer factor kt", payload.get("kt"), minimum=0.01, maximum=1.0)
        if ccrit is not None and cs is not None and ccrit >= cs:
            errors.append("Chloride thresholds: Ccrit must be lower than Cs.")
    else:
        numeric("Nominal cover (mm)", payload.get("cover_nominal"), minimum=10.0, maximum=150.0)
    numeric("Execution allowance (mm)", payload.get("execution_allowance"), minimum=0.0, maximum=30.0)
    if payload.get("minimum_cover_override") is not None:
        numeric("Minimum durability-cover override (mm)", payload.get("minimum_cover_override"), minimum=0.0, maximum=150.0)
    if payload.get("target_years") not in (50, 75, 100):
        errors.append("Corrosion-initiation target: select 50, 75, or 100 years.")

    numeric("Steel emission factor (kg CO₂-eq/kg)", payload.get("steel_ef"), minimum=0.01, maximum=10.0)
    if not payload.get("allowed_long"):
        errors.append("Longitudinal-bar candidates: select at least one diameter.")
    if not payload.get("allowed_links"):
        errors.append("Link candidates: select at least one diameter.")
    for value in payload.get("allowed_long", []) or []:
        if int(_f(value, -1)) != _f(value, -1) or not 8 <= _f(value, -1) <= 50:
            errors.append(f"Longitudinal-bar diameter {value!r}: use an integer between 8 and 50 mm.")
    for value in payload.get("allowed_links", []) or []:
        if int(_f(value, -1)) != _f(value, -1) or not 6 <= _f(value, -1) <= 25:
            errors.append(f"Link diameter {value!r}: use an integer between 6 and 25 mm.")
    if payload.get("required_bar_count") is not None and int(_f(payload.get("required_bar_count"), 0)) not in (4, 8, 12, 16, 20, 24, 28):
        errors.append("Longitudinal bar count: use one of the offered discrete counts or Any feasible count.")

    numeric("Steel partial factor gamma_s", payload.get("gamma_s"), minimum=1.0, maximum=1.5)
    numeric("Concrete partial factor gamma_c", payload.get("gamma_c"), minimum=1.0, maximum=2.0)
    numeric("Reinforcement yield strength fyk (MPa)", payload.get("fyk"), minimum=300, maximum=700)
    numeric("Shear factor gamma_v", payload.get("gamma_v"), minimum=1.0, maximum=2.0)
    numeric("Concrete stress-block factor ktc", payload.get("k_tc"), minimum=0.5, maximum=1.0)
    numeric("Shear-field cot theta", payload.get("cot_theta_shear"), minimum=1.0, maximum=3.0)
    numeric("Minimum confinement factor k", payload.get("minimum_confinement_factor_k"), minimum=0.005, maximum=0.10)
    numeric("Effective creep ratio phi_eff", payload.get("effective_creep_ratio_local"), minimum=0.0, maximum=4.0)
    numeric("Maximum aggregate size (mm)", payload.get("max_aggregate"), minimum=4.0, maximum=32.0)
    numeric("Maximum longitudinal reinforcement ratio (%)", payload.get("max_reinf_ratio"), minimum=1.0, maximum=4.0)

    if storeys is not None and int(storeys) not in (5, 15, 30):
        warnings.append("The story count differs from the three principal study scenarios; review the prescribed first-order moments.")
    if payload.get("mixture_source") == "User-defined mixture":
        warnings.append("User-defined material inputs should be based on measured mixture properties and documented GWP factors.")
    if payload.get("cover_mode") == "Enter nominal cover directly":
        warnings.append("Direct nominal cover bypasses chloride-initiation cover estimation; document the basis for the selected value.")
    return errors, warnings


def _input_summary_pairs(payload: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Return a reproducible, human-readable record of all material and design inputs."""
    recipe = payload.get("mixture_recipe", {}) or {}
    recipe_text = "; ".join(
        f"{name}: {_f(recipe.get(key)):.1f} kg/m³" for name, key in COMPONENT_KEYS.items()
    )
    efs = payload.get("component_efs", {}) or {}
    ef_text = "; ".join(f"{name}: {_f(efs.get(name)):.5f}" for name in COMPONENT_KEYS) if efs else "Study defaults"
    if payload.get("load_mode") == "Manual N_Ed":
        axial_definition = f"Manual Nₑd = {_f(payload.get('manual_n_ed')):.1f} kN"
    else:
        axial_definition = (
            f"Tributary area = {_f(payload.get('tributary_area')):.2f} m²; "
            f"Gₖ = {_f(payload.get('dead_load')):.2f} kN/m²; "
            f"Qₖ = {_f(payload.get('live_load')):.2f} kN/m²"
        )
    section_text = (
        f"{payload.get('min_section', '—')}–{payload.get('max_section', '—')} mm; "
        f"increment {payload.get('section_step', '—')} mm"
        if payload.get("section_mode") == "Search section range"
        else f"Fixed {payload.get('fixed_section', '—')} mm"
    )
    bar_count = payload.get("required_bar_count")
    advanced = (
        f"fᵧₖ = {_f(payload.get('fyk')):.1f} MPa; γₛ = {_f(payload.get('gamma_s')):.2f}; "
        f"γ꜀ = {_f(payload.get('gamma_c')):.2f}; γᵥ = {_f(payload.get('gamma_v')):.2f}; "
        f"kₜ꜀ = {_f(payload.get('k_tc')):.2f}; cot θ = {_f(payload.get('cot_theta_shear')):.2f}; "
        f"k = {_f(payload.get('minimum_confinement_factor_k')):.3f}; "
        f"φₑff = {_f(payload.get('effective_creep_ratio_local')):.2f}; "
        f"max. aggregate = {_f(payload.get('max_aggregate')):.1f} mm; "
        f"max. longitudinal ratio = {_f(payload.get('max_reinf_ratio')):.2f}%"
    )
    pairs = [
        ("Mixture", f"{payload.get('mixture_id', '—')} · {payload.get('mixture_source', '—')}"),
        ("Mean 28-day cube strength", f"{_f(payload.get('f_cube_28')):.2f} MPa"),
        ("Concrete GWP intensity", f"{_f(payload.get('concrete_gwp')):.2f} kg CO₂-eq/m³"),
        ("Mixture proportions", recipe_text),
        ("Component emission factors", ef_text + " kg CO₂-eq/kg"),
        ("Axial-load mode", str(payload.get("load_mode", "—"))),
        ("Axial-load definition", axial_definition),
        ("Story count", str(payload.get("storeys", "—"))),
        ("Geometry", f"Clear height {_f(payload.get('clear_height_m')):.2f} m; effective length {_f(payload.get('effective_length_m')):.2f} m"),
        ("Concrete unit weight", f"{_f(payload.get('unit_weight')):.2f} kN/m³"),
        ("First-order moments", f"Mₑd,x = {_f(payload.get('mx_kNm')):.1f}; Mₑd,y = {_f(payload.get('my_kNm')):.1f} kN·m"),
        ("ULS load factors", f"γG = {_f(payload.get('gamma_G')):.2f}; γQ = {_f(payload.get('gamma_Q')):.2f}"),
        ("Cover mode / basis", f"{payload.get('cover_mode', '—')} / {payload.get('cover_basis', '—')}"),
        ("Initiation target", f"{payload.get('target_years', '—')} years"),
        ("Migration coefficient", f"{_f(payload.get('dnssm')):.4f} × 10⁻¹² m²/s" if payload.get('dnssm') is not None else "Direct-cover mode"),
        ("Required effective cover", f"{_f(payload.get('cover_required_effective')):.1f} mm" if payload.get('cover_required_effective') is not None else "Direct-cover mode"),
        ("Aging exponent / chloride thresholds / transfer factor", f"α = {_f(payload.get('alpha')):.2f}; Ccrit = {_f(payload.get('ccrit')):.2f}; Cs = {_f(payload.get('cs')):.2f}; kt = {_f(payload.get('kt')):.3f}" if payload.get('cover_mode') == "Estimate from chloride migration" else "Direct-cover mode"),
        ("Execution allowance", f"{_f(payload.get('execution_allowance')):.1f} mm"),
        ("Minimum-cover override", f"{_f(payload.get('minimum_cover_override')):.1f} mm" if payload.get('minimum_cover_override') is not None else "Code minimum"),
        ("Nominal cover", f"{_f(payload.get('cover_nominal')):.1f} mm" if payload.get('cover_nominal') is not None else "Derived during the design search"),
        ("Section definition", str(payload.get("section_mode", "—"))),
        ("Section search", section_text),
        ("Design objective", str(payload.get("objective_label", payload.get("objective", "—")))),
        ("Longitudinal-bar candidates", "Ø" + ", Ø".join(map(str, payload.get("allowed_long", []))) + " mm"),
        ("Link candidates", "Ø" + ", Ø".join(map(str, payload.get("allowed_links", []))) + " mm"),
        ("Longitudinal bar-count constraint", str(bar_count) if bar_count is not None else "Any feasible count"),
        ("Steel emission factor", f"{_f(payload.get('steel_ef')):.3f} kg CO₂-eq/kg"),
        ("Advanced design parameters", advanced),
    ]
    return pairs


# ---------------------------------------------------------------------------
# Engine/data loading
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def load_engine_and_database(workbook_path: str, engine_file_path: str,
                             workbook_fingerprint: str = "", engine_fingerprint: str = ""):
    """Load the compatible calculation module and the 16-mixture study data."""
    engine_path = Path(engine_file_path).expanduser()
    workbook = Path(workbook_path).expanduser()
    if not engine_path.is_file():
        raise FileNotFoundError(f"Calculation engine file does not exist: {engine_path}")
    if not workbook.is_file():
        raise FileNotFoundError(f"Concrete workbook does not exist: {workbook}")

    spec = importlib.util.spec_from_file_location("scc_column_design_engine_rev99", engine_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load the calculation engine from {engine_path}")
    engine = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = engine
    spec.loader.exec_module(engine)

    engine_version = str(getattr(engine, "THEME_VERSION", "unknown"))
    required_engine_members = (
        "COLUMN_ASSUMPTIONS", "XLSXReader", "read_sheet", "load_experimental",
        "read_simapro", "group_for_mix", "_calculate_column_design",
        "_cover_model_for_input", "_column_axis_capacity_cached", "_derive_column_actions",
    )
    missing_engine_members = [name for name in required_engine_members if not hasattr(engine, name)]
    if missing_engine_members:
        raise RuntimeError(f"Rev99 engine is missing required web-app interfaces: {missing_engine_members}")
    if engine_version != REQUIRED_ENGINE_VERSION:
        raise RuntimeError(
            f"Incompatible calculation engine: found {engine_version}; "
            f"this application requires {REQUIRED_ENGINE_VERSION}."
        )

    # Keep the app's standard UI candidates synchronized with the engine release.
    global STANDARD_LONGITUDINAL_DIAMETERS_MM, STANDARD_LINK_DIAMETERS_MM
    assumptions = engine.COLUMN_ASSUMPTIONS
    STANDARD_LONGITUDINAL_DIAMETERS_MM = sorted({int(round(float(x))) for x in assumptions["longitudinal_bar_diameters_mm"]})
    STANDARD_LINK_DIAMETERS_MM = sorted({int(round(float(x))) for x in assumptions["link_diameters_mm"]})
    reader = engine.XLSXReader(str(workbook))
    raw = engine.load_experimental(str(workbook), reader=reader)
    if len(raw.get("order", [])) != 16:
        raise ValueError(f"Expected 16 study mixtures; found {len(raw.get('order', []))}.")
    simapro = engine.read_simapro(reader, raw["md"])

    # Keep the full mixture recipe, including limestone powder and both aggregate
    # fractions. The engine's compact md dictionary intentionally omits these fields.
    recipe_by_mix: Dict[str, Dict[str, Any]] = {}
    for row in engine.read_sheet(reader, "Mix_Design", 15):
        if not row or not row[0]:
            continue
        mid = str(row[0])
        recipe_by_mix[mid] = {
            "cement_kg_m3": _f(row[5]),
            "ggbfs_kg_m3": _f(row[6]),
            "sf_kg_m3": _f(row[7]),
            "water_kg_m3": _f(row[8]),
            "coarse_aggregate_kg_m3": _f(row[9]),
            "fine_aggregate_kg_m3": _f(row[10]),
            "lp_kg_m3": _f(row[11]),
            "PCE_kg_m3": _f(row[12]),
        }
    missing_recipes = sorted(set(raw["order"]) - set(recipe_by_mix))
    if missing_recipes:
        raise ValueError(f"Mixture proportions missing from Mix_Design: {missing_recipes}")

    mix_db: Dict[str, Dict[str, Any]] = {}
    for mid in raw["order"]:
        md = raw["md"][mid]
        recipe = recipe_by_mix[mid]
        d_stats = raw["D_stats"][mid]
        if (mid, 28) not in raw["cs_stats"]:
            raise KeyError(f"Missing 28-day strength data for {mid}.")
        mix_db[mid] = {
            "Mix_ID": mid,
            "Mixture_Code": md["code"],
            "Binder_Group": engine.group_for_mix(mid, raw["md"]),
            "cement_kg_m3": recipe["cement_kg_m3"],
            "ggbfs_kg_m3": recipe["ggbfs_kg_m3"],
            "sf_kg_m3": recipe["sf_kg_m3"],
            "water_kg_m3": recipe["water_kg_m3"],
            "coarse_aggregate_kg_m3": recipe["coarse_aggregate_kg_m3"],
            "fine_aggregate_kg_m3": recipe["fine_aggregate_kg_m3"],
            "lp_kg_m3": recipe["lp_kg_m3"],
            "PCE_kg_m3": recipe["PCE_kg_m3"],
            "f_cube_28_MPa": raw["cs_stats"][(mid, 28)][0],
            "f_cube_28_SD_MPa": raw["cs_stats"][(mid, 28)][1],
            "Dnssm_90d_1e-12_m2_s": d_stats["scaled"],
            "Dnssm_SD_1e-12_m2_s": d_stats["scaled_sd"],
            "Dnssm_90d_n": d_stats["n"],
            "GWP_SimaPro_kgCO2e_m3": simapro["totals"].get(mid),
        }

    # The corrected M15 RCMT result is a release check for the currently supplied workbook.
    if "M15" in mix_db:
        m15_d = _f(mix_db["M15"].get("Dnssm_90d_1e-12_m2_s"), float("nan"))
        if not math.isfinite(m15_d) or abs(m15_d - 0.860304) > 5e-4:
            raise ValueError(
                "The study-mixture data do not contain the expected M15 RCMT coefficient "
                "(0.860304 × 10⁻¹² m²/s). Please contact the site administrator."
            )
    return engine, mix_db


def _f(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def gwp_from_recipe(recipe: Dict[str, float], efs: Dict[str, float]) -> Tuple[float, Dict[str, float]]:
    contributions = {
        name: max(0.0, _f(recipe.get(key))) * max(0.0, _f(efs.get(name)))
        for name, key in COMPONENT_KEYS.items()
    }
    return sum(contributions.values()), contributions


# ---------------------------------------------------------------------------
# Calculation context: safely override user-editable model inputs per run
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def _get_shared_engine_config_lock():
    """Return one process-wide lock for the cached calculation engine."""
    return threading.RLock()


_ENGINE_CONFIG_LOCK = _get_shared_engine_config_lock()


@contextmanager
def configured_engine(engine, config: Dict[str, Any], manual_n_ed_kN: Optional[float] = None):
    """Temporarily configure shared engine inputs atomically and restore them on exit."""
    # The cached engine is shared across Streamlit sessions; its assumptions and
    # capacity cache are mutable, so configuration must not interleave between users.
    with _ENGINE_CONFIG_LOCK:
        assumptions = engine.COLUMN_ASSUMPTIONS
        old_assumptions = copy.deepcopy(assumptions)
        old_derive = engine._derive_column_actions
        try:
            assumptions.update(config)
            if manual_n_ed_kN is not None:
                n_manual = float(manual_n_ed_kN)

                def manual_actions(storeys: int, section_mm: float):
                    return {
                        "storeys": int(storeys),
                        "N_Ed_floor_G_kN": 0.0,
                        "N_Ed_floor_Q_kN": 0.0,
                        "N_Ed_column_selfweight_kN": 0.0,
                        "N_Ed_floors_kN": n_manual,
                        "N_Ed_total_kN": n_manual,
                        "ULS_floor_combination": "User-specified design axial force N_Ed",
                        "tributary_area_m2": None,
                        "gamma_G": None,
                        "gamma_Q": None,
                        "unit_weight_concrete_kN_m3": assumptions.get("unit_weight_concrete_kN_m3"),
                        "manual_N_Ed_kN": n_manual,
                    }
                engine._derive_column_actions = manual_actions
            if hasattr(engine._column_axis_capacity_cached, "cache_clear"):
                engine._column_axis_capacity_cached.cache_clear()
            yield
        finally:
            assumptions.clear()
            assumptions.update(old_assumptions)
            engine._derive_column_actions = old_derive
            if hasattr(engine._column_axis_capacity_cached, "cache_clear"):
                engine._column_axis_capacity_cached.cache_clear()


def _make_section_candidates(min_b: int, max_b: int, step_b: int) -> List[int]:
    if min_b < 300 or max_b > 1500 or min_b > max_b:
        raise ValueError("Section search bounds must satisfy 300 ≤ minimum ≤ maximum ≤ 1500 mm.")
    if step_b <= 0:
        raise ValueError("Section increment must be positive.")
    values = list(range(int(min_b), int(max_b) + 1, int(step_b)))
    if not values:
        raise ValueError("No candidate section sizes were generated.")
    return values


def _pareto_front(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Non-dominated alternatives for column GWP and total reinforcement mass."""
    valid = [r for r in rows if _f(r.get("GWP_column_kgCO2e"), float("inf")) < float("inf")]
    out = []
    for candidate in valid:
        g = _f(candidate.get("GWP_column_kgCO2e"), float("inf"))
        s = _f(candidate.get("total_steel_mass_kg"), float("inf"))
        dominated = False
        for other in valid:
            if other is candidate:
                continue
            og = _f(other.get("GWP_column_kgCO2e"), float("inf"))
            os_ = _f(other.get("total_steel_mass_kg"), float("inf"))
            if (og <= g and os_ <= s) and (og < g or os_ < s):
                dominated = True
                break
        if not dominated:
            out.append(candidate)
    return sorted(out, key=lambda r: (_f(r.get("GWP_column_kgCO2e")),
                                     _f(r.get("total_steel_mass_kg"))))


def run_design(
    engine,
    *,
    f_cube_28: float,
    cover_nominal_mm: float,
    mix_name: str,
    target_years: int,
    gwp_concrete: float,
    steel_ef: float,
    storeys: int,
    mx_kNm: float,
    my_kNm: float,
    clear_height_m: float,
    effective_length_m: float,
    load_mode: str,
    tributary_area_m2: float,
    dead_load_kN_m2: float,
    live_load_kN_m2: float,
    gamma_G: float,
    gamma_Q: float,
    unit_weight_kN_m3: float,
    manual_n_ed_kN: Optional[float],
    section_mode: str,
    min_section_mm: int,
    max_section_mm: int,
    section_step_mm: int,
    fixed_section_mm: Optional[int],
    long_bar_diameters: Sequence[int],
    link_diameters: Sequence[int],
    required_bar_count: Optional[int],
    max_aggregate_size_mm: float,
    max_reinf_ratio: float,
    k_tc: float,
    cot_theta_shear: float,
    minimum_confinement_factor_k: float,
    effective_creep_ratio_local: float,
    objective: str,
):
    if f_cube_28 <= 0:
        raise ValueError("The 28-day mean cube strength must be positive.")
    if cover_nominal_mm < 10:
        raise ValueError("Nominal cover must be at least 10 mm.")
    if gwp_concrete <= 0:
        raise ValueError("Concrete GWP intensity must be positive.")
    if not long_bar_diameters or not link_diameters:
        raise ValueError("Select at least one longitudinal-bar and one link diameter.")
    if storeys < 1 or clear_height_m <= 0 or effective_length_m <= 0:
        raise ValueError("Story count, clear height and effective length must be positive.")

    if section_mode == "Fixed square section":
        if fixed_section_mm is None or fixed_section_mm < 300 or fixed_section_mm > 1500:
            raise ValueError("Fixed square-section width must be between 300 and 1500 mm.")
        candidate_sections = [int(fixed_section_mm)]
    else:
        candidate_sections = _make_section_candidates(min_section_mm, max_section_mm, section_step_mm)

    config = {
        "storeys": int(storeys),
        "M_Ed_x_kNm": float(mx_kNm),
        "M_Ed_y_kNm": float(my_kNm),
        "clear_height_m": float(clear_height_m),
        "effective_length_assumed_m": float(effective_length_m),
        "tributary_area_m2": float(tributary_area_m2),
        "dead_load_kN_m2": float(dead_load_kN_m2),
        "live_load_kN_m2": float(live_load_kN_m2),
        "gamma_G": float(gamma_G),
        "gamma_Q": float(gamma_Q),
        "unit_weight_concrete_kN_m3": float(unit_weight_kN_m3),
        "candidate_square_sections_mm": candidate_sections,
        "longitudinal_bar_diameters_mm": [float(x) for x in long_bar_diameters],
        "link_diameters_mm": [float(x) for x in link_diameters],
        "max_aggregate_size_mm": float(max_aggregate_size_mm),
        "longitudinal_reinforcement_max_ratio": float(max_reinf_ratio) / 100.0,
        "k_tc": float(k_tc),
        "cot_theta_shear": float(cot_theta_shear),
        "minimum_confinement_factor_k": float(minimum_confinement_factor_k),
        "effective_creep_ratio_local": float(effective_creep_ratio_local),
        "transverse_spacing_increment_mm": 50.0,
        "reinforcement_EF_kgCO2e_per_kg_main": float(steel_ef),
    }

    if load_mode == "Automatic from tributary floors":
        manual_n = None
    else:
        if manual_n_ed_kN is None or manual_n_ed_kN <= 0:
            raise ValueError("Enter a positive manual N_Ed value in kN.")
        manual_n = float(manual_n_ed_kN)

    # The engine enumerates all feasible discrete bar/tie layouts. Additional
    # app-level objectives are selected from this same feasible candidate set.
    engine_selection = "all" if objective in {
        "Minimum reinforcement mass", "Pareto alternatives", "Show all feasible alternatives"
    } or section_mode == "Fixed square section" or required_bar_count is not None else objective
    if engine_selection not in {"all", "min_gwp", "smallest_section"}:
        engine_selection = "all"

    with configured_engine(engine, config, manual_n_ed_kN=manual_n):
        raw_result = engine._calculate_column_design(
            float(f_cube_28), float(cover_nominal_mm), str(mix_name), int(target_years),
            selection_rule=engine_selection,
            gwp_concrete=float(gwp_concrete),
            mx_action_kNm=float(mx_kNm), my_action_kNm=float(my_kNm),
            storeys=int(storeys),
        )

    if isinstance(raw_result, dict):
        if raw_result.get("Status", "").startswith("NO_FEASIBLE"):
            return None, [], candidate_sections
        feasible = [raw_result]
    else:
        feasible = list(raw_result or [])
    if required_bar_count is not None:
        feasible = [r for r in feasible if int(r.get("longitudinal_bars_n", 0)) == int(required_bar_count)]
    if not feasible:
        return None, [], candidate_sections

    if objective == "min_gwp":
        chosen = min(feasible, key=lambda r: (_f(r.get("GWP_column_kgCO2e"), float("inf")),
                                             _f(r.get("b_mm")),
                                             _f(r.get("total_steel_mass_kg"))))
    elif objective == "smallest_section":
        min_b = min(_f(r.get("b_mm"), float("inf")) for r in feasible)
        same = [r for r in feasible if _f(r.get("b_mm")) == min_b]
        chosen = min(same, key=lambda r: (_f(r.get("total_steel_mass_kg")),
                                          _f(r.get("GWP_column_kgCO2e"))))
    elif objective == "Minimum reinforcement mass":
        chosen = min(feasible, key=lambda r: (_f(r.get("total_steel_mass_kg")),
                                             _f(r.get("GWP_column_kgCO2e")),
                                             _f(r.get("b_mm"))))
    elif objective == "Pareto alternatives":
        front = _pareto_front(feasible)
        chosen = front[0] if front else min(feasible, key=lambda r: _f(r.get("GWP_column_kgCO2e")))
        feasible = front
    elif objective == "Show all feasible alternatives":
        chosen = min(feasible, key=lambda r: (_f(r.get("GWP_column_kgCO2e")),
                                             _f(r.get("b_mm"))))
    elif objective == "Fixed section – minimum reinforcement mass":
        chosen = min(feasible, key=lambda r: (_f(r.get("total_steel_mass_kg")),
                                             _f(r.get("GWP_column_kgCO2e"))))
    else:  # fixed section – minimum GWP
        chosen = min(feasible, key=lambda r: (_f(r.get("GWP_column_kgCO2e")),
                                             _f(r.get("total_steel_mass_kg"))))

    chosen = dict(chosen)
    chosen["App_selection_objective"] = objective
    chosen["App_mix_recipe_GWP_kgCO2e_m3"] = float(gwp_concrete)
    chosen["App_steel_emission_factor_kgCO2e_kg"] = float(steel_ef)
    chosen["App_clear_height_m"] = float(clear_height_m)
    chosen["App_effective_length_m"] = float(effective_length_m)
    chosen["App_design_axial_mode"] = load_mode
    chosen["App_first_order_moment_x_kNm"] = float(mx_kNm)
    chosen["App_first_order_moment_y_kNm"] = float(my_kNm)
    return chosen, sorted(feasible, key=lambda r: (_f(r.get("GWP_column_kgCO2e")),
                                                    _f(r.get("b_mm")),
                                                    _f(r.get("total_steel_mass_kg")))), candidate_sections


# ---------------------------------------------------------------------------
# Durability cover
# ---------------------------------------------------------------------------
def derive_nominal_cover(engine, target_years: int, dnssm_scaled: float,
                         alpha: float, ccrit: float, cs: float, kt: float,
                         max_bar_diameter: float, execution_allowance_mm: float,
                         minimum_cover_override_mm: Optional[float] = None) -> Tuple[float, float]:
    c_req = engine._cover_model_for_input(
        float(dnssm_scaled), int(target_years), alpha=float(alpha),
        ccrit=float(ccrit), cs=float(cs), kt_v=float(kt)
    )
    cmin_map = {
        50: float(engine.COLUMN_ASSUMPTIONS["XRDS5_XS1_min_durability_cover_50y_mm"]),
        75: float(engine.COLUMN_ASSUMPTIONS["XRDS5_XS1_min_durability_cover_75y_mm"]),
        100: float(engine.COLUMN_ASSUMPTIONS["XRDS5_XS1_min_durability_cover_100y_mm"]),
    }
    if not math.isfinite(c_req):
        raise ValueError("The chloride-cover model returned a non-finite value. Check Ccrit/Cs and Dnssm.")
    c_min = (float(minimum_cover_override_mm) if minimum_cover_override_mm is not None
             else cmin_map[int(target_years)])
    c_nom = max(float(c_req), c_min, float(max_bar_diameter), 10.0) + float(execution_allowance_mm)
    return float(c_req), float(c_nom)


# ---------------------------------------------------------------------------
# Actual reinforcement drawing based on the selected calculation output
# ---------------------------------------------------------------------------
def draw_design_detail(result: Dict[str, Any], show_labels: bool = True):
    """Draw the selected square section and every calculated transverse-link level.

    Elevation uses the five physical zones (bottom end, lower intermediate, lap,
    upper intermediate, top end). Pitches are calculated by the member-design
    engine and are not replaced by a single uniform drawing pitch.
    """
    b = float(result["b_mm"])
    h = float(result.get("clear_height_m", result.get("App_clear_height_m", 3.0))) * 1000.0
    cover = float(result["c_nom_mm"])
    db = float(result["longitudinal_bar_diameter_mm"])
    link_d = float(result["link_diameter_mm"])
    nbar = int(result["longitudinal_bars_n"])
    points_per_face = int(round((nbar + 4) / 4))
    pts = _ENGINE._column_bar_positions(b, cover, db, points_per_face, link_d)

    navy, ink, red = APP_PALETTE["navy"], APP_PALETTE["ink"], APP_PALETTE["red"]
    # High-contrast, colour-vision-conscious reinforcement palette.
    # The three elevation zones use clearly separated hues and tinted backgrounds;
    # the two orthogonal cross-tie directions are also distinct in the section view.
    tie_colors = {
        "hoop": "#0072B2",          # blue: enclosing closed link
        "cross_tie_h": "#D55E00",   # vermilion: horizontal cross-tie
        "cross_tie_v": "#7A5195",   # purple: vertical cross-tie
        "end": "#D55E00",           # vermilion: end confinement zones
        "intermediate": "#0072B2",  # blue: intermediate zones
        "lap": "#7A5195",           # purple: lap region
    }
    zone_fills = {
        "end": "#FCE5D6",
        "intermediate": "#DCEEF8",
        "lap": "#EEE3F5",
    }
    blue = tie_colors["hoop"]

    fig = plt.figure(figsize=(13.2, 7.0), constrained_layout=False)
    gs = fig.add_gridspec(1, 2, width_ratios=[1.00, 1.30], wspace=0.22,
                          left=0.04, right=0.985, top=0.88, bottom=0.12)
    ax_s = fig.add_subplot(gs[0, 0])
    ax_e = fig.add_subplot(gs[0, 1])

    # (a) Section view: real longitudinal bar coordinates, cover, hoop and cross-ties.
    pad = max(0.15 * b, 100)
    ax_s.set_xlim(-pad, b + pad)
    ax_s.set_ylim(-pad * 1.12, b + pad)
    ax_s.set_aspect("equal")
    ax_s.axis("off")
    ax_s.add_patch(Rectangle((0, 0), b, b, facecolor="#F2F4F6",
                             edgecolor=navy, linewidth=1.8, zorder=1))
    ax_s.add_patch(Rectangle((cover, cover), b - 2 * cover, b - 2 * cover,
                             facecolor="none", edgecolor=blue,
                             linewidth=1.6, zorder=2))
    if points_per_face > 2:
        r = b / 2 - cover - link_d - db / 2
        internal = np.linspace(-r, r, points_per_face)[1:-1]
        for q in internal:
            ax_s.plot([b/2-r, b/2+r], [b/2+q, b/2+q],
                      color=tie_colors["cross_tie_h"], lw=2.0,
                      solid_capstyle="round", zorder=2.5)
            ax_s.plot([b/2+q, b/2+q], [b/2-r, b/2+r],
                      color=tie_colors["cross_tie_v"], lw=2.0, ls=(0, (4, 2)),
                      solid_capstyle="round", zorder=2.6)
    for x, y in pts:
        ax_s.add_patch(Circle((b/2+x, b/2+y), db/2, facecolor=ink,
                              edgecolor="white", linewidth=0.7, zorder=4))
    ax_s.annotate("", xy=(0, b*0.58), xytext=(cover, b*0.58),
                  arrowprops=dict(arrowstyle="<->", color=red, lw=1.25))
    ax_s.text(cover/2, b*0.61, f"c_nom = {cover:.1f} mm", color=red,
              ha="center", va="bottom", fontsize=8.8)
    top_x = sorted({round(b/2+x, 4) for x, y in pts if abs(y-max(q[1] for q in pts)) < 1e-4})
    if len(top_x) >= 2:
        clear = top_x[1] - top_x[0] - db
        ax_s.text(b/2, b + pad*0.20, f"Clear bar spacing = {clear:.1f} mm",
                  ha="center", va="bottom", fontsize=8.6, color=blue)
    ax_s.annotate("", xy=(0, -pad*0.38), xytext=(b, -pad*0.38),
                  arrowprops=dict(arrowstyle="<->", color=ink, lw=0.95))
    ax_s.text(b/2, -pad*0.48, f"b = {b:.0f} mm", ha="center", va="top", fontsize=9.2)
    ax_s.set_title("(a) Designed square cross-section", loc="left", fontsize=11.2,
                   fontweight="bold", color=navy, pad=9)
    from matplotlib.lines import Line2D
    section_legend = [
        Line2D([0], [0], color=tie_colors["hoop"], lw=2.2, label="Closed hoop"),
        Line2D([0], [0], color=tie_colors["cross_tie_h"], lw=2.0, label="Horizontal cross-tie"),
        Line2D([0], [0], color=tie_colors["cross_tie_v"], lw=2.0,
               ls=(0, (4, 2)), label="Vertical cross-tie"),
    ]
    ax_s.legend(handles=section_legend, loc="upper left", bbox_to_anchor=(0.0, 1.02),
                ncol=1, frameon=True, framealpha=0.96, facecolor="white",
                edgecolor="#D6DEE8", fontsize=7.1, handlelength=1.55,
                borderpad=0.42, labelspacing=0.30)

    # (b) Elevation. Five zones are drawn in their calculated physical lengths.
    ax_e.set_xlim(-0.18, 2.45)
    ax_e.set_ylim(-0.105*h, 1.07*h)
    ax_e.axis("off")
    col_x, col_w = 0.05, 0.58
    end_len = float(result.get("end_zone_length_each_mm", b))
    mid_len = float(result.get("middle_zone_length_each_mm", 0.0))
    lap_len = float(result.get("lap_zone_length_mm", 0.0))
    n_end = int(result.get("end_zone_intervals_each", 0))
    n_mid = int(result.get("middle_zone_intervals_each", 0))
    n_lap = int(result.get("lap_zone_intervals", 0))
    if n_end < 1 or n_lap < 1:
        raise ValueError("The calculation output must contain at least one end-zone and lap-region interval.")
    zones = [
        {"name":"Bottom end", "group":"End", "length":end_len, "n":n_end,
         "color":tie_colors["end"], "fill":zone_fills["end"], "linestyle":"solid"},
        {"name":"Lower intermediate", "group":"Intermediate", "length":mid_len, "n":n_mid,
         "color":tie_colors["intermediate"], "fill":zone_fills["intermediate"], "linestyle":(0, (4, 2))},
        {"name":"Lap", "group":"Lap", "length":lap_len, "n":n_lap,
         "color":tie_colors["lap"], "fill":zone_fills["lap"], "linestyle":"dashdot"},
        {"name":"Upper intermediate", "group":"Intermediate", "length":mid_len, "n":n_mid,
         "color":tie_colors["intermediate"], "fill":zone_fills["intermediate"], "linestyle":(0, (4, 2))},
        {"name":"Top end", "group":"End", "length":end_len, "n":n_end,
         "color":tie_colors["end"], "fill":zone_fills["end"], "linestyle":"solid"},
    ]
    total_length = sum(z["length"] for z in zones)
    if abs(total_length-h) > 1.0:
        raise ValueError(f"Calculated link-zone lengths ({total_length:.1f} mm) do not sum to clear height ({h:.1f} mm).")

    ax_e.add_patch(Rectangle((col_x, 0), col_w, h, facecolor="#F2F4F6",
                             edgecolor=navy, linewidth=1.55, zorder=1))
    for x in (col_x+0.13, col_x+col_w-0.13):
        ax_e.plot([x, x], [0, h], color=ink, lw=1.85, zorder=3)

    y = 0.0
    level_records = [(0.0, "Bottom end", tie_colors["end"], "solid")]
    region_meta = []
    boundaries = [0.0]
    for z in zones:
        length, n_intervals = z["length"], z["n"]
        if length < 0 or (length > 1e-8 and n_intervals < 1):
            raise ValueError(f"Invalid interval allocation in {z['name']} zone.")
        if length <= 1e-8:
            continue
        y0, y1 = y, y+length
        actual_pitch = length/n_intervals
        ax_e.add_patch(Rectangle((col_x+0.012, y0), col_w-0.024, length,
                                 facecolor=z["fill"], edgecolor="none", alpha=0.94, zorder=1.5))
        # Draw each link at its calculated physical coordinate within this zone.
        for j in range(1, n_intervals+1):
            yv = y0 + length*j/n_intervals
            level_records.append((yv, z["name"], z["color"], z["linestyle"]))
        region_meta.append({**z, "y0":y0, "y1":y1, "pitch":actual_pitch})
        y = y1
        boundaries.append(y)

    # Show zone boundaries and the actual calculated transverse reinforcement levels.
    for boundary in boundaries[1:-1]:
        ax_e.plot([col_x-0.015, col_x+col_w+0.015], [boundary, boundary],
                  color="#7D8A98", lw=0.7, ls=(0,(3,2)), zorder=3.2)
    for yv, zone_name, color, linestyle in level_records:
        ax_e.plot([col_x+0.035, col_x+col_w-0.035], [yv, yv],
                  color=color, lw=1.85, ls=linestyle,
                  solid_capstyle="butt", zorder=4)

    expected_levels = int(result.get("number_of_links", len(level_records)))
    if len(level_records) != expected_levels:
        raise ValueError(f"Drawing generated {len(level_records)} link levels but the design calculation reports {expected_levels}.")
    expected_intervals = 2*n_end + 2*n_mid + n_lap
    if expected_levels != expected_intervals + 1:
        raise ValueError("Link-level count is inconsistent with the five-zone interval schedule.")

    ax_e.annotate("", xy=(col_x-0.08, 0), xytext=(col_x-0.08, h),
                  arrowprops=dict(arrowstyle="<->", color=ink, lw=0.85))
    ax_e.text(col_x-0.12, h/2, f"h_s = {h/1000:.2f} m", rotation=90,
              ha="right", va="center", fontsize=8.6, color=ink)
    ax_e.text(col_x+col_w/2, -0.050*h,
              f"{expected_levels} link levels  |  {expected_intervals} intervals total",
              ha="center", va="top", fontsize=8.4, color=navy, fontweight="bold")

    details = [
        ("END ZONES · 2 ×", float(result.get("end_zone_spacing_actual_mm", 0)),
         float(result.get("smax_end_limit_mm", 0)), float(result.get("s_end_adopted_spacing_mm", 0)),
         n_end, end_len, tie_colors["end"],
         region_meta[0]["y0"] + region_meta[0]["length"]*0.55),
        ("INTERMEDIATE · 2 ×", float(result.get("middle_zone_spacing_actual_mm", 0)),
         float(result.get("smax_mid_limit_mm", 0)), float(result.get("s_mid_adopted_spacing_mm", 0)),
         n_mid, mid_len, tie_colors["intermediate"],
         next((r["y0"]+r["length"]*0.52 for r in region_meta if r["name"]=="Lower intermediate"), h*0.5)),
        ("CENTRAL LAP · 1 ×", float(result.get("lap_zone_spacing_actual_mm", 0)),
         float(result.get("smax_lap_limit_mm", 0)), float(result.get("s_lap_adopted_spacing_mm", 0)),
         n_lap, lap_len, tie_colors["lap"],
         next((r["y0"]+r["length"]*0.52 for r in region_meta if r["name"]=="Lap"), h*0.5)),
    ]
    label_positions = [0.82*h, 0.57*h, 0.32*h]
    for (name, actual, calc_limit, adopted, intervals, length, color, y_target), y_label in zip(details, label_positions):
        note = (f"{name}\ns_calc,max = {calc_limit:.1f} mm\n"
                f"s_adopt = {adopted:.0f} mm  |  s_actual = {actual:.1f} mm\n"
                f"{intervals} intervals  |  L = {length:.0f} mm")
        ax_e.annotate(note, xy=(col_x+col_w, y_target), xytext=(0.82, y_label),
                      ha="left", va="center", fontsize=7.55, color=ink,
                      bbox=dict(boxstyle="square,pad=0.32", facecolor="white",
                                edgecolor=color, linewidth=1.35),
                      arrowprops=dict(arrowstyle="-", color=color, lw=1.45,
                                      connectionstyle="angle3,angleA=0,angleB=90"))

    # Colour and line pattern both identify the three independent elevation zones.
    legend_handles = [
        Line2D([0],[0], color=tie_colors["end"], lw=2.5, ls="solid", label="End zones"),
        Line2D([0],[0], color=tie_colors["intermediate"], lw=2.5,
               ls=(0, (4, 2)), label="Intermediate zones"),
        Line2D([0],[0], color=tie_colors["lap"], lw=2.5, ls="dashdot", label="Lap region"),
    ]
    ax_e.legend(handles=legend_handles, loc="upper right", bbox_to_anchor=(0.99,1.015),
                ncol=3, frameon=True, framealpha=0.96, facecolor="white", edgecolor="#D6DEE8",
                fontsize=7.7, handlelength=1.6, columnspacing=0.95, borderpad=0.4)
    ax_e.set_title("(b) Column elevation and calculated link-spacing zones", loc="left",
                   fontsize=11.2, fontweight="bold", color=navy, pad=9)
    fig.suptitle(
        f"Selected layout: {b:.0f} × {b:.0f} mm  |  {nbar}Ø{db:.0f}  |  closed links Ø{link_d:.0f}",
        fontsize=12.6, fontweight="bold", color=navy, y=0.97
    )
    fig.text(0.5, 0.035,
             "Regional maximum spacing is rounded down to a 50-mm grid; the actual equalized pitch may be smaller after integer interval allocation.",
             ha="center", va="center", fontsize=8.2, color=APP_PALETTE["muted"])
    return fig


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------
def _result_table_row(r: Dict[str, Any]) -> Dict[str, Any]:
    fields = [
        ("b_mm", "Section (mm)"), ("longitudinal_bars_n", "Bars"),
        ("longitudinal_bar_diameter_mm", "Bar Ø (mm)"), ("link_diameter_mm", "Link Ø (mm)"),
        ("link_spacing_mm", "Minimum zone spacing (mm)"),
        ("end_zone_spacing_actual_mm", "sₑₙd (mm)"),
        ("middle_zone_spacing_actual_mm", "sₘᵢd (mm)"),
        ("lap_zone_spacing_actual_mm", "sₗₐₚ actual (mm)"),
        ("s_end_adopted_spacing_mm", "sₑₙd adopted (mm)"),
        ("s_mid_adopted_spacing_mm", "sₘᵢd adopted (mm)"),
        ("s_lap_adopted_spacing_mm", "sₗₐₚ adopted (mm)"),
        ("smax_end_limit_mm", "sₑₙd calculated limit (mm)"),
        ("smax_mid_limit_mm", "sₘᵢd calculated limit (mm)"),
        ("smax_lap_limit_mm", "sₗₐₚ calculated limit (mm)"),
        ("number_of_links", "Link levels"),
        ("clear_bar_spacing_mm", "Clear bar spacing (mm)"),
        ("total_steel_mass_kg", "Steel (kg)"), ("concrete_volume_m3", "Concrete (m³)"),
        ("GWP_concrete_column_kgCO2e", "Concrete GWP (kg CO₂-eq)"),
        ("GWP_steel_column_kgCO2e", "Steel GWP (kg CO₂-eq)"),
        ("GWP_column_kgCO2e", "Total GWP (kg CO₂-eq)"),
        ("biaxial_interaction_utilization", "Biaxial utilization"),
        ("shear_method", "Shear design method"),
        ("cot_theta", "cot θ"),
        ("tau_Ed_resultant_MPa", "Resultant shear stress (MPa)"),
        ("compression_chord_depth_ratio", "Compression-chord depth ratio"),
        ("f_tdy_optimum_MPa", "Optimum transverse tie stress (MPa)"),
    ]
    return {label: r.get(key) for key, label in fields}


def _csv_bytes(rows: Sequence[Dict[str, Any]]) -> bytes:
    if not rows:
        return b""
    keys = list(rows[0].keys())
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=keys, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


def _download_figure(fig, fmt="png", dpi=300) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format=fmt, dpi=dpi, bbox_inches="tight")
    return buf.getvalue()


def _cached_figure_assets(cache_key, figure_factory, *, formats=("png",), display_dpi=150):
    """Render a figure once per result key and reuse its bytes across Streamlit reruns."""
    cache = st.session_state.setdefault("_scc_figure_assets", {})
    if cache_key not in cache:
        fig = figure_factory()
        try:
            assets = {"display": _download_figure(fig, "png", dpi=display_dpi)}
            for fmt in formats:
                assets[fmt] = _download_figure(fig, fmt, dpi=300)
        finally:
            plt.close(fig)
        cache[cache_key] = assets
        # Keep session memory bounded when the user explores many input combinations.
        while len(cache) > 8:
            oldest_key = next(iter(cache))
            if oldest_key == cache_key and len(cache) == 1:
                break
            cache.pop(oldest_key, None)
    return cache[cache_key]


def _polish_formula_artists(fig):
    """Apply subscript typography to labels from the unchanged detail drawing routine."""
    # MathText avoids missing-glyph boxes in fonts that lack Latin Unicode subscripts.
    replacements = {"c_nom": r"c$_{\mathrm{nom}}$",
                    "s_calc,max": r"s$_{\mathrm{calc,max}}$",
                    "s_adopt": r"s$_{\mathrm{adopt}}$",
                    "s_actual": r"s$_{\mathrm{actual}}$"}
    for ax in getattr(fig, "axes", []):
        for artist in ax.texts:
            label = artist.get_text()
            for source, target in replacements.items():
                label = label.replace(source, target)
            artist.set_text(label)
    return fig


def draw_gwp_breakdown(result: Dict[str, Any]):
    """Publication-style stacked bar for the selected member A1–A3 GWP."""
    concrete = max(0.0, _f(result.get("GWP_concrete_column_kgCO2e")))
    steel = max(0.0, _f(result.get("GWP_steel_column_kgCO2e")))
    total = concrete + steel
    fig, ax = plt.subplots(figsize=(8.6, 2.15))
    ax.barh([0], [concrete], color=APP_PALETTE["blue"], height=0.38,
            edgecolor=APP_PALETTE["white"], linewidth=0.6, label="Concrete")
    ax.barh([0], [steel], left=[concrete], color=APP_PALETTE["muted"], height=0.38,
            edgecolor=APP_PALETTE["white"], linewidth=0.6, label="Reinforcement")
    if concrete > 0 and concrete / max(total, 1e-12) > 0.12:
        ax.text(concrete / 2, 0, f"{concrete:,.1f}", ha="center", va="center",
                color="white", fontsize=9, fontweight="bold")
    if steel > 0 and steel / max(total, 1e-12) > 0.12:
        ax.text(concrete + steel / 2, 0, f"{steel:,.1f}", ha="center", va="center",
                color="white", fontsize=9, fontweight="bold")
    ax.set_yticks([0], ["3.0-m column segment"])
    ax.set_xlabel(r"A1–A3 GWP (kg CO$_2$-eq)")
    ax.set_xlim(0, max(total * 1.12, 1.0))
    ax.grid(axis="x", color=APP_PALETTE["grid"], linewidth=0.65)
    ax.set_axisbelow(True)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_visible(False)
    ax.spines["bottom"].set_color(APP_PALETTE["grid"])
    ax.tick_params(axis="y", length=0, colors=APP_PALETTE["ink"])
    ax.tick_params(axis="x", colors=APP_PALETTE["muted"])
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, 1.28), ncol=2,
              frameon=False, fontsize=8.5)
    fig.tight_layout()
    return fig


def _sort_comparison_rows(rows: Sequence[Dict[str, Any]], sort_by: str,
                          descending: bool = False) -> List[Dict[str, Any]]:
    rows = list(rows)
    def status_feasible(row):
        return str(row.get("Status", "")).startswith("FEASIBLE")
    def section_width(row):
        value = str(row.get("Square-section width b (mm)", ""))
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)", value)
        return float(match.group(1)) if match else float("inf")
    def number(row, key):
        try:
            value = float(row.get(key))
            return value if math.isfinite(value) else float("inf")
        except (TypeError, ValueError):
            return float("inf")
    if sort_by == "Study order":
        return rows
    if sort_by == "Feasibility":
        feasible = sorted([r for r in rows if status_feasible(r)],
                          key=lambda r: number(r, "Column GWP (kg CO₂-eq)"), reverse=descending)
        infeasible = [r for r in rows if not status_feasible(r)]
        return feasible + infeasible
    if sort_by == "Concrete GWP intensity":
        key_fn = lambda r: number(r, "Concrete GWP (kg CO₂-eq/m³)")
    elif sort_by == "Column GWP":
        key_fn = lambda r: number(r, "Column GWP (kg CO₂-eq)")
    elif sort_by == "Section size":
        key_fn = section_width
    elif sort_by == "Reinforcement mass":
        key_fn = lambda r: number(r, "Steel (kg)")
    else:
        return rows
    # Keep infeasible / missing values at the bottom in either sort direction.
    def stable_sort_key(row):
        value = key_fn(row)
        missing = not math.isfinite(value)
        return (1 if missing else 0, 0.0 if missing else (-value if descending else value))
    return sorted(rows, key=stable_sort_key)


def draw_comparison_gwp(rows: Sequence[Dict[str, Any]], mix_db: Dict[str, Dict[str, Any]]):
    """Coordinated comparison of concrete GWP intensity and total column GWP."""
    if not rows:
        return None
    labels = [str(r.get("Mix_ID", "—")) for r in rows]
    concrete_values = [_f(r.get("Concrete GWP (kg CO₂-eq/m³)"), float("nan")) for r in rows]
    column_values = [_f(r.get("Column GWP (kg CO₂-eq)"), float("nan")) for r in rows]
    colours = [BINDER_GROUP_COLORS.get(mix_db.get(mid, {}).get("Binder_Group", "Ternary"), APP_PALETTE["muted"])
               for mid in labels]
    x = np.arange(len(labels))
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12.8, 5.0), sharex=True,
                                   gridspec_kw={"height_ratios": [1.0, 1.15]})
    ax1.bar(x, concrete_values, width=0.72, color=colours, edgecolor="white", linewidth=0.55)
    ax1.set_ylabel("Concrete GWP intensity\n(kg CO$_2$-eq/m$^3$)", fontsize=8.5, labelpad=9)
    ax1.set_title("Material intensity and member-level impact", loc="left", fontsize=11, fontweight="bold", color=APP_PALETTE["navy"])
    ax1.grid(axis="y", color=APP_PALETTE["grid"], linewidth=0.65)
    ax1.set_axisbelow(True)
    ax2.bar(x, column_values, width=0.72, color=colours, edgecolor="white", linewidth=0.55)
    for i, value in enumerate(column_values):
        if not np.isfinite(value):
            ax2.text(i, 0.02, "n/a", ha="center", va="bottom", rotation=90,
                     color=APP_PALETTE["red"], fontsize=7.5, fontweight="bold")
    ax2.set_ylabel("Total column GWP\n(kg CO$_2$-eq per 3.0-m segment)", fontsize=8.5, labelpad=9)
    ax2.set_xlabel("Study mixture")
    ax2.set_xticks(x, labels)
    ax2.grid(axis="y", color=APP_PALETTE["grid"], linewidth=0.65)
    ax2.set_axisbelow(True)
    group_handles = [
        Patch(facecolor=BINDER_GROUP_COLORS[g], edgecolor="none", label=g)
        for g in ("Reference", "GGBFS-only", "SF-only", "Ternary")
        if any(mix_db.get(mid, {}).get("Binder_Group") == g for mid in labels)
    ]
    fig.legend(handles=group_handles, loc="upper right", bbox_to_anchor=(0.985, 0.99),
               ncol=max(1, len(group_handles)), frameon=False, fontsize=8.0)
    for ax in (ax1, ax2):
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.spines["left"].set_color(APP_PALETTE["grid"])
        ax.spines["bottom"].set_color(APP_PALETTE["grid"])
        ax.tick_params(colors=APP_PALETTE["muted"], labelsize=8)
    fig.subplots_adjust(left=0.155, right=0.985, top=0.88, bottom=0.12, hspace=0.22)
    return fig



# ---------------------------------------------------------------------------
# User-facing labels for compact calculation status codes.
# ---------------------------------------------------------------------------
def _display_result_code(value: Any) -> str:
    raw = str(value or "—")
    labels = {
        "INCLUDED_BY_NOMINAL_CURVATURE": "Included by nominal-curvature method",
        "NOT_REQUIRED_BY_SLENDERNESS_CRITERION": "Not required by the slenderness criterion",
        "EN_1992-1-1_Annex_G_Table_G1": "EN 1992-1-1:2023, Annex G, Table G.1",
        "FEASIBLE_SCREENING_WITH_LOCAL_SECOND_ORDER_AND_DETAILING_CHECKS": "Feasible; second-order and detailing checks included",
        "FEASIBLE_SCREENING_WITH_DETAILING_CHECKS": "Feasible; detailing checks included",
        "FEASIBLE_SCREENING": "Feasible",
        "NO_FEASIBLE_LAYOUT": "No feasible layout",
    }
    if raw in labels:
        return labels[raw]
    return raw.replace("_", " ").strip()


# ---------------------------------------------------------------------------
# Comprehensive PDF report (uses the existing result and drawing functions)
# ---------------------------------------------------------------------------
def _report_structural_check_rows(result: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    axial = _f(result.get("axial_ratio_r"))
    biax = _f(result.get("biaxial_interaction_utilization"))
    rows = [
        ("Axial resistance", f"N_Ed/N_Rd,screen = {axial:.3f}", "PASS" if axial < 1 else "CHECK"),
        ("Biaxial interaction", f"Utilization = {biax:.3f}", "PASS" if biax <= 1 else "CHECK"),
        ("Longitudinal reinforcement ratio", f"{_f(result.get('rho_longitudinal_pct')):.2f}%", str(result.get("longitudinal_reinforcement_ratio_check", "—"))),
        ("Clear bar spacing", f"{_f(result.get('clear_bar_spacing_mm')):.1f} mm (minimum {_f(result.get('minimum_clear_spacing_mm')):.1f} mm)", str(result.get("bar_spacing_check", "—"))),
        ("Bar pitch", f"{_f(result.get('centre_bar_pitch_mm')):.1f} mm", str(result.get("longitudinal_bar_pitch_check", "—"))),
        ("Biaxial moment resistance", f"MEd,x { _f(result.get('M_Ed_x_checked_kNm')):.1f}/{_f(result.get('M_Rd_x_at_N_kNm')):.1f}; MEd,y {_f(result.get('M_Ed_y_checked_kNm')):.1f}/{_f(result.get('M_Rd_y_at_N_kNm')):.1f} kN·m", "PASS" if biax <= 1 else "CHECK"),
        ("Second-order effects", f"M2,x = {_f(result.get('second_order_moment_x_kNm')):.1f}; M2,y = {_f(result.get('second_order_moment_y_kNm')):.1f} kN·m; {_display_result_code(result.get('second_order_status'))}", str(result.get("second_order_gate_status", "—"))),
        ("Shear design route", f"{_display_result_code(result.get('shear_method'))}; cot θ = {_f(result.get('cot_theta')):.2f}; VEd,x/y = {_f(result.get('M_reverse_shear_envelope_x_kN')):.1f}/{_f(result.get('M_reverse_shear_envelope_y_kN')):.1f} kN", str(result.get("shear_check_status", "—"))),
        ("Compression-field check", f"τEd = {_f(result.get('tau_Ed_resultant_MPa')):.3f} MPa; chord ratio = {_f(result.get('compression_chord_depth_ratio')):.3f}; ftdy = {_f(result.get('f_tdy_optimum_MPa')):.2f} MPa", str(result.get("compression_strut_check", "—"))),
        ("Anchorage and lap", f"lbd = {_f(result.get('l_bd_mm')):.0f} mm; lsd = {_f(result.get('l_sd_mm')):.0f} mm", str(result.get("lap_anchorage_check", "—"))),
        ("Transverse reinforcement", f"Ø{_f(result.get('link_diameter_mm')):.0f}; {int(result.get('number_of_links', 0))} link levels; {int(result.get('cross_ties_total', 0))} cross-ties", str(result.get("tie_spacing_check", "—"))),
    ]
    for label, key_actual, key_adopt, key_limit in (
        ("End-zone link spacing", "end_zone_spacing_actual_mm", "s_end_adopted_spacing_mm", "smax_end_limit_mm"),
        ("Intermediate-zone link spacing", "middle_zone_spacing_actual_mm", "s_mid_adopted_spacing_mm", "smax_mid_limit_mm"),
        ("Lap-zone link spacing", "lap_zone_spacing_actual_mm", "s_lap_adopted_spacing_mm", "smax_lap_limit_mm"),
    ):
        actual, adopted, limit = (_f(result.get(k)) for k in (key_actual, key_adopt, key_limit))
        rows.append((label, f"Actual {actual:.1f} mm; adopted {adopted:.0f} mm; calculated limit {limit:.1f} mm", "PASS" if actual <= adopted + 1e-6 and adopted <= limit + 1e-6 else "CHECK"))
    return rows


def _report_zone_rows(result: Dict[str, Any]) -> List[Tuple[str, str, str, str, str]]:
    return [
        ("End zones (2)", f"{_f(result.get('end_zone_length_each_mm')):.0f}", f"{_f(result.get('smax_end_limit_mm')):.0f}", f"{_f(result.get('s_end_adopted_spacing_mm')):.0f}", f"{_f(result.get('end_zone_spacing_actual_mm')):.1f}"),
        ("Intermediate zones (2)", f"{_f(result.get('middle_zone_length_each_mm')):.0f}", f"{_f(result.get('smax_mid_limit_mm')):.0f}", f"{_f(result.get('s_mid_adopted_spacing_mm')):.0f}", f"{_f(result.get('middle_zone_spacing_actual_mm')):.1f}"),
        ("Lap region (1)", f"{_f(result.get('lap_zone_length_mm')):.0f}", f"{_f(result.get('smax_lap_limit_mm')):.0f}", f"{_f(result.get('s_lap_adopted_spacing_mm')):.0f}", f"{_f(result.get('lap_zone_spacing_actual_mm')):.1f}"),
    ]


def _build_comprehensive_report_pdf(result: Dict[str, Any], input_payload: Dict[str, Any],
                                    mix_label: str, comparison_rows: Optional[Sequence[Dict[str, Any]]] = None,
                                    mix_db: Optional[Dict[str, Dict[str, Any]]] = None) -> bytes:
    """Create a multi-page PDF report with the actual selected layout and checks."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak
        from xml.sax.saxutils import escape
    except ImportError as exc:
        raise RuntimeError("PDF report generation requires the reportlab package. Install it with: pip install reportlab") from exc

    output = io.BytesIO()
    # Use a Unicode-capable font where available so scientific symbols and subscripts
    # remain readable in the exported report. Fall back to PDF core fonts otherwise.
    font_regular, font_bold = "Helvetica", "Helvetica-Bold"
    font_candidates = [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
        ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ]
    for regular_path, bold_path in font_candidates:
        if Path(regular_path).is_file() and Path(bold_path).is_file():
            try:
                pdfmetrics.registerFont(TTFont("SCCUnicode", regular_path))
                pdfmetrics.registerFont(TTFont("SCCUnicode-Bold", bold_path))
                font_regular, font_bold = "SCCUnicode", "SCCUnicode-Bold"
                break
            except Exception:
                continue
    navy = colors.HexColor("#203B56")
    blue = colors.HexColor("#3568A8")
    pale = colors.HexColor("#EEF3F7")
    grid = colors.HexColor("#D7E0E8")
    muted = colors.HexColor("#586B7D")
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="ReportTitle", parent=styles["Title"], fontName=font_bold, fontSize=19, leading=23, textColor=navy, alignment=TA_LEFT, spaceAfter=5))
    styles.add(ParagraphStyle(name="ReportSub", parent=styles["Normal"], fontName=font_regular, fontSize=9, leading=13, textColor=muted, spaceAfter=8))
    styles.add(ParagraphStyle(name="ReportH2", parent=styles["Heading2"], fontName=font_bold, fontSize=12.5, leading=16, textColor=navy, spaceBefore=8, spaceAfter=6))
    styles.add(ParagraphStyle(name="ReportBody", parent=styles["BodyText"], fontName=font_regular, fontSize=8.2, leading=11, textColor=colors.HexColor("#263445")))
    styles.add(ParagraphStyle(name="ReportSmall", parent=styles["BodyText"], fontName=font_regular, fontSize=7.2, leading=9, textColor=muted))
    styles.add(ParagraphStyle(name="ReportCell", parent=styles["BodyText"], fontName=font_regular, fontSize=7.1, leading=9, textColor=colors.HexColor("#263445")))
    styles.add(ParagraphStyle(name="ReportCellHead", parent=styles["BodyText"], fontName=font_bold, fontSize=7.2, leading=9, textColor=colors.white))
    styles.add(ParagraphStyle(name="ReportStatus", parent=styles["BodyText"], fontName=font_bold, fontSize=7.1, leading=9, textColor=navy, alignment=TA_CENTER))

    def P(value, style="ReportCell"):
        display = _format_formula_text(str(value))
        display = display.replace("EN_1992-1-1_Annex_G_Table_G1", "EN 1992-1-1:2023 Annex G, Table G.1")
        display = display.replace("INCLUDED_BY_NOMINAL_CURVATURE", "Included by nominal curvature")
        return Paragraph(escape(display).replace("\n", "<br/>"), styles[style])

    def make_table(data, widths, header=True, font=7.1):
        converted = []
        for ri, row in enumerate(data):
            converted.append([P(v, "ReportCellHead" if (header and ri == 0) else "ReportCell") for v in row])
        table = Table(converted, colWidths=widths, repeatRows=1 if header else 0, hAlign="LEFT")
        commands = [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("GRID", (0, 0), (-1, -1), 0.35, grid),
        ]
        if header:
            commands.append(("BACKGROUND", (0, 0), (-1, 0), navy))
            for r in range(1, len(data)):
                if r % 2 == 0:
                    commands.append(("BACKGROUND", (0, r), (-1, r), pale))
        table.setStyle(TableStyle(commands))
        return table

    def footer(canvas, doc):
        canvas.saveState()
        w, h = A4
        canvas.setStrokeColor(grid)
        canvas.setLineWidth(0.5)
        canvas.line(16*mm, 13*mm, w-16*mm, 13*mm)
        canvas.setFont(font_regular, 7)
        canvas.setFillColor(muted)
        canvas.drawString(16*mm, 8.5*mm, "SCC Column Design & GWP Assessment · Member-level design and GWP")
        canvas.drawRightString(w-16*mm, 8.5*mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(output, pagesize=A4, rightMargin=16*mm, leftMargin=16*mm,
                            topMargin=15*mm, bottomMargin=19*mm,
                            title="Reinforced-concrete column design and GWP report",
                            author="SCC Column Design & GWP Assessment")
    story = []
    story.append(Paragraph("Column design and environmental assessment", styles["ReportTitle"]))
    story.append(Paragraph("Selected 3.0-m reinforced-concrete column segment · Product-stage A1–A3 GWP", styles["ReportSub"]))
    story.append(Paragraph("Selected design", styles["ReportH2"]))
    raw_status = str(result.get("Status", "—"))
    display_status = "Feasible" if raw_status.startswith("FEASIBLE") else _display_result_code(raw_status)
    design_rows = [
        ("Mixture", mix_label),
        ("Status", display_status),
        ("Section", f"{_f(result.get('b_mm')):.0f} × {_f(result.get('h_mm')):.0f} mm"),
        ("Longitudinal reinforcement", f"{int(result.get('longitudinal_bars_n', 0))} bars · Ø{_f(result.get('longitudinal_bar_diameter_mm')):.0f} mm"),
        ("Transverse reinforcement", f"Ø{_f(result.get('link_diameter_mm')):.0f} mm · {int(result.get('number_of_links', 0))} link levels · {int(result.get('cross_ties_total', 0))} cross-ties"),
        ("Nominal cover", f"{_f(result.get('c_nom_mm')):.1f} mm"),
        ("Reinforcement mass", f"{_f(result.get('total_steel_mass_kg')):.2f} kg"),
        ("Column GWP", f"{_f(result.get('GWP_column_kgCO2e')):.1f} kg CO₂-eq"),
        ("Concrete / steel GWP", f"{_f(result.get('GWP_concrete_column_kgCO2e')):.1f} / {_f(result.get('GWP_steel_column_kgCO2e')):.1f} kg CO₂-eq"),
        ("Actions", f"N_Ed = {_f(result.get('N_Ed_kN')):.1f} kN; M_Ed,x = {_f(result.get('M_Ed_x_first_order_kNm')):.1f}; M_Ed,y = {_f(result.get('M_Ed_y_first_order_kNm')):.1f} kN·m"),
        ("Scenario", f"{result.get('storeys', input_payload.get('storeys', '—'))} stories · {input_payload.get('target_years', '—')}-year corrosion-initiation target"),
    ]
    story.append(make_table([["Item", "Selected value"]] + [list(r) for r in design_rows], [48*mm, 130*mm]))
    story.append(Spacer(1, 7))
    story.append(PageBreak())
    story.append(Paragraph("Reproducible input record", styles["ReportH2"]))
    input_pairs = _input_summary_pairs(input_payload)
    story.append(make_table([["Input", "Value"]] + [list(r) for r in input_pairs], [48*mm, 130*mm]))
    story.append(Spacer(1, 6))

    # Reinforcement detail generated from the current calculation result.
    story.append(PageBreak())
    story.append(Paragraph("Reinforcement layout and link-spacing zones", styles["ReportH2"]))
    detail_fig = _polish_formula_artists(draw_design_detail(result))
    detail_buffer = io.BytesIO()
    detail_fig.savefig(detail_buffer, format="png", dpi=260, bbox_inches="tight", facecolor="white")
    plt.close(detail_fig)
    detail_buffer.seek(0)
    story.append(Image(detail_buffer, width=178*mm, height=94*mm, kind="proportional"))
    story.append(Spacer(1, 8))
    story.append(Paragraph("Regional transverse-reinforcement schedule", styles["ReportH2"]))
    zone_header = ["Region", "Length (mm)", "Calculated limit (mm)", "Adopted spacing (mm)", "Actual pitch (mm)"]
    story.append(make_table([zone_header] + [list(r) for r in _report_zone_rows(result)], [48*mm, 28*mm, 34*mm, 34*mm, 34*mm]))
    total_intervals = 2*int(result.get("end_zone_intervals_each", 0)) + 2*int(result.get("middle_zone_intervals_each", 0)) + int(result.get("lap_zone_intervals", 0))
    story.append(Spacer(1, 6))
    story.append(Paragraph(f"Interval audit: {total_intervals} intervals and {int(result.get('number_of_links', 0))} link levels. Actual equalized pitches are reported separately for end, intermediate, and lap regions.", styles["ReportSmall"]))

    story.append(PageBreak())
    story.append(Paragraph("Structural response and member checks", styles["ReportH2"]))
    checks = _report_structural_check_rows(result)
    story.append(make_table([["Check", "Value", "Status"]] + [list(row) for row in checks], [43*mm, 112*mm, 23*mm]))
    story.append(Spacer(1, 8))
    story.append(Paragraph("Interpretation", styles["ReportH2"]))
    story.append(Paragraph("PASS denotes a satisfied design criterion; CHECK identifies a criterion flagged by the member-design checks.", styles["ReportBody"]))

    story.append(PageBreak())
    story.append(Paragraph("Product-stage A1–A3 global warming potential", styles["ReportH2"]))
    gwp_fig = draw_gwp_breakdown(result)
    gwp_buffer = io.BytesIO()
    gwp_fig.savefig(gwp_buffer, format="png", dpi=260, bbox_inches="tight", facecolor="white")
    plt.close(gwp_fig)
    gwp_buffer.seek(0)
    story.append(Image(gwp_buffer, width=178*mm, height=47*mm, kind="proportional"))
    story.append(Spacer(1, 8))
    gwp_rows = [
        ("Concrete volume", f"{_f(result.get('concrete_volume_m3')):.4f} m³"),
        ("Concrete GWP intensity", f"{_f(result.get('GWP_concrete_kgCO2e_m3')):.2f} kg CO₂-eq/m³"),
        ("Concrete contribution", f"{_f(result.get('GWP_concrete_column_kgCO2e')):.2f} kg CO₂-eq"),
        ("Reinforcement mass", f"{_f(result.get('total_steel_mass_kg')):.2f} kg"),
        ("Steel emission factor", f"{_f(result.get('App_steel_emission_factor_kgCO2e_kg')):.3f} kg CO₂-eq/kg"),
        ("Reinforcement contribution", f"{_f(result.get('GWP_steel_column_kgCO2e')):.2f} kg CO₂-eq"),
        ("Total column GWP", f"{_f(result.get('GWP_column_kgCO2e')):.2f} kg CO₂-eq"),
    ]
    story.append(make_table([["Component", "Quantity / result"]] + [list(r) for r in gwp_rows], [65*mm, 113*mm]))
    story.append(Spacer(1, 8))

    story.append(Spacer(1, 8))
    story.append(Paragraph("Functional unit: one 3.0-m reinforced-concrete column segment. Member GWP combines concrete and reinforcement contributions; concrete GWP intensity is reported per cubic metre.", styles["ReportSmall"]))

    if comparison_rows:
        story.append(PageBreak())
        story.append(Paragraph("Comparison of the 16 study mixtures", styles["ReportH2"]))
        if mix_db:
            comp_fig = draw_comparison_gwp(comparison_rows, mix_db)
            if comp_fig is not None:
                comp_buffer = io.BytesIO()
                comp_fig.savefig(comp_buffer, format="png", dpi=250, bbox_inches="tight", facecolor="white")
                plt.close(comp_fig)
                comp_buffer.seek(0)
                story.append(Image(comp_buffer, width=178*mm, height=68*mm, kind="proportional"))
                story.append(Spacer(1, 6))
        comp_rows = [["Mix", "Section", "Longitudinal bars", "Steel (kg)", "Column GWP (kg CO₂-eq)", "Status / note"]]
        for row in comparison_rows:
            raw_row_status = str(row.get("Status", "—"))
            row_status = ("Feasible" if raw_row_status.startswith("FEASIBLE") else
                          "No feasible layout" if raw_row_status == "NO_FEASIBLE_LAYOUT" else
                          raw_row_status.replace("_", " ").title())
            steel_value = row.get("Steel (kg)")
            gwp_value = row.get("Column GWP (kg CO₂-eq)")
            comp_rows.append([
                row.get("Mix_ID", "—"), row.get("Square-section width b (mm)", "—"),
                row.get("Longitudinal bars", "—"), f"{_f(steel_value):.1f}" if steel_value is not None else "—",
                f"{_f(gwp_value):.1f}" if gwp_value is not None else "—", row_status
            ])
        story.append(make_table(comp_rows, [16*mm, 27*mm, 30*mm, 20*mm, 53*mm, 32*mm]))

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


# ---------------------------------------------------------------------------
# One-click export of all selected-design and comparison results
# ---------------------------------------------------------------------------
def _build_results_bundle_zip(result: Dict[str, Any], input_payload: Dict[str, Any],
                              mix_label: str, *, input_widget_values: Optional[Dict[str, Any]] = None,
                              alternatives: Optional[Sequence[Dict[str, Any]]] = None,
                              comparison_rows: Optional[Sequence[Dict[str, Any]]] = None,
                              mix_db: Optional[Dict[str, Dict[str, Any]]] = None) -> bytes:
    """Bundle the current calculated record, reports, tables, and drawings."""
    output = io.BytesIO()
    mix_db = mix_db or {}
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        pdf_bytes = _build_comprehensive_report_pdf(
            result, input_payload, mix_label,
            comparison_rows=comparison_rows, mix_db=mix_db
        )
        archive.writestr("01_Comprehensive_Design_Report.pdf", pdf_bytes)
        archive.writestr("02_Selected_Inputs_Preset.json", _make_preset_json(input_widget_values))
        archive.writestr("03_Selected_Design.csv", _csv_bytes([result]))
        if alternatives:
            archive.writestr("04_Feasible_Alternatives.csv", _csv_bytes(list(alternatives)))

        zone_rows = [
            {"Region": r[0], "Length (mm)": r[1], "Calculated limit (mm)": r[2],
             "Adopted spacing (mm)": r[3], "Actual pitch (mm)": r[4]}
            for r in _report_zone_rows(result)
        ]
        archive.writestr("05_Regional_Transverse_Reinforcement.csv", _csv_bytes(zone_rows))
        check_rows = [
            {"Check": label, "Value": value, "Status": status}
            for label, value, status in _report_structural_check_rows(result)
        ]
        archive.writestr("06_Structural_Checks.csv", _csv_bytes(check_rows))
        gwp_rows = [
            {"Component": "Concrete", "Quantity": _f(result.get("concrete_volume_m3")), "Unit": "m³",
             "GWP": _f(result.get("GWP_concrete_column_kgCO2e")), "GWP unit": "kg CO₂-eq"},
            {"Component": "Reinforcement", "Quantity": _f(result.get("total_steel_mass_kg")), "Unit": "kg",
             "GWP": _f(result.get("GWP_steel_column_kgCO2e")), "GWP unit": "kg CO₂-eq"},
            {"Component": "Total column", "Quantity": "—", "Unit": "3.0-m column segment",
             "GWP": _f(result.get("GWP_column_kgCO2e")), "GWP unit": "kg CO₂-eq"},
        ]
        archive.writestr("07_GWP_Breakdown.csv", _csv_bytes(gwp_rows))

        detail_fig = _polish_formula_artists(draw_design_detail(result))
        archive.writestr("08_Reinforcement_Detail.png", _download_figure(detail_fig, "png"))
        archive.writestr("09_Reinforcement_Detail.pdf", _download_figure(detail_fig, "pdf"))
        plt.close(detail_fig)
        gwp_fig = draw_gwp_breakdown(result)
        archive.writestr("10_GWP_Breakdown.png", _download_figure(gwp_fig, "png"))
        archive.writestr("11_GWP_Breakdown.pdf", _download_figure(gwp_fig, "pdf"))
        plt.close(gwp_fig)

        if comparison_rows:
            archive.writestr("12_16_Mixture_Comparison.csv", _csv_bytes(list(comparison_rows)))
            comparison_fig = draw_comparison_gwp(comparison_rows, mix_db)
            if comparison_fig is not None:
                archive.writestr("13_16_Mixture_Comparison.png", _download_figure(comparison_fig, "png"))
                archive.writestr("14_16_Mixture_Comparison.pdf", _download_figure(comparison_fig, "pdf"))
                plt.close(comparison_fig)

        manifest = (
            "SCC Column Design & GWP Assessment - complete results export\n"
            "Contains the comprehensive PDF report, the input preset corresponding to the selected design, "
            "selected-design and optional alternatives CSV files, transverse-reinforcement schedule, "
            "structural checks, GWP breakdown, and PNG/PDF figures. The 16-mixture comparison is included "
            "only when a comparison run matching the selected design's common analysis settings is available.\n"
            "Assessment basis: one 3.0-m square column segment under prescribed member actions; "
            "environmental results cover product-stage modules A1-A3.\n"
        )
        archive.writestr("00_README.txt", manifest)
    return output.getvalue()


# ---------------------------------------------------------------------------
# Streamlit interface — article-based calculation application
# ---------------------------------------------------------------------------
ARTICLE_TITLE = (
    "Linking chloride migration to column-level global warming potential in "
    "self-compacting concrete containing blast-furnace slag and silica fume"
)
ARTICLE_AUTHORS = "Farhad D. Kabiri¹, Vahab Toufigh²,*"
ARTICLE_AFFILIATIONS = (
    "¹ Graduate Student, Department of Civil Engineering, Sharif University of Technology, Tehran, Iran  ·  "
    "² Professor, Department of Civil Engineering, Sharif University of Technology, Tehran, Iran"
)
ARTICLE_ABSTRACT = (
    "Sixteen self-compacting concrete (SCC) mixtures containing ground granulated blast-furnace slag "
    "(GGBFS) and silica fume (SF) were tested for compressive strength, water absorption, electrical "
    "resistivity, and chloride migration after 90 days of curing. Mean 28-day cube strengths informed "
    "column design; migration coefficients informed a fib-based corrosion-initiation assessment. "
    "M16 (40% GGBFS, 13% SF) had a mean 28-day cube strength of 62.87 MPa (5.5% above M01) and the "
    "lowest migration coefficient, 0.414 × 10⁻¹² m²/s (96.4% below M01). All 144 3.0-m column designs "
    "were feasible across 5-, 15-, and 30-story scenarios and 50-, 75-, and 100-year initiation "
    "targets. For structural class S4 and marine-atmospheric exposure XS1, nominal cover was "
    "45.0–53.5 mm. M16 also had the lowest A1–A3 concrete GWP intensity (313.21 kg CO₂-eq/m³), "
    "32.8% below M01. At 75 years, M16, M15, and M15 minimized column GWP for 5-, 15-, and "
    "30-story scenarios, with savings of 24.8%, 22.5%, and 22.4% relative to M01. Scaling both "
    "prescribed first-order bending-moment components to 0.8–1.2 times baseline shifted the "
    "30-story optimum from M15 to M13. Column GWP also depended on section dimensions and "
    "reinforcement mass; the lowest-intensity mixture did not always minimize member GWP. "
    "An interactive web application implements the workflow."
)

# Calculation resources are deployment-side resources. They are deliberately
# not exposed as user-facing dataset/file tabs or upload controls.
engine_path = discover_engine_file()
workbook_path = discover_workbook_file()
_RESOURCE_SIGNATURE = hashlib.sha256(json.dumps({
    "engine": _resource_fingerprint(engine_path),
    "workbook": _resource_fingerprint(workbook_path),
}, sort_keys=True).encode("utf-8")).hexdigest()
ENGINE_READY = False
ENGINE_ERROR = None
_ENGINE = None
MIX_DB = {}
ENGINE_VERSION = "not loaded"
if engine_path is not None and workbook_path is not None:
    try:
        _ENGINE, MIX_DB = load_engine_and_database(
            str(workbook_path), str(engine_path),
            _resource_fingerprint(workbook_path), _resource_fingerprint(engine_path)
        )
        ENGINE_VERSION = str(getattr(_ENGINE, "THEME_VERSION", "unknown"))
        ENGINE_READY = True
    except Exception as exc:
        ENGINE_ERROR = "The application could not be initialized."
else:
    missing = []
    if engine_path is None:
        missing.append("Rev99 calculation engine")
    if workbook_path is None:
        missing.append("current study mixture workbook")
    ENGINE_ERROR = "The application could not be initialized."

# Main navigation: the guide is intentionally the final tab.
tab_overview, tab_design, tab_guide = st.tabs([
    "Study overview & design basis",
    "Column design & GWP",
    "User guide",
])

with tab_overview:
    st.markdown(_tab_hero(
        "overview", "STRUCTURAL MATERIALS · DURABILITY · ENVIRONMENTAL PERFORMANCE",
        "Study overview and design basis",
        "Research context, assessment scope, standards, and baseline assumptions behind the member-level design and A1–A3 GWP model.",
        ["EN 1992-1-1:2023", "fib-based cover model", "A1–A3 GWP", "5 · 15 · 30 stories", "16 SCC mixtures"],
    ), unsafe_allow_html=True)
    intro_left, intro_right = st.columns([1.08, 0.92], gap="large")
    with intro_left:
        with st.container(border=True):
            st.markdown('<div class="panel-heading blue">RESEARCH ARTICLE · Study background and principal findings</div>', unsafe_allow_html=True)
            st.markdown(f"### {ARTICLE_TITLE}")
            st.markdown(
                '<div class="author-list">'
                '<div class="author-name">Farhad D. Kabiri<sup>1</sup></div>'
                '<div class="affiliation-line"><sup>1</sup> Graduate Student, Department of Civil Engineering, Sharif University of Technology, Tehran, Iran</div>'
                '<div class="author-name">Vahab Toufigh<sup>2</sup>,* <span class="corresponding-tag">Corresponding author</span></div>'
                '<div class="affiliation-line"><sup>2</sup> Professor, Department of Civil Engineering, Sharif University of Technology, Tehran, Iran</div>'
                '</div>', unsafe_allow_html=True
            )
        with st.container(border=True):
            st.subheader("Abstract")
            st.write(ARTICLE_ABSTRACT)
            st.markdown(
                "**Keywords:** Chloride migration; Corrosion initiation; Concrete cover; "
                "Service-life prediction; Life cycle assessment; Reinforced-concrete column; Self-compacting concrete."
            )
    with intro_right:
        with st.container(border=True):
            st.markdown('<div class="panel-heading green">APPLICATION OVERVIEW · Purpose and workflow</div>', unsafe_allow_html=True)
            st.markdown(
                '<p>The workspace links concrete-mixture properties and chloride-transport inputs to nominal-cover estimation, square-column design, reinforcement detailing, and product-stage A1–A3 GWP.</p>'
                '<p>Choose a study or user-defined mixture, specify member actions and geometry, and select reinforcement candidates and design objectives. Results include the selected layout, material quantities, zone-specific link spacing, member checks, and column GWP.</p>'
                '<p class="subtle">Member-level design and environmental assessment for square reinforced-concrete column segments under the prescribed action scenarios.</p>',
                unsafe_allow_html=True,
            )
        st.markdown(
            '<div class="workflow-strip">'
            '<div class="workflow-card"><b>01 · Materials</b><span>Mixture composition, strength, migration, and GWP factors.</span></div>'
            '<div class="workflow-card"><b>02 · Actions</b><span>Axial force, biaxial moments, height, and effective length.</span></div>'
            '<div class="workflow-card"><b>03 · Design</b><span>Cover, section search, discrete bar sizes, and checks.</span></div>'
            '<div class="workflow-card"><b>04 · Assessment</b><span>Reinforcement drawing, quantities, checks, and A1–A3 GWP.</span></div>'
            '</div>', unsafe_allow_html=True
        )
    st.markdown(
        '<div style="display:flex;flex-wrap:wrap;gap:.55rem;margin:.15rem 0 .65rem 0;">'
        '<span style="background:#555555;color:#fff;border-radius:999px;padding:.22rem .62rem;font-size:.76rem;">Reference</span>'
        '<span style="background:#3568A8;color:#fff;border-radius:999px;padding:.22rem .62rem;font-size:.76rem;">GGBFS-only</span>'
        '<span style="background:#C44E52;color:#fff;border-radius:999px;padding:.22rem .62rem;font-size:.76rem;">SF-only</span>'
        '<span style="background:#3A8D68;color:#fff;border-radius:999px;padding:.22rem .62rem;font-size:.76rem;">Ternary</span>'
        '</div>', unsafe_allow_html=True
    )
    st.divider()
    with st.container(border=True):
        st.subheader("Study at a glance")
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("SCC mixtures", "16", "4 × 4 GGBFS–SF matrix")
        k2.metric("Loading scenarios", "5 / 15 / 30", "stories")
        k3.metric("Initiation targets", "50 / 75 / 100", "years")
        k4.metric("Functional unit", "3.0 m", "column segment")
        st.markdown(
            "Member-level GWP combines concrete GWP intensity with the concrete volume and "
            "reinforcement mass required for the selected column segment."
        )

        workflow_figure = APP_DIR / "Application_Workflow_Figure_A1.png"
        if workflow_figure.is_file():
            st.image(str(workflow_figure), caption="Application structure and calculation workflow", use_container_width=True)

    st.divider()
    with st.container(border=True):
        st.markdown('<div class="section-ribbon"><span class="step">SCOPE</span> Assessment scope</div>', unsafe_allow_html=True)
        st.markdown(
            "**Structural scope.** The functional unit is one 3.0-m square reinforced-concrete column "
            "segment. The 5-, 15-, and 30-story scenarios are represented by their prescribed member-level "
            "action sets. Member design covers axial and biaxial actions, resistance, stability, shear, and "
            "reinforcement detailing."
        )
        st.markdown(
            "**Durability scope.** The 90-day non-steady-state chloride migration coefficient, "
            "Dₙₛₛₘ,90, is used in the fib-based model to estimate effective cover for 50-, 75-, and "
            "100-year corrosion-initiation targets. Nominal cover, cₙₒₘ, incorporates the adopted code "
            "minimum and execution allowance."
        )
        st.markdown(
            "**Environmental scope.** The assessment reports product-stage GWP for modules A1–A3. "
            "Column GWP combines concrete and reinforcement contributions. Concrete GWP intensity is "
            "expressed per cubic metre, whereas member GWP is expressed per 3.0-m column segment."
        )

    with st.container(border=True):
        st.markdown('<div class="section-ribbon"><span class="step">BASIS</span> Standards and methodological references</div>', unsafe_allow_html=True)
        standards = [
            {"Area": "Structural actions", "Reference": "EN 1990; EN 1991-1-1; EN 1991-1-4", "Application in the study": "ULS combination, floor actions, and framework for wind actions; first-order moments remain prescribed study inputs."},
            {"Area": "Member design and detailing", "Reference": "EN 1992-1-1:2023; EN 10080:2005", "Application in the study": "Resistance, second-order effects, reinforcement limits, anchorage, lap, shear, and transverse reinforcement checks; weldable reinforcing steel."},
            {"Area": "Concrete exposure and cover", "Reference": "EN 206:2013+A2:2021", "Application in the study": "XS1 exposure classification and durability-cover basis."},
            {"Area": "Chloride transport", "Reference": "NT BUILD 492; fib Model Code 2010; fib Bulletin 76", "Application in the study": "90-day migration testing and fib-based corrosion-initiation / age-dependency modelling."},
            {"Area": "Life-cycle assessment", "Reference": "ISO 14040; ISO 14044", "Application in the study": "LCA principles, framework, requirements, and reporting."},
            {"Area": "Climate-change impact assessment", "Reference": "IPCC 2021 GWP100", "Application in the study": "Climate-change characterization for the A1–A3 product-stage assessment."},
        ]
        st.dataframe(standards, use_container_width=True, hide_index=True)

    with st.container(border=True):
        st.markdown('<div class="section-ribbon"><span class="step">INPUTS</span> Baseline design assumptions</div>', unsafe_allow_html=True)
        assumptions = [
            {"Parameter": "Column segment", "Baseline value": "3.0 m", "Unit / note": "Pinned-equivalent end restraint in both principal planes"},
            {"Parameter": "Tributary area", "Baseline value": "36", "Unit / note": "m² per column"},
            {"Parameter": "Characteristic floor loads", "Baseline value": "Gₖ = 6.0; Qₖ = 2.5", "Unit / note": "kN/m²"},
            {"Parameter": "ULS partial factors", "Baseline value": "γ₍G₎ = 1.35; γ₍Q₎ = 1.50", "Unit / note": "EN 1990 combination adopted in the study"},
            {"Parameter": "Concrete unit weight", "Baseline value": "25", "Unit / note": "kN/m³; column-stack self-weight included"},
            {"Parameter": "First-order moments (Mₑd,x / Mₑd,y)", "Baseline value": "150/60; 450/180; 900/360", "Unit / note": "kN·m for 5-, 15-, and 30-story scenarios"},
            {"Parameter": "Effective length", "Baseline value": "l₀ = 3.0", "Unit / note": "m in both principal planes"},
            {"Parameter": "Concrete strength conversion", "Baseline value": "0.80 × mean 28-day cube strength", "Unit / note": "Used to derive the mixture-specific design-strength input"},
            {"Parameter": "Reference material strengths", "Baseline value": "fᵧₖ = 500; f꜀ₖ,ref = 40", "Unit / note": "MPa; γ_s = 1.15; γ_c = 1.50"},
            {"Parameter": "Longitudinal reinforcement limits", "Baseline value": "4% outside laps; 8% in lap zones", "Unit / note": "Member-design detailing criteria"},
            {"Parameter": "Second-order calculation", "Baseline value": "φₑff,b = 2.0; kᵣ = 1.0; c₁/r = 8; eᵢ = l₀/400", "Unit / note": "Nominal-curvature method under EN 1992-1-1:2023 Annex O.7 where required"},
            {"Parameter": "Shear design", "Baseline value": "γ_v = 1.40; cot θ = 3.0; d_g,max = 9", "Unit / note": "Axial-force-dependent shear-field checks; selected designs use Annex G, Table G.1 where applicable"},
            {"Parameter": "Confinement and link detailing", "Baseline value": "k = 0.02; 20-mm clear gap; 50-mm spacing grid", "Unit / note": "Regional end, intermediate, and lap-zone spacing; calculated limits rounded down"},
            {"Parameter": "Transverse-link spacing increment", "Baseline value": "50 mm; round down", "Unit / note": "Each governing regional maximum is rounded down to the next 50-mm increment before interval counts are calculated"},
            {"Parameter": "Durability cover", "Baseline value": "S4/XS1: 35 + 10; S6/XS1: 45 + 10", "Unit / note": "mm minimum durability cover + execution allowance; S6 is the 100-year assessment case"},
            {"Parameter": "Chloride model", "Baseline value": "k_t = 0.10; α = 0.30; C_crit = 0.6; C_s = 3.0; C₀ = 0.1", "Unit / note": "Chloride concentrations in wt.% of cement mass; α dimensionless"},
            {"Parameter": "Chloride model temperature and age", "Baseline value": "T = 290 K; T_ref = 293 K; 4800 K; t_ref = 90 days", "Unit / note": "fib temperature and aging terms; reference age matches RCMT age"},
            {"Parameter": "Baseline search space", "Baseline value": "300–1500; 50-mm increments", "Unit / note": "Square sections; standard candidates are Ø20/Ø25 longitudinal bars and Ø6/Ø8/Ø10 links"},
            {"Parameter": "Concrete stress-block factor", "Baseline value": "kₜ꜀ = 0.85", "Unit / note": "Used in the mixture-specific design-strength proxy"},
            {"Parameter": "Reinforcement steel GWP", "Baseline value": "1.85", "Unit / note": "kg CO₂-eq/kg; sensitivity values 1.71 and 2.00"},
        ]
        st.dataframe(assumptions, use_container_width=True, hide_index=True)

        with st.expander("Constituent-level concrete GWP coefficients", expanded=False):
            st.caption("These coefficients represent the constituent contributions used in the study-mixture assessment. For other material sources, enter source-specific factors.")
            coefficient_rows = [
                {"Constituent": name, "E_F (kg CO₂-eq/kg)": value}
                for name, value in DEFAULT_EFS.items()
            ]
            st.dataframe(coefficient_rows, use_container_width=True, hide_index=True)

        st.info("User-entered values override the corresponding baseline assumptions for each design run.")

with tab_design:
    # A mixture selected in the comparison table is applied before its widgets render.
    pending_mix = st.session_state.pop("_scc_pending_mixture_transfer", None)
    if pending_mix in MIX_DB:
        st.session_state["scc_input__mixture_source_choice"] = "Study mixtures"
        st.session_state[_stable_input_key("selectbox", "Study mixture")] = pending_mix
        _clear_selected_design_result()
        st.session_state["_scc_transfer_notice"] = pending_mix

    transferred_mix_notice = st.session_state.pop("_scc_transfer_notice", None)
    if transferred_mix_notice:
        st.success(f"{transferred_mix_notice} is selected in the design workspace. Review its inputs and run Calculate / Update design.")

    st.markdown(_tab_hero(
        "design", "COLUMN DESIGN WORKSPACE",
        "Column design and GWP",
        "Set material properties, structural actions, durability cover, and reinforcement candidates; then review the selected member layout and A1–A3 environmental result.",
        ["Member-level design", "300–1500 mm sections", "Ø20/Ø25 bars · Ø6/8/10 links", "A1–A3 GWP"],
    ), unsafe_allow_html=True)
    with st.expander("Input presets · save, load, or reset", expanded=False):
        preset_left, preset_right = st.columns([1.2, 1.0])
        with preset_left:
            preset_upload = st.file_uploader(
                "Load a saved input preset (.json)", type=["json"],
                key="scc_input_preset_upload", help="Restores the input values saved in a previous preset. Run the calculation again to refresh results."
            )
            load_preset_clicked = st.button("Load preset", key="scc_load_input_preset", disabled=preset_upload is None)
            if load_preset_clicked and preset_upload is not None:
                try:
                    imported_values = _read_preset_json(preset_upload.getvalue())
                    st.session_state["_scc_input_preset_values"] = dict(imported_values)
                    for input_key, input_value in imported_values.items():
                        st.session_state[input_key] = input_value
                    _clear_design_results()
                    st.success("Preset loaded. Review the restored inputs and run the calculation when ready.")
                    st.rerun()
                except Exception as exc:
                    st.error("This preset could not be loaded. Check that it is a valid input preset exported by this application.")
        with preset_right:
            st.download_button(
                "Download current input preset (JSON)", data=_make_preset_json(),
                file_name="SCC_Column_Design_Input_Preset.json", mime="application/json",
                use_container_width=True,
                help="Exports the current widget inputs so they can be restored in a later session."
            )
            reset_clicked = st.button("Reset to study defaults", key="scc_reset_all_inputs", use_container_width=True)
            if reset_clicked:
                st.session_state["_scc_confirm_reset_inputs"] = True
        if st.session_state.get("_scc_confirm_reset_inputs", False):
            st.warning("This will replace all edited inputs with the study defaults and clear the selected design and comparison results.")
            confirm_col, cancel_col = st.columns(2)
            if confirm_col.button("Confirm reset", key="scc_confirm_reset_inputs", type="primary", use_container_width=True):
                for state_key in list(st.session_state.keys()):
                    if isinstance(state_key, str) and state_key.startswith(PERSISTENT_WIDGET_PREFIX):
                        st.session_state.pop(state_key, None)
                st.session_state["_scc_input_preset_values"] = {}
                st.session_state["_scc_input_default_values"] = {}
                st.session_state["_scc_input_widget_labels"] = {}
                st.session_state["_scc_confirm_reset_inputs"] = False
                _clear_design_results()
                st.rerun()
            if cancel_col.button("Cancel", key="scc_cancel_reset_inputs", use_container_width=True):
                st.session_state["_scc_confirm_reset_inputs"] = False
                st.rerun()

    with st.container(key="design-commandbar"):
        command_left, command_compare, command_note = st.columns([1.35, 1.0, 1.65])
        run = command_left.button(
            "Calculate / Update design", type="primary", use_container_width=True,
            disabled=not ENGINE_READY,
            help="Run the column-design search using the current input values and refresh the selected design."
        )
        benchmark_button = command_compare.button(
            "Compare 16 mixtures", use_container_width=True,
            disabled=not ENGINE_READY,
            help="Run the selected objective for all 16 study mixtures under the same structural inputs."
        )
        command_note.caption("After changing inputs, recalculate to refresh the displayed result.")
    left_col, right_col = st.columns([1.04, 0.96], gap="large")
    with left_col:
        if not ENGINE_READY:
            st.error("The design workspace could not be initialized. Reload the page to continue.")
            # Internal resource details are intentionally not exposed in the public interface.
        else:
            with st.container(border=True):
                st.markdown('<div class="section-ribbon blue"><span class="step">01</span> Concrete mixture and material properties</div>', unsafe_allow_html=True)
                c1, c2, c3 = st.columns([1.1, 1.0, 1.0])
                with c1:
                    mix_choice = st.radio(
                        "Mixture source", ["Study mixtures", "User-defined mixture"],
                        horizontal=True, key="scc_input__mixture_source_choice"
                    )
                    if mix_choice == "Study mixtures":
                        mid = st.selectbox("Study mixture", list(MIX_DB.keys()), index=0,
                                           format_func=lambda x: f"{x} – {MIX_DB[x]['Mixture_Code']}")
                        default_recipe = dict(MIX_DB[mid])
                        mix_label = f"{mid} – {MIX_DB[mid]['Mixture_Code']}"
                    else:
                        mid = "USER-MIX"
                        default_recipe = {
                            "cement_kg_m3": 350.0, "ggbfs_kg_m3": 60.0, "sf_kg_m3": 20.0,
                            "lp_kg_m3": 142.0, "fine_aggregate_kg_m3": 1040.0,
                            "coarse_aggregate_kg_m3": 500.0, "water_kg_m3": 185.0,
                            "PCE_kg_m3": 3.5,
                        }
                        mix_label = "User-defined mixture"
                with c2:
                    default_fcube = _f(default_recipe.get("f_cube_28_MPa"), 60.0)
                    f_cube_28 = st.number_input("Mean 28-day cube strength f꜀ᵤᵦₑ,mean (MPa)", min_value=1.0,
                                                max_value=150.0, value=float(default_fcube), step=0.5,
                                                help="The study model converts the measured mean cube strength to a comparative cylinder-strength proxy. For a user-defined mixture, enter the measured value.",
                                                key=f"scc_input__fcube_{mid}")
                    if mix_choice == "Study mixtures":
                        st.metric("Concrete GWP intensity (A1–A3)", f"{_f(default_recipe.get('GWP_SimaPro_kgCO2e_m3')):.2f} kg CO₂-eq/m³")
                with c3:
                    if mix_choice == "Study mixtures":
                        default_d = _f(default_recipe.get("Dnssm_90d_1e-12_m2_s"), 0.0)
                        d_sd = default_recipe.get("Dnssm_SD_1e-12_m2_s")
                        st.metric("Dₙₛₛₘ,90", f"{default_d:.3f} × 10⁻¹² m²/s")
                        if d_sd is not None:
                            st.caption(f"Specimen SD: {_f(d_sd):.3f} × 10⁻¹² m²/s")
                    else:
                        default_d = 0.0
                        default_d = st.number_input("Dₙₛₛₘ,90 (×10⁻¹² m²/s; optional)", min_value=0.0,
                                                    max_value=100.0, value=0.0, step=0.1,
                                                    help="Enter a measured migration coefficient to estimate cover. Leave at zero to use a manually specified cover.",
                                                    key=f"scc_input__dnssm_optional_{mid}")

                st.markdown("**Mixture proportions (kg/m³)**")
                mix_cols = st.columns(4)
                recipe: Dict[str, float] = {}
                mix_items = list(COMPONENT_KEYS.items())
                for i, (component, key) in enumerate(mix_items):
                    with mix_cols[i % 4]:
                        recipe[key] = st.number_input(
                            component, min_value=0.0, max_value=3000.0,
                            value=float(_f(default_recipe.get(key), 0.0)), step=1.0,
                            key=f"scc_input__recipe_{key}_{mix_choice}_{mid}"
                        )
                if sum(recipe.values()) <= 0:
                    st.error("Mixture proportions cannot all be zero.")
                    st.stop()

                efs: Dict[str, float] = dict(DEFAULT_EFS)
                with st.expander("Advanced: component emission factors", expanded=False):
                    ef_cols = st.columns(4)
                    for i, comp in enumerate(COMPONENT_KEYS):
                        with ef_cols[i % 4]:
                            efs[comp] = st.number_input(
                                f"{comp} EF (kg CO₂-eq/kg)", min_value=0.0, max_value=100.0,
                                value=float(DEFAULT_EFS[comp]), step=0.001, format="%.5f", key=f"scc_input__ef_{comp}"
                            )

                factor_derived_gwp, component_gwp = gwp_from_recipe(recipe, efs)
                reference_gwp = _f(default_recipe.get("GWP_SimaPro_kgCO2e_m3"), 0.0) if mix_choice == "Study mixtures" else 0.0
                # Use the stored SimaPro product-system result for the 16 study
                # mixtures. The reconstructed constituent factors remain an
                # explicit cross-check and are used for user-defined mixtures.
                concrete_gwp = reference_gwp if reference_gwp > 0 else factor_derived_gwp
                g1, g2, g3 = st.columns(3)
                g1.metric("Applied concrete GWP", f"{concrete_gwp:.2f} kg CO₂-eq/m³")
                if reference_gwp > 0:
                    delta = factor_derived_gwp - reference_gwp
                    g2.metric("Constituent-based estimate", f"{factor_derived_gwp:.2f} kg CO₂-eq/m³", delta=f"{delta:+.3f}")
                    g3.metric("Assessment boundary", "A1–A3 product stage")
                else:
                    g2.metric("Total mixture mass", f"{sum(recipe.values()):.1f} kg/m³")
                    g3.metric("GWP boundary", "A1–A3")
                with st.expander("Mixture-level GWP contribution by constituent", expanded=False):
                    comp_rows = [
                        {"Constituent": comp, "Mass (kg/m³)": recipe[key],
                         "EF (kg CO₂-eq/kg)": efs[comp],
                         "GWP contribution (kg CO₂-eq/m³)": component_gwp[comp]}
                        for comp, key in COMPONENT_KEYS.items()
                    ]
                    st.dataframe(comp_rows, use_container_width=True, hide_index=True)

            st.divider()
            with st.container(border=True):
                st.markdown('<div class="section-ribbon red"><span class="step">02</span> Structural actions and column geometry</div>', unsafe_allow_html=True)
                load_col1, load_col2, load_col3 = st.columns(3)
                with load_col1:
                    load_mode = st.radio("Axial-load input", ["Automatic from tributary floors", "Manual N_Ed"],
                                         horizontal=True,
                                         format_func=lambda value: "Manual Nₑd" if value == "Manual N_Ed" else value)
                    storeys = st.number_input("Number of stories n", min_value=1, max_value=100,
                                              value=5, step=1)
                    clear_height_m = st.number_input("Clear height h_s (m)", min_value=1.0,
                                                     max_value=10.0, value=3.0, step=0.1)
                    effective_length_m = st.number_input("Effective length l₀ (m)", min_value=0.5,
                                                          max_value=20.0, value=3.0, step=0.1)
                with load_col2:
                    if load_mode == "Automatic from tributary floors":
                        tributary_area = st.number_input("Tributary area (m²)", min_value=0.1,
                                                          max_value=500.0, value=36.0, step=1.0)
                        dead_load = st.number_input("Characteristic floor dead load Gₖ (kN/m²)", min_value=0.0,
                                                    max_value=50.0, value=6.0, step=0.1)
                        live_load = st.number_input("Characteristic floor live load Qₖ (kN/m²)", min_value=0.0,
                                                    max_value=50.0, value=2.5, step=0.1)
                    else:
                        manual_n_ed = st.number_input("Total design axial force Nₑd (kN)", min_value=1.0,
                                                      max_value=200000.0, value=2235.5, step=10.0,
                                                      help="Enter the total design axial action at the column segment, including the effects you intend to model.")
                        tributary_area, dead_load, live_load = 36.0, 6.0, 2.5
                    gamma_G = st.number_input("ULS factor γ₍G₎", min_value=1.0, max_value=2.0,
                                              value=1.35, step=0.05)
                    gamma_Q = st.number_input("ULS factor γ₍Q₎", min_value=1.0, max_value=2.0,
                                              value=1.50, step=0.05)
                with load_col3:
                    scenario_defaults = {5: (150.0, 60.0), 15: (450.0, 180.0), 30: (900.0, 360.0)}
                    m_default = scenario_defaults.get(int(storeys), (30.0*float(storeys), 12.0*float(storeys)))
                    mx_kNm = st.number_input("First-order moment Mₑd,x (kN·m)", min_value=0.0,
                                             max_value=100000.0, value=float(m_default[0]), step=10.0,
                                             key=f"scc_input__moment_x_{int(storeys)}")
                    my_kNm = st.number_input("First-order moment Mₑd,y (kN·m)", min_value=0.0,
                                             max_value=100000.0, value=float(m_default[1]), step=10.0,
                                             key=f"scc_input__moment_y_{int(storeys)}")
                    unit_weight = st.number_input("Concrete unit weight (kN/m³)", min_value=10.0,
                                                  max_value=35.0, value=25.0, step=0.5)
                    st.caption("First-order moments are prescribed, editable study inputs: 150/60, 450/180, and 900/360 kN·m for the 5-, 15-, and 30-story scenarios. For other story counts, initial values are linearly scaled; review them before calculation. Automatic Nₑd includes 1.35G + 1.50Q and trial column-stack self-weight.")

            st.divider()
            with st.container(border=True):
                st.markdown('<div class="section-ribbon green"><span class="step">03</span> Durability and nominal cover</div>', unsafe_allow_html=True)
                cover_mode = st.radio(
                    "Cover input", ["Estimate from chloride migration", "Enter nominal cover directly"],
                    index=(0 if mix_choice == "Study mixtures" else 1), horizontal=True
                )
                cover_basis = st.radio(
                    "Minimum durability-cover basis",
                    ["S4/XS1 baseline", "S6/XS1 cross-check (100-year only)"],
                    horizontal=True,
                    help="The article baseline uses 35 mm for S4/XS1. The separate S6/XS1 check uses 45 mm for the 100-year case."
                )
                if cover_basis == "S6/XS1 cross-check (100-year only)":
                    target_years = 100
                    minimum_cover_override = float(_ENGINE.COLUMN_ASSUMPTIONS["S6_XS1_min_durability_cover_mm_separate_crosscheck"])
                    st.info("S6/XS1 cross-check selected: the corrosion-initiation target is set to 100 years and the minimum durability cover is 45 mm.")
                else:
                    target_years = st.selectbox("Corrosion-initiation target (years)", [50, 75, 100], index=1)
                    minimum_cover_override = None
                if cover_mode == "Estimate from chloride migration":
                    cover_col1, cover_col2, cover_col3 = st.columns(3)
                    with cover_col1:
                        if mix_choice == "Study mixtures":
                            dnssm = _f(default_recipe.get("Dnssm_90d_1e-12_m2_s"), 0.0)
                            dnssm = st.number_input("Dₙₛₛₘ,90 (×10⁻¹² m²/s)", min_value=0.001,
                                                    max_value=100.0, value=max(0.001, dnssm), step=0.01,
                                                    key=f"scc_input__dnssm_cover_{mid}")
                        else:
                            dnssm = st.number_input("Dₙₛₛₘ,90 (×10⁻¹² m²/s)", min_value=0.001,
                                                    max_value=100.0, value=max(0.001, _f(default_d, 0.001)), step=0.01,
                                                    key=f"scc_input__dnssm_cover_{mid}")
                    with cover_col2:
                        alpha = st.number_input("Aging exponent α", min_value=0.05, max_value=1.0,
                                                value=0.30, step=0.05)
                        ccrit = st.number_input("Critical chloride concentration C₍crit₎ (wt.% of cement mass)", min_value=0.1,
                                                max_value=2.0, value=0.60, step=0.05)
                    with cover_col3:
                        cs = st.number_input("Surface chloride concentration Cₛ (wt.% of cement mass)", min_value=0.2,
                                             max_value=10.0, value=3.0, step=0.1)
                        kt = st.number_input("Transfer factor kₜ", min_value=0.01, max_value=1.0,
                                             value=0.10, step=0.01)
                        execution_allowance = st.number_input("Execution allowance (mm)", min_value=0.0,
                                                               max_value=30.0, value=10.0, step=1.0)
                    try:
                        c_req_mm = float(_ENGINE._cover_model_for_input(
                            float(dnssm), int(target_years), alpha=float(alpha),
                            ccrit=float(ccrit), cs=float(cs), kt_v=float(kt)
                        ))
                        if not math.isfinite(c_req_mm):
                            raise ValueError("The chloride-cover model returned a non-finite value. Check the input parameters.")
                        cover_nominal = None  # finalized after the user selects bar-diameter candidates
                        st.caption("The final nominal cover is calculated after selecting the longitudinal-bar diameter candidates.")
                    except Exception as exc:
                        st.error("The cover could not be calculated from the current inputs. Review the migration coefficient and chloride parameters.")
                        st.stop()
                else:
                    c_req_mm = None
                    cover_nominal = st.number_input("Nominal cover cₙₒₘ to outside of link (mm)", min_value=10.0,
                                                    max_value=150.0, value=45.0, step=1.0)
                    execution_allowance = 10.0

            st.divider()
            with st.container(border=True):
                st.markdown('<div class="section-ribbon orange"><span class="step">04</span> Design search and reinforcement preferences</div>', unsafe_allow_html=True)
                dcol1, dcol2, dcol3 = st.columns(3)
                with dcol1:
                    section_mode = st.radio("Section definition", ["Search section range", "Fixed square section"],
                                           horizontal=True)
                    if section_mode == "Fixed square section":
                        fixed_section = st.number_input("Fixed square-section width (mm)", min_value=300,
                                                         max_value=1500, value=450, step=25)
                        min_section, max_section, section_step = int(fixed_section), int(fixed_section), 50
                    else:
                        fixed_section = None
                        min_section = st.number_input("Minimum section width (mm)", min_value=300,
                                                       max_value=1500, value=300, step=50)
                        max_section = st.number_input("Maximum section width (mm)", min_value=300,
                                                       max_value=1500, value=1500, step=50)
                        section_step = st.selectbox("Section increment (mm)", [25, 50, 100], index=1)
                with dcol2:
                    objective_options = [
                        "Minimum total column GWP",
                        "Smallest feasible section, then minimum steel",
                        "Minimum reinforcement mass",
                        "Pareto alternatives (GWP vs steel mass)",
                        "Show all feasible alternatives",
                    ]
                    if section_mode == "Fixed square section":
                        objective_options = ["Fixed section – minimum total GWP",
                                             "Fixed section – minimum reinforcement mass"]
                    objective_label = st.selectbox("Design objective", objective_options)
                    if objective_label in ("Minimum total column GWP", "Fixed section – minimum total GWP"):
                        objective = "min_gwp"
                    elif objective_label == "Smallest feasible section, then minimum steel":
                        objective = "smallest_section"
                    elif objective_label == "Minimum reinforcement mass":
                        objective = "Minimum reinforcement mass"
                    elif objective_label == "Pareto alternatives (GWP vs steel mass)":
                        objective = "Pareto alternatives"
                    elif objective_label == "Show all feasible alternatives":
                        objective = "Show all feasible alternatives"
                    elif objective_label == "Fixed section – minimum reinforcement mass":
                        objective = "Fixed section – minimum reinforcement mass"
                    else:
                        objective = "Fixed section – minimum GWP"
                with dcol3:
                    steel_ef_choice = st.selectbox("Steel emission factor (E₍F,s₎)", [1.71, 1.85, 2.00, "Custom"], index=1,
                                                   format_func=lambda x: f"{x:.2f} kg CO₂-eq/kg" if isinstance(x, (int, float)) else x)
                    if steel_ef_choice == "Custom":
                        steel_ef = st.number_input("Custom steel EF (kg CO₂-eq/kg)", min_value=0.01,
                                                   max_value=10.0, value=1.85, step=0.01)
                    else:
                        steel_ef = float(steel_ef_choice)
                    st.markdown("**Reinforcement diameters as discrete design variables**")
                    st.caption("Default candidates are Ø20/Ø25 longitudinal bars and Ø6/Ø8/Ø10 links. Additional diameters extend the search beyond the study candidates.")
                    allowed_long_standard = st.multiselect(
                        "Longitudinal-bar diameter candidates Øₗ (mm)",
                        STANDARD_LONGITUDINAL_DIAMETERS_MM, default=[20, 25],
                        help="The standard study search uses Ø20 and Ø25 mm. Custom values are exploratory extensions."
                    )
                    custom_long_text = st.text_input(
                        "Additional exploratory longitudinal-bar diameters (mm; comma-separated)",
                        value="", placeholder="e.g. 28, 32",
                        help="Optional additional integer nominal diameters; entering values extends the standard study search space."
                    )
                    allowed_link_standard = st.multiselect(
                        "Link diameter candidates Øᵥ (mm)",
                        STANDARD_LINK_DIAMETERS_MM, default=[6, 8, 10],
                        help="The minimum link-to-longitudinal-bar diameter requirement is also checked."
                    )
                    custom_link_text = st.text_input(
                        "Additional exploratory link diameters (mm; comma-separated)",
                        value="", placeholder="e.g. 12, 14",
                        help="Optional additional integer link diameters; entering values extend the standard study search space."
                    )
                    try:
                        allowed_long = parse_diameter_candidates(
                            allowed_long_standard, custom_long_text, label="Longitudinal-bar diameters",
                            min_mm=8, max_mm=50, max_candidates=8
                        )
                        allowed_links = parse_diameter_candidates(
                            allowed_link_standard, custom_link_text, label="Link diameters",
                            min_mm=6, max_mm=25, max_candidates=6
                        )
                        st.caption(f"Longitudinal-bar candidates: Ø{', Ø'.join(map(str, allowed_long))} mm  ·  Link candidates: Ø{', Ø'.join(map(str, allowed_links))} mm")
                    except ValueError as exc:
                        st.error(str(exc))
                        st.stop()
                    bar_count_choice = st.selectbox("Longitudinal bar count", ["Any feasible count", 4, 8, 12, 16, 20, 24, 28])
                    required_bar_count = None if bar_count_choice == "Any feasible count" else int(bar_count_choice)

                if cover_mode == "Estimate from chloride migration":
                    # Nominal cover accounts for the maximum selected longitudinal-bar size.
                    c_req_mm, cover_nominal = derive_nominal_cover(
                        _ENGINE, int(target_years), float(dnssm), float(alpha), float(ccrit),
                        float(cs), float(kt), float(max(allowed_long)), float(execution_allowance),
                        minimum_cover_override_mm=minimum_cover_override
                    )
                    st.success(
                        f"Required effective cover cᵣₑq = {c_req_mm:.1f} mm  ·  "
                        f"nominal cover cₙₒₘ = {cover_nominal:.1f} mm  ·  "
                        f"largest selected longitudinal bar = Ø{max(allowed_long)} mm."
                    )

                with st.expander("Advanced design parameters", expanded=False):
                    assumptions = _ENGINE.COLUMN_ASSUMPTIONS
                    a1, a2, a3, a4 = st.columns(4)
                    with a1:
                        max_aggregate = st.number_input("Maximum aggregate size (mm)", min_value=4.0,
                                                        max_value=32.0, value=float(assumptions["max_aggregate_size_mm"]), step=1.0)
                        max_reinf_ratio = st.number_input("Maximum longitudinal ratio outside laps (%)",
                                                          min_value=1.0, max_value=4.0, value=4.0, step=0.5,
                                                          help="The search cap is 4% outside lap regions; the design checks the 8% lap-zone limit separately.")
                    with a2:
                        gamma_s = st.number_input("Steel partial factor γ₍s₎", min_value=1.0,
                                                  max_value=1.5, value=float(assumptions["gamma_s"]), step=0.05)
                        gamma_c = st.number_input("Concrete partial factor γ₍c₎", min_value=1.0,
                                                  max_value=2.0, value=float(assumptions["gamma_c"]), step=0.05)
                    with a3:
                        fyk = st.number_input("Reinforcement yield strength fᵧₖ (MPa)",
                                              min_value=300.0, max_value=700.0,
                                              value=float(assumptions["f_yk_MPa"]), step=10.0)
                        k_tc = st.number_input("Concrete stress-block factor kₜ꜀", min_value=0.50,
                                               max_value=1.00, value=float(assumptions["k_tc"]), step=0.05)
                    with a4:
                        gamma_v = st.number_input("Shear factor γv", min_value=1.0, max_value=2.0,
                                                  value=float(assumptions["gamma_v"]), step=0.05)
                        cot_theta_shear = st.number_input("Shear-field cot θ", min_value=1.0, max_value=3.0,
                                                          value=float(assumptions["cot_theta_shear"]), step=0.1)
                        minimum_confinement_factor_k = st.number_input(
                            "Minimum confinement factor k", min_value=0.005, max_value=0.10,
                            value=float(assumptions["minimum_confinement_factor_k"]), step=0.005, format="%.3f")
                        effective_creep_ratio_local = st.number_input(
                            "Effective creep ratio φ_eff", min_value=0.0, max_value=4.0,
                            value=float(assumptions["effective_creep_ratio_local"]), step=0.1)

                st.info("The search covers **square sections from 300 to 1500 mm** and discrete reinforcement candidates. Checks include axial and biaxial resistance, second-order effects, reinforcement and lap limits, clear spacing, anchorage, member shear, and compression-field requirements. Custom diameters extend the standard study candidates.")

            signature_payload = {
                "mixture_source": mix_choice, "mixture_id": mid, "mixture_recipe": dict(recipe),
                "f_cube_28": f_cube_28, "concrete_gwp": concrete_gwp, "component_gwp": component_gwp,
                "component_efs": dict(efs),
                "load_mode": load_mode, "storeys": storeys, "clear_height_m": clear_height_m,
                "effective_length_m": effective_length_m, "tributary_area": tributary_area,
                "dead_load": dead_load, "live_load": live_load, "manual_n_ed": manual_n_ed if load_mode == "Manual N_Ed" else None,
                "gamma_G": gamma_G, "gamma_Q": gamma_Q, "mx_kNm": mx_kNm, "my_kNm": my_kNm,
                "unit_weight": unit_weight, "cover_mode": cover_mode, "cover_basis": cover_basis,
                "minimum_cover_override": minimum_cover_override, "target_years": target_years,
                "cover_required_effective": c_req_mm,
                "dnssm": dnssm if cover_mode == "Estimate from chloride migration" else None,
                "alpha": alpha if cover_mode == "Estimate from chloride migration" else None,
                "ccrit": ccrit if cover_mode == "Estimate from chloride migration" else None,
                "cs": cs if cover_mode == "Estimate from chloride migration" else None,
                "kt": kt if cover_mode == "Estimate from chloride migration" else None,
                "execution_allowance": execution_allowance, "cover_nominal": cover_nominal,
                "section_mode": section_mode, "fixed_section": fixed_section,
                "min_section": min_section, "max_section": max_section, "section_step": section_step,
                "objective": objective, "objective_label": objective_label, "steel_ef": steel_ef, "allowed_long": allowed_long,
                "allowed_links": allowed_links, "required_bar_count": required_bar_count,
                "max_aggregate": max_aggregate, "max_reinf_ratio": max_reinf_ratio,
                "gamma_s": gamma_s, "gamma_c": gamma_c, "fyk": fyk, "gamma_v": gamma_v,
                "k_tc": k_tc, "cot_theta_shear": cot_theta_shear,
                "minimum_confinement_factor_k": minimum_confinement_factor_k,
                "effective_creep_ratio_local": effective_creep_ratio_local,
            }
            current_design_signature = hashlib.sha256(
                json.dumps(signature_payload, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            current_benchmark_signature = _benchmark_signature(signature_payload)
            validation_errors, validation_warnings = _validate_design_inputs(signature_payload)
            with st.expander("Current input summary and validation", expanded=False):
                if validation_errors:
                    for validation_message in validation_errors:
                        st.error(validation_message)
                elif validation_warnings:
                    st.warning("Inputs passed the basic checks. Review the notes below before calculation.")
                else:
                    st.success("Basic input checks passed.")
                for validation_message in validation_warnings:
                    st.info(validation_message)
                if validation_errors:
                    st.markdown("**Fields requiring correction**")
                    st.dataframe([{"Field-level validation message": message} for message in validation_errors],
                                 use_container_width=True, hide_index=True)
                st.caption("Labels identify whether each value comes from the study, user input, or an application calculation.")
                st.dataframe(
                    _input_summary_table_rows(signature_payload),
                    use_container_width=True, hide_index=True
                )
            previous_inputs = st.session_state.get("last_design_input_summary")
            if previous_inputs and st.session_state.get("last_design_input_signature") != current_design_signature:
                changed_groups = _changed_input_groups(previous_inputs, signature_payload)
                if changed_groups:
                    st.warning("Modified since the last calculation: " + "; ".join(changed_groups) + ". Recalculate to refresh the selected design and its report.")

            design_result_is_current = (
                st.session_state.get("last_design_input_signature") == current_design_signature
                and st.session_state.get("last_design_app_version") == APP_VERSION
                and st.session_state.get("last_design_resource_signature") == _RESOURCE_SIGNATURE
                and "last_design_result" in st.session_state
            )
            if run and design_result_is_current:
                st.info("The selected design already matches these inputs. The existing calculated result has been reused.")
            elif run and validation_errors:
                st.error("The design was not run because one or more input checks failed. Correct the items above and try again.")
            elif run:
                try:
                    # Update only the advanced model inputs exposed above; all other model
                    # assumptions remain the study baseline assumptions.
                    # Advanced model settings are applied in a temporary context and then restored.
                    # The context manager is nested to preserve the temporary user choices.
                    with configured_engine(_ENGINE, {
                        "gamma_s": float(gamma_s), "gamma_c": float(gamma_c),
                        "f_yk_MPa": float(fyk), "gamma_v": float(gamma_v),
                    }):
                        result, alternatives, candidate_sections = run_design(
                            _ENGINE,
                            f_cube_28=float(f_cube_28), cover_nominal_mm=float(cover_nominal),
                            mix_name=mix_label, target_years=int(target_years),
                            gwp_concrete=float(concrete_gwp), steel_ef=float(steel_ef),
                            storeys=int(storeys), mx_kNm=float(mx_kNm), my_kNm=float(my_kNm),
                            clear_height_m=float(clear_height_m), effective_length_m=float(effective_length_m),
                            load_mode=load_mode, tributary_area_m2=float(tributary_area),
                            dead_load_kN_m2=float(dead_load), live_load_kN_m2=float(live_load),
                            gamma_G=float(gamma_G), gamma_Q=float(gamma_Q),
                            unit_weight_kN_m3=float(unit_weight),
                            manual_n_ed_kN=(float(manual_n_ed) if load_mode == "Manual N_Ed" else None),
                            section_mode=section_mode,
                            min_section_mm=int(min_section), max_section_mm=int(max_section),
                            section_step_mm=int(section_step),
                            fixed_section_mm=(int(fixed_section) if fixed_section is not None else None),
                            long_bar_diameters=[int(x) for x in allowed_long],
                            link_diameters=[int(x) for x in allowed_links],
                            required_bar_count=required_bar_count,
                            max_aggregate_size_mm=float(max_aggregate), max_reinf_ratio=float(max_reinf_ratio),
                            k_tc=float(k_tc), cot_theta_shear=float(cot_theta_shear),
                            minimum_confinement_factor_k=float(minimum_confinement_factor_k),
                            effective_creep_ratio_local=float(effective_creep_ratio_local),
                            objective=objective,
                        )
                    # run_design has exited its configuration context; gamma_s/f_yk etc
                    # are restored by the outer context too.
                    if result is None:
                        st.error("No feasible layout was found for the selected inputs and constraints. Expand the section range, review the load/cover inputs, or allow additional bar/link options.")
                    else:
                        st.session_state["last_design_result"] = result
                        st.session_state["last_design_alternatives"] = alternatives
                        st.session_state["last_design_mix_gwp"] = concrete_gwp
                        st.session_state["last_design_components"] = component_gwp
                        st.session_state["last_design_cover_req"] = c_req_mm
                        st.session_state["last_design_target_years"] = int(target_years)
                        st.session_state["last_design_mix_label"] = mix_label
                        st.session_state["last_design_storeys"] = int(storeys)
                        st.session_state["last_design_input_signature"] = current_design_signature
                        st.session_state["last_design_app_version"] = APP_VERSION
                        st.session_state["last_design_resource_signature"] = _RESOURCE_SIGNATURE
                        st.session_state["last_design_input_summary"] = dict(signature_payload)
                        st.session_state["last_design_widget_inputs"] = _collect_current_preset_values()
                        st.session_state.pop("_scc_results_bundle_zip", None)
                        st.session_state.pop("_scc_results_bundle_signature", None)
                        st.success("A feasible layout was found for the selected inputs.")
                except Exception:
                    st.error("The design could not be completed with the current inputs. Review the values and try again.")

            benchmark_result_is_current = (
                st.session_state.get("last_benchmark_input_signature") == current_benchmark_signature
                and st.session_state.get("last_benchmark_app_version") == APP_VERSION
                and st.session_state.get("last_benchmark_resource_signature") == _RESOURCE_SIGNATURE
                and len(st.session_state.get("last_benchmark_rows", [])) == len(MIX_DB)
            )
            if benchmark_button and benchmark_result_is_current:
                st.info("The 16-mixture comparison already matches these inputs. The existing results have been reused.")
            elif benchmark_button and validation_errors:
                st.error("The mixture comparison was not run because one or more input checks failed. Correct the items above and try again.")
            elif benchmark_button:
                benchmark_rows = []
                progress = st.progress(0.0, text="Preparing mixture comparison…")
                try:
                    for i, (benchmark_mid, rec) in enumerate(MIX_DB.items(), start=1):
                        gwp_i, _ = gwp_from_recipe(rec, efs)
                        if mix_choice == "Study mixtures":
                            gwp_i = _f(rec.get("GWP_SimaPro_kgCO2e_m3"), gwp_i) or gwp_i
                        if cover_mode == "Estimate from chloride migration":
                            _, cnom_i = derive_nominal_cover(
                                _ENGINE, int(target_years),
                                _f(rec.get("Dnssm_90d_1e-12_m2_s")),
                                float(alpha), float(ccrit), float(cs), float(kt),
                                float(max(allowed_long)), float(execution_allowance),
                                minimum_cover_override_mm=minimum_cover_override
                            )
                        else:
                            cnom_i = float(cover_nominal)
                        with configured_engine(_ENGINE, {
                            "gamma_s": float(gamma_s), "gamma_c": float(gamma_c),
                            "f_yk_MPa": float(fyk), "gamma_v": float(gamma_v),
                        }):
                            r_i, _, _ = run_design(
                                _ENGINE,
                                f_cube_28=float(rec["f_cube_28_MPa"]),
                                cover_nominal_mm=float(cnom_i),
                                mix_name=f"{benchmark_mid} – {rec['Mixture_Code']}",
                                target_years=int(target_years), gwp_concrete=float(gwp_i),
                                steel_ef=float(steel_ef), storeys=int(storeys),
                                mx_kNm=float(mx_kNm), my_kNm=float(my_kNm),
                                clear_height_m=float(clear_height_m), effective_length_m=float(effective_length_m),
                                load_mode=load_mode, tributary_area_m2=float(tributary_area),
                                dead_load_kN_m2=float(dead_load), live_load_kN_m2=float(live_load),
                                gamma_G=float(gamma_G), gamma_Q=float(gamma_Q),
                                unit_weight_kN_m3=float(unit_weight),
                                manual_n_ed_kN=(float(manual_n_ed) if load_mode == "Manual N_Ed" else None),
                                section_mode=section_mode,
                                min_section_mm=int(min_section), max_section_mm=int(max_section),
                                section_step_mm=int(section_step),
                                fixed_section_mm=(int(fixed_section) if fixed_section is not None else None),
                                long_bar_diameters=[int(x) for x in allowed_long],
                                link_diameters=[int(x) for x in allowed_links],
                                required_bar_count=required_bar_count,
                                max_aggregate_size_mm=float(max_aggregate),
                                max_reinf_ratio=float(max_reinf_ratio),
                                k_tc=float(k_tc), cot_theta_shear=float(cot_theta_shear),
                                minimum_confinement_factor_k=float(minimum_confinement_factor_k),
                                effective_creep_ratio_local=float(effective_creep_ratio_local),
                                objective=objective,
                            )
                        if r_i is None:
                            benchmark_rows.append({
                                "Mix_ID": benchmark_mid, "Mixture": rec["Mixture_Code"],
                                "f꜀ᵤᵦₑ,28 (MPa)": rec["f_cube_28_MPa"],
                                "Dₙₛₛₘ,90 (10⁻¹² m²/s)": rec["Dnssm_90d_1e-12_m2_s"],
                                "cₙₒₘ (mm)": cnom_i,
                                "Square-section width b (mm)": "—", "Longitudinal bars": "—", "Links": "—",
                                "Steel (kg)": None, "Concrete GWP (kg CO₂-eq/m³)": gwp_i,
                                "Column GWP (kg CO₂-eq)": None, "Biaxial utilization": None,
                                "Status": "NO_FEASIBLE_LAYOUT",
                                "Design note": "No feasible layout was found for the selected design settings."
                            })
                        else:
                            benchmark_rows.append({
                                "Mix_ID": benchmark_mid, "Mixture": rec["Mixture_Code"],
                                "f꜀ᵤᵦₑ,28 (MPa)": rec["f_cube_28_MPa"],
                                "Dₙₛₛₘ,90 (10⁻¹² m²/s)": rec["Dnssm_90d_1e-12_m2_s"],
                                "cₙₒₘ (mm)": cnom_i,
                                "Square-section width b (mm)": f"{r_i['b_mm']} × {r_i['h_mm']}",
                                "Longitudinal bars": f"{r_i['longitudinal_bars_n']}Ø{r_i['longitudinal_bar_diameter_mm']:.0f}",
                                "Links": f"Ø{r_i['link_diameter_mm']:.0f}",
                                "Steel (kg)": r_i["total_steel_mass_kg"],
                                "Concrete GWP (kg CO₂-eq/m³)": gwp_i,
                                "Column GWP (kg CO₂-eq)": r_i["GWP_column_kgCO2e"],
                                "Biaxial utilization": r_i["biaxial_interaction_utilization"],
                                "Status": r_i["Status"], "Design note": "",
                            })
                        progress.progress(i / len(MIX_DB), text=f"Calculated {i}/{len(MIX_DB)} mixtures")
                    st.session_state["last_benchmark_rows"] = benchmark_rows
                    st.session_state["last_benchmark_input_signature"] = current_benchmark_signature
                    st.session_state["last_benchmark_app_version"] = APP_VERSION
                    st.session_state["last_benchmark_resource_signature"] = _RESOURCE_SIGNATURE
                    st.session_state.pop("_scc_results_bundle_zip", None)
                    st.session_state.pop("_scc_results_bundle_signature", None)
                    progress.empty()
                    st.success("The 16-mixture comparison is complete.")
                except Exception:
                    progress.empty()
                    st.error("The mixture comparison could not be completed. Review the inputs and try again.")


    with right_col:
        if not ENGINE_READY:
            st.markdown('<div class="panel-heading orange">Design outputs</div>', unsafe_allow_html=True)
        elif "last_design_result" in st.session_state:
            res = st.session_state["last_design_result"]
            with st.container(key="selected-design-preview"):
                with st.container(border=True):
                    st.markdown('<div class="section-ribbon green"><span class="step">RESULT</span> Selected design at a glance</div>', unsafe_allow_html=True)
                    selected_mix_label = st.session_state.get("last_design_mix_label", "Selected mixture")
                    selected_storeys = st.session_state.get("last_design_storeys", res.get("storeys", "—"))
                    selected_target = st.session_state.get("last_design_target_years", "—")
                    is_stale = (st.session_state.get("last_design_input_signature") != current_design_signature
                                or st.session_state.get("last_design_app_version") != APP_VERSION
                                or st.session_state.get("last_design_resource_signature") != _RESOURCE_SIGNATURE)
                    st.caption(f"{selected_mix_label} · {selected_storeys} stories · {selected_target}-year initiation target")
                    if is_stale:
                        changed_groups = _changed_input_groups(st.session_state.get("last_design_input_summary"), signature_payload)
                        group_note = (" Changed groups: " + "; ".join(changed_groups) + ".") if changed_groups else ""
                        st.warning("Inputs have changed since this result was calculated. Select Calculate / Update design to refresh it." + group_note)
                    else:
                        st.success("Current result · feasible")
                    m1, m2 = st.columns(2)
                    m1.metric("Square section", f"{int(res['b_mm'])} × {int(res['h_mm'])} mm")
                    m2.metric("Longitudinal bars", f"{int(res['longitudinal_bars_n'])}Ø{_f(res['longitudinal_bar_diameter_mm']):.0f}")
                    m3, m4 = st.columns(2)
                    m3.metric("Transverse links", f"Ø{_f(res['link_diameter_mm']):.0f}")
                    m4.metric("Total reinforcement", f"{_f(res['total_steel_mass_kg']):,.1f} kg")
                    st.metric("Column GWP", f"{_f(res['GWP_column_kgCO2e']):,.1f} kg CO₂-eq")
                    detail_signature = st.session_state.get("last_design_input_signature", "current")
                    detail_assets = _cached_figure_assets(
                        (APP_VERSION, _RESOURCE_SIGNATURE, "design_detail", detail_signature),
                        lambda: _polish_formula_artists(draw_design_detail(res)),
                        formats=("png", "pdf"),
                    )
                    st.image(detail_assets["display"], use_container_width=True)
                    st.caption("Detailed spacing, material contributions, structural checks and exports are provided below the input workspace.")
        else:
            with st.container(border=True):
                st.markdown('<div class="panel-heading green">Selected design and environmental result</div>', unsafe_allow_html=True)
                st.info("Complete the inputs and select Calculate / Update design to display the selected column layout and GWP results.")
                q1, q2 = st.columns(2)
                q1.metric("Section", "—")
                q2.metric("Column GWP", "—")

    # Data-heavy results are intentionally full-width for readable tables and checks.
    if ENGINE_READY and "last_benchmark_rows" in st.session_state:
        with st.container(border=True):
            st.markdown('<div class="panel-heading blue">Comparison of the 16 study mixtures</div>', unsafe_allow_html=True)
            raw_bench = list(st.session_state["last_benchmark_rows"])
            benchmark_is_stale = (st.session_state.get("last_benchmark_input_signature") != current_benchmark_signature
                                   or st.session_state.get("last_benchmark_app_version") != APP_VERSION
                                   or st.session_state.get("last_benchmark_resource_signature") != _RESOURCE_SIGNATURE)
            if benchmark_is_stale:
                st.warning("This comparison uses different structural settings from the current design. Run Compare 16 mixtures again to align the results.")
            sort_col, order_col = st.columns([1.5, 1.0])
            sort_by = sort_col.selectbox(
                "Sort comparison by",
                ["Study order", "Feasibility", "Concrete GWP intensity", "Column GWP", "Section size", "Reinforcement mass"],
                key="_scc_ui_comparison_sort"
            )
            sort_order = order_col.radio("Order", ["Ascending", "Descending"], horizontal=True,
                                         key="_scc_ui_comparison_order")
            bench = _sort_comparison_rows(raw_bench, sort_by, descending=(sort_order == "Descending"))
            feasible_rows = [r for r in raw_bench if str(r.get("Status", "")).startswith("FEASIBLE")]
            infeasible_rows = [r for r in raw_bench if r not in feasible_rows]
            sm1, sm2, sm3 = st.columns(3)
            sm1.metric("Feasible mixtures", f"{len(feasible_rows)} / {len(raw_bench)}")
            sm2.metric("No feasible layout", str(len(infeasible_rows)))
            best_member = min(feasible_rows, key=lambda r: _f(r.get("Column GWP (kg CO₂-eq)"), float("inf"))) if feasible_rows else None
            if best_member:
                sm3.metric("Lowest column GWP", f"{_f(best_member.get('Column GWP (kg CO₂-eq)')):.1f} kg CO₂-eq", delta=str(best_member.get("Mix_ID", "")))
            else:
                sm3.metric("Lowest column GWP", "—")
            if infeasible_rows:
                st.warning(f"{len(infeasible_rows)} mixture(s) have no feasible layout within the selected search settings. Review the section range, cover requirements, and reinforcement candidates for these cases.")
            comparison_signature = st.session_state.get("last_benchmark_input_signature", "comparison")
            comparison_assets = _cached_figure_assets(
                (APP_VERSION, _RESOURCE_SIGNATURE, "comparison_gwp", comparison_signature, sort_by, sort_order),
                lambda: draw_comparison_gwp(bench, MIX_DB),
                formats=("png", "pdf"),
            ) if bench else None
            if comparison_assets is not None:
                st.image(comparison_assets["display"], use_container_width=True)
                st.caption("The upper panel reports concrete GWP intensity per cubic metre. The lower panel reports total GWP per 3.0-m column segment. The panels present material-level and member-level environmental results, respectively.")
                st.download_button("Download coordinated comparison chart (PNG)",
                                   data=comparison_assets["png"],
                                   file_name="Sixteen_Mixture_GWP_Comparison.png", mime="image/png")
                st.download_button("Download coordinated comparison chart (PDF)",
                                   data=comparison_assets["pdf"],
                                   file_name="Sixteen_Mixture_GWP_Comparison.pdf", mime="application/pdf")
            st.dataframe(bench, use_container_width=True, hide_index=True)
            st.download_button("Download 16-mixture comparison (CSV)", data=_csv_bytes(bench),
                               file_name="Sixteen_Mixture_Column_Comparison.csv", mime="text/csv")
            feasible_ids = [str(r.get("Mix_ID")) for r in feasible_rows if str(r.get("Mix_ID")) in MIX_DB]
            if feasible_ids:
                transfer_cols = st.columns([1.6, 1.0])
                transfer_mix = transfer_cols[0].selectbox(
                    "Load a feasible mixture into the single-design inputs", feasible_ids,
                    key="_scc_ui_transfer_mixture"
                )
                if transfer_cols[1].button("Use selected mixture", key="scc_transfer_mixture_to_design", use_container_width=True):
                    st.session_state["_scc_pending_mixture_transfer"] = transfer_mix
                    st.rerun()

    if ENGINE_READY and "last_design_result" in st.session_state:
        res = st.session_state["last_design_result"]
        with st.container(border=True):
            st.markdown('<div class="panel-heading blue">Calculated transverse-reinforcement distribution and spacing</div>', unsafe_allow_html=True)
            def spacing_check(actual, adopted, calculated):
                return "PASS" if (actual <= adopted + 1e-6 and adopted <= calculated + 1e-6) else "CHECK"
            zone_rows = [
                {"Region": "End zones (2)", "Zone length each (mm)": _f(res.get("end_zone_length_each_mm")),
                 "Calculated limit (mm)": _f(res.get("smax_end_limit_mm")),
                 "Adopted 50-mm grid (mm)": _f(res.get("s_end_adopted_spacing_mm")),
                 "Actual equalized pitch (mm)": _f(res.get("end_zone_spacing_actual_mm")),
                 "Minimum centre pitch (mm)": _f(res.get("minimum_transverse_center_spacing_mm")),
                 "Intervals per zone": int(res.get("end_zone_intervals_each", 0)),
                 "Check": "PASS" if (spacing_check(_f(res.get("end_zone_spacing_actual_mm")), _f(res.get("s_end_adopted_spacing_mm")), _f(res.get("smax_end_limit_mm"))) == "PASS" and _f(res.get("end_zone_spacing_actual_mm")) + 1e-6 >= _f(res.get("minimum_transverse_center_spacing_mm"))) else "CHECK"},
                {"Region": "Intermediate zones (2)", "Zone length each (mm)": _f(res.get("middle_zone_length_each_mm")),
                 "Calculated limit (mm)": _f(res.get("smax_mid_limit_mm")),
                 "Adopted 50-mm grid (mm)": _f(res.get("s_mid_adopted_spacing_mm")),
                 "Actual equalized pitch (mm)": _f(res.get("middle_zone_spacing_actual_mm")),
                 "Minimum centre pitch (mm)": _f(res.get("minimum_transverse_center_spacing_mm")),
                 "Intervals per zone": int(res.get("middle_zone_intervals_each", 0)),
                 "Check": "PASS" if (spacing_check(_f(res.get("middle_zone_spacing_actual_mm")), _f(res.get("s_mid_adopted_spacing_mm")), _f(res.get("smax_mid_limit_mm"))) == "PASS" and _f(res.get("middle_zone_spacing_actual_mm")) + 1e-6 >= _f(res.get("minimum_transverse_center_spacing_mm"))) else "CHECK"},
                {"Region": "Central lap region (1)", "Zone length (mm)": _f(res.get("lap_zone_length_mm")),
                 "Calculated limit (mm)": _f(res.get("smax_lap_limit_mm")),
                 "Adopted 50-mm grid (mm)": _f(res.get("s_lap_adopted_spacing_mm")),
                 "Actual equalized pitch (mm)": _f(res.get("lap_zone_spacing_actual_mm")),
                 "Minimum centre pitch (mm)": _f(res.get("minimum_transverse_center_spacing_mm")),
                 "Intervals": int(res.get("lap_zone_intervals", 0)),
                 "Check": "PASS" if (spacing_check(_f(res.get("lap_zone_spacing_actual_mm")), _f(res.get("s_lap_adopted_spacing_mm")), _f(res.get("smax_lap_limit_mm"))) == "PASS" and _f(res.get("lap_zone_spacing_actual_mm")) + 1e-6 >= _f(res.get("minimum_transverse_center_spacing_mm"))) else "CHECK"},
            ]
            st.dataframe(zone_rows, use_container_width=True, hide_index=True)
            total_intervals = 2*int(res.get("end_zone_intervals_each", 0)) + 2*int(res.get("middle_zone_intervals_each", 0)) + int(res.get("lap_zone_intervals", 0))
            st.caption(f"Spacing procedure: sₐdₒₚₜ = 50 × floor(sₘₐₓ,calc/50); nᵢ = ceil(Lᵢ/sₐdₒₚₜ); actual equalized pitch sᵢ = Lᵢ/nᵢ. The actual pitch is not rounded again and may be smaller than the adopted 50-mm-grid limit. It must also remain at least the minimum centre-to-centre pitch from link diameter plus the adopted clear-gap requirement. Interval audit: 2×n_end + 2×n_mid + n_lap = {total_intervals}; link levels = {int(res.get('number_of_links', 0))} (= intervals + 1).")
            p1, p2, p3 = st.columns(3)
            p1.download_button("Download reinforcement detail (PNG)", data=detail_assets["png"],
                               file_name="Designed_Column_Detail.png", mime="image/png", use_container_width=True)
            p2.download_button("Download reinforcement detail (PDF)", data=detail_assets["pdf"],
                               file_name="Designed_Column_Detail.pdf", mime="application/pdf", use_container_width=True)
            p3.download_button("Download selected design (CSV)", data=_csv_bytes([res]),
                               file_name="Selected_Column_Design.csv", mime="text/csv", use_container_width=True)
        with st.container(border=True):
            st.markdown('<div class="panel-heading green">Product-stage A1–A3 GWP breakdown</div>', unsafe_allow_html=True)
            b1, b2, b3 = st.columns(3)
            b1.metric("Concrete contribution", f"{_f(res.get('GWP_concrete_column_kgCO2e')):,.1f} kg CO₂-eq")
            b2.metric("Reinforcement contribution", f"{_f(res.get('GWP_steel_column_kgCO2e')):,.1f} kg CO₂-eq")
            b3.metric("Total material GWP", f"{_f(res.get('GWP_column_kgCO2e')):,.1f} kg CO₂-eq")
            st.caption(f"Concrete: {_f(res.get('concrete_volume_m3')):.3f} m³ × {_f(res.get('GWP_concrete_kgCO2e_m3')):.2f} kg CO₂-eq/m³. Steel: {_f(res.get('total_steel_mass_kg')):.2f} kg × {_f(res.get('App_steel_emission_factor_kgCO2e_kg')):.2f} kg CO₂-eq/kg.")
            gwp_assets = _cached_figure_assets(
                (APP_VERSION, _RESOURCE_SIGNATURE, "gwp_breakdown", st.session_state.get("last_design_input_signature", "current")),
                lambda: draw_gwp_breakdown(res),
                formats=("png",),
            )
            st.image(gwp_assets["display"], use_container_width=True)
            st.download_button("Download GWP breakdown (PNG)", data=gwp_assets["png"],
                               file_name="Column_GWP_Breakdown.png", mime="image/png")
            with st.expander("Steel emission-factor sensitivity for this selected layout", expanded=False):
                steel_sensitivity = []
                for ef_i in (1.71, 1.85, 2.00):
                    steel_part_i = _f(res.get("total_steel_mass_kg")) * ef_i
                    concrete_part_i = _f(res.get("GWP_concrete_column_kgCO2e"))
                    steel_sensitivity.append({
                        "Steel EF (kg CO₂-eq/kg)": ef_i,
                        "Concrete GWP (kg CO₂-eq)": concrete_part_i,
                        "Steel GWP (kg CO₂-eq)": steel_part_i,
                        "Total column GWP (kg CO₂-eq)": concrete_part_i + steel_part_i,
                    })
                st.dataframe(steel_sensitivity, use_container_width=True, hide_index=True)
        with st.container(border=True):
            st.markdown('<div class="panel-heading orange">Structural response and design checks</div>', unsafe_allow_html=True)
            action_cols = st.columns(4)
            action_cols[0].metric("Nₑd", f"{_f(res.get('N_Ed_kN')):,.1f} kN")
            action_cols[1].metric("Mₑd,x", f"{_f(res.get('M_Ed_x_first_order_kNm')):,.1f} kN·m")
            action_cols[2].metric("Mₑd,y", f"{_f(res.get('M_Ed_y_first_order_kNm')):,.1f} kN·m")
            action_cols[3].metric("Biaxial utilization", f"{_f(res.get('biaxial_interaction_utilization')):.3f}")
            check_rows = [
                {"Check": "Axial resistance ratio Nₑd/Nᵣd", "Value": f"{_f(res.get('axial_ratio_r')):.3f}", "Design status": "PASS" if _f(res.get('axial_ratio_r')) < 1 else "CHECK"},
                {"Check": "Biaxial interaction utilization", "Value": f"{_f(res.get('biaxial_interaction_utilization')):.3f}", "Design status": "PASS" if _f(res.get('biaxial_interaction_utilization')) <= 1 else "CHECK"},
                {"Check": "Longitudinal reinforcement ratio", "Value": f"{_f(res.get('rho_longitudinal_pct')):.2f}%", "Design status": res.get("longitudinal_reinforcement_ratio_check", "—")},
                {"Check": "Clear longitudinal-bar spacing", "Value": f"{_f(res.get('clear_bar_spacing_mm')):.1f} mm (min {_f(res.get('minimum_clear_spacing_mm')):.1f})", "Design status": res.get("bar_spacing_check", "—")},
                {"Check": "Bar pitch", "Value": f"{_f(res.get('centre_bar_pitch_mm')):.1f} mm", "Design status": res.get("longitudinal_bar_pitch_check", "—")},
                {"Check": "Link spacing – end zones", "Value": f"sₘₐₓ = {_f(res.get('smax_end_limit_mm')):.1f}; s_adopt = {_f(res.get('s_end_adopted_spacing_mm')):.0f}; s_actual = {_f(res.get('end_zone_spacing_actual_mm')):.1f} mm; {_f(res.get('end_zone_intervals_each')):.0f} intervals/end", "Design status": "PASS" if (_f(res.get('end_zone_spacing_actual_mm')) <= _f(res.get('s_end_adopted_spacing_mm')) + 1e-6 and _f(res.get('s_end_adopted_spacing_mm')) <= _f(res.get('smax_end_limit_mm')) + 1e-6) else "CHECK"},
                {"Check": "Link spacing – intermediate zones", "Value": f"sₘₐₓ = {_f(res.get('smax_mid_limit_mm')):.1f}; s_adopt = {_f(res.get('s_mid_adopted_spacing_mm')):.0f}; s_actual = {_f(res.get('middle_zone_spacing_actual_mm')):.1f} mm; {_f(res.get('middle_zone_intervals_each')):.0f} intervals/zone", "Design status": "PASS" if (_f(res.get('middle_zone_spacing_actual_mm')) <= _f(res.get('s_mid_adopted_spacing_mm')) + 1e-6 and _f(res.get('s_mid_adopted_spacing_mm')) <= _f(res.get('smax_mid_limit_mm')) + 1e-6) else "CHECK"},
                {"Check": "Link spacing – lap region", "Value": f"sₘₐₓ = {_f(res.get('smax_lap_limit_mm')):.1f}; s_adopt = {_f(res.get('s_lap_adopted_spacing_mm')):.0f}; s_actual = {_f(res.get('lap_zone_spacing_actual_mm')):.1f} mm; {_f(res.get('lap_zone_intervals')):.0f} intervals", "Design status": "PASS" if (_f(res.get('lap_zone_spacing_actual_mm')) <= _f(res.get('s_lap_adopted_spacing_mm')) + 1e-6 and _f(res.get('s_lap_adopted_spacing_mm')) <= _f(res.get('smax_lap_limit_mm')) + 1e-6) else "CHECK"},
                {"Check": "Transverse reinforcement quantity", "Value": f"Ø{_f(res.get('link_diameter_mm')):.0f}; {int(res.get('number_of_links', 0))} link levels; {int(res.get('cross_ties_total', 0))} cross-ties", "Design status": res.get("tie_spacing_check", "—")},
                {"Check": "Anchorage and lap", "Value": f"lᵦd = {_f(res.get('l_bd_mm')):.0f} mm; lₛd = {_f(res.get('l_sd_mm')):.0f} mm", "Design status": res.get("lap_anchorage_check", "—")},
                {"Check": "Biaxial moment resistance", "Value": f"Mₑd,x/Mᵣd,x = {_f(res.get('M_Ed_x_checked_kNm')):.1f}/{_f(res.get('M_Rd_x_at_N_kNm')):.1f} kN·m; Mₑd,y/Mᵣd,y = {_f(res.get('M_Ed_y_checked_kNm')):.1f}/{_f(res.get('M_Rd_y_at_N_kNm')):.1f} kN·m", "Design status": "PASS" if _f(res.get('biaxial_interaction_utilization')) <= 1.0 else "CHECK"},
                {"Check": "Second-order effects", "Value": f"M2,x = {_f(res.get('second_order_moment_x_kNm')):.1f}; M2,y = {_f(res.get('second_order_moment_y_kNm')):.1f} kN·m; {_display_result_code(res.get('second_order_status'))}", "Design status": res.get("second_order_gate_status", "—")},
                {"Check": "Shear design route", "Value": f"{_display_result_code(res.get('shear_method'))}; cot θ = {_f(res.get('cot_theta')):.2f}; VEd,x/y = {_f(res.get('M_reverse_shear_envelope_x_kN')):.1f}/{_f(res.get('M_reverse_shear_envelope_y_kN')):.1f} kN", "Design status": res.get("shear_check_status", "—")},
                {"Check": "Shear stress and compression field", "Value": f"τEd,resultant = {_f(res.get('tau_Ed_resultant_MPa')):.3f} MPa; compression-chord depth ratio = {_f(res.get('compression_chord_depth_ratio')):.3f}; optimum ftdy = {_f(res.get('f_tdy_optimum_MPa')):.2f} MPa", "Design status": res.get("compression_strut_check", "—")},
            ]
            st.dataframe(check_rows, use_container_width=True, hide_index=True)
            st.caption("Actual equalized pitches are reported separately for the end, intermediate, and lap regions. Each region is checked against its adopted spacing limit and minimum centre-to-centre clearance.")
        with st.container(border=True):
            alternatives = st.session_state.get("last_design_alternatives", [])
            if alternatives:
                st.markdown('<div class="panel-heading blue">Feasible design alternatives</div>', unsafe_allow_html=True)
                alt_rows = [_result_table_row(r) for r in alternatives[:100]]
                st.dataframe(alt_rows, use_container_width=True, hide_index=True)
                st.download_button("Download feasible alternatives (CSV)", data=_csv_bytes(alternatives),
                                   file_name="Feasible_Column_Alternatives.csv", mime="text/csv")

        with st.container(border=True):
            st.markdown('<div class="panel-heading blue">Comprehensive design report</div>', unsafe_allow_html=True)
            report_input_payload = st.session_state.get("last_design_input_summary", {})
            report_mix_label = st.session_state.get("last_design_mix_label", "Selected mixture")
            report_signature = st.session_state.get("last_design_input_signature")
            report_is_stale = (report_signature != current_design_signature
                               or st.session_state.get("last_design_app_version") != APP_VERSION
                               or st.session_state.get("last_design_resource_signature") != _RESOURCE_SIGNATURE)
            if report_is_stale:
                st.warning("The report uses the last completed calculation. Recalculate to include your latest input changes.")
            st.caption("The PDF combines the selected design, input record, reinforcement drawing, regional link-spacing schedule, structural checks, and the A1–A3 GWP breakdown. The 16-mixture comparison is included when available.")
            try:
                comparison_signature = st.session_state.get("last_benchmark_input_signature")
                report_comparison_rows = (
                    st.session_state.get("last_benchmark_rows")
                    if (comparison_signature == _benchmark_signature(report_input_payload)
                        and st.session_state.get("last_benchmark_app_version") == APP_VERSION
                        and st.session_state.get("last_benchmark_resource_signature") == _RESOURCE_SIGNATURE) else None
                )
                if st.session_state.get("last_benchmark_rows") and report_comparison_rows is None:
                    st.caption("The last 16-mixture comparison used different design settings. Run the comparison again to include results matched to this design.")
                report_comparison_key = (
                    st.session_state.get("last_benchmark_input_signature")
                    if report_comparison_rows is not None else None
                )
                report_cache_key = (APP_VERSION, _RESOURCE_SIGNATURE, report_signature, report_comparison_key)
                report_cache = st.session_state.get("_scc_report_pdf_cache")
                report_cache_is_current = (
                    isinstance(report_cache, dict) and report_cache.get("key") == report_cache_key
                )
                if report_cache_is_current:
                    st.download_button(
                        "Download comprehensive design report (PDF)", data=report_cache["data"],
                        file_name="SCC_Column_Design_Comprehensive_Report.pdf", mime="application/pdf",
                        type="primary", use_container_width=True
                    )
                elif st.button("Generate comprehensive design report (PDF)", key="scc_generate_report_pdf",
                               type="primary", use_container_width=True):
                    with st.spinner("Preparing the comprehensive PDF report…"):
                        comprehensive_pdf = _build_comprehensive_report_pdf(
                            res, report_input_payload, report_mix_label,
                            comparison_rows=report_comparison_rows, mix_db=MIX_DB
                        )
                    st.session_state["_scc_report_pdf_cache"] = {
                        "key": report_cache_key, "data": comprehensive_pdf
                    }
                    st.download_button(
                        "Download comprehensive design report (PDF)", data=comprehensive_pdf,
                        file_name="SCC_Column_Design_Comprehensive_Report.pdf", mime="application/pdf",
                        type="primary", use_container_width=True
                    )
                else:
                    st.caption("Generate the PDF only when needed; the design workspace remains lighter during normal use.")
                if st.button("Prepare complete results package (ZIP)", key="scc_prepare_all_results_zip", use_container_width=True):
                    try:
                        widget_snapshot = st.session_state.get("last_design_widget_inputs", {})
                        alternatives_snapshot = st.session_state.get("last_design_alternatives", [])
                        bundle_bytes = _build_results_bundle_zip(
                            res, report_input_payload, report_mix_label,
                            input_widget_values=widget_snapshot,
                            alternatives=alternatives_snapshot,
                            comparison_rows=report_comparison_rows, mix_db=MIX_DB
                        )
                        st.session_state["_scc_results_bundle_zip"] = bundle_bytes
                        st.session_state["_scc_results_bundle_signature"] = report_signature
                        st.success("The complete results package is ready.")
                        st.rerun()
                    except Exception:
                        st.error("The results package could not be prepared. Try again after recalculating the selected design.")
                cached_bundle = st.session_state.get("_scc_results_bundle_zip")
                if cached_bundle:
                    st.download_button(
                        "Download all results (ZIP)", data=cached_bundle,
                        file_name="SCC_Column_Design_All_Results.zip", mime="application/zip",
                        use_container_width=True
                    )
            except Exception:
                st.error("The PDF report could not be generated. Recalculate the design and try again.")

with tab_guide:
    st.markdown(_tab_hero(
        "guide", "COLUMN DESIGN WORKSPACE · USER SUPPORT",
        "User guide",
        "A detailed guide to mixture definition, member-level design, reinforcement detailing, environmental assessment, input management, and result export.",
        ["10 topics", "Input presets and validation", "16-mixture comparison", "Comprehensive reporting"],
    ), unsafe_allow_html=True)
    st.markdown(
        '<div class="guide-intro"><b>Using this guide.</b> Follow the sequence below to define mixture properties, specify member actions and geometry, run the column-design search, and interpret the results for a 3.0-m square reinforced-concrete column segment.</div>',
        unsafe_allow_html=True,
    )
    st.markdown('<div class="section-ribbon blue"><span class="step">01</span> Application structure and study basis</div>', unsafe_allow_html=True)
    st.markdown("The three tabs provide the study basis, the column-design workspace, and this user guide. The overview presents the research context, assessment basis, standards, and principal findings.")
    st.markdown('<div class="section-ribbon blue"><span class="step">02</span> Command bar and input presets</div>', unsafe_allow_html=True)
    st.markdown("Use **Calculate / Update design** to run the current single-mixture design and **Compare 16 mixtures** to repeat the selected design objective for all study mixtures under common structural settings. The input-preset expander downloads current widget values as JSON, restores compatible values from JSON, or resets edited inputs to study defaults after confirmation.")
    st.markdown("A preset contains the saved input values. After loading it, run the design or comparison to calculate results for those values.")
    st.markdown('<div class="section-ribbon blue"><span class="step">03</span> Mixture selection and material properties</div>', unsafe_allow_html=True)
    st.markdown("Select **Study mixtures** to use one of the 16 characterized SCC mixtures, or select **User-defined mixture** to enter a custom recipe. Review the 28-day mean cube strength, 90-day non-steady-state migration coefficient, and concrete GWP intensity. For a user-defined mixture, enter the mean cube strength and migration coefficient for the selected recipe.")
    st.markdown('<div class="section-ribbon blue"><span class="step">04</span> Mixture proportions and constituent GWP</div>', unsafe_allow_html=True)
    st.markdown("Review the constituent masses in kg/m³ and, when needed, edit the emission factors in the advanced panel. The application reports concrete GWP intensity and the contribution of each constituent. Study-mixture results are presented alongside the constituent-based estimate.")
    st.markdown('<div class="section-ribbon blue"><span class="step">05</span> Structural actions and column geometry</div>', unsafe_allow_html=True)
    st.markdown("Choose automatic axial force from tributary-floor loads or enter it directly. Specify the story count, clear height, effective length, tributary area, floor loads, ULS factors, concrete unit weight, and first-order moments about both principal axes. Moments are prescribed member-level inputs for each study scenario.")
    st.markdown('<div class="section-ribbon blue"><span class="step">06</span> Durability cover and design search</div>', unsafe_allow_html=True)
    st.markdown("Select migration-based cover estimation or enter nominal cover directly. For migration-based estimation, choose the initiation target and cover basis, then review the migration coefficient, aging exponent, chloride thresholds, transfer factor, and execution allowance. Next, define a section range or fixed square section, choose the design objective and steel emission factor, and select longitudinal-bar and link diameters.")
    st.markdown("Nominal cover combines the calculated effective cover, the selected minimum-cover basis, the largest selected longitudinal-bar diameter, and the execution allowance. The search evaluates square sections and discrete reinforcement candidates.")
    st.markdown('<div class="section-ribbon green"><span class="step">07</span> Selected design and reinforcement drawing</div>', unsafe_allow_html=True)
    st.markdown("The result panel summarizes the selected section, longitudinal bars, transverse links, reinforcement mass, and column GWP. The drawing presents the calculated cross-section and column elevation, including the five transverse-link zones.")
    st.markdown('<div class="section-ribbon green"><span class="step">08</span> Transverse-reinforcement schedule and structural checks</div>', unsafe_allow_html=True)
    st.markdown("Review the end-, intermediate-, and lap-zone lengths, maximum permitted spacing, adopted spacing, and actual equalized pitch. Regional limits are rounded down to the 50-mm grid before interval counts are determined. The structural-check table summarizes resistance, stability, shear, anchorage, lap, and detailing checks.")
    st.markdown("PASS denotes a satisfied design criterion; CHECK identifies a criterion flagged by the member-design checks.")
    st.markdown('<div class="section-ribbon green"><span class="step">09</span> Environmental results and 16-mixture comparison</div>', unsafe_allow_html=True)
    st.markdown("The GWP breakdown separates concrete and reinforcement contributions for one 3.0-m segment. The 16-mixture comparison reports concrete GWP intensity and total column GWP as separate measures. Sort the table by feasibility, concrete GWP intensity, column GWP, section size, or reinforcement mass, then transfer a selected mixture to the design workspace.")
    st.markdown('<div class="section-ribbon orange"><span class="step">10</span> Input validation and comprehensive exports</div>', unsafe_allow_html=True)
    st.markdown("The input summary brings together material, action, geometry, durability, cover, and search settings. Origin labels distinguish study values, user inputs, and calculated quantities. Validation messages identify entries to correct, while change indicators show which settings differ from the last calculation.")
    st.markdown("The PDF report includes the selected design, input record, cover calculation, reinforcement drawing, regional spacing schedule, structural checks, material quantities, and A1–A3 GWP breakdown. The ZIP package brings together the report, input preset, CSV tables, and design figures.")
    with st.container(border=True):
        st.markdown('<div class="panel-heading orange">Interpreting the results</div>', unsafe_allow_html=True)
        st.markdown(
            "**Feasible result.** A feasible status indicates that the selected member-design criteria are satisfied for the displayed inputs.\n\n"
            "**GWP measures.** Concrete GWP intensity is reported in kg CO₂-eq/m³. Column GWP combines concrete and reinforcement contributions in kg CO₂-eq per 3.0-m segment. The measures use distinct functional units.\n\n"
            "The structural, durability, and environmental assessment basis is presented in the overview tab."
        )
    st.caption("For the governing equations, parameter definitions, and experimental program, see the article.")
