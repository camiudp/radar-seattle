#!/usr/bin/env python3
"""
Radar de empleos (Seattle + Miami/Weston).

Lee companies.yaml, consulta los portales públicos de empleo (Greenhouse,
Lever, Ashby y Workday), filtra por título y ubicación según el área de
cada empresa, clasifica lo que dice cada aviso sobre patrocinio de visa
y escribe docs/data/jobs.json, que es lo que muestra la página.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote, quote_plus, urlparse

import requests
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "companies.yaml"
OUT_PATH = ROOT / "docs" / "data" / "jobs.json"
TIMEOUT = 25
HEADERS = {"User-Agent": "Mozilla/5.0 (job-radar personal project)"}
MAX_DESCRIPTION = 8000      # caracteres del aviso que se guardan para el prompt del CV
WORKDAY_SEARCH = ["data", "analyst", "analytics", "insights"]
WORKDAY_MAX_DETAILS = 40

# ----------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------

def html_to_text(raw: str) -> str:
    raw = html.unescape(raw or "")
    raw = re.sub(r"<(br|/p|/li|/div|/h\d)[^>]*>", "\n", raw, flags=re.I)
    raw = re.sub(r"<li[^>]*>", "\n- ", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = re.sub(r"[ \t\xa0]+", " ", raw)
    return re.sub(r"\n\s*\n+", "\n\n", raw).strip()


def to_date(value) -> str | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return dt.datetime.fromtimestamp(value / 1000, dt.timezone.utc).date().isoformat()
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return str(value)[:10] if re.match(r"\d{4}-\d{2}-\d{2}", str(value)) else None


def contains_any(text: str, terms) -> bool:
    text = (text or "").lower()
    return any(str(t).lower() in text for t in terms or [])


# ----------------------------------------------------------------------
# Filtros
# ----------------------------------------------------------------------

def title_matches(title: str, cfg: dict) -> bool:
    t = (title or "").lower()
    if contains_any(t, cfg.get("title_exclude")):
        return False
    return any(re.search(rf"\b{re.escape(str(term).lower())}\b", t) for term in cfg.get("title_include") or [])


def location_matches(location: str, cfg: dict, region: dict) -> bool:
    if contains_any(location, cfg.get("location_exclude")):
        return False
    return contains_any(location, region.get("location_include"))


# ----------------------------------------------------------------------
# Portales (todos devuelven una lista de dicts uniformes)
# ----------------------------------------------------------------------

def fetch_greenhouse(slug: str, **_) -> list[dict]:
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                     params={"content": "true"}, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [{
        "id": f"gh-{slug}-{j.get('id')}",
        "title": j.get("title", ""),
        "url": j.get("absolute_url"),
        "location": (j.get("location") or {}).get("name", ""),
        "posted": to_date(j.get("first_published") or j.get("updated_at")),
        "text": html_to_text(j.get("content", "")),
    } for j in r.json().get("jobs", [])]


def fetch_lever(slug: str, **_) -> list[dict]:
    r = requests.get(f"https://api.lever.co/v0/postings/{slug}",
                     params={"mode": "json"}, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise ValueError("Lever no devolvió una lista")
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        location = cats.get("location") or ", ".join(cats.get("allLocations") or [])
        if j.get("workplaceType") == "remote" and "remote" not in location.lower():
            location = f"{location} (Remote)".strip()
        parts = [j.get("descriptionPlain", "")]
        parts += [f"{lst.get('text', '')}\n{html_to_text(lst.get('content', ''))}" for lst in j.get("lists") or []]
        parts.append(j.get("additionalPlain", ""))
        jobs.append({
            "id": f"lv-{slug}-{j.get('id')}",
            "title": j.get("text", ""),
            "url": j.get("hostedUrl"),
            "location": location,
            "posted": to_date(j.get("createdAt")),
            "text": "\n\n".join(p for p in parts if p and p.strip()),
        })
    return jobs


def fetch_ashby(slug: str, **_) -> list[dict]:
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                     params={"includeCompensation": "true"}, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        location = j.get("location") or ""
        extra = [s.get("location", "") for s in j.get("secondaryLocations") or []]
        if extra:
            location = ", ".join([location, *extra])
        if j.get("isRemote") and "remote" not in location.lower():
            location = f"{location} (Remote)".strip()
        jobs.append({
            "id": f"ab-{slug}-{j.get('id')}",
            "title": j.get("title", ""),
            "url": j.get("jobUrl") or j.get("applyUrl"),
            "location": location,
            "posted": to_date(j.get("publishedAt")),
            "text": j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
        })
    return jobs


def parse_workday(url: str) -> tuple[str, str, str]:
    """https://ukg.wd5.myworkdayjobs.com/en-US/UKGCareers -> (host, tenant, site)"""
    p = urlparse(url.strip())
    host = p.netloc
    tenant = host.split(".")[0]
    segs = [s for s in p.path.split("/") if s and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", s)]
    if not segs or "myworkdayjobs.com" not in host:
        raise ValueError("URL de Workday no reconocida")
    return host, tenant, segs[0]


def fetch_workday(url: str, cfg: dict, **_) -> list[dict]:
    host, tenant, site = parse_workday(url)
    base = f"https://{host}/wday/cxs/{tenant}/{site}"
    found: dict[str, dict] = {}
    for term in WORKDAY_SEARCH:
        for offset in range(0, 100, 20):
            r = requests.post(f"{base}/jobs", json={"appliedFacets": {}, "limit": 20, "offset": offset,
                                                    "searchText": term},
                              headers={**HEADERS, "Accept": "application/json"}, timeout=TIMEOUT)
            r.raise_for_status()
            postings = r.json().get("jobPostings", [])
            for p in postings:
                if p.get("externalPath") and title_matches(p.get("title", ""), cfg):
                    found.setdefault(p["externalPath"], p)
            if len(postings) < 20:
                break

    jobs = []
    for path, p in list(found.items())[:WORKDAY_MAX_DETAILS]:
        try:
            d = requests.get(f"{base}{path}", headers={**HEADERS, "Accept": "application/json"},
                             timeout=TIMEOUT).json().get("jobPostingInfo", {})
        except Exception:  # noqa: BLE001
            d = {}
        locs = [d.get("location") or p.get("locationsText", "")] + list(d.get("additionalLocations") or [])
        jobs.append({
            "id": f"wd-{tenant}-{path.rsplit('_', 1)[-1]}",
            "title": d.get("title") or p.get("title", ""),
            "url": d.get("externalUrl") or f"https://{host}/{site}{path}",
            "location": ", ".join(l for l in locs if l),
            "posted": to_date(d.get("startDate")),
            "text": html_to_text(d.get("jobDescription", "")),
        })
    return jobs


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}

# ----------------------------------------------------------------------
# Detección automática del portal
# ----------------------------------------------------------------------

LINK_PATTERNS = [
    ("greenhouse", r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)"),
    ("greenhouse", r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/(?!embed)([A-Za-z0-9_-]+)"),
    ("lever", r"jobs\.lever\.co/([A-Za-z0-9_.-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)"),
    ("workday", r"(https?://[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?[A-Za-z0-9_-]+)"),
]


def discover_from_careers(url: str) -> tuple[str, str] | None:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    page = r.text
    for ats, pat in LINK_PATTERNS:
        m = re.search(pat, page)
        if m:
            return ats, m.group(1)
    return None


def slug_candidates(c: dict) -> list[str]:
    name = c["name"].lower()
    base = [c.get("slug") or "", re.sub(r"[^a-z0-9]", "", name), re.sub(r"[^a-z0-9]+", "-", name).strip("-")]
    out = []
    for b in base:
        for s in (b, f"{b}hq", f"{b}inc"):
            if s and s not in out:
                out.append(s)
    return out


def fetch_company(c: dict, cfg: dict) -> tuple[str, str, list[dict]]:
    """Devuelve (portal, slug o url, avisos)."""
    ats = (c.get("ats") or "auto").lower()
    if c.get("workday_url"):
        return "workday", c["workday_url"], fetch_workday(c["workday_url"], cfg)
    if ats == "workday":
        raise ValueError("Falta workday_url")
    if ats in FETCHERS:
        return ats, c["slug"], FETCHERS[ats](c["slug"])

    empty_hit = None   # un portal que respondió pero sin avisos (puede ser un slug equivocado)
    for slug in slug_candidates(c):
        for name, fn in FETCHERS.items():
            try:
                jobs = fn(slug)
            except Exception:  # noqa: BLE001
                continue
            if jobs:
                return name, slug, jobs
            empty_hit = empty_hit or (name, slug, jobs)

    if c.get("careers_url"):
        try:
            hit = discover_from_careers(c["careers_url"])
        except Exception as exc:  # noqa: BLE001
            if empty_hit:
                return empty_hit
            raise RuntimeError(f"No se pudo abrir careers_url ({exc.__class__.__name__})") from exc
        if hit:
            ats_found, ident = hit
            if ats_found == "workday":
                return "workday", ident, fetch_workday(ident, cfg)
            return ats_found, ident, FETCHERS[ats_found](ident)
    if empty_hit:
        return empty_hit
    if c.get("careers_url"):
        raise RuntimeError("careers_url no tiene links a Greenhouse, Lever, Ashby ni Workday")
    raise RuntimeError("Ningún slug funcionó y no hay careers_url")


# ----------------------------------------------------------------------
# Clasificación de visa
# ----------------------------------------------------------------------

NEGATIVE = [
    r"\b(not|unable to|cannot|can['’]t|won['’]t|will not|do not|does not|don['’]t|is not able to|are not able to)\b[^.\n]{0,60}\bsponsor",
    r"\bno\s+(visa\s+)?sponsorship\b",
    r"without\s+(the\s+)?(current\s+or\s+future\s+)?(need|requirement|necessity)\s+(for|of)\b[^.\n]{0,40}sponsorship",
    r"sponsorship\s+(is\s+)?not\s+(available|offered|provided|possible)",
    r"\bmust\s+be\s+(a\s+)?(u\.?s\.?\s+citizen|green\s+card\s+holder|permanent\s+resident)",
    r"(u\.?s\.?\s+citizenship|security\s+clearance)\s+(is\s+)?required",
]
POSITIVE = [
    r"\b(visa\s+)?sponsorship\s+(is\s+)?(available|offered|provided)\b",
    r"\bwill(ing)?\s+(to\s+)?sponsor\b",
    r"\bopen\s+to\s+sponsor",
    r"\bh-?1b1?\b",
]


def snippet(text: str, start: int, end: int, width: int = 90) -> str:
    a, b = max(0, start - width), min(len(text), end + width)
    s = text[a:b].replace("\n", " ").strip()
    return ("…" if a > 0 else "") + s + ("…" if b < len(text) else "")


def classify_visa(text: str, sponsor_history: bool) -> dict:
    for pat in NEGATIVE:
        m = re.search(pat, text, flags=re.I)
        if m:
            return {"status": "red", "reason": "El aviso dice que no patrocina", "quote": snippet(text, m.start(), m.end())}
    for pat in POSITIVE:
        m = re.search(pat, text, flags=re.I)
        if m:
            return {"status": "green", "reason": "El aviso menciona patrocinio", "quote": snippet(text, m.start(), m.end())}
    if sponsor_history:
        return {"status": "green", "reason": "El aviso no lo menciona, pero la empresa ha patrocinado antes", "quote": None}
    return {"status": "yellow", "reason": "El aviso no menciona visas", "quote": None}


# ----------------------------------------------------------------------
# Principal
# ----------------------------------------------------------------------

def company_links(c: dict) -> dict:
    gd = c.get("glassdoor") or {}
    name = c["name"]
    return {
        "glassdoor_url": gd.get("url") or f"https://www.glassdoor.com/Search/results.htm?keyword={quote_plus(name)}",
        "indeed_url": c.get("indeed_url") or f"https://www.indeed.com/cmp/{quote(name.replace(' ', '-'))}",
        "rating": gd.get("rating"),
        "reviews": gd.get("reviews"),
        "rating_checked": str(gd.get("checked")) if gd.get("checked") else None,
    }


def load_previous() -> dict:
    if OUT_PATH.exists():
        try:
            data = json.loads(OUT_PATH.read_text(encoding="utf-8"))
            return {j["id"]: j.get("first_seen") for j in data.get("jobs", [])}
        except (json.JSONDecodeError, KeyError):
            pass
    return {}


def run() -> int:
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    regions = cfg.get("regions") or {}
    previous = load_previous()
    today = dt.date.today().isoformat()
    jobs_out, companies_out = [], []

    for c in cfg.get("companies", []):
        region_key = c.get("region") or next(iter(regions), "seattle")
        region = regions.get(region_key, {})
        info = {"name": c["name"], "region": region_key, "slug": c.get("slug"),
                "sponsor_history": bool(c.get("sponsor_history")), **company_links(c)}
        try:
            ats, ident, jobs = fetch_company(c, cfg)
            info.update(ok=True, ats=ats, detected=ident, total=len(jobs), error=None)
        except Exception as exc:  # noqa: BLE001
            info.update(ok=False, ats=None, detected=None, total=0, matched=0, error=str(exc))
            companies_out.append(info)
            print(f"[x] {c['name']}: {exc}", file=sys.stderr)
            continue

        matched = 0
        for j in jobs:
            if not (title_matches(j["title"], cfg) and location_matches(j["location"], cfg, region)):
                continue
            matched += 1
            jobs_out.append({
                "id": j["id"], "title": j["title"], "url": j["url"],
                "company": c["name"], "region": region_key,
                "location": j["location"], "posted": j["posted"],
                "first_seen": previous.get(j["id"]) or today,
                "visa": classify_visa(j["text"], info["sponsor_history"]),
                "description": (j["text"] or "")[:MAX_DESCRIPTION],
            })
        info["matched"] = matched
        companies_out.append(info)
        print(f"[ok] {c['name']} ({ats}: {ident}): {len(jobs)} avisos, {matched} calzan")

    jobs_out.sort(key=lambda j: (j["first_seen"], j["posted"] or ""), reverse=True)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "today": today,
        "regions": {k: {"label": v.get("label", k)} for k, v in regions.items()},
        "companies": companies_out,
        "jobs": jobs_out,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Listo: {len(jobs_out)} vacantes guardadas en {OUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
