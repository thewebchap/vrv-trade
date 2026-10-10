"""PDF -> per-page text lines.

Order of preference:
  1. native PDF text, when the page has a reliable text layer
  2. OCR (macOS Vision, fully local) for image pages or unreliable text layers:
       pass A  300 dpi, unprocessed                           (primary)
       pass B  400 dpi, grayscale + autocontrast + deskew
       pass C  200 dpi, unprocessed
     Pass A is the text the model reads. It is reconciled against B and C:
       - a number that B and C both read differently is replaced by their reading
         (majority of three independent renderings);
       - a line that B and C both contain but A lacks is inserted (A dropped it);
       - confusable letters inside numbers ('1O0', '2O25') are fixed.
     Every change is recorded on the page with its reason.

Preprocessing is used for a second opinion, not as the primary text: grayscale/autocontrast
fixes some faint print but darkens watermarks into the text (it turned "COPPER SCRAP DRUID"
under a diagonal ORIGINAL stamp into noise), and Vision's confidence does not flag such lines.
Binarisation (Otsu thresholding) made results worse on these documents and is not used.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
from difflib import SequenceMatcher

import pymupdf

from . import normalize as N
from .config import CONFIG
from .models import Page

log = logging.getLogger(__name__)

PIPELINE_VERSION = "ocr5"
MIN_NATIVE_CHARS = 50   # below this, a page is treated as an image (scanned) page

# OCR sometimes returns look-alike Cyrillic/Greek letters inside Latin words
# ("КМTC" for "KMTC"); map them back so identifiers compare correctly.
HOMOGLYPHS = str.maketrans("АВСЕНІЈКМОРЅТХУасеіјорхуΑΒΕΗΙΚΜΝΟΡΤΧΥΖ",
                           "ABCEHIJKMOPSTXYaceijopxyABEHIKMNOPTXYZ")


def fix_homoglyphs(text: str) -> str:
    return text.translate(HOMOGLYPHS)


def _group_lines(items: list[tuple[float, float, float, str]]) -> str:
    """items: (x0, y_top, y_bottom, text) in top-down coordinates."""
    items = sorted(items, key=lambda i: (i[1] + i[2]) / 2)
    lines: list[dict] = []
    for it in items:
        centre, height = (it[1] + it[2]) / 2, max(it[2] - it[1], 1e-6)
        if lines and abs(centre - lines[-1]["c"]) < height * 0.5:
            lines[-1]["w"].append(it)
        else:
            lines.append({"c": centre, "w": [it]})
    return "\n".join(" ".join(w[3] for w in sorted(l["w"], key=lambda w: w[0])) for l in lines)


# --------------------------------------------------------------------------- native text

def native_reliable(text: str) -> tuple[bool, str]:
    """A text layer is trusted unless it is short, full of replacement glyphs / (cid:n) codes, or
    mostly non-alphanumeric (a broken font encoding)."""
    t = text.strip()
    if len(t) < MIN_NATIVE_CHARS:
        return False, "no text layer"
    junk = t.count("�") + len(re.findall(r"\(cid:\d+\)", t))
    alnum = sum(c.isalnum() for c in t) / max(1, len(t.replace(" ", "")))
    if junk / len(t) > 0.02:
        return False, f"text layer has {junk} unreadable glyphs"
    if alnum < 0.5:
        return False, f"text layer is only {alnum:.0%} alphanumeric (broken encoding)"
    return True, ""


# --------------------------------------------------------------------------- image preprocessing

def _deskew_angle(gray) -> float:
    """Small-angle skew estimate: the rotation that makes text rows sharpest (max row-sum variance)."""
    import numpy as np
    from PIL import Image

    small = gray.resize((800, int(800 * gray.height / gray.width)))
    ink = Image.fromarray(((np.asarray(small) < 128) * 255).astype("uint8"))
    best, best_score = 0.0, -1.0
    for angle in [a / 4 for a in range(-12, 13)]:          # -3.0 .. +3.0 degrees
        rows = np.asarray(ink.rotate(angle, fillcolor=0)).sum(axis=1)
        score = float(np.var(rows))
        if score > best_score:
            best, best_score = angle, score
    return best


def _preprocess(img) -> tuple[object, str]:
    from PIL import ImageOps

    gray = ImageOps.autocontrast(ImageOps.grayscale(img), cutoff=1)
    angle = _deskew_angle(gray)
    if abs(angle) >= 0.5:
        gray = gray.rotate(angle, expand=True, fillcolor=255)
        return gray, f"grayscale+autocontrast, deskewed {angle:+.2f} deg"
    return gray, "grayscale+autocontrast"


def _render(page: pymupdf.Page, dpi: int):
    from PIL import Image
    return Image.open(io.BytesIO(page.get_pixmap(dpi=dpi).tobytes("png")))


def _vision_lines(img) -> str:
    try:
        from ocrmac import ocrmac  # macOS Vision framework; fully local
    except ImportError as e:  # pragma: no cover - platform dependent
        raise RuntimeError("No OCR engine available (install 'ocrmac' on macOS)") from e
    obs = ocrmac.OCR(img, recognition_level="accurate", language_preference=["en-US"]).recognize()
    # Vision boxes are normalised (x, y, w, h) with origin bottom-left.
    return fix_homoglyphs(_group_lines([(b[0], 1 - b[1] - b[3], 1 - b[1], text) for text, _conf, b in obs]))


# --------------------------------------------------------------------------- reconciliation

# Standalone numbers only: digits glued to letters ("S00008", "KMTC1669512") are identifiers and
# are never voted on; a trailing weight unit ("17,450kg") is allowed.
_NUM = re.compile(r"(?<![A-Za-z0-9])\d(?:\d|[.,]\s?(?=\d))*(?:(?![A-Za-z])|(?=(?:KGS?|kgs?|MTS?|mts?)\b))")


def _squash(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def _best_line(line: str, candidates: list[str], threshold: float = 0.7) -> str | None:
    target = _squash(line)
    if len(target) < 4:
        return None
    best, score = None, 0.0
    for c in candidates:
        r = SequenceMatcher(None, target, _squash(c), autojunk=False).ratio()
        if r > score:
            best, score = c, r
    return best if score >= threshold else None


def reconcile(primary: str, others: list[tuple[str, str]]) -> tuple[str, list[dict]]:
    """Fix the primary OCR text using the other passes. others: [(pass name, text)] (two passes)."""
    other_lines = [(name, [fix_numeric_line(l)[0] for l in text.splitlines()]) for name, text in others]
    out, corrections = [], []
    for ln, line in enumerate(primary.splitlines()):
        fixed, char_fixes = fix_numeric_line(line)
        for c in char_fixes:
            corrections.append({**c, "line": ln + 1})
        fixed = _vote_numbers(fixed, ln, other_lines, corrections)
        out.append(fixed)
    out = _recover_lines(out, other_lines, corrections)
    return "\n".join(out), corrections


def _vote_numbers(fixed: str, ln: int, other_lines, corrections: list[dict]) -> str:
    """A number that both other passes read identically, with different digits, takes their reading."""
    matches = [(name, _best_line(fixed, lines)) for name, lines in other_lines]
    toks = list(_NUM.finditer(fixed))
    alt_toks = [(name, [m.group(0) for m in _NUM.finditer(l)]) for name, l in matches if l]
    if not toks or len(alt_toks) < 2 or any(len(a) != len(toks) for _, a in alt_toks):
        return fixed
    digits = lambda x: re.sub(r"\D", "", x)
    new, pos = [], 0
    for i, m in enumerate(toks):
        readings = {a[i].replace(" ", "") for _, a in alt_toks}
        mine = m.group(0).replace(" ", "")
        # vote only on a classic misread: same number of digits, exactly ONE digit different.
        # Separator-only differences are left to the number parser; anything bigger is a different number
        # (the passes may have ordered the numbers on the line differently), not a misread.
        theirs = digits(next(iter(readings))) if len(readings) == 1 else None
        if theirs and len(theirs) == len(digits(mine)) and sum(a != b for a, b in zip(theirs, digits(mine))) == 1:
            other = readings.pop()
            new += [fixed[pos:m.start()], other]
            corrections.append({"from": mine, "to": other, "line": ln + 1,
                                "reason": "majority of 3 OCR passes: " + " and ".join(n for n, _ in alt_toks)
                                          + f" both read {other}"})
        else:
            new += [fixed[pos:m.start()], m.group(0)]
        pos = m.end()
    new.append(fixed[pos:])
    return "".join(new)


def _recover_lines(out: list[str], other_lines, corrections: list[dict]) -> list[str]:
    """Insert lines that both other passes read but the primary pass lost, after the line that
    precedes them in the first other pass."""
    if len(other_lines) < 2:
        return out
    (name_b, lines_b), (name_c, lines_c) = other_lines[:2]
    result = list(out)
    anchor = -1
    inserted = 0
    for lb in lines_b:
        if len(_squash(lb)) < 8:
            continue
        hit = _best_line(lb, result)
        if hit is not None:
            anchor = result.index(hit)
            continue
        if _best_line(lb, result, threshold=0.5) is not None:
            continue    # a variant reading of a line the primary has (possibly with other numbers): never injected
        if _best_line(lb, lines_c) is None:          # only one other pass has it: not trusted
            continue
        result.insert(anchor + 1, lb)
        anchor += 1
        inserted += 1
        corrections.append({"from": None, "to": lb, "line": anchor + 1,
                            "reason": f"line missing from the primary OCR pass; read by both {name_b} and {name_c}"})
    return result


def fix_numeric_line(line: str) -> tuple[str, list[dict]]:
    return N.fix_numeric_ocr(line, field_is_numeric=False)


def _ocr_page(page: pymupdf.Page) -> tuple[str, list[str], list[dict], list[str]]:
    dpi, hi, lo = CONFIG.ocr_dpi, int(CONFIG.ocr_dpi * 4 / 3), int(CONFIG.ocr_dpi * 2 / 3)
    img_b, how_b = _preprocess(_render(page, hi))
    text_a, text_b, text_c = _vision_lines(_render(page, dpi)), _vision_lines(img_b), _vision_lines(_render(page, lo))
    text, corrections = reconcile(text_a, [(f"{hi} dpi {how_b}", text_b), (f"{lo} dpi", text_c)])
    return text, [text_b, text_c], corrections, [f"OCR primary: {dpi} dpi unprocessed; second opinion: {hi} dpi {how_b}; "
                                                 f"third: {lo} dpi unprocessed"]


# --------------------------------------------------------------------------- entry points

def file_sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_pages(path: str) -> list[Page]:
    cache_file = os.path.join(CONFIG.cache_dir, "text", f"{file_sha(path)}_{CONFIG.ocr_dpi}_{PIPELINE_VERSION}.json")
    if os.path.exists(cache_file):
        with open(cache_file) as f:
            return [Page(**p) for p in json.load(f)]

    pages = []
    with pymupdf.open(path) as doc:
        for i, page in enumerate(doc):
            native = page.get_text()
            ok, why = native_reliable(native)
            if ok:
                words = page.get_text("words")
                pages.append(Page(i + 1, _group_lines([(w[0], w[1], w[3], w[4]) for w in words]), "pdf_text"))
                continue
            text, alts, corrections, notes = _ocr_page(page)
            if native.strip():          # an unreliable text layer is kept for reference
                alts.append(native)
                notes.append(f"native text layer not used: {why}")
            pages.append(Page(i + 1, text, "ocr", alts, corrections, notes))
            if corrections:
                log.info("[OCR] %s p.%d: %d correction(s)", os.path.basename(path), i + 1, len(corrections))

    os.makedirs(os.path.dirname(cache_file), exist_ok=True)
    with open(cache_file, "w") as f:
        json.dump([p.__dict__ for p in pages], f)
    return pages


def render_page_png(path: str, page_number: int, dpi: int = 110) -> bytes:
    with pymupdf.open(path) as doc:
        return doc[page_number - 1].get_pixmap(dpi=dpi).tobytes("png")
