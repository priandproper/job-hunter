"""Load the resume-builder profile library.

`resume-builder/src/data/profile.json` is the single source of truth for the
candidate's resume content: a canonical contact block, education, and — most
importantly — a set of `prebuilt` variants. Each variant is already a
resume-ready body (summary + headline + skills + experience[]), i.e. a curated
combination of keywords tuned for a lane (analytics, PMM, GTM, ...).

The matcher (stage 2) scores a job against each variant; the payload builder
(stage 4) emits the winning variant as a resume-builder `ResumeInput`.
"""

import json
from pathlib import Path


class Profile:
    def __init__(self, data: dict):
        self.contact = data.get("contact", {}) or {}
        self.education = data.get("education", []) or []
        self.variants = data.get("prebuilt", []) or []

    def variant_terms(self, variant: dict) -> list[str]:
        """All lowercase keyword tokens that characterize a variant — its skills
        and summary — used to score job fit."""
        terms: list[str] = []
        for group in variant.get("skills", []) or []:
            for item in group.get("items", []) or []:
                terms.append(item.lower())
        summary = variant.get("summary") or ""
        if summary:
            terms.append(summary.lower())
        return terms


def load_profile(config: dict, repo_root: Path) -> Profile:
    path = (repo_root / config["resume_builder"]["profile_json"]).resolve()
    data = json.loads(path.read_text())
    return Profile(data)
