#!/usr/bin/env python3
"""Website audit: is every listed address really that institution's website?

Runs on the computer in India (.github/workflows/audit.yml, started by hand),
because many college sites refuse servers abroad.

For each source in sources.json (optionally only some states) it opens the
listed address. When that fails, or the page doesn't look like the right
institution, it also tries likely alternatives: the same name with another
ending (.ac.in, .edu.in, .in, .org, .com ...) and, for government colleges,
the usual "gc<town>" style names. More addresses to try can be listed in
audit_candidates.json as {"<source name>": ["https://...", ...]}.

For every page it records the final address, the title, whether the page
mentions the institution's name and town, signs of a parked / for-sale / spam
domain, and links that look like recruitment or notice pages.
The result is written to audit.json (and uploaded with --push).
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import re
import socket
import sys
import time
import urllib.parse

import requests

import check_notices as cn

HERE = os.path.dirname(os.path.abspath(__file__))
WORKERS = int(os.environ.get("AUDIT_WORKERS", "32"))
CHECK_DNS = os.environ.get("AUDIT_NO_DNS") != "1"     # only switched off for local testing

GENERIC = set("""govt government college colleges for of the and in at women woman girls girl boys co ed coed
mahavidyalaya mahavidyalya mahavidhyalaya mahavidalaya mahila kanya post graduate pg degree institute institution
education educational university campus memorial arts science sciences commerce sr senior secondary school
haryana punjab rajasthan delhi uttar pradesh district distt dist teh tehsil city cantt road nagar new old vill
village vpo technology technical management studies research centre center national state constituent aided
private evening morning trust society shri sri smt dr""".split())

PARKED = re.compile(
    r"domain\s+(name\s+)?(is\s+|may\s+be\s+)?for\s+sale|buy\s+this\s+domain|(this\s+)?domain\s+(name\s+)?has\s+expired|"
    r"domain\s+expired|parked\s+(free|domain|by)|domain\s+parking|sedoparking|hugedomains|afternic|\bdan\.com\b|"
    r"account\s+(has\s+been\s+)?suspended|apache2?\s+\w*\s*default\s+page|welcome\s+to\s+nginx|\bit\s+works!|"
    r"iis\s+windows\s+server|index\s+of\s+/|future\s+home\s+of|renew\s+(this|your)\s+domain|cgi-sys/defaultwebpage|"
    r"web\s+hosting\s+(by|from)\s+\w+|this\s+site\s+is\s+(currently\s+)?unavailable", re.I)
SOON = re.compile(r"coming\s+soon|under\s+(construction|maintenance)", re.I)
SPAM = re.compile(r"slot\s*gacor|\bgacor\b|\btogel\b|\bjudi\b|\bcasino\b|sbobet|maxwin|\bslot\s+(online|demo|88)|"
                  r"\bbetting\b|\bpoker\b|viagra|cialis|\bescorts?\b|\bbandar\b|\bqq\b", re.I)
RECRUIT = re.compile(r"recruit|vacanc|career|\bjobs?\b|advertis|walk[\s-]*in|extension\s*lectur|guest\s*(faculty|lectur)|"
                     r"faculty\s*(position|requirement|wanted)|teaching\s*posts?|भर्ती|रिक्ति|विज्ञापन", re.I)
NOTICE = re.compile(r"notices?\b|notice\s*board|\bnews\b|announcements?|circulars?|latest|updates?|सूचना", re.I)
FILE_END = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar", ".ppt", ".pptx")
TLDS = ["ac.in", "edu.in", "in", "org", "com", "org.in", "co.in", "net"]
ALL_TLDS = sorted(TLDS + ["gov.in", "nic.in", "res.in", "info", "co"], key=len, reverse=True)


def squash(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def norm(s):
    return " " + re.sub(r"[^a-z0-9]+", " ", (s or "").lower()) + " "


def _clean_place(p):
    p = re.sub(r"\(.*?\)", " ", p or "")
    p = re.sub(r"\b(distt?|district|teh(sil)?|vpo|vill(age)?|city|cantt|p\.?\s*o)\b\.?", " ", p, flags=re.I)
    return " ".join(p.split())


def places_of(src):
    """(town, other places). The part of the name after the first comma is the town;
    later parts of the name and the district are only weak hints."""
    parts = [_clean_place(p) for p in src["name"].split(",")[1:]]
    parts = [p for p in parts if len(squash(p)) >= 3]
    town = parts[0] if parts else ""
    if not town:
        m = re.search(r"\(([^)]+)\)", src["name"])
        words = re.findall(r"[A-Za-z]+", src["name"])
        if m and len(squash(m.group(1))) >= 3:
            town = _clean_place(m.group(1))
        elif words and words[-1].lower() not in GENERIC and len(words[-1]) >= 4:
            town = words[-1]
    others = []
    for p in parts[1:] + [_clean_place(src.get("district", ""))]:
        if p and squash(p) != squash(town) and p not in others:
            others.append(p)
    return town, others


def name_tokens(name):
    words = re.findall(r"[a-z]+", name.replace(".", "").lower())
    return [w for w in dict.fromkeys(words) if len(w) >= 3 and w not in GENERIC]


def judge(src, title, text):
    hay = norm(title + " " + text)
    hay_sq = squash(title + " " + text)
    town, others = places_of(src)
    skip = set(name_tokens(town)) | {squash(o) for o in others}
    core = [t for t in name_tokens(src["name"]) if t not in skip]
    return {"town": town, "townHit": bool(town) and squash(town) in hay_sq,
            "otherHits": [p for p in others if squash(p) in hay_sq],
            "core": core, "coreFound": [t for t in core if f" {t} " in hay],
            "inTitle": [t for t in name_tokens(src["name"]) if f" {t} " in norm(title)],
            "collegeWord": bool(re.search(r"college|mahavidyalay|university|vidyalaya|institute|"
                                          r"महाविद्यालय|विश्वविद्यालय", title + " " + text[:200000], re.I))}


def looks_right(t):
    """The page opened, isn't parked or spam, and names this institution (and its town)."""
    if not t.get("ok") or t.get("parked") or t.get("spam") or "core" not in t:
        return False
    core, found = t["core"], t["coreFound"]
    core_ok = len(found) >= max(1, (len(core) + 1) // 2) if core else True
    if t["town"]:
        return t["townHit"] and core_ok
    return core_ok and bool(found or t["otherHits"])


def resolves(host):
    if not CHECK_DNS:
        return True
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return True
    except socket.gaierror:
        return False
    except Exception:  # noqa: BLE001
        return True


def short_error(e):
    s = f"{type(e).__name__}: {e}"
    if "NameResolution" in s or "nodename nor servname" in s or "Name or service not known" in s:
        return "no such domain"
    if "refused" in s.lower():
        return "connection refused"
    if isinstance(e, requests.exceptions.ConnectTimeout) or "timed out" in s.lower():
        return "no answer (timeout)"
    if isinstance(e, requests.exceptions.SSLError):
        return "SSL error"
    return s[:140]


def swap_host(url, new_host):
    p = urllib.parse.urlsplit(url)
    netloc = new_host + (f":{p.port}" if p.port else "")
    return urllib.parse.urlunsplit((p.scheme, netloc, p.path or "/", p.query, ""))


def probe(src, url):
    res = {"url": url}
    if "://" not in url:
        url = "http://" + url
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or "").lower()
    if not host or "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
        res.update(ok=False, error="bad address")
        return res
    first = url
    if not resolves(host):
        alt = host[4:] if host.startswith("www.") else "www." + host
        if not resolves(alt):
            res.update(ok=False, error="no such domain")
            return res
        first = swap_host(url, alt)
    scheme = urllib.parse.urlsplit(first).scheme
    attempts = [first, ("https" if scheme == "http" else "http") + first[len(scheme):]]
    r, err = None, ""
    for u in attempts:
        try:
            r = cn._get(u)
            break
        except requests.exceptions.RequestException as e:
            err = short_error(e)
            res.setdefault("tried", []).append([u, err])
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {str(e)[:120]}"
            res.setdefault("tried", []).append([u, err])
    if r is None:
        res.update(ok=False, error=err)
        return res
    try:
        raw = r.content[:1500000]
        ctype = r.headers.get("content-type", "")
        enc = r.encoding if "charset" in ctype.lower() else None
        try:
            html = raw.decode(enc or "utf-8")
        except (UnicodeDecodeError, LookupError):
            html = raw.decode("latin-1", errors="replace")
    except Exception as e:  # noqa: BLE001
        res.update(ok=False, error=f"could not read page: {type(e).__name__}", status=r.status_code, final=r.url)
        return res
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = " ".join((m.group(1) if m else "").split())[:160]
    tp = cn.TextParser()
    try:
        tp.feed(html)
    except Exception:  # noqa: BLE001
        pass
    text = " ".join(" ".join(tp.parts).split())
    final = r.url
    j = judge(src, title, text[:400000])
    recruit, notices = [], []
    try:
        links = cn.links_on(final, html)
    except Exception:  # noqa: BLE001
        links = []
    same = cn.site_key(final)
    for t, u, _ctx in links:
        low = u.lower().split("?")[0].split("#")[0]
        if cn.site_key(u) != same or low.endswith(FILE_END) or u.rstrip("/") == final.rstrip("/"):
            continue
        path = urllib.parse.unquote(urllib.parse.urlsplit(u).path)
        if RECRUIT.search(t or "") or RECRUIT.search(path):
            if u not in [x[1] for x in recruit] and len(recruit) < 8:
                recruit.append([(t or "")[:80], u])
        elif NOTICE.search(t or "") or NOTICE.search(path):
            if u not in [x[1] for x in notices] and len(notices) < 8:
                notices.append([(t or "")[:80], u])
    head = (title + " " + text[:20000])
    pk, sp, soon = PARKED.search(head), SPAM.search(title + " " + text[:60000]), SOON.search(head)
    res.update(ok=r.status_code < 400 and len(html) > 200, status=r.status_code, final=final,
               offsite=cn.site_key(final) != cn.site_key(url), title=title, bytes=len(raw),
               nLinks=len(links), parked=pk.group(0) if pk else "", spam=sp.group(0) if sp else "",
               soon=soon.group(0) if soon else "", sample=text[:240], recruit=recruit, notices=notices, **j)
    res["looksRight"] = looks_right(res)
    return res


def label_of(host):
    h = host[4:] if host.startswith("www.") else host
    for t in ALL_TLDS:
        if h.endswith("." + t):
            return h[: -len(t) - 1]
    return h.rsplit(".", 1)[0]


def name_labels(src):
    """Usual website names of government colleges: gc<town>, gcw<town>, govtcollege<town> ..."""
    n = src["name"].lower()
    if src.get("type") != "Government college" and not n.startswith(("govt", "government")):
        return []
    towns = [squash(places_of(src)[0])]
    pre = ["gc", "govtcollege", "governmentcollege"]
    if "women" in n:
        pre = ["gcw", "gcwomen", "govtcollegeforwomen", "gcfw"] + pre
    elif "girls" in n:
        pre = ["gcg", "gcgirls", "govtcollegeforgirls"] + pre
    if "education" in n:
        pre = ["gce", "gcoe", "govtcollegeofeducation"] + pre
    return [p + t for p in pre for t in towns if t]


def variants(src, url):
    host = (urllib.parse.urlsplit(url if "://" in url else "http://" + url).hostname or "").lower()
    labels = []
    lab = label_of(host) if host else ""
    if lab and "." not in lab:
        labels.append(lab)
    labels += [x for x in name_labels(src) if x not in labels]
    hosts = []
    for lab in labels[:8]:
        for t in TLDS:
            h = f"{lab}.{t}"
            if h not in (host, host[4:]) and h not in hosts:
                hosts.append(h)
    return hosts


def main():
    ap = argparse.ArgumentParser(description="Check that every listed address is the right website")
    ap.add_argument("--states", default="", help="comma-separated states to audit (default: all)")
    ap.add_argument("--push", action="store_true", help="upload audit.json through the GitHub API")
    args = ap.parse_args()
    t0 = time.time()
    sources = json.load(open(os.path.join(HERE, "sources.json"), encoding="utf-8"))["sources"]
    try:
        cands = json.load(open(os.path.join(HERE, "audit_candidates.json"), encoding="utf-8"))
    except Exception:  # noqa: BLE001
        cands = {}
    cands = {k: v for k, v in cands.items() if not k.startswith("_")}
    states = {s.strip().lower() for s in args.states.split(",") if s.strip()}
    colleges = ("Government college", "Govt-aided college", "University college")
    chosen = [s for s in sources if (not states or s["state"].lower() in states)
              and (s.get("url") or s["name"] in cands or s.get("type") in colleges)]
    print(f"Auditing {len(chosen)} sources ({len(cands)} with extra addresses to try)")

    # 1. the listed address of every source
    rows = {s["name"]: {"name": s["name"], "state": s["state"], "type": s.get("type", ""), "district": s.get("district", ""),
                        "current": s.get("url", ""), "check": s.get("check", True), "tests": []} for s in chosen}
    by_name = {s["name"]: s for s in chosen}
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(probe, s, s["url"]): s["name"] for s in chosen if s.get("url")}
        for f in cf.as_completed(futs):
            rows[futs[f]]["tests"].append(dict(f.result(), kind="listed"))
    print(f"Listed addresses done in {time.time() - t0:.0f}s")

    # 2. addresses from audit_candidates.json
    jobs = [(n, u) for n, us in cands.items() if n in by_name for u in us]
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(probe, by_name[n], u): n for n, u in jobs}
        for f in cf.as_completed(futs):
            rows[futs[f]]["tests"].append(dict(f.result(), kind="candidate"))
    print(f"{len(jobs)} candidate addresses done in {time.time() - t0:.0f}s")

    # 3. likely alternatives for sources whose listed address failed or looks wrong
    todo = []
    for n, row in rows.items():
        listed = [t for t in row["tests"] if t["kind"] == "listed"]
        good = [t for t in row["tests"] if looks_right(t)]
        if good:
            continue
        if listed or not row["current"]:
            todo.append((n, variants(by_name[n], row["current"] or "")))
    hosts = sorted({h for _n, hs in todo for h in hs})
    with cf.ThreadPoolExecutor(max_workers=64) as ex:
        alive = {h for h, ok in zip(hosts, ex.map(resolves, hosts)) if ok}
    jobs = [(n, "http://" + h + "/") for n, hs in todo for h in hs if h in alive]
    print(f"Tried {len(hosts)} possible names for {len(todo)} sources: {len(alive)} exist")
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(probe, by_name[n], u): n for n, u in jobs}
        for f in cf.as_completed(futs):
            rows[futs[f]]["tests"].append(dict(f.result(), kind="guess"))
    for n, hs in todo:
        rows[n]["guessesTried"] = len(hs)

    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    out = {"ranAt": dt.datetime.now(ist).isoformat(timespec="seconds"), "states": sorted(states),
           "seconds": round(time.time() - t0), "results": sorted(rows.values(), key=lambda r: (r["state"], r["type"], r["name"]))}
    path = os.path.join(HERE, "audit.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=0)
    ok = sum(1 for r in rows.values() if any(looks_right(t) for t in r["tests"]))
    print(f"Done in {time.time() - t0:.0f}s: {ok} of {len(rows)} sources have a page that looks right")
    if args.push:
        cn.push_file(path, "audit.json", "Website audit " + out["ranAt"][:10])
    return 0


if __name__ == "__main__":
    sys.exit(main())
