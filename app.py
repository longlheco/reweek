"""Streamlit UI for CSV compare."""

from __future__ import annotations

import base64
from pathlib import Path

import pandas as pd
import streamlit as st
from botocore.exceptions import BotoCoreError, ClientError

from compare import JOIN_KEY, compare, label_from_name
from store import (
    QuotaError,
    configure,
    delete_csv,
    get_csv,
    list_inventory,
    put_csv,
    r2_enabled,
    usage,
)

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"

st.set_page_config(page_title="REWEEK", layout="wide")
_gotham = base64.b64encode((ROOT / "static" / "GothamBold.otf").read_bytes()).decode()
st.markdown(
    f"""
    <style>
    @font-face {{
      font-family: Gotham;
      src: url("data:font/otf;base64,{_gotham}") format("opentype");
      font-weight: 700;
    }}
    .stApp h1 {{
      font-family: Gotham, sans-serif !important;
      font-weight: 700 !important;
      letter-spacing: 0.04em;
    }}
    [class*="viewerBadge"],
    #GithubIcon {{
      display: none !important;
    }}
    </style>
    """,
    unsafe_allow_html=True,
)


def _secrets_r2() -> dict[str, object]:
    try:
        block = st.secrets.get("r2", {})
    except Exception:
        return {}
    return dict(block)


def _password() -> str | None:
    try:
        return str(st.secrets["password"])
    except Exception:
        return None


def _fmt_bytes(n: int) -> str:
    if n >= 1024**3:
        return f"{n / 1024**3:.2f} GiB"
    if n >= 1024**2:
        return f"{n / 1024**2:.2f} MiB"
    if n >= 1024:
        return f"{n / 1024:.1f} KiB"
    return f"{n} B"


def require_login() -> None:
    """Shared-password gate. Stops the script until session is authed."""
    expected = _password()
    if expected is None:
        st.error("Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml`.")
        st.stop()
    if st.session_state.get("ok"):
        return
    st.title("REWEEK")
    with st.form("login"):
        entered = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login")
    if submitted:
        if entered == expected:
            st.session_state.ok = True
            st.rerun()
        st.error("Sai mật khẩu")
    st.stop()


@st.dialog("Xác nhận xóa")
def confirm_delete(files: list[str]) -> None:
    """Modal: list files, Hủy or Xóa."""
    st.write(f"Xóa {len(files)} file? Không hoàn tác được từ app.")
    for name in files:
        st.markdown(f"- `{name}`")
    cancel, ok = st.columns(2)
    if cancel.button("Hủy", use_container_width=True):
        st.rerun()
    if ok.button("Xóa", type="primary", use_container_width=True):
        try:
            for name in files:
                delete_csv(name)
        except (ValueError, OSError, ClientError, BotoCoreError) as exc:
            st.error(str(exc))
            return
        st.session_state.store_rev += 1
        st.rerun()


def style_table(frame: pd.DataFrame, growth_cols: tuple[str, ...]) -> pd.Styler:
    """Green / red on Growth* columns; percent format. Numbers stay float in `frame`."""

    def _color(val: object) -> str:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return ""
        try:
            number = float(val)
        except (TypeError, ValueError):
            return ""
        if number > 0:
            return "color: #188038"
        if number < 0:
            return "color: #c5221f"
        return ""

    styler = frame.style
    growth = [col for col in growth_cols if col in frame.columns]
    value_cols = [
        col
        for col in frame.columns
        if col != JOIN_KEY and col not in growth
    ]

    def _num(val: object) -> str:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return "None"
        try:
            number = float(val)
        except (TypeError, ValueError):
            return "None"
        if number.is_integer():
            return f"{number:,.0f}"
        return f"{number:,.2f}"

    if value_cols:
        styler = styler.format(_num, subset=value_cols, na_rep="None")
    if growth:
        styler = styler.map(_color, subset=growth)
        styler = styler.format(
            lambda x: "None" if pd.isna(x) else f"{x:.2%}",
            subset=growth,
            na_rep="None",
        )
    return styler


require_login()
configure(_secrets_r2(), DATA_DIR)
if "store_rev" not in st.session_state:
    st.session_state.store_rev = 0


@st.cache_data(ttl=60)
def _cached_inventory(rev: int) -> list[tuple[str, int, object]]:
    _ = rev
    return list_inventory()


@st.cache_data(ttl=60)
def _cached_usage(rev: int) -> tuple[int, int]:
    _ = rev
    return usage()


st.title("REWEEK")
tab_compare, tab_files = st.tabs(["So sánh", "Quản lý file"])
try:
    inventory = _cached_inventory(st.session_state.store_rev)
except (ClientError, BotoCoreError, OSError) as exc:
    st.error(f"R2: {exc}")
    st.stop()
names = [row[0] for row in inventory]

with st.sidebar:
    used, cap = _cached_usage(st.session_state.store_rev)
    ratio = 0.0 if cap <= 0 else min(used / cap, 1.0)
    st.progress(ratio)
    st.caption(
        f"{_fmt_bytes(used)} / {_fmt_bytes(cap)}"
        + (
            " · R2"
            if r2_enabled()
            else " · local data/ (điền access_key_id + secret_access_key trong secrets)"
        )
    )
    uploaded = st.file_uploader(
        "Import CSV",
        type=["csv"],
        accept_multiple_files=True,
        key=f"import_{st.session_state.store_rev}",
    )
    if uploaded:
        try:
            for item in uploaded:
                put_csv(item.name, item.getvalue())
        except (QuotaError, ValueError, ClientError, BotoCoreError) as exc:
            st.error(str(exc))
        else:
            st.session_state.store_rev += 1
            st.rerun()
    if names:
        baseline_name = st.selectbox(
            "Đối chứng",
            names,
            index=len(names) - 1,
        )
        target_name = st.selectbox("Mục tiêu", names, index=0)
        if target_name == baseline_name:
            st.warning("Chọn hai file khác nhau.")
    else:
        target_name = ""
        baseline_name = ""
        st.caption("Chưa có CSV — import ở sidebar.")
    sidebar_extra = st.container()
    _ = sidebar_extra

with tab_files:
    files_df = pd.DataFrame(
        [
            {"file": name, "size": size, "updated": updated}
            for name, size, updated in inventory
        ]
    )
    if files_df.empty:
        st.caption("Không có file.")
    else:
        table_col, btn_col = st.columns([6, 1], vertical_alignment="top")
        with table_col:
            event = st.dataframe(
                files_df,
                width="content",
                height="auto",
                hide_index=True,
                on_select="rerun",
                selection_mode="multi-row",
                key="files_table",
                column_config={
                    "size": st.column_config.NumberColumn("size (bytes)", format="%d"),
                    "updated": st.column_config.DatetimeColumn(
                        "updated", format="YYYY-MM-DD HH:mm"
                    ),
                },
            )
        rows = list(event.selection.rows)
        selected = files_df["file"].iloc[rows].tolist() if rows else []
        with btn_col:
            st.caption(" ")
            if st.button(
                "Xóa",
                type="primary",
                disabled=not selected,
                use_container_width=True,
            ):
                confirm_delete(selected)
        if selected:
            st.caption(f"Đã chọn {len(selected)} file.")

with tab_compare:
    if len(names) < 2:
        st.warning("Cần ít nhất 2 file CSV. Import ở sidebar.")
    elif target_name != baseline_name:
        result = compare(
            get_csv(target_name),
            get_csv(baseline_name),
            label_from_name(target_name),
            label_from_name(baseline_name),
        )
        metrics = [name.removeprefix("Growth ") for name in result.growth_cols]
        picked = st.multiselect("Hiện bộ", metrics, default=metrics)
        keep = [JOIN_KEY]
        for metric in picked:
            keep.extend(
                [
                    f"{metric} {result.baseline_label}",
                    f"{metric} {result.target_label}",
                    f"Growth {metric}",
                ]
            )
        view = result.table[[col for col in keep if col in result.table.columns]]
        num_cols = [col for col in view.columns if col != JOIN_KEY]
        filter_l, filter_m, filter_r = st.columns(3)
        with filter_l:
            search_col = st.selectbox(
                "Search cột",
                list(view.columns) if len(view.columns) else [JOIN_KEY],
            )
        with filter_m:
            query = st.text_input("Contains")
        with filter_r:
            drop_none = st.multiselect("≠ None", num_cols)
        if query and search_col in view.columns:
            view = view[
                view[search_col].astype(str).str.contains(query, case=False, na=False)
            ]
        for col in drop_none:
            if col in view.columns:
                view = view[view[col].notna()]
        st.dataframe(
            style_table(view, tuple(f"Growth {m}" for m in picked)),
            width="content",
            height="auto",
            hide_index=True,
        )
        st.download_button(
            "Export CSV",
            data=view.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"reweek_{label_from_name(target_name)}_{label_from_name(baseline_name)}.csv",
            mime="text/csv",
        )
    below_table = st.container()
    _ = below_table
