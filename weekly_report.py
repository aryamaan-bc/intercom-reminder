#!/usr/bin/env python3
"""Intercom Weekly Trend Report — rolling 7 days.

Runs weekly via GitHub Actions. Posts a Slack DM with:
  - volume by category (Intercom topics → canonical buckets)
  - median/p90 response times (first reply + follow-ups)
  - top N repeat-question clusters (aggressive token-based clustering)
  - wk-over-wk deltas

Required env vars:
    INTERCOM_TOKEN
    SLACK_USER_TOKEN
"""

import json
import os
import re
import sys
import time
from collections import defaultdict
from html import unescape
from urllib import request

INTERCOM_TOKEN = os.environ.get("INTERCOM_TOKEN", "")
SLACK_TOKEN = os.environ.get("SLACK_USER_TOKEN", "")
ARYAMAAN_SLACK = "U0ASWEQBQHF"

EXCLUDED_DOMAINS = ("basiccapital.com", "textql.com")

# Intercom topic name → canonical bucket. Case-insensitive.
# Ordered: more-specific topics should appear before less-specific ones.
TOPIC_CATEGORIES = {
    "Withdrawals": "Withdrawals & Rollovers",
    "like withdraw": "Withdrawals & Rollovers",
    "outbound rollover": "Withdrawals & Rollovers",
    "rollovers": "Withdrawals & Rollovers",
    "contribution": "Contributions",
    "like change": "Contributions",
    "ira": "Contributions",
    "backdoor roth": "Contributions",
    "mbdr": "Contributions",
    "identity verification": "IDV / Onboarding",
    "idv": "IDV / Onboarding",
    "onboarding": "IDV / Onboarding",
    "name change": "Account Changes",
    "email change": "Account Changes",
    "phone": "Account Changes",
    "tax": "Tax Docs",
    "1099": "Tax Docs",
    "5498": "Tax Docs",
    "bug": "Platform Issues",
    "error": "Platform Issues",
    "2fa": "Platform Issues",
    "retirement mortgage": "Retirement Mortgage",
    "financing": "Retirement Mortgage",
    "employer": "Employer / Plan Sponsor",
    "match": "Employer / Plan Sponsor",
}

# Fallback: AI Title keywords → category (used when no Intercom topic matches).
AI_TITLE_KEYWORDS = [
    (r"\bwithdraw|withdrawal|close account|outbound rollover|transfer out\b", "Withdrawals & Rollovers"),
    (r"\brollover (?:in|from|request)|rolling over|transfer in\b", "Withdrawals & Rollovers"),
    (r"\brollover\b", "Withdrawals & Rollovers"),
    (r"\broth conversion|backdoor|mega backdoor|mbdr\b", "Contributions"),
    (r"\bcontribut|(?:un)?invest|allocation|balance (?:inquiry|not)\b", "Contributions"),
    (r"\bemployer match|paycheck|payroll|deduction|deferral\b", "Employer / Plan Sponsor"),
    (r"\bidv|identity verif|onefootprint|id expired|invite acceptance\b", "IDV / Onboarding"),
    (r"\b1099|5498|tax doc|tax form\b", "Tax Docs"),
    (r"\bname change|email (?:change|provided|update)|phone number\b", "Account Changes"),
    (r"\b2fa|password|login|something went wrong|app (?:bug|issue)\b", "Platform Issues"),
    (r"\bretirement mortgage|financing|llc|leverage\b", "Retirement Mortgage"),
]

# Titles to drop from clustering — these are admin-logged events or outbound
# system emails, not customer questions.
CLUSTER_DENY_REGEXES = [
    re.compile(r"^inbound phone call", re.I),
    re.compile(r"^re:\s*(?:annual notice|summary plan description|spd)\b", re.I),
    re.compile(r"^(?:schedule|scheduled) call\b", re.I),
    re.compile(r"^conversation \d+$", re.I),
]

# ── Intercom helpers ────────────────────────────────────────────────────


def ic(method, path, body=None):
    url = f"https://api.intercom.io{path}"
    data = json.dumps(body).encode() if body else None
    r = request.Request(url, data=data, method=method)
    r.add_header("Authorization", f"Bearer {INTERCOM_TOKEN}")
    r.add_header("Accept", "application/json")
    r.add_header("Intercom-Version", "2.11")
    if body:
        r.add_header("Content-Type", "application/json")
    try:
        with request.urlopen(r, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  API ERROR {method} {path}: {e}", file=sys.stderr)
        return None


def slack_dm(user_id, text):
    body = json.dumps({"users": user_id}).encode()
    req = request.Request(
        "https://slack.com/api/conversations.open", data=body, method="POST"
    )
    req.add_header("Authorization", f"Bearer {SLACK_TOKEN}")
    req.add_header("Content-Type", "application/json")
    try:
        with request.urlopen(req, timeout=15) as resp:
            dm = json.loads(resp.read())
        ch = dm.get("channel", {}).get("id")
        if not ch:
            print(f"  Slack open failed: {dm}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"  Slack open error: {e}", file=sys.stderr)
        return False
    body = json.dumps({"channel": ch, "text": text}).encode()
    req = request.Request(
        "https://slack.com/api/chat.postMessage", data=body, method="POST"
    )
    req.add_header("Authorization", f"Bearer {SLACK_TOKEN}")
    req.add_header("Content-Type", "application/json")
    try:
        with request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
        if not result.get("ok"):
            print(f"  Slack post failed: {result.get('error')}", file=sys.stderr)
            return False
        print(f"  Slack DM sent to {user_id}", file=sys.stderr)
        return True
    except Exception as e:
        print(f"  Slack post error: {e}", file=sys.stderr)
        return False


def fetch_convs(since_ts):
    """Paginate POST /conversations/search for all convs updated since `since_ts`."""
    q = {
        "query": {"field": "updated_at", "operator": ">", "value": since_ts},
        "pagination": {"per_page": 50},
    }
    convs = []
    while True:
        r = ic("POST", "/conversations/search", q)
        if not r:
            break
        convs.extend(r.get("conversations", []))
        nxt = (r.get("pages") or {}).get("next")
        sa = nxt.get("starting_after") if isinstance(nxt, dict) else None
        if not sa:
            break
        q["pagination"]["starting_after"] = sa
    return convs


def is_excluded(conv):
    src = conv.get("source") or {}
    email = ((src.get("author") or {}).get("email") or "").lower()
    if any(email.endswith("@" + d) for d in EXCLUDED_DOMAINS):
        return True
    # Admin-logged phone-call records: not written customer support tickets.
    body = re.sub(r"<[^>]+>", " ", src.get("body") or "").strip().lower()
    if body in ("inbound phone call", "outbound phone call"):
        return True
    return False


# ── Categorization ──────────────────────────────────────────────────────


def category_for(full):
    topics = [(t or {}).get("name", "").lower()
              for t in ((full.get("topics") or {}).get("topics") or [])]
    for t in topics:
        for key, bucket in TOPIC_CATEGORIES.items():
            if key.lower() == t:
                return bucket
    # Fallback: AI Title keyword match
    ai = ((full.get("custom_attributes") or {}).get("AI Title") or "").lower()
    if ai:
        for pat, bucket in AI_TITLE_KEYWORDS:
            if re.search(pat, ai):
                return bucket
    return "Other"


def is_cluster_denied(title):
    return any(r.search(title) for r in CLUSTER_DENY_REGEXES)


def has_customer_message(full):
    for p in (full.get("conversation_parts") or {}).get("conversation_parts") or []:
        a = p.get("author") or {}
        if a.get("type") == "user" and (p.get("body") or "").strip():
            return True
    src = full.get("source") or {}
    if ((src.get("author") or {}).get("type") in ("user", "lead")
            and (src.get("body") or "").strip()):
        return True
    return False


# ── Response times ─────────────────────────────────────────────────────


def response_times(full):
    """Returns (ttfr_seconds_or_None, [followup_seconds, ...])."""
    stats = full.get("statistics") or {}
    ttfr = stats.get("time_to_admin_reply")
    first_admin = stats.get("first_admin_reply_at")

    followups = []
    parts = (full.get("conversation_parts") or {}).get("conversation_parts") or []
    last_user_ts = None
    for p in parts:
        if p.get("part_type") != "comment":
            continue
        a = p.get("author") or {}
        t = a.get("type")
        ts = p.get("created_at") or 0
        body = (p.get("body") or "").strip()
        if not body:
            continue
        if t == "user":
            last_user_ts = ts
        elif t == "admin" and last_user_ts and ts > last_user_ts:
            # Skip if this admin part IS the first-reply already counted in TTFR
            if first_admin and abs(ts - first_admin) < 2:
                last_user_ts = None
                continue
            followups.append(ts - last_user_ts)
            last_user_ts = None
    return ttfr, followups


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return None
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2


def p90(xs):
    xs = sorted(xs)
    if not xs:
        return None
    k = max(0, int(round(0.9 * (len(xs) - 1))))
    return xs[k]


def fmt_dur(seconds):
    if seconds is None:
        return "—"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


# ── Clustering for repeat questions ─────────────────────────────────────

_STOPWORDS = set((
    "a an the is are was were be been being am of to for in on at by with from "
    "i you my me our we your their his her they them he she it its this that "
    "these those and or but if then so as about regarding re please hi hello hey "
    "can could should would will want need would like trying try have has had "
    "do does did done how what when where why who get got gets getting any some "
    "also just only still yet even much more most less few "
).split())

_STEMS = {
    "withdrawal": "withdraw", "withdrawals": "withdraw", "withdrawing": "withdraw",
    "withdrawn": "withdraw", "withdrew": "withdraw",
    "contributions": "contribution", "contributed": "contribution",
    "contributing": "contribution", "contribute": "contribution",
    "rollovers": "rollover", "rolling": "rollover", "rolled": "rollover", "roll": "rollover",
    "deposits": "deposit", "depositing": "deposit", "deposited": "deposit",
    "transfers": "transfer", "transferring": "transfer", "transferred": "transfer",
    "accounts": "account",
    "funds": "fund", "funded": "fund", "funding": "fund",
    "enrollments": "enrollment", "enrolling": "enrollment", "enrolled": "enrollment",
    "enroll": "enrollment",
    "verifications": "verification", "verifying": "verification", "verify": "verification",
    "passwords": "password",
    "statements": "statement",
    "taxes": "tax",
    "employers": "employer", "employment": "employer",
    "paychecks": "paycheck",
    "distributions": "distribution", "distributed": "distribution",
    "conversions": "conversion", "converted": "conversion", "converting": "conversion",
    "convert": "conversion",
    "beneficiaries": "beneficiary",
    "changes": "change", "changed": "change", "changing": "change",
    "updates": "update", "updated": "update", "updating": "update",
    "questions": "question",
}


def tokens_of(s):
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    toks = []
    for raw in s.split():
        if len(raw) <= 2 or raw in _STOPWORDS:
            continue
        toks.append(_STEMS.get(raw, raw))
    return toks


def jaccard(a, b):
    if not a or not b:
        return 0.0
    a, b = set(a), set(b)
    return len(a & b) / len(a | b)


def cluster_titles(items, threshold=0.5, min_size=2):
    """items: list of dicts {title, conv_id}. Returns clusters with size >= min_size."""
    clusters = []
    for it in items:
        toks = set(tokens_of(it["title"]))
        if not toks:
            continue
        best = None
        best_sim = 0.0
        for c in clusters:
            sim = jaccard(toks, c["tokens"])
            if sim > best_sim and sim >= threshold:
                best = c
                best_sim = sim
        if best is not None:
            best["items"].append(it)
            best["tokens"] |= toks
        else:
            clusters.append({"tokens": toks, "items": [it]})
    clusters = [c for c in clusters if len(c["items"]) >= min_size]
    clusters.sort(key=lambda c: -len(c["items"]))
    return clusters


def cluster_label(cluster):
    """Pick the shortest non-empty title in the cluster as the label."""
    titles = [x["title"] for x in cluster["items"]]
    titles.sort(key=len)
    return titles[0]


# ── Main ────────────────────────────────────────────────────────────────


def render_report(cur, prev):
    """cur, prev: dicts produced by compute_window(). Prev may be None."""
    now = int(time.time())
    from datetime import datetime, timezone
    d_from = datetime.fromtimestamp(now - 7 * 86400, tz=timezone.utc).strftime("%Y-%m-%d")
    d_to = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")

    lines = []
    lines.append(f":chart_with_upwards_trend: *Intercom Weekly Trend Report* — {d_from} \u2192 {d_to}")
    lines.append("")

    # Volume
    delta_vol = (cur["n"] - prev["n"]) if prev else None
    arrow = ""
    if delta_vol is not None:
        arrow = f"  ({':arrow_up:' if delta_vol > 0 else ':arrow_down:' if delta_vol < 0 else '='} {abs(delta_vol)} vs prior week)"
    lines.append(f"*Volume:* {cur['n']} tickets touched (updated in window){arrow}")
    lines.append("")

    # Categories
    lines.append("*Top categories:*")
    sorted_cats = sorted(cur["categories"].items(), key=lambda x: -x[1])
    total = max(1, cur["n"])
    for i, (cat, n) in enumerate(sorted_cats[:6], 1):
        pct = round(100 * n / total)
        lines.append(f"  {i}. {cat} \u2014 {n}  ({pct}%)")
    lines.append("")

    # Response times — for latency, down = faster = good
    def cmp(cur_val, prev_val):
        if cur_val is None or prev_val is None:
            return ""
        delta = cur_val - prev_val
        if abs(delta) < 60:
            return ""
        qualifier = "faster" if delta < 0 else "slower"
        arrow = ":arrow_down:" if delta < 0 else ":arrow_up:"
        return f"  ({arrow} {fmt_dur(abs(delta))} {qualifier} vs prior week)"

    lines.append("*Response times:*")
    lines.append(
        f"  First reply  \u2014 median {fmt_dur(cur['ttfr_median'])}, p90 {fmt_dur(cur['ttfr_p90'])}"
        + (cmp(cur["ttfr_median"], prev["ttfr_median"]) if prev else "")
    )
    lines.append(
        f"  Follow-ups   \u2014 median {fmt_dur(cur['fu_median'])}, p90 {fmt_dur(cur['fu_p90'])}"
        + (cmp(cur["fu_median"], prev["fu_median"]) if prev else "")
    )
    lines.append("")

    # Clusters
    lines.append("*Top repeat questions (aggressive clustering):*")
    for i, c in enumerate(cur["clusters"][:5], 1):
        label = cluster_label(c)
        lines.append(f"  {i}. *{label}* \u2014 {len(c['items'])}\u00d7")
        samples = [x["title"] for x in c["items"][:3] if x["title"] != label]
        for s in samples[:2]:
            lines.append(f"     \u2022 _{s}_")

    return "\n".join(lines)


def compute_window(convs, window_start, window_end):
    """convs: already-filtered list; window_start/end: epoch bounds.
    Returns dict with n, categories, ttfr_median, ttfr_p90, fu_median, fu_p90, clusters."""
    in_window = [c for c in convs if window_start <= c.get("updated_at", 0) < window_end]

    categories = defaultdict(int)
    ttfrs = []
    followups_all = []
    title_items = []

    for c in in_window:
        cid = c["id"]
        full = ic("GET", f"/conversations/{cid}?display_as=plaintext")
        if not full:
            continue
        if is_excluded(full):
            continue
        categories[category_for(full)] += 1
        ttfr, fus = response_times(full)
        if ttfr is not None:
            ttfrs.append(ttfr)
        followups_all.extend(fus)
        src = full.get("source") or {}
        ai_title = (full.get("custom_attributes") or {}).get("AI Title")
        title = (ai_title or full.get("title") or src.get("subject") or
                 unescape(re.sub(r"<[^>]+>", " ", src.get("body") or "")).strip()[:80])
        title = (title or "").strip()
        # Only cluster real customer questions: needs a customer message,
        # and title must not be an admin-logged/outbound-system artifact
        if title and has_customer_message(full) and not is_cluster_denied(title):
            title_items.append({"title": title, "conv_id": cid})

    return {
        "n": sum(categories.values()),
        "categories": dict(categories),
        "ttfr_median": median(ttfrs),
        "ttfr_p90": p90(ttfrs),
        "fu_median": median(followups_all),
        "fu_p90": p90(followups_all),
        "clusters": cluster_titles(title_items),
    }


def main():
    if not INTERCOM_TOKEN:
        print("ERROR: INTERCOM_TOKEN not set", file=sys.stderr)
        return 1
    if not SLACK_TOKEN:
        print("ERROR: SLACK_USER_TOKEN not set", file=sys.stderr)
        return 1

    print("Testing Intercom connectivity...", file=sys.stderr)
    test = ic("GET", "/me")
    if not test:
        slack_dm(ARYAMAAN_SLACK, ":warning: Intercom Weekly Report: failed to connect to Intercom API.")
        return 1
    print(f"Connected: {(test.get('app') or {}).get('name')}", file=sys.stderr)

    now = int(time.time())
    week_start = now - 7 * 86400
    prev_week_start = now - 14 * 86400

    # Fetch everything touched in the last 14 days (one search, then split)
    convs = fetch_convs(prev_week_start)
    convs = [c for c in convs if not is_excluded(c)]
    print(f"{len(convs)} convs updated in last 14d (excluded filter applied)", file=sys.stderr)

    include_prev = "--no-compare" not in sys.argv
    cur = compute_window(convs, week_start, now + 1)
    prev = compute_window(convs, prev_week_start, week_start) if include_prev else None

    text = render_report(cur, prev)
    if "--dry-run" in sys.argv:
        print(text)
        return 0

    # DM to user only (per scope for v1)
    slack_dm(ARYAMAAN_SLACK, text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
