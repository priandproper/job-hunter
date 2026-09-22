"""Verified sponsorship signal — checks an employer's *legal* name against the
public USCIS H-1B Employer Data Hub (and, in the browser, the E-Verify registry).

This is the server-side (stdlib-only) half of the two-part hiring-entity program:
Part B's H-1B lookup. The Data Hub endpoint is EXACT-match and UPPERCASE, and there
is no bulk download (the unfiltered view caps at the top ~100 filers), so the
employer's *legal petitioner name* is what matters:

    "STRIPE INC"      -> FOUND (63 approvals, FY2026)
    "Stripe"          -> NOT_FOUND      (wrong case / no suffix)
    "STRIPE INC."     -> NOT_FOUND      (trailing period)
    "OPENAI OPCO LLC" -> FOUND (304)    ("OPENAI" / "OPENAI INC" both miss)
    "ANTHROPIC PBC"   -> FOUND (143)

Because a wrong name silently returns nothing, we NEVER turn a NOT_FOUND into
"this employer does not sponsor" (that is the one error the brief forbids). A
NOT_FOUND means "this legal name was not in the FY file" — nothing more. A network
or endpoint failure is SEARCH_ERROR, never NOT_FOUND.

Legal names are resolved from data/legal_names.json (a brand -> legal-petitioner
map we seed and grow). When a brand is not in the map we still try a few mechanical
variants, but an unmapped miss is reported as UNRESOLVED_NAME, not NOT_FOUND, so the
UI can say "couldn't confirm the legal entity" instead of implying no sponsorship.

E-Verify is a browser-only source (a Tableau dashboard on uscis.dhs.gov); it is not
fetched here. `verdict()` accepts an E-Verify result passed in from that browser step.
"""

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

_H1B_HOST = "https://bigdataanalyticspub-sb.uscis.dhs.gov"
_H1B_VIEW = "/views/H1BEmployerDataHub-Final/H1B-EmployerDataHub.csv"
_SUFFIXES = ("INC", "LLC", "LTD", "CORP", "CO", "LLP", "LP", "PBC", "OPCO LLC")


def _default_ctx():
    """certifi-verified TLS context, matching lib/ats (macOS needs the bundle)."""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 — fall back to the system store
        return ssl.create_default_context()


_CTX = None


def _fetch_h1b(name: str, fy: str):
    """Real Data Hub fetch. Returns (ok: bool, text: str). Isolated so tests inject
    a fake and stay offline (zero-dependency harness)."""
    global _CTX
    if _CTX is None:
        _CTX = _default_ctx()
    q = urllib.parse.urlencode({"Employer (Petitioner) Name": name, "Fiscal Year": fy})
    url = f"{_H1B_HOST}{_H1B_VIEW}?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20, context=_CTX) as r:
            return (200 <= r.status < 300), r.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return False, ""


def _parse_row(csv_text: str):
    """Parse the single data row the Data Hub returns. Header is
    'Employer (Petitioner) Name,Rank,Total Approvals'; approvals may be quoted
    ("4,113"). Returns {name, rank, approvals:int} or None when there is no data row."""
    lines = (csv_text or "").strip().splitlines()
    if len(lines) < 2:
        return None
    cells = re.findall(r'("[^"]*"|[^,]+)', lines[1])
    cells = [c.strip().strip('"') for c in cells]
    if len(cells) < 3:
        return None
    try:
        approvals = int(cells[2].replace(",", ""))
    except ValueError:
        approvals = None
    return {"name": cells[0], "rank": cells[1], "approvals": approvals}


def name_variants(name: str) -> list[str]:
    """Mechanical UPPERCASE variants to try when a legal name isn't in the map.
    Covers the easy cases ('Stripe' -> 'STRIPE INC'); it cannot invent the hard
    ones ('OPENAI OPCO LLC', 'ANTHROPIC PBC') — those must live in the map."""
    base = re.sub(r"\s+", " ", (name or "").strip()).upper()
    if not base:
        return []
    bare = base.replace(".", "").replace(",", "").strip()
    out = [base, bare]
    # already ends in a known suffix -> also try the comma'd form ("X INC" / "X, INC.")
    m = re.match(r"^(.*) (" + "|".join(_SUFFIXES) + r")$", bare)
    if m:
        stem, suf = m.group(1), m.group(2)
        out += [f"{stem}, {suf}.", f"{stem} {suf}."]
    else:
        for suf in ("INC", "LLC"):  # bare brand -> most common corporate forms
            out.append(f"{bare} {suf}")
    seen, uniq = set(), []
    for v in out:
        if v and v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


def lookup_h1b(legal_name: str, fy: str = "2026", fetch=None) -> dict:
    """Query the H-1B Data Hub for one legal name (trying its variants).
    Returns {status: FOUND|NOT_FOUND|SEARCH_ERROR, approvals, rank, matched, fy, tried}.
    Never converts a network failure into NOT_FOUND."""
    fetch = fetch or _fetch_h1b
    tried, had_error = [], False
    for cand in name_variants(legal_name):
        tried.append(cand)
        ok, text = fetch(cand, fy)
        if not ok:
            had_error = True
            continue
        if text.strip().startswith("{"):  # endpoint handed back a JSON error blob
            had_error = True
            continue
        row = _parse_row(text)
        if row:
            return {"status": "FOUND", "approvals": row["approvals"], "rank": row["rank"],
                    "matched": row["name"], "fy": fy, "tried": tried}
    return {"status": "SEARCH_ERROR" if had_error else "NOT_FOUND",
            "approvals": None, "rank": None, "matched": None, "fy": fy, "tried": tried}


def load_legal_names(root) -> dict:
    """brand (lowercased) -> legal petitioner name, from data/legal_names.json:
    {"legal_names": {"stripe": "STRIPE INC", "openai": "OPENAI OPCO LLC"}}."""
    try:
        d = json.loads((Path(root) / "data" / "legal_names.json").read_text())
        return {(k or "").strip().lower(): v for k, v in d.get("legal_names", {}).items() if v}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def load_everify(root) -> dict:
    """brand (lowercased) -> saved E-Verify result, from data/everify.json:
    {"everify": {"stripe": {"status": "OPEN", "records": [...], "verified_at": "2026-09-22"}}}.
    E-Verify runs only in the browser (Tableau dashboard on uscis.dhs.gov), so its
    results are persisted here for the worker to fold back into the verdict."""
    try:
        d = json.loads((Path(root) / "data" / "everify.json").read_text())
        return {(k or "").strip().lower(): v for k, v in d.get("everify", {}).items() if v}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def resolve_legal_name(company_name: str, legal_map: dict):
    """Look a brand up in the map. Returns (legal_name, source):
    ('STRIPE INC', 'map') when known, (company_name, 'guess') otherwise."""
    key = (company_name or "").strip().lower()
    if key in legal_map:
        return legal_map[key], "map"
    return company_name, "guess"


def verdict(company_name: str, legal_map: dict | None = None, everify: dict | None = None,
            fy: str = "2026", fetch=None) -> dict:
    """Full verified-sponsorship verdict for one employer.

    everify (optional, from the browser step): {status, records:[...]}. When absent,
    e_verify is 'not_checked'. The H-1B half runs here against the resolved legal name.
    """
    legal_map = legal_map or {}
    legal_name, name_source = resolve_legal_name(company_name, legal_map)
    h1b = lookup_h1b(legal_name, fy=fy, fetch=fetch)

    # An unmapped guess that misses is UNRESOLVED_NAME, not NOT_FOUND — we can't claim
    # "no sponsorship" when we're not even sure we had the right legal entity.
    h1b_status = h1b["status"]
    if h1b_status == "NOT_FOUND" and name_source == "guess":
        h1b_status = "UNRESOLVED_NAME"

    ev = (everify or {}).get("status", "not_checked")
    return {
        "brand": company_name,
        "legal_name": legal_name,
        "legal_name_source": name_source,          # map | guess
        "h1b_status": h1b_status,                   # FOUND | NOT_FOUND | UNRESOLVED_NAME | SEARCH_ERROR
        "h1b_approvals": h1b["approvals"],          # int approvals in the fiscal year, when FOUND
        "h1b_fy": fy,
        "h1b_matched_name": h1b["matched"],
        "e_verify": ev,                             # OPEN | TERMINATED | NOT_FOUND | not_checked | ...
        "e_verify_records": (everify or {}).get("records", []),
        "summary": _summarize(company_name, legal_name, name_source, h1b_status,
                              h1b["approvals"], fy, ev),
    }


def _summarize(brand, legal, src, h1b_status, approvals, fy, ev) -> str:
    if h1b_status == "FOUND":
        head = f"{legal} filed {approvals} approved H-1B petition(s) in FY{fy} — a verified active sponsor."
    elif h1b_status == "UNRESOLVED_NAME":
        head = (f"Couldn't confirm {brand}'s legal petitioner name, so the H-1B file "
                f"couldn't be checked reliably. This is NOT evidence they don't sponsor.")
    elif h1b_status == "NOT_FOUND":
        head = (f"{legal} was not in the FY{fy} H-1B file. That is not proof they don't "
                f"sponsor — they may file under another entity or not have filed this year.")
    else:
        head = f"H-1B lookup for {legal} hit a search error — try again."
    ev_txt = {"OPEN": " E-Verify: enrolled (Open).", "TERMINATED": " E-Verify: terminated.",
              "NOT_FOUND": " E-Verify: no matching account found.",
              "not_checked": ""}.get(ev, "")
    return head + ev_txt
