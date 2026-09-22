"""
DataVinci HAR Tracking Filter — Streamlit front end.

Deploy this for free on Streamlit Community Cloud so analysts can strip a
HAR down to tracking-only requests in the browser, no Python or Claude Pro
needed on their end. See README.md for the two-step deploy.
"""

import io
import json
import zipfile
from pathlib import Path

import streamlit as st

from har_filter import filter_har, load_patterns

st.set_page_config(page_title="DataVinci HAR Tracking Filter", page_icon="🔎")

st.title("HAR Tracking Filter")
st.caption(
    "Upload a HAR file exported per the No-Access Audit SOP (Step 2.1). "
    "This strips it down to only marketing/analytics-relevant requests — "
    "GA4, Meta, Google Ads, and any secondary platforms detected — so what "
    "you paste into Claude / Open WebUI is small and clean instead of a "
    "60MB dump."
)

uploaded = st.file_uploader("HAR file", type=["har"])

if uploaded is not None:
    tmp_path = Path("/tmp") / uploaded.name
    tmp_path.write_bytes(uploaded.getvalue())

    config_path = Path(__file__).parent / "patterns.json"
    patterns = load_patterns(config_path)

    with st.spinner("Filtering..."):
        matched, review, domain_summary = filter_har(tmp_path, patterns)

    st.success(
        f"Kept {len(matched)} tracking requests · "
        f"{len(review)} flagged for manual review · "
        f"{len(domain_summary)} unique domains seen"
    )

    platforms_seen = sorted({r["platform"] for r in matched})
    if platforms_seen:
        st.write("**Platforms detected:** " + ", ".join(platforms_seen))

    if review:
        st.warning(
            f"{len(review)} request(s) matched a tracking-adjacent keyword "
            "but not a known platform pattern — check the review file before "
            "assuming they don't matter."
        )

    stem = Path(uploaded.name).stem
    filtered_bytes = json.dumps(matched, indent=2, ensure_ascii=False).encode("utf-8")
    review_bytes = json.dumps(review, indent=2, ensure_ascii=False).encode("utf-8")

    domain_csv_lines = ["domain,count,sample_url"]
    for row in domain_summary:
        sample = row["sample_url"].replace('"', '""')
        domain_csv_lines.append(f'{row["domain"]},{row["count"]},"{sample}"')
    domain_csv_bytes = "\n".join(domain_csv_lines).encode("utf-8")

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{stem}_filtered.json", filtered_bytes)
        zf.writestr(f"{stem}_review.json", review_bytes)
        zf.writestr(f"{stem}_domain_summary.csv", domain_csv_bytes)

    st.download_button(
        "Download all outputs (.zip)",
        data=zip_buf.getvalue(),
        file_name=f"{stem}_filtered_bundle.zip",
        mime="application/zip",
    )

    st.download_button(
        "Download filtered JSON only",
        data=filtered_bytes,
        file_name=f"{stem}_filtered.json",
        mime="application/json",
    )

    with st.expander("Preview filtered output"):
        st.json(matched[:10] if len(matched) > 10 else matched)
