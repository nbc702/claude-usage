#!/usr/bin/env python3
"""Claude plan usage dashboard.

Run on demand: reads the sign-in Claude Code already stored on this Mac,
fetches current usage, appends the numbers to a local history file, writes
~/claude-usage/dashboard.html and opens it. Nothing runs in the background.

Python 3 standard library only.
"""

import datetime as dt
import html
import json
import os
import platform
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

OUT_DIR = Path(os.environ.get("CLAUDE_USAGE_DIR", Path.home() / "claude-usage")).expanduser()
DASHBOARD = OUT_DIR / "dashboard.html"
HISTORY = OUT_DIR / "history.json"
HISTORY_DAYS = 90

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_SOURCE = "api.anthropic.com/api/oauth/usage (undocumented; the same call Claude Code's /usage makes)"
CRED_SOURCE = "Claude Code sign-in on this Mac (Keychain or ~/.claude/.credentials.json)"
HISTORY_SOURCE = "your past runs, stored in ~/claude-usage/history.json"

WINDOW_HOURS = {"session": 5, "weekly": 7 * 24}


class SourceError(Exception):
    """An error whose message is plain English, safe to show on the dashboard."""


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def parse_time(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        secs = value / 1000 if value > 1e11 else value
        return dt.datetime.fromtimestamp(secs, dt.timezone.utc)
    try:
        t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


# --------------------------------------------------------------------------
# Auth: reuse what Claude Code stored. The token stays in memory only.
# --------------------------------------------------------------------------

def _keychain_blob():
    if platform.system() != "Darwin":
        return None
    service = "Claude Code-credentials"
    attempts = [["-a", os.environ.get("USER", ""), "-s", service], ["-s", service]]
    for args in attempts:
        try:
            r = subprocess.run(["security", "find-generic-password", *args, "-w"],
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return None


def load_credentials():
    """Return (access_token, info) where info has no secrets in it."""
    blob, where = _keychain_blob(), "macOS Keychain"
    if blob is None:
        config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser()
        path = config_dir / ".credentials.json"
        if path.exists():
            blob, where = path.read_text(), str(path).replace(str(Path.home()), "~")
    if blob is None:
        raise SourceError(
            "Couldn't find a Claude Code sign-in on this Mac. Open Terminal, run `claude`, "
            "sign in with your Claude account, then run this again. If macOS asked to allow "
            "Keychain access and you clicked Deny, run it again and click Always Allow.")
    try:
        oauth = json.loads(blob).get("claudeAiOauth") or {}
    except (ValueError, AttributeError):
        raise SourceError(f"The Claude Code sign-in stored in {where} couldn't be read.")
    token = oauth.get("accessToken")
    if not token:
        raise SourceError(
            "Claude Code is signed in with an API key or console account, not a Claude plan "
            "(Pro/Max/Team). Plan limits are only available for Claude account sign-ins.")
    expires = parse_time(oauth.get("expiresAt"))
    info = {
        "where": where,
        "plan": oauth.get("subscriptionType"),
        "tier": oauth.get("rateLimitTier"),
        "expires": expires,
    }
    if expires and expires <= now_utc():
        raise SourceError(
            "Your Claude Code sign-in has expired. Open Terminal, run `claude`, send any "
            "message (it renews the sign-in automatically), quit it, then run this again.")
    return token, info


# --------------------------------------------------------------------------
# UNDOCUMENTED ENDPOINT. Everything that touches it lives in this function,
# so when Anthropic changes it, this is the only place to fix.
# --------------------------------------------------------------------------

def fetch_plan_usage(token):
    fixture = os.environ.get("CLAUDE_USAGE_FIXTURE")
    if fixture:  # offline testing only
        return json.loads(Path(fixture).read_text())

    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
        "User-Agent": "claude-usage-dashboard/1.0",
    })

    def call(ctx):
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        try:
            return call(ssl.create_default_context())
        except urllib.error.URLError as e:
            # python.org builds on macOS sometimes ship without CA certs; the
            # system bundle still gives full verification.
            if isinstance(e.reason, ssl.SSLCertVerificationError) and Path("/etc/ssl/cert.pem").exists():
                return call(ssl.create_default_context(cafile="/etc/ssl/cert.pem"))
            raise
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise SourceError(
                "Anthropic rejected the Claude Code sign-in (it has expired or was revoked). "
                "Run `claude` in Terminal once to renew it, then run this again.")
        if e.code == 403:
            raise SourceError(
                "This sign-in isn't allowed to read plan usage. Tokens made with "
                "`claude setup-token` can't; sign in normally with `claude` and /login.")
        if e.code == 429:
            raise SourceError("Anthropic is rate-limiting usage checks. Wait a minute and try again.")
        if e.code == 404:
            raise SourceError(
                "The usage endpoint wasn't found. It's undocumented and may have moved; "
                "fetch_plan_usage() in claude_usage.py needs updating.")
        raise SourceError(f"Anthropic's usage service returned an error (HTTP {e.code}). Try again later.")
    except urllib.error.URLError as e:
        raise SourceError(f"Couldn't reach Anthropic ({e.reason}). Check your internet connection.")
    except (TimeoutError, OSError):
        raise SourceError("Anthropic didn't answer in time. Check your connection and try again.")
    except ValueError:
        raise SourceError("Anthropic sent a response this script couldn't understand. "
                          "The endpoint may have changed; fetch_plan_usage() needs updating.")


LEGACY_WINDOWS = [
    ("five_hour", "Current session", "session"),
    ("seven_day", "Weekly · all models", "weekly"),
    ("seven_day_opus", "Weekly · Opus", "weekly"),
    ("seven_day_sonnet", "Weekly · Sonnet", "weekly"),
    ("seven_day_oauth_apps", "Weekly · connected apps", "weekly"),
]


def parse_windows(raw):
    """Normalize the response into [{key, label, kind, percent, resets_at}]."""
    windows = []
    limits = raw.get("limits")
    if isinstance(limits, list) and any(isinstance(x, dict) and "percent" in x for x in limits):
        for row in limits:
            if not isinstance(row, dict) or row.get("percent") is None:
                continue
            kind = str(row.get("kind") or "")
            group = str(row.get("group") or "")
            model = ((row.get("scope") or {}).get("model") or {}).get("display_name")
            if kind == "session":
                label, cls = "Current session", "session"
            elif kind == "weekly_all":
                label, cls = "Weekly · all models", "weekly"
            elif kind == "weekly_scoped" or (group == "weekly" and model):
                label, cls = f"Weekly · {model or 'model-specific'}", "weekly"
            else:
                label = (model or kind or group or "Limit").replace("_", " ").capitalize()
                cls = group if group in WINDOW_HOURS else None
            windows.append({"key": f"{kind}:{model or ''}", "label": label, "kind": cls,
                            "percent": float(row["percent"]), "resets_at": row.get("resets_at")})
        if windows:
            return windows
    for key, label, cls in LEGACY_WINDOWS:
        w = raw.get(key)
        if isinstance(w, dict) and w.get("utilization") is not None:
            windows.append({"key": key, "label": label, "kind": cls,
                            "percent": float(w["utilization"]), "resets_at": w.get("resets_at")})
    return windows


def parse_extra(raw):
    e = raw.get("extra_usage")
    if not isinstance(e, dict):
        return None
    return {
        "enabled": bool(e.get("is_enabled")),
        "used": e.get("used_credits"),        # minor units (cents)
        "cap": e.get("monthly_limit"),        # minor units, None = none reported
        "utilization": e.get("utilization"),
        "currency": (e.get("currency") or "USD").upper(),
    }


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------

def load_history():
    try:
        data = json.loads(HISTORY.read_text())
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_history(history, snapshot):
    cutoff = now_utc() - dt.timedelta(days=HISTORY_DAYS)
    kept = [h for h in history if (parse_time(h.get("ts")) or cutoff) > cutoff]
    if snapshot:
        kept.append(snapshot)
    tmp = HISTORY.with_suffix(".tmp")
    tmp.write_text(json.dumps(kept, indent=1))
    tmp.replace(HISTORY)
    return kept


# --------------------------------------------------------------------------
# Derived values (always labelled as estimates)
# --------------------------------------------------------------------------

def burn_rate(win, history, now):
    """Return dict with rate (%/h), basis text, and projection, or None."""
    resets = parse_time(win["resets_at"])
    pct = win["percent"]
    if pct >= 100:
        return {"rate": None, "basis": "", "projection": "Limit reached"}

    rate, basis = None, None
    # Prefer the change since an earlier run in this same window.
    earlier = []
    for h in history:
        t = parse_time(h.get("ts"))
        for hw in h.get("windows", []):
            hr = parse_time(hw.get("resets_at"))
            if (hw.get("key") == win["key"] and t and resets and hr
                    and abs((hr - resets).total_seconds()) < 900
                    and (now - t).total_seconds() >= 600):
                earlier.append((t, hw["percent"]))
    if earlier:
        # Use the most recent run that is at least 10 minutes old.
        t0, p0 = max(earlier)
        hours = (now - t0).total_seconds() / 3600
        rate = max(pct - p0, 0) / hours
        basis = f"change since your run at {fmt_local(t0, short=True)}"
    elif resets and win["kind"] in WINDOW_HOURS:
        start = resets - dt.timedelta(hours=WINDOW_HOURS[win["kind"]])
        hours = (now - start).total_seconds() / 3600
        if hours > 0.05:
            rate = pct / hours
            basis = "average since this window opened"
    if rate is None:
        return None

    if rate <= 0:
        projection = "Not on pace to hit the limit before it resets"
    else:
        hit = now + dt.timedelta(hours=(100 - pct) / rate)
        if resets and hit >= resets:
            projection = "Not on pace to hit the limit before it resets"
        else:
            projection = f"Limit in {fmt_duration(hit - now)} (around {fmt_local(hit, short=True)})"
    return {"rate": rate, "basis": basis, "projection": projection}


def daily_spend(history, now):
    """Spend per local day this calendar month, from differences between runs."""
    local_now = now.astimezone()
    month_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    days = {}
    prev = None
    for h in sorted(history, key=lambda x: x.get("ts", "")):
        ex = h.get("extra") or {}
        used = ex.get("used")
        t = parse_time(h.get("ts"))
        if used is None or t is None:
            continue
        if prev is not None:
            delta = used - prev if used >= prev else used  # a drop means the period reset
            lt = t.astimezone()
            if lt >= month_start and delta > 0:
                days[lt.date()] = days.get(lt.date(), 0) + delta
        prev = used
    out = []
    d = month_start.date()
    while d <= local_now.date():
        out.append((d, days.get(d, 0)))
        d += dt.timedelta(days=1)
    return out


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

def fmt_local(t, short=False):
    lt = t.astimezone()
    today = dt.datetime.now().astimezone().date()
    clock = lt.strftime("%-I:%M %p")
    if short and lt.date() == today:
        return clock
    if short and lt.date() == today + dt.timedelta(days=1):
        return f"tomorrow {clock}"
    return lt.strftime("%a %b %-d, ") + clock


def fmt_duration(delta):
    secs = max(int(delta.total_seconds()), 0)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def money(minor, currency):
    if minor is None:
        return None
    sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency, "")
    val = f"{minor / 100:,.2f}"
    return f"{sym}{val}" if sym else f"{val} {currency}"


def esc(s):
    return html.escape(str(s), quote=True)


def level(pct):
    return "red" if pct > 90 else "amber" if pct > 75 else "ok"


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#14161a;--mute:#646b76;--line:#e3e6eb;--track:#eceef2;
--ok:#2f6fe4;--amber:#c77700;--red:#d1352b;--errbg:#fdf0ef;--errink:#8a1f17}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a20;--ink:#eef0f3;--mute:#9aa2ae;
--line:#262b33;--track:#252a32;--ok:#5b93ff;--amber:#f0a32a;--red:#ff6259;--errbg:#2a1715;--errink:#ffb4ad}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text","Helvetica Neue",sans-serif}
main{max-width:980px;margin:0 auto;padding:32px 20px 48px}
header{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:20px}
h1{font-size:22px;margin:0;letter-spacing:-.01em}h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;
color:var(--mute);margin:28px 0 10px;font-weight:600}
.sub{color:var(--mute);font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(290px,100%),1fr));gap:14px}
dd,.src,.err{overflow-wrap:anywhere}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 18px 14px}
.card h3{margin:0 0 12px;font-size:15px;font-weight:600}
.big{display:flex;gap:22px;margin-bottom:12px}.big div{display:flex;flex-direction:column}
.num{font-size:34px;font-weight:650;letter-spacing:-.02em;line-height:1.05;font-variant-numeric:tabular-nums}
.lbl{font-size:12px;color:var(--mute);margin-top:3px}
.bar{height:8px;background:var(--track);border-radius:99px;overflow:hidden;margin:4px 0 8px}
.bar i{display:block;height:100%;border-radius:99px;background:var(--ok)}
.amber .bar i{background:var(--amber)}.red .bar i{background:var(--red)}
.amber .num.rem{color:var(--amber)}.red .num.rem{color:var(--red)}
.row{display:flex;justify-content:space-between;gap:10px;font-size:13px;padding:3px 0}
.row span:first-child{color:var(--mute)}.row span:last-child{text-align:right}
.est{font-size:11px;border:1px solid var(--line);border-radius:6px;padding:0 5px;color:var(--mute);margin-left:4px}
.src{font-size:11.5px;color:var(--mute);margin-top:10px;border-top:1px solid var(--line);padding-top:8px}
.na{color:var(--mute);font-style:italic}
.err{background:var(--errbg);color:var(--errink);border-radius:12px;padding:14px 16px;margin:0 0 14px}
.err b{display:block;margin-bottom:2px}
.chart{display:flex;align-items:flex-end;gap:3px;height:90px;margin:8px 0 4px}
.chart div{flex:1;background:var(--ok);border-radius:3px 3px 0 0;min-height:1px;opacity:.85}
.chart div.zero{background:var(--track);opacity:1}
.axis{display:flex;justify-content:space-between;font-size:11px;color:var(--mute)}
dl{margin:0;font-size:13px;display:grid;grid-template-columns:max-content 1fr;gap:4px 14px}
dt{color:var(--mute)}dd{margin:0}
"""

JS = """
function fmt(s){s=Math.max(0,Math.floor(s/60));var d=Math.floor(s/1440),h=Math.floor(s%1440/60),m=s%60;
return d?d+'d '+h+'h':h?h+'h '+m+'m':m+'m';}
function tick(){var n=Date.now();
document.querySelectorAll('[data-reset]').forEach(function(e){var t=Date.parse(e.dataset.reset)-n;
e.textContent=t>0?fmt(t/1000):'reset';});
var u=document.getElementById('age');if(u){var a=(n-Date.parse(u.dataset.ts))/60000;
u.textContent=a<1?'just now':fmt(a*60)+' ago';}}
tick();setInterval(tick,30000);
"""


def window_card(win, history, now):
    pct = max(0.0, min(win["percent"], 100.0))
    lv = level(pct)
    resets = parse_time(win["resets_at"])
    if resets:
        reset_num = f'<span class="num" data-reset="{esc(resets.isoformat())}">{esc(fmt_duration(resets - now))}</span>'
        reset_row = f'<div class="row"><span>Resets</span><span>{esc(fmt_local(resets))}</span></div>'
    else:
        reset_num = '<span class="num na">—</span>'
        reset_row = '<div class="row"><span>Resets</span><span class="na">unavailable (not in response)</span></div>'

    br = burn_rate(win, history, now)
    if br is None:
        burn_rows = ('<div class="row"><span>Burn rate</span><span class="na">unavailable: '
                     'needs a reset time or an earlier run in this window</span></div>')
    else:
        rate_txt = "—" if br["rate"] is None else f'{br["rate"]:.1f}% per hour'
        burn_rows = (f'<div class="row"><span>Burn rate<span class="est">estimate</span></span>'
                     f'<span>{esc(rate_txt)}</span></div>'
                     f'<div class="row"><span>Projection<span class="est">estimate</span></span>'
                     f'<span>{esc(br["projection"])}</span></div>')
        if br["basis"]:
            burn_rows += f'<div class="row"><span></span><span class="sub">based on {esc(br["basis"])}</span></div>'

    return f"""<section class="card {lv}"><h3>{esc(win["label"])}</h3>
<div class="big"><div><span class="num rem">{100 - pct:.0f}%</span><span class="lbl">remaining</span></div>
<div>{reset_num}<span class="lbl">resets in</span></div></div>
<div class="bar"><i style="width:{pct:.1f}%"></i></div>
<div class="row"><span>Used</span><span>{pct:.0f}% used · {100 - pct:.0f}% left</span></div>
{reset_row}{burn_rows}
<div class="src">Source: usage endpoint (percent only; Anthropic doesn't expose message or token counts). Burn rate: estimate from your runs.</div>
</section>"""


def extra_card(extra, history, now):
    src = '<div class="src">Source: usage endpoint.'
    if extra is None:
        return ('<section class="card"><h3>Extra usage credits</h3><p class="na">Unavailable: the usage '
                'response had no extra-usage section, so your plan may not support it.</p>' + src + '</div></section>')
    if not extra["enabled"]:
        return ('<section class="card"><h3>Extra usage credits</h3><p>Extra usage is turned off for this account. '
                'You can turn it on in claude.ai → Settings → Usage.</p>' + src + '</div></section>')
    cur = extra["currency"]
    used = money(extra["used"], cur)
    cap = money(extra["cap"], cur)
    left = money(extra["cap"] - extra["used"], cur) if extra["cap"] is not None and extra["used"] is not None else None
    pct = None
    if extra["cap"]:
        pct = min(100.0, 100.0 * (extra["used"] or 0) / extra["cap"])
    lv = level(pct) if pct is not None else "ok"
    na = '<span class="na">unavailable</span>'

    big = (f'<div class="big"><div><span class="num rem">{esc(left) if left else "—"}</span>'
           f'<span class="lbl">left under your cap</span></div>'
           f'<div><span class="num">{esc(used) if used else "—"}</span><span class="lbl">used this period</span></div></div>')
    bar = f'<div class="bar"><i style="width:{pct:.1f}%"></i></div>' if pct is not None else ""
    rows = (f'<div class="row"><span>Used this billing period</span><span>{esc(used) if used else na}</span></div>'
            f'<div class="row"><span>Monthly spend cap</span><span>{esc(cap) if cap else "No cap reported"}</span></div>'
            f'<div class="row"><span>Left under cap</span><span>{esc(left) if left else na}</span></div>'
            '<div class="row"><span>Prepaid balance</span><span class="na">unavailable: not exposed by the usage endpoint</span></div>')

    days = daily_spend(history, now)
    peak = max((v for _, v in days), default=0)
    bars = "".join(
        f'<div class="{"zero" if v == 0 else ""}" style="height:{(100 * v / peak) if peak else 0:.0f}%" '
        f'title="{esc(d.strftime("%b %-d"))}: {esc(money(v, cur))}"></div>' for d, v in days)
    chart = (f'<div class="row" style="margin-top:10px"><span>Spend by day this month</span><span></span></div>'
             f'<div class="chart">{bars}</div><div class="axis"><span>{esc(days[0][0].strftime("%b %-d"))}</span>'
             f'<span>{esc(days[-1][0].strftime("%b %-d"))}</span></div>')
    note = (' Daily bars: estimate from the change between your runs, counted on the day of the later run. '
            'Assumes the billing period is the calendar month.')
    return (f'<section class="card {lv}"><h3>Extra usage credits</h3>{big}{bar}{rows}{chart}'
            f'{src}{esc(note)}</div></section>')


def render(now, info, windows, extra, history, errors):
    plan = (info or {}).get("plan")
    plan_txt = f"Claude {plan.capitalize()}" if plan else "Claude plan"
    err_html = "".join(f'<div class="err"><b>{esc(t)}</b>{esc(m)}</div>' for t, m in errors)

    if windows:
        win_html = '<div class="grid">' + "".join(window_card(w, history, now) for w in windows) + "</div>"
    elif errors:
        win_html = '<p class="na">Limits unavailable: see the error above.</p>'
    else:
        win_html = '<p class="na">Unavailable: the usage response listed no limit windows for this plan.</p>'

    if errors and not windows and extra is None:
        credit_html = '<p class="na">Credits unavailable: see the error above.</p>'
    else:
        credit_html = '<div class="grid">' + extra_card(extra, history, now) + "</div>"

    runs = len(history)
    sources = (f"<dl><dt>Usage endpoint (limits, resets, credits)</dt><dd>{esc(USAGE_SOURCE)}</dd>"
               f"<dt>Plan name</dt><dd>{esc(CRED_SOURCE)}"
               f"{' (read from ' + esc(info['where']) + ')' if info else ''}</dd>"
               f"<dt>Burn rate, projections, daily chart</dt><dd>estimates from {esc(HISTORY_SOURCE)} "
               f"({runs} run{'s' if runs != 1 else ''} in the last {HISTORY_DAYS} days)</dd></dl>")

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Claude Usage</title>
<style>{CSS}</style></head><body><main>
<header><div><h1>{esc(plan_txt)} usage</h1>
<div class="sub">Last updated {esc(fmt_local(now))} (<span id="age" data-ts="{esc(now.isoformat())}">just now</span>).
Run the launcher again to refresh.</div></div></header>
{err_html}
<h2>Limits</h2>{win_html}
<h2>Extra usage</h2>{credit_html}
<h2>Data sources</h2><section class="card">{sources}</section>
</main><script>{JS}</script></body></html>"""


# --------------------------------------------------------------------------

def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    now = now_utc()
    errors, info, windows, extra, snapshot = [], None, [], None, None

    try:
        token, info = load_credentials()
        raw = fetch_plan_usage(token)
        del token
        if not isinstance(raw, dict):
            raise SourceError("Anthropic sent an unexpected response shape; fetch_plan_usage() needs updating.")
        windows = parse_windows(raw)
        extra = parse_extra(raw)
        snapshot = {"ts": now.isoformat(),
                    "windows": [{k: w[k] for k in ("key", "label", "percent", "resets_at")} for w in windows],
                    "extra": extra}
    except SourceError as e:
        errors.append(("Couldn't get your latest usage.", str(e)))

    history = load_history()
    prior = list(history)
    try:
        history = save_history(history, snapshot)
    except OSError as e:
        errors.append(("Couldn't save history.", f"Writing {HISTORY} failed ({e.strerror}). Burn rate and the daily chart may be incomplete."))

    DASHBOARD.write_text(render(now, info, windows, extra, prior if snapshot is None else history, errors))
    print(f"Dashboard written to {DASHBOARD}")
    for t, m in errors:
        print(f"Problem: {t} {m}", file=sys.stderr)
    if not os.environ.get("CLAUDE_USAGE_NO_OPEN"):
        webbrowser.open(DASHBOARD.as_uri())


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # last resort: still show something readable
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        DASHBOARD.write_text(
            "<!doctype html><meta charset=utf-8><title>Claude Usage</title><body style='font:15px "
            "-apple-system,sans-serif;max-width:640px;margin:48px auto;padding:0 16px'>"
            "<h1>Claude usage dashboard hit a bug</h1><p>The script crashed before it could build the "
            f"dashboard: <code>{html.escape(type(exc).__name__)}: {html.escape(str(exc))}</code></p>")
        print(f"Crashed: {exc!r}", file=sys.stderr)
        if not os.environ.get("CLAUDE_USAGE_NO_OPEN"):
            webbrowser.open(DASHBOARD.as_uri())
        sys.exit(1)
