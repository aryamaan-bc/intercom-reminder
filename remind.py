#!/usr/bin/env python3
"""Intercom Daily Reminder — GitHub Actions version.

Finds open Intercom conversations where an external customer has been
waiting 1–30 days for a reply. Filters acks, phone handoffs, internal
tickets, and tickets with recent admin notes. Posts Slack DMs via the
Slack Web API using a User OAuth Token.

Required env vars:
    INTERCOM_TOKEN  — Intercom Personal Access Token
    SLACK_USER_TOKEN — Slack User OAuth Token (xoxp-…) with chat:write, im:write
"""

import json
import os
import re
import sys
import time
from html import unescape
from urllib import error, request

# ── config ──────────────────────────────────────────────────────────────

INTERCOM_TOKEN = os.environ.get("INTERCOM_TOKEN", "")
SLACK_TOKEN = os.environ.get("SLACK_USER_TOKEN", "")
APP_ID = "k7w32l2g"
MIN_DAYS, MAX_DAYS = 1, 30
NOTE_WINDOW_SEC = 24 * 3600

ARYAMAAN_SLACK = "U0ASWEQBQHF"
TAYLOR_SLACK = "U0A83N4HHSN"

# Excluded email domains (test accounts, internal)
EXCLUDED_DOMAINS = ("basiccapital.com", "textql.com")

# Excluded conversation IDs (one-offs handled off-channel).
# Tip: instead of adding IDs here, close the Intercom conversation or add
# an admin note — both will remove it from the reminder automatically.
EXCLUDED_CONVERSATIONS = {
    "215473775499392",  # Daniel Brass — reached out on another chat
    "215473563201973",  # Benjamin Bartolome — solved on call
}

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


# ── Slack helpers ───────────────────────────────────────────────────────


def _slack_call(endpoint, body):
    req = request.Request(
        f"https://slack.com/api/{endpoint}", data=json.dumps(body).encode(), method="POST"
    )
    req.add_header("Authorization", f"Bearer {SLACK_TOKEN}")
    req.add_header("Content-Type", "application/json")
    with request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def slack_open_dm(user_id):
    try:
        dm = _slack_call("conversations.open", {"users": user_id})
        ch = dm.get("channel", {}).get("id")
        if not ch:
            print(f"  Slack conversations.open failed for {user_id}: {dm}", file=sys.stderr)
        return ch
    except Exception as e:
        print(f"  Slack conversations.open error: {e}", file=sys.stderr)
        return None


def slack_post_message(channel, text, thread_ts=None):
    """Post a message to a Slack channel. Returns the message ts on success, else None."""
    body = {"channel": channel, "text": text}
    if thread_ts:
        body["thread_ts"] = thread_ts
    try:
        result = _slack_call("chat.postMessage", body)
        if not result.get("ok"):
            print(f"  Slack postMessage failed: {result.get('error')}", file=sys.stderr)
            return None
        return result.get("ts")
    except Exception as e:
        print(f"  Slack postMessage error: {e}", file=sys.stderr)
        return None


def slack_dm(user_id, text):
    """Back-compat: open DM and post. Returns (channel, ts) or (None, None)."""
    ch = slack_open_dm(user_id)
    if not ch:
        return None, None
    ts = slack_post_message(ch, text)
    if ts:
        print(f"  Slack DM sent to {user_id}", file=sys.stderr)
    return ch, ts


def slack_post(channel_id, text):
    """Legacy wrapper: open DM + post, return bool."""
    _, ts = slack_dm(channel_id, text)
    return ts is not None


# ── text / filter helpers ───────────────────────────────────────────────

ACK_PHRASES = (
    "thanks", "thank you", "thx", "tyanks", "thnks", "sounds good", "got it",
    "perfect", "awesome", "amazing", "wonderful", "great", "ok", "okay",
    "alright", "ok thanks", "ok thank you", "okay thanks", "appreciate it",
    "much appreciated", "will do", "no worries", "all good", "have a great day",
    "have a good day", "apologies for the confusion", "sorry for the confusion",
    "understood", "makes sense", "that works", "no problem", "not a problem",
    "noted", "all set", "good to know", "that helps", "no further questions",
    "nothing else", "that's all", "thats all", "cheers", "glad to hear",
    "works for me", "it worked", "that worked", "talk to you then", "talk then",
    "i'm good", "im good", "i am good",
)

ACK_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(p).replace(r"\ ", r"\s+") for p in ACK_PHRASES) + r")\b",
    re.I,
)
FOLLOWUP_RE = re.compile(
    r"(\?|\bstill\b|\bbut\b|\bhowever\b|\bthough\b|\bissue\b|\bproblem\b|"
    r"\berror\b|\bbug\b|\bstuck\b|\bbroken\b|\bdoesn'?t\b|\bcan'?t\b|"
    r"\bwon'?t\b|\bnot\s+working\b|\bany\s+update\b|\bany\s+news\b|\bbumping\b|"
    r"\blet'?s\s+(?:pause|roll|transfer|move|do)\b)",
    re.I,
)
CALL_RE = re.compile(
    r"(jump\s+on\s+a\s+call|hop\s+on\s+a\s+call|"
    r"have\s+time\s+(?:for|to\s+discuss)\s+[^.?!]{0,30}?\s*call|"
    r"discuss\s+on\s+[^.?!]{0,30}?\s*call|schedule\s+a\s+(?:call|chat|sync)|"
    r"give\s+me\s+a\s+call|what'?s\s+your\s+phone|get\s+your\s+(?:phone|number))",
    re.I,
)
PHONE_RE = re.compile(r"^\s*[\d\s\-()+.]{7,20}\s*$")


def txt(s):
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</p\s*>", "\n", s, flags=re.I)
    return unescape(re.sub(r"<[^>]+>", " ", s)).strip()


def is_ack(t):
    t = t.strip().lower()
    if not t or len(t.split()) > 25 or not ACK_RE.search(t):
        return False
    scrubbed = re.sub(
        r"\bno(?:\s+(?:problem|issue|further\s+questions))\b|\bnot\s+a\s+problem\b", " ", t
    )
    return not FOLLOWUP_RE.search(scrubbed)


def is_handoff(full):
    parts = (full.get("conversation_parts") or {}).get("conversation_parts") or []
    la = next(
        (p for p in reversed(parts)
         if (p.get("author") or {}).get("type") == "admin"
         and (p.get("body") or "").strip()
         and p.get("part_type") != "note"),
        None,
    )
    lu = next(
        (p for p in reversed(parts)
         if (p.get("author") or {}).get("type") == "user"
         and (p.get("body") or "").strip()),
        None,
    )
    if not la or not lu:
        return False
    if not CALL_RE.search(txt(la.get("body", ""))):
        return False
    u = txt(lu.get("body", "")).strip()
    if PHONE_RE.match(u):
        return True
    digs = sum(c.isdigit() for c in u)
    return digs >= 7 and len(u.split()) <= 15


def has_recent_note(full, now):
    cutoff = now - NOTE_WINDOW_SEC
    for p in (full.get("conversation_parts") or {}).get("conversation_parts") or []:
        if p.get("part_type") != "note":
            continue
        if (p.get("author") or {}).get("type") != "admin":
            continue
        latest = max(int(p.get("created_at") or 0), int(p.get("updated_at") or 0))
        if latest >= cutoff:
            return True
    return False


# ── main ────────────────────────────────────────────────────────────────


def render(entries, suffix_note=True):
    if not entries:
        return ":white_check_mark: No Intercom tickets awaiting reply (1\u201330d). Inbox zero."
    lines = [
        f":bell: *{len(entries)} Intercom ticket{'s' if len(entries) != 1 else ''}"
        f" with customer waiting {MIN_DAYS}\u2013{MAX_DAYS}d*",
        "",
    ]
    for e in entries:
        label = f"{e['hours'] // 24}d" if e["hours"] >= 48 else f"{e['hours']}h"
        tag = "  :memo:" if (suffix_note and e.get("has_recent_note")) else ""
        lines.append(f"\u2022 {label} \u2014 _{e['customer']}_ \u2014 <{e['url']}|{e['title']}>{tag}")
    return "\n".join(lines)


def _fmt_when(ts):
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return "?"


def _is_deflection(body):
    b = body.lower()
    return ("another" in b and "thread" in b) or ("continue" in b and "there" in b)


def render_enrichment(entry, full, max_turns=6, max_chars=240):
    """Per-ticket thread reply: topics + AI title + customer/admin message arc."""
    src = full.get("source") or {}
    auth = src.get("author") or {}
    topics = [(t or {}).get("name") for t in ((full.get("topics") or {}).get("topics") or [])]
    topics = [t for t in topics if t]
    ai_title = (full.get("custom_attributes") or {}).get("AI Title")

    # Build message arc: source body (original customer msg) + subsequent parts
    turns = []
    src_body = txt(src.get("body") or "")
    if src_body:
        turns.append(("user", auth.get("name") or "Customer",
                      src.get("delivered_at") or src.get("created_at"), src_body))
    for p in (full.get("conversation_parts") or {}).get("conversation_parts") or []:
        if p.get("part_type") != "comment":
            continue
        a = p.get("author") or {}
        t = a.get("type")
        if t not in ("user", "admin"):
            continue
        body = txt(p.get("body") or "")
        if not body:
            continue
        if t == "admin" and _is_deflection(body):
            continue
        turns.append((t, a.get("name") or t.title(),
                      p.get("created_at"), body))

    # Keep the last `max_turns` turns (most recent context)
    turns = turns[-max_turns:]

    lines = []
    lines.append(f"*<{entry['url']}|{entry['title']}>*  _({entry['customer']})_")
    meta = []
    if ai_title and ai_title != entry["title"]:
        meta.append(f"AI title: _{ai_title}_")
    if topics:
        meta.append(f"Topics: {', '.join(topics)}")
    if meta:
        lines.append(" · ".join(meta))
    lines.append("")
    lines.append("*Thread arc:*")
    for t, name, ts, body in turns:
        label = "[customer]" if t == "user" else "[admin]"
        snippet = body[:max_chars] + ("\u2026" if len(body) > max_chars else "")
        # Collapse internal whitespace for Slack readability
        snippet = re.sub(r"\s+", " ", snippet).strip()
        lines.append(f"{label} *{name}* · {_fmt_when(ts)}")
        lines.append(f"> {snippet}")
    return "\n".join(lines)


def main():
    if not INTERCOM_TOKEN:
        print("ERROR: INTERCOM_TOKEN not set", file=sys.stderr)
        return 1
    if not SLACK_TOKEN:
        print("ERROR: SLACK_USER_TOKEN not set", file=sys.stderr)
        return 1

    # Connectivity test
    print("Testing Intercom connectivity...", file=sys.stderr)
    test = ic("GET", "/me")
    if not test:
        slack_post(ARYAMAAN_SLACK, ":warning: Intercom Reminder bot failed to connect to Intercom API.")
        return 1
    print(f"Connected: {(test.get('app') or {}).get('name')}", file=sys.stderr)

    now = int(time.time())
    cutoff = now - MAX_DAYS * 86_400

    # Search open conversations updated in the last MAX_DAYS days
    q = {
        "query": {
            "operator": "AND",
            "value": [
                {"field": "state", "operator": "=", "value": "open"},
                {"field": "updated_at", "operator": ">", "value": cutoff},
            ],
        },
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
    print(f"Found {len(convs)} open convs", file=sys.stderr)

    # Filter to customer-waiting within window
    waiting = []
    for c in convs:
        ws = c.get("waiting_since")
        a = (c.get("source") or {}).get("author") or {}
        if not ws or a.get("type") != "user":
            continue
        email = (a.get("email") or "").lower()
        if any(email.endswith("@" + d) for d in EXCLUDED_DOMAINS):
            continue
        wait = now - int(ws)
        if wait < MIN_DAYS * 86_400 or wait > MAX_DAYS * 86_400:
            continue
        waiting.append(c)
    print(f"{len(waiting)} waiting in window", file=sys.stderr)

    # Fetch each and apply filters
    entries = []
    for c in waiting:
        cid = c["id"]
        if str(cid) in EXCLUDED_CONVERSATIONS:
            continue
        full = ic("GET", f"/conversations/{cid}?display_as=plaintext")
        if not full:
            continue
        parts = (full.get("conversation_parts") or {}).get("conversation_parts") or []
        lu_parts = [
            p for p in parts
            if (p.get("author") or {}).get("type") == "user" and (p.get("body") or "").strip()
        ]
        last_msg = txt(
            lu_parts[-1].get("body")
            if lu_parts
            else ((full.get("source") or {}).get("body") or "")
        )
        if is_handoff(full):
            continue
        if is_ack(last_msg[:300]):
            continue
        src = full.get("source") or {}
        auth = src.get("author") or {}
        name = unescape(auth.get("name") or auth.get("email") or "Unknown")
        title = (
            full.get("title")
            or src.get("subject")
            or txt(src.get("body", ""))[:80]
            or f"Conversation {cid}"
        ).strip()
        if len(title) > 70:
            title = title[:67] + "\u2026"
        wh = (now - int(c["waiting_since"])) // 3600
        entries.append({
            "hours": wh,
            "customer": name,
            "title": title,
            "url": f"https://app.intercom.com/a/inbox/{APP_ID}/inbox/shared/all/conversation/{cid}",
            "has_recent_note": has_recent_note(full, now),
            "_full": full,
        })

    entries.sort(key=lambda e: -e["hours"])
    entries_taylor = [e for e in entries if not e.get("has_recent_note")]

    print(f"Done: {len(entries)} total, {len(entries_taylor)} for Taylor", file=sys.stderr)

    # Send DMs
    text_all = render(entries, suffix_note=True)
    text_taylor = render(entries_taylor, suffix_note=False)

    self_only = "--self-only" in sys.argv

    def post_with_enrichment(user_id, entries_for_thread, parent_text):
        channel = slack_open_dm(user_id)
        if not channel:
            return False
        parent_ts = slack_post_message(channel, parent_text)
        if not parent_ts:
            return False
        print(f"  Slack DM sent to {user_id}", file=sys.stderr)
        for e in entries_for_thread:
            reply = render_enrichment(e, e["_full"])
            ok_ts = slack_post_message(channel, reply, thread_ts=parent_ts)
            if not ok_ts:
                print(f"  Threaded reply failed for conv at {e['url']}", file=sys.stderr)
        return True

    # Aryamaan always — post parent DM + threaded enrichment per ticket
    post_with_enrichment(ARYAMAAN_SLACK, entries, text_all)

    # Taylor only if there are un-noted tickets (and not --self-only)
    if len(entries_taylor) > 0 and not self_only:
        post_with_enrichment(TAYLOR_SLACK, entries_taylor, text_taylor)
    elif self_only:
        print("--self-only: skipping Taylor DM", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
