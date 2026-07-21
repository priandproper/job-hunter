"""Stage 4 — build the resume-builder payload.

Given the winning resume variant (from stage 2) and the profile's canonical
contact/education, assemble a `ResumeInput` object exactly matching the contract
in `resume-builder/src/types/resume.ts`. Also produce a click-to-open URL that
imports it into the running resume-builder app (`#import=<url-encoded JSON>`).

The only hard requirement in the contract is `contact.fullName`.
"""

import base64
import json
import urllib.parse


def build_resume_input(job: dict, variant: dict, profile, contact_cfg: dict) -> dict:
    """Assemble a ResumeInput dict for one job."""
    # Contact: profile's canonical block, with config overrides filling blanks.
    contact = dict(profile.contact)
    for k, v in (contact_cfg or {}).items():
        if v:
            contact[k] = v
    # The variant's headline positions the resume for this lane.
    if variant.get("headline"):
        contact["headline"] = variant["headline"]

    company = job.get("company", "")
    title = job.get("title", "")

    return {
        "label": f"{company} — {title}"[:80],
        "contact": contact,
        "summary": variant.get("summary", ""),
        "experience": variant.get("experience", []),
        "education": profile.education,
        "skills": variant.get("skills", []),
    }


def import_url(resume_input: dict, app_url: str, encoding: str = "base64") -> str:
    """A URL that, when opened, imports this resume into the builder app.

    The builder's ingress accepts raw URL-encoded JSON or base64-encoded JSON.
    base64 is the default: for JSON (lots of quotes/braces) it produces a much
    shorter query string than percent-encoding the raw JSON.
    """
    # Payload goes in the URL *hash* (#import=), not the query string: a full
    # resume JSON overflows the server request-line limit as a query string
    # (GitHub Pages -> "414 URI Too Long"), but the hash is never sent upstream.
    raw = json.dumps(resume_input, separators=(",", ":"))
    if encoding == "base64":
        b64 = base64.b64encode(raw.encode("utf-8")).decode("ascii")
        return f"{app_url.rstrip('/')}/#import={urllib.parse.quote(b64)}"
    return f"{app_url.rstrip('/')}/#import={urllib.parse.quote(raw)}"
