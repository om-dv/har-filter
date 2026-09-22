#!/usr/bin/env python3
"""
har_filter.py — strips a browser-exported HAR file down to only the
tracking/marketing-relevant requests, for use with the DataVinci No-Access
Audit SOP (v2.2).

Why this exists: a raw HAR — even with response bodies already stripped at
capture time per Step 2.1 — still contains every static asset, font,
stylesheet and third-party widget request. On a real journey capture that's
routinely 50-100MB+ once dumped as text, which blows past LLM context limits
long before it reaches Claude/Open WebUI. This script keeps only requests
that are actually tracking pixels/beacons, decodes batched GA4 POST bodies
and base64-obfuscated first-party proxy params, and writes a small, clean
JSON file plus a domain-frequency summary so nothing silently disappears.

Usage:
    python har_filter.py capture.har
    python har_filter.py capture.har --config patterns.json --outdir out/

Output (written next to --outdir, default ./filtered_out/):
    <name>_filtered.json   — matched tracking requests, ready to paste/upload
    <name>_domain_summary.csv — every domain seen, ranked by hit count
    <name>_review.json     — Tier 3 (keyword-only) matches that need a human glance

Stdlib only. No dependencies, so it runs anywhere Python 3 runs — including
free-tier Streamlit/Colab, per the DataVinci internal tooling notes.
"""

import argparse
import base64
import csv
import json
import sys
from pathlib import Path
from urllib.parse import urlparse, parse_qsl, unquote


# --------------------------------------------------------------------------
# Config loading
# --------------------------------------------------------------------------

def load_patterns(config_path: Path) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# Matching logic
# --------------------------------------------------------------------------

def match_tier1(url_lower: str, patterns: dict):
    for entry in patterns["tier1"]:
        for pat in entry["match"]:
            if pat.lower() in url_lower:
                return entry["platform"]
    return None


def try_base64_decode(value: str) -> str | None:
    """Attempt to base64-decode a query param value. Returns decoded text
    if it looks like plausible ASCII/UTF-8, else None."""
    if not value or len(value) < 6:
        return None
    candidate = unquote(value)
    # normalize urlsafe / padding
    candidate = candidate.replace("-", "+").replace("_", "/")
    padding = (-len(candidate)) % 4
    candidate += "=" * padding
    try:
        decoded = base64.b64decode(candidate, validate=False)
        text = decoded.decode("utf-8", errors="strict")
        # require it to look like a query-string fragment, not random bytes
        if "=" in text and all(32 <= ord(c) < 127 or c in "\r\n" for c in text[:200]):
            return text
    except Exception:
        return None
    return None


def match_tier2(url_lower: str, query_pairs: list, post_text: str, patterns: dict):
    """Structural signature match, including base64-decoded query param
    values (catches first-party proxies / sGTM / obfuscated paths)."""
    haystacks = [url_lower]
    if post_text:
        haystacks.append(post_text.lower())

    decoded_hits = {}
    for k, v in query_pairs:
        decoded = try_base64_decode(v)
        if decoded:
            decoded_hits[k] = decoded
            haystacks.append(decoded.lower())

    combined = "\n".join(haystacks)

    for platform, sig in patterns["tier2_signatures"].items():
        if any(req in combined for req in sig["required_any"]):
            return platform, decoded_hits
    return None, decoded_hits


def match_tier3(domain_lower: str, patterns: dict) -> bool:
    return any(kw in domain_lower for kw in patterns["tier3_keywords"])


def is_excluded_noise(domain_lower: str, patterns: dict) -> bool:
    return any(noise in domain_lower for noise in patterns["excluded_noise_domains"])


# --------------------------------------------------------------------------
# POST body parsing (GA4 batched payloads are the critical case per SOP
# Step 3.2's "CRITICAL PARSING RULE": text/plain body with events separated
# by \r\n, each line its own query-string-shaped event)
# --------------------------------------------------------------------------

def parse_post_body(post_data: dict):
    if not post_data:
        return None
    text = post_data.get("text")
    if not text:
        return None
    mime = (post_data.get("mimeType") or "").lower()

    # Batched text/plain payload (GA4's multi-event POST format)
    if "\r\n" in text or ("\n" in text and text.count("=") > text.count("\n")):
        lines = [ln for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]
        if len(lines) > 1 and all("=" in ln for ln in lines):
            return {
                "type": "batched_events",
                "count": len(lines),
                "events": [dict(parse_qsl(ln)) for ln in lines],
            }

    # Single query-string-shaped body
    if "=" in text and "&" in text and "{" not in text[:1]:
        return {"type": "querystring", "fields": dict(parse_qsl(text))}

    # JSON body
    if text.strip()[:1] in "{[":
        try:
            parsed = json.loads(text)
            return {"type": "json", "body": parsed}
        except Exception:
            pass

    # Fallback: raw text, truncated so one giant body can't blow the budget
    truncated = text[:2000]
    return {"type": "raw", "text": truncated, "truncated": len(text) > 2000}


# --------------------------------------------------------------------------
# Page-context tracking (which navigated page a given tracking hit belongs to)
# --------------------------------------------------------------------------

def is_document_nav(entry: dict) -> bool:
    if entry.get("_resourceType") == "document":
        return True
    resp = entry.get("response", {})
    mime = (resp.get("content", {}) or {}).get("mimeType", "")
    return mime.startswith("text/html") and entry["request"]["method"] == "GET"


# --------------------------------------------------------------------------
# Core filter
# --------------------------------------------------------------------------

def filter_har(har_path: Path, patterns: dict):
    with open(har_path, "r", encoding="utf-8", errors="replace") as f:
        data = json.load(f)

    entries = data.get("log", {}).get("entries", [])

    matched = []
    review = []
    domain_counts = {}
    domain_samples = {}

    current_page = None

    for idx, entry in enumerate(entries):
        request = entry.get("request", {})
        url = request.get("url", "")
        if not url:
            continue
        method = request.get("method", "GET")
        parsed = urlparse(url)
        domain = parsed.netloc
        domain_lower = domain.lower()
        url_lower = url.lower()

        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        domain_samples.setdefault(domain, url)

        if is_document_nav(entry):
            current_page = url

        if is_excluded_noise(domain_lower, patterns):
            continue

        query_pairs = parse_qsl(parsed.query)
        post_data = request.get("postData")
        post_parsed = parse_post_body(post_data)
        post_text_lower = (post_data.get("text") if post_data else "") or ""

        platform = match_tier1(url_lower, patterns)
        tier = 1
        decoded_hits = {}

        if not platform:
            platform, decoded_hits = match_tier2(
                url_lower, query_pairs, post_text_lower, patterns
            )
            tier = 2

        record = {
            "seq": idx,
            "time": entry.get("startedDateTime"),
            "method": method,
            "domain": domain,
            "url": url,
            "page_context": current_page,
            "query_params": dict(query_pairs),
            "post_body": post_parsed,
        }
        if decoded_hits:
            record["decoded_proxy_params"] = decoded_hits

        if platform:
            record["platform"] = platform
            record["tier"] = tier
            matched.append(record)
        elif match_tier3(domain_lower, patterns):
            record["tier"] = 3
            record["note"] = "unmatched domain, keyword fallback — needs manual review"
            review.append(record)
        # else: silently dropped (not tracking-relevant), still counted in domain_counts

    domain_summary = sorted(
        (
            {"domain": d, "count": c, "sample_url": domain_samples[d]}
            for d, c in domain_counts.items()
        ),
        key=lambda r: -r["count"],
    )

    return matched, review, domain_summary


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("har_file", type=Path, help="Path to the .har file exported per SOP Step 2.1")
    ap.add_argument("--config", type=Path, default=Path(__file__).parent / "patterns.json")
    ap.add_argument("--outdir", type=Path, default=Path("filtered_out"))
    args = ap.parse_args()

    if not args.har_file.exists():
        sys.exit(f"HAR file not found: {args.har_file}")

    patterns = load_patterns(args.config)
    args.outdir.mkdir(parents=True, exist_ok=True)

    matched, review, domain_summary = filter_har(args.har_file, patterns)

    stem = args.har_file.stem
    filtered_path = args.outdir / f"{stem}_filtered.json"
    review_path = args.outdir / f"{stem}_review.json"
    domain_csv_path = args.outdir / f"{stem}_domain_summary.csv"

    with open(filtered_path, "w", encoding="utf-8") as f:
        json.dump(matched, f, indent=2, ensure_ascii=False)

    with open(review_path, "w", encoding="utf-8") as f:
        json.dump(review, f, indent=2, ensure_ascii=False)

    with open(domain_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["domain", "count", "sample_url"])
        writer.writeheader()
        writer.writerows(domain_summary)

    total_in = len(domain_summary) and sum(r["count"] for r in domain_summary)
    print(f"Input requests scanned : {total_in}")
    print(f"Tracking hits kept     : {len(matched)}  -> {filtered_path}")
    print(f"Needs manual review    : {len(review)}  -> {review_path}")
    print(f"Unique domains seen    : {len(domain_summary)}  -> {domain_csv_path}")


if __name__ == "__main__":
    main()
