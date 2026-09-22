# HAR Tracking Filter

Strips a browser-exported HAR down to only tracking/marketing-relevant
requests, so it's small enough to paste into Claude / Open WebUI without
blowing past context limits. Built for the DataVinci No-Access Audit SOP
(v2.2), Section 2/3.

## Files

- `har_filter.py` — the actual filter. Stdlib only, no dependencies.
- `patterns.json` — Tier 1 domain/path patterns (edit this, not the script,
  when a new endpoint shows up).
- `streamlit_app.py` — browser front end, for hosting on Streamlit Cloud.
- `test_sample.har` — a small synthetic HAR covering the tricky cases
  (batched GA4 POST, base64-obfuscated first-party proxy, unknown-vendor
  keyword fallback, noise domains) — run the script against it to sanity
  check after any edit.

## How the filtering works

Three tiers, matching the SOP's own Step 1A/1B/1C/1D logic:

1. **Tier 1 — known domain + path.** The baseline list from the SOP
   (GA4, GTM, Google Ads, Meta, Microsoft Ads, TikTok, Snapchat, Pinterest,
   LinkedIn, Reddit, Criteo, Klaviyo, Hotjar, Adform, etc).
2. **Tier 2 — structural signature, not domain.** Catches first-party
   proxies and server-side GTM: if a request doesn't match Tier 1, the
   script checks whether its query string (or any base64-decoded query
   param) contains GA4/Meta/Ads-shaped parameters (`tid=G-`, `ev=PageView`,
   `gclid=`, etc). This is what catches a setup like AQON PURE's
   `s.aqon-pure.com` proxy, where the real GA4/GTM payload is base64-encoded
   inside an opaque query param on a domain that means nothing on its own.
3. **Tier 3 — keyword fallback, flagged not dropped.** A domain that
   contains a tracking-adjacent word (`analytics`, `pixel`, `track`, etc)
   but matched nothing else goes into a separate `_review.json` file
   instead of being silently discarded.

GA4's batched POST bodies (multiple `\r\n`-separated events in one request,
per the SOP's "CRITICAL PARSING RULE") are split into individual events
automatically, so the filtered output already has one clean event per line
rather than a wall of raw text.

Known non-marketing noise (fonts, Trustpilot, CookieYes, Cloudflare,
Tawk.to, PCI iframe hosts, etc) is dropped from the output but still
counted in the domain summary, so nothing vanishes invisibly — a quick scan
of the summary CSV catches anything the filter didn't anticipate.

## Running it locally

```bash
python3 har_filter.py path/to/capture.har
```

Outputs land in `filtered_out/`:
- `<name>_filtered.json` — the tracking requests, ready to paste/upload
- `<name>_review.json` — Tier 3 matches needing a manual glance
- `<name>_domain_summary.csv` — every domain seen, ranked by hit count

## Deploying for the team (no local Python needed)

Streamlit Community Cloud is free and needs no login for analysts to use it.

1. Push this folder to a GitHub repo (public or private — Streamlit Cloud
   supports both with a free account).
2. Go to [share.streamlit.io](https://share.streamlit.io), sign in with
   GitHub, click "New app," point it at this repo and `streamlit_app.py`.
3. Deploy. You get a URL like `dvtracker.streamlit.app`.

Analyst workflow from there: open the link, drop the HAR, download the
filtered bundle. No code, no environment setup, no Claude Pro required —
the filtering happens entirely in the browser session on Streamlit's free
tier before anything reaches an LLM.

## Updating patterns

If a client's stack uses an endpoint the filter doesn't catch yet (new ad
platform, a differently-shaped first-party proxy), add it to
`patterns.json` — either as a new Tier 1 domain/path entry, or as a new
Tier 2 signature if it's structurally variable. No code changes needed.
