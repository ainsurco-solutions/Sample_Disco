from __future__ import annotations

def palette(theme: str | None) -> dict[str, str]:
    if theme == "dark":
        return {
            "primary": "#77B7D8",
            "hover": "#A0D0E8",
            "green": "#7ABF9B",
            "orange": "#D8AC67",
            "red": "#E38B87",
            "muted": "#C2C2C5",
            "border": "#454545",
            "disabled": "#A4A4A8",
        }
    return {
        "primary": "#1B1464",
        "hover": "#271D8F",
        "green": "#1E7B34",
        "orange": "#B26A00",
        "red": "#B3261E",
        "muted": "#5B6B78",
        "border": "#D8D9DB",
        "disabled": "#6B6C70",
    }

def semantic_colour(name: str) -> str:
    return f"light-dark({palette('light')[name]}, {palette('dark')[name]})"

def stylesheet(theme: str | None = None) -> str:
    c = {name: semantic_colour(name) for name in palette("light")}
    dialog_scheme = "dark" if theme == "dark" else "light"
    return f"""
    <style>
      [role="dialog"] {{ color-scheme:{dialog_scheme}; }}
      [data-testid="stAppViewContainer"] {{ letter-spacing:0; }}
      [data-testid="stMainBlockContainer"] {{ padding-top:3rem; padding-bottom:2rem; }}
      h1, h2, h3, h4, h5, h6 {{ letter-spacing:0 !important; }}
      button[data-testid="stBaseButton-primary"],
      button[data-testid="stBaseButton-primaryFormSubmit"],
      .stButton button[kind="primary"],
      [data-testid="stFormSubmitButton"] button[kind="primary"] {{
        background:transparent; color:{c["primary"]};
        border:1.5px solid {c["primary"]}; font-weight:600;
      }}
      button[data-testid="stBaseButton-primary"]:hover,
      button[data-testid="stBaseButton-primaryFormSubmit"]:hover,
      .stButton button[kind="primary"]:hover,
      [data-testid="stFormSubmitButton"] button[kind="primary"]:hover {{
        background:color-mix(in srgb, {c["primary"]} 7%, transparent);
        color:{c["hover"]}; border-color:{c["hover"]};
      }}
      button[data-testid="stBaseButton-primary"]:disabled,
      button[data-testid="stBaseButton-primaryFormSubmit"]:disabled,
      .stButton button[kind="primary"]:disabled,
      [data-testid="stFormSubmitButton"] button[kind="primary"]:disabled {{
        background:transparent; color:{c["disabled"]}; border-color:{c["border"]};
      }}
      .stButton button:focus-visible,
      [data-testid="stFormSubmitButton"] button:focus-visible {{
        outline:2px solid {c["primary"]}; outline-offset:2px;
      }}
      [data-testid="stMetricValue"] {{
        font-size:1.5rem; font-weight:600; font-variant-numeric:tabular-nums;
      }}
      [data-testid="stMetricLabel"] {{ font-size:.875rem; }}
      .mh-summary-row, .hl-row {{ gap:.6rem; margin:.15rem 0 .5rem; }}
      .mh-summary-card, .hl-row-primary .hl-card {{
        padding:.65rem .85rem; background:rgba(128,128,128,.1);
        border:1px solid rgba(128,128,128,.38); border-radius:8px;
      }}
      .mh-summary-value, .hl-row-primary .hl-count {{ font-size:2.1rem; }}
      .mh-summary-title, .hl-row-primary .hl-name {{
        font-size:.95rem; line-height:1.2; letter-spacing:0;
      }}
      .mh-summary-note, .hl-note {{ font-size:.8rem; opacity:.75; line-height:1.3; }}
      .mh-summary-card[data-empty="true"] {{ opacity:1; }}
      .mh-summary-card[data-empty="true"] .mh-summary-value {{ color:{c["muted"]}; }}
      .mh-summary-card[data-tone="green"]:not([data-empty="true"]) .mh-summary-value,
      .hl-good, .fb-total-ok, .sp-ready .sp-state {{ color:{c["green"]}; }}
      .mh-summary-card[data-tone="orange"]:not([data-empty="true"]) .mh-summary-value,
      .hl-warn, .fb-total, .dl-warn, .sp-todo .sp-state {{ color:{c["orange"]}; }}
      .mh-summary-card[data-tone="red"]:not([data-empty="true"]) .mh-summary-value,
      .hl-bad, .dl-bad, .sp-error .sp-state, .fn-gap, .fn-drop {{ color:{c["red"]}; }}
      .mh-summary-card[data-tone="blue"]:not([data-empty="true"]) .mh-summary-value {{
        color:{c["primary"]};
      }}
      .sp-opt {{ color:{c["muted"]}; }}
      .sp-req {{ color:{c["red"]}; }}
      .rc-card-empty {{ opacity:1; }}
      .rc-card-empty .rc-count, .rc-card-empty .rc-name {{ color:{c["muted"]}; }}
      .rc-label, .fb-total-label, .sp-badge, .sp-state {{ letter-spacing:0; }}
      @media (max-width:640px) {{
        [data-testid="stMainBlockContainer"] {{ padding:2rem 1rem; }}
        h1 {{ font-size:2rem !important; overflow-wrap:anywhere; }}
        .mh-summary-card {{ flex-basis:140px; min-width:0; }}
      }}
    </style>
    """
