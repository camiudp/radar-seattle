#!/usr/bin/env python3
"""
Radar de empleos Seattle.

Lee companies.yaml, consulta los portales públicos de empleo (Greenhouse,
Lever, Ashby), filtra por título y ubicación, clasifica cada aviso según
lo que dice sobre patrocinio de visa y escribe docs/data/jobs.json,
que es lo que muestra la página.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote, quote_plus

import requests
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "companies.yaml"
OUT_PATH = ROOT / "docs" / "data" / "jobs.json"
TIMEOUT = 25
HEADERS = {"User-Agent": "job-radar/1.0 (personal job search project)"}

# ----------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------

def html_to_text(raw: str) -> str:
    raw = html.unescape(raw or "")
    raw = re.sub(r"<(br|/p|/li|/div|/h\d)[^>]*>", "\n", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"[ \t\xa0]+", " ", raw).strip()


def to_date(value) -> str | None:
    """Normaliza fechas ISO o milisegundos a AAAA-MM-DD."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return dt.datetime.fromtimestamp(value / 1000, dt.timezone.utc).date().isoformat()
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return str(value)[:10]


# ----------------------------------------------------------------------
# Lectores de cada portal (todos devuelven una lista de dicts uniformes)
# ----------------------------------------------------------------------

def fetch_greenhouse(slug: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    r = requests.get(url, params={"content": "true"}, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        jobs.append({
            "id": f"gh-{slug}-{j.get('id')}",
            "title": j.get("title", ""),
            "url": j.get("absolute_url"),
            "location": (j.get("location") or {}).get("name", ""),
            "posted": to_date(j.get("first_published") or j.get("updated_at")),
            "text": html_to_text(j.get("content", "")),
        })
    return jobs


def fetch_lever(slug: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{slug}"
    r = requests.get(url, params={"mode": "json"}, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise ValueError("Lever no devolvió una lista de avisos")
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        location = cats.get("location") or ", ".join(cats.get("allLocations") or [])
        if j.get("workplaceType") == "remote" and "remote" not in location.lower():
            location = f"{location} (Remote)".strip()
        parts = [j.get("descriptionPlain", ""), j.get("additionalPlain", "")]
        parts += [html_to_text(lst.get("content", "")) for lst in j.get("lists") or []]
        jobs.append({
            "id": f"lv-{slug}-{j.get('id')}",
            "title": j.get("text", ""),
            "url": j.get("hostedUrl"),
            "location": location,
            "posted": to_date(j.get("createdAt")),
            "text": "\n".join(p for p in parts if p),
        })
    return jobs


def fetch_ashby(slug: str) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    r = requests.get(url, params={"includeCompensation": "true"}, headers=HEADERS, timeout=TIMEOUT)
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


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


def fetch_company(ats: str, slug: str) -> tuple[str, list[dict]]:
    """Devuelve (portal detectado, avisos). Con ats=auto prueba los tres."""
    order = list(FETCHERS) if ats in (None, "", "auto") else [ats]
    errors = []
    for name in order:
        try:
            return name, FETCHERS[name](slug)
        except Exception as exc:  # noqa: BLE001 - queremos seguir con el siguiente portal
            errors.append(f"{name}: {exc.__class__.__name__}")
    raise RuntimeError("; ".join(errors))


# ----------------------------------------------------------------------
# Filtros
# ----------------------------------------------------------------------

def contains_any(text: str, terms: list[str]) -> bool:
    text = (text or "").lower()
    return any(t.lower() in text for t in terms or [])


def title_matches(title: str, cfg: dict) -> bool:
    t = (title or "").lower()
    if contains_any(t, cfg.get("title_exclude")):
        return False
    for term in cfg.get("title_include") or []:
        # palabra completa, para que "data" no calce con "metadata"
        if re.search(rf"\b{re.escape(term.lower())}\b", t):
            return True
    return False


def location_matches(location: str, cfg: dict) -> bool:
    if contains_any(location, cfg.get("location_exclude")):
        return False
    return contains_any(location, cfg.get("location_include"))


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
            return {"status": "red", "reason": "El aviso dice que no patrocina",
                    "quote": snippet(text, m.start(), m.end())}
    for pat in POSITIVE:
        m = re.search(pat, text, flags=re.I)
        if m:
            return {"status": "green", "reason": "El aviso menciona patrocinio",
                    "quote": snippet(text, m.start(), m.end())}
    if sponsor_history:
        return {"status": "green", "reason": "El aviso no lo menciona, pero la empresa ha patrocinado antes",
                "quote": None}
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
    previous = load_previous()
    today = dt.date.today().isoformat()
    jobs_out, companies_out = [], []

    for c in cfg.get("companies", []):
        info = {"name": c["name"], "slug": c["slug"],
                "sponsor_history": bool(c.get("sponsor_history")), **company_links(c)}
        try:
            ats, jobs = fetch_company(c.get("ats", "auto"), c["slug"])
            info.update(ok=True, ats=ats, total=len(jobs), error=None)
        except Exception as exc:  # noqa: BLE001
            info.update(ok=False, ats=None, total=0, matched=0, error=str(exc))
            companies_out.append(info)
            print(f"[x] {c['name']}: {exc}", file=sys.stderr)
            continue

        matched = 0
        for j in jobs:
            if not (title_matches(j["title"], cfg) and location_matches(j["location"], cfg)):
                continue
            matched += 1
            visa = classify_visa(j["text"], info["sponsor_history"])
            jobs_out.append({
                "id": j["id"], "title": j["title"], "url": j["url"],
                "company": c["name"], "location": j["location"], "posted": j["posted"],
                "first_seen": previous.get(j["id"]) or today,
                "visa": visa,
            })
        info["matched"] = matched
        companies_out.append(info)
        print(f"[ok] {c['name']} ({ats}): {len(jobs)} avisos, {matched} calzan")

    jobs_out.sort(key=lambda j: (j["first_seen"], j["posted"] or ""), reverse=True)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "today": today,
        "companies": companies_out,
        "jobs": jobs_out,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Listo: {len(jobs_out)} vacantes guardadas en {OUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
