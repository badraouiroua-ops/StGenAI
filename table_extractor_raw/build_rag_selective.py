"""
build_rag_selective.py — Transforme outJason/ → Rag_selective/

Section detection : double source (outline pypdf + Contents page) + Y-position fallback.
Cross-validation entre les sources, logging par niveau de confiance.

Utilisation :
    python build_rag_selective.py [--all] [--family F] [--pdf NAME]

Intégré dans main.py (process_pdf → build_rag_selective.process_pdf).
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional

import pdfplumber
import pypdf

logger = logging.getLogger("build_rag")

# ── Patterns ────────────────────────────────────────────────────────────────────
SECTION_HEADING_PATTERN = re.compile(
    r"^(?:Table\s+\d+\.\s*)?(\d+(?:\.\d+)*)\s{1,4}([A-Z][A-Za-zéèêëàâäùûüôöîïç\s\-/(),.°µΩ%]+)"
    r"(?:\s+\(continued\))?\s*$"
)

TOC_LINE_PATTERN = re.compile(
    r"^(\d+(?:\.\d+){0,3})\s+([A-Za-z][A-Za-z0-9\s\-,/°µΩ()]+?)\s*\.\s*(?:\.\s*)*(\d+)$"
)
# ex: "5.3.6 Supply current characteristics . . . . . . . . 42"

# ── Configuration des chemins ────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).parent.parent
OUTPUT_DIR = REPO_ROOT / "outJason"
RAG_DIR = REPO_ROOT / "Rag_selective"


# ═══════════════════════════════════════════════════════════════════════════════════
#  Source A — Outline natif du PDF (pypdf)
# ═══════════════════════════════════════════════════════════════════════════════════

TABLE_FIGURE_RE = re.compile(r"^(Table|Figure)\s+\d")

def get_bookmarks(pdf_path: str) -> list[tuple[int, str]]:
    """
    Extrait les headings de section du PDF via pypdf.PdfReader.outline.
    Utilise get_destination_page_number() pour la résolution de page.
    Exclut les entrées "Table X…" / "Figure X…".
    Retourne [(page_num_1_indexed, title), ...] trié par page,
    ordre original de l'outline conservé à page égale.
    """
    reader = pypdf.PdfReader(pdf_path)
    entries: list[tuple[int, str]] = []

    def _walk(items):
        for item in items:
            if isinstance(item, list):
                _walk(item)
            else:
                title = (item.get("/Title", "") or "").strip()
                if not title or TABLE_FIGURE_RE.match(title):
                    continue
                if item.get("/Page") is None:
                    continue
                try:
                    page_num = reader.get_destination_page_number(item) + 1
                except Exception:
                    continue
                entries.append((page_num, title))

    try:
        _walk(reader.outline or [])
    except Exception:
        pass

    return sorted(entries, key=lambda x: x[0])


# ═══════════════════════════════════════════════════════════════════════════════════
#  Source B — Page "Contents" parsée
# ═══════════════════════════════════════════════════════════════════════════════════

def get_toc_from_contents_page(pdf, max_scan_pages: int = 15) -> list[tuple[int, str]]:
    """
    Scanne le DÉBUT du PDF (premières pages) + la FIN (dernières pages) à la
    recherche de la page "Contents" et en extrait les lignes de section avec
    numéros de page.

    Reçoit un objet pdfplumber.PDF déjà ouvert (évite une réouverture coûteuse).
    """
    from core.toc_detector import _st_to_actual_page

    entries: list[tuple[int, str]] = []
    n_pages = len(pdf.pages)

    scan_pages = set(range(min(max_scan_pages, n_pages)))
    scan_pages.update(
        range(max(0, n_pages - max_scan_pages), n_pages)
    )

    for pg_idx in sorted(scan_pages):
        page = pdf.pages[pg_idx]
        text = page.extract_text() or ""
        if "Contents" not in text:
            continue
        for line in text.split("\n"):
            m = TOC_LINE_PATTERN.match(line.strip())
            if m:
                num = m.group(1)
                title = m.group(2).strip()
                st_page = int(m.group(3))
                actual_page = _st_to_actual_page(pdf, st_page)
                entries.append((actual_page, f"{num} {title}"))

    return sorted(entries, key=lambda x: x[0])


# ═══════════════════════════════════════════════════════════════════════════════════
#  Helpers section
# ═══════════════════════════════════════════════════════════════════════════════════

def _normalize_section(s: str) -> str:
    """Normalise : lowercase, collapse whitespace, strip nbsp/zero-width-space."""
    s = s.replace("\xa0", " ").replace("\u200b", "")
    return re.sub(r"\s+", " ", s.lower()).strip()


def get_section_for_page(entries: list[tuple[int, str]], target_page: int) -> str:
    """Dernière entrée avec page ≤ target_page (tri stable = ordre outline)."""
    best = ""
    for page, title in entries:
        if page <= target_page:
            best = title
        else:
            break
    return best


# ═══════════════════════════════════════════════════════════════════════════════════
#  Cross-validation dual source
# ═══════════════════════════════════════════════════════════════════════════════════

def get_section_dual(
    pdf_path: str,
    page_num: int,
    caption: str,
    bookmarks: list[tuple[int, str]],
    toc_entries: list[tuple[int, str]],
    pdf_pages: list,
    section_cache: dict[int, list[tuple[float, str]]],
) -> dict:
    """
    Résout la section d'une table.
    Priorité : Y-position (précis, marche arrière page par page)
    → outline/TOC (cross-validation).
    Retourne {"section": str, "confidence": str, "source": str, "alt_section": str}.

    Niveaux de confiance (du meilleur au pire) :
      confirmed_dual       — Y-position + au moins une source d'accord
      confirmed_y          — Y-position trouvé, en désaccord avec les sources
      single_source        — un seul disponible (outline ou toc uniquement)
      conflict             — outline et toc en désaccord (prend outline)
      y_position_fallback  — Y-position seul (sans cross-validation)
      none                 — rien trouvé du tout
    """

    def _matches(n1: str, n2: str) -> bool:
        return bool(n1 and n2 and (n1 == n2 or n1 in n2 or n2 in n1))

    # 1. Y-position (primaire)
    y_sec = _resolve_section_from_page(page_num, caption, pdf_pages, section_cache)
    n_y = _normalize_section(y_sec) if y_sec else ""

    # 2. Outline / TOC (secondaire, cross-validation)
    sec_a = get_section_for_page(bookmarks, page_num)
    sec_b = get_section_for_page(toc_entries, page_num)
    n_a = _normalize_section(sec_a) if sec_a else ""
    n_b = _normalize_section(sec_b) if sec_b else ""

    if n_y:
        if _matches(n_y, n_a) or _matches(n_y, n_b):
            return {"section": y_sec, "confidence": "confirmed_dual",
                    "source": "y_position", "alt_section": sec_a or sec_b}
        # Y-position en désaccord avec les sources → on trust Y
        return {"section": y_sec, "confidence": "confirmed_y",
                "source": "y_position", "alt_section": sec_b or sec_a}

    # 3. Fallback outline / TOC
    if n_a and n_b:
        if _matches(n_a, n_b):
            return {"section": sec_a, "confidence": "confirmed_dual",
                    "source": "both", "alt_section": ""}
        return {"section": sec_a, "confidence": "conflict",
                "source": "outline", "alt_section": sec_b}

    if n_a:
        return {"section": sec_a, "confidence": "single_source",
                "source": "outline", "alt_section": ""}

    if n_b:
        return {"section": sec_b, "confidence": "single_source",
                "source": "toc_page", "alt_section": ""}

    return {"section": "", "confidence": "none",
            "source": "none", "alt_section": ""}


# ═══════════════════════════════════════════════════════════════════════════════════
#  Section detection Y-position (fallback interne, gardé tel quel)
# ═══════════════════════════════════════════════════════════════════════════════════

_page_words_cache: dict[int, list] = {}

def _find_caption_y(page, caption: str) -> Optional[float]:
    page_id = id(page)
    if page_id not in _page_words_cache:
        _page_words_cache[page_id] = page.extract_words() or []
    words = _page_words_cache[page_id]
    caption_words = caption.lower().split()[:5]
    if not caption_words or not words:
        return None
    for idx, word in enumerate(words):
        w_clean = word["text"].lower().split("(")[0].rstrip(".,:;!?")
        c_clean = caption_words[0].lower().split("(")[0].rstrip(".,:;!?")
        if w_clean == c_clean:
            match_count = 1
            for k in range(1, len(caption_words)):
                if idx + k < len(words):
                    w2 = words[idx + k]["text"].lower().split("(")[0].rstrip(".,:;!?")
                    c2 = caption_words[k].lower().split("(")[0].rstrip(".,:;!?")
                    if w2 == c2:
                        match_count += 1
                    else:
                        break
            if match_count >= min(3, len(caption_words)):
                return word["bottom"]
    return None


def _build_section_cache(pdf, pdf_type: int = 1, table_pages: set[int] | None = None) -> dict[int, list[tuple[float, str]]]:
    cache: dict[int, list[tuple[float, str]]] = {}
    TABLE_CONTENT_RE = re.compile(
        r'\b(Yes|No|N/A|NA|Enabled|Disabled)\b', re.IGNORECASE
    )
    from core.toc_detector import _extract_toc_section_numbers, _section_in_whitelist
    toc_whitelist = _extract_toc_section_numbers(pdf, pdf_type)
    seen_nums: set[str] = set()

    if table_pages:
        min_pg = max(1, min(table_pages) - 20)
        max_pg = max(table_pages)
        page_range = range(min_pg - 1, max_pg)
    else:
        page_range = range(len(pdf.pages))

    for pg_idx in page_range:
        pgnum = pg_idx + 1
        page = pdf.pages[pg_idx]
        lines = page.extract_text_lines() or []
        for line_info in lines:
            text = line_info["text"].strip()
            top = line_info["top"]
            if "..." in text or ". ." in text:
                continue
            m = SECTION_HEADING_PATTERN.match(text)
            if m:
                sec_num = m.group(1)
                sec_title = m.group(2).strip().rstrip(".")
                sec_title = re.sub(r'\s{2,}', ' ', sec_title)
                if not sec_title or TABLE_CONTENT_RE.search(sec_title):
                    continue
                if not _section_in_whitelist(sec_num, toc_whitelist):
                    continue
                if sec_num in seen_nums:
                    continue
                seen_nums.add(sec_num)
                label = f"{sec_num} {sec_title}"
                cache.setdefault(pgnum, []).append((top, label))
    for headings in cache.values():
        headings.sort(key=lambda x: x[0])
    return cache


def _resolve_section_from_page(
    page_num: int,
    caption: str,
    pdf_pages: list,
    cache: dict[int, list[tuple[float, str]]],
) -> str:
    """
    Remonte page par page depuis la position de la table
    pour trouver l'en-tête de section le plus proche au-dessus.
    """
    for pgn in range(page_num, 0, -1):
        page = pdf_pages[pgn - 1]
        headings = cache.get(pgn, [])
        if not headings:
            continue
        y = _find_caption_y(page, caption)
        if y is not None:
            best = ""
            for hy, label in headings:
                if hy <= y:
                    best = label
                else:
                    break
            if best:
                return best
        else:
            # Caption introuvable sur cette page → prendre le dernier heading
            return headings[-1][1]
    return ""


# ═══════════════════════════════════════════════════════════════════════════════════
#  Transformation — fonction pure, testable isolément
# ═══════════════════════════════════════════════════════════════════════════════════

def _section_title(section: str) -> str:
    """Extract the title part after the section number.
    Ex: "5.3.13 I/O port characteristics" → "I/O port characteristics"
    """
    if not section:
        return ""
    m = re.match(r'^[\d.]+\s+(.*)', section)
    return m.group(1).strip() if m else section


def _text_helper(caption: str, section: str, headers: list, rows_count: int) -> str:
    """Build text helper string."""
    parts = [caption]
    if section:
        parts.append(f" — section {section}")
    parts.append(".")
    if headers:
        preview = headers[:5]
        preview_str = ", ".join(preview)
        if len(headers) > 5:
            preview_str += f", +{len(headers) - 5} autres"
        parts.append(f" {preview_str}.")
    else:
        parts.append(".")
    if rows_count == 0:
        parts.append(" (table vide)")
    text_helper = "".join(parts)
    if len(text_helper) > 300:
        text_helper = text_helper[:297] + "..."
    return text_helper


def transform_table(
    raw_json: dict,
    section: str = "",
    features_data: dict = None,
) -> Optional[dict]:
    """Format \"une seule table\" pour Rag_selective/."""
    try:
        table_id = raw_json.get("table_id", "")
        caption = raw_json.get("caption", "")
        pdf_name = raw_json.get("pdf_name", "")
        merged_pages = raw_json.get("merged_pages", [])
        page = raw_json.get("page", merged_pages[0] if merged_pages else 1)
        headers = raw_json.get("headers", [])
        rows = raw_json.get("rows", [])

        if not section:
            section = raw_json.get("section", "") or ""

        doc_ref = features_data.get("doc_ref", "") if features_data else ""
        revision = features_data.get("revision", "") if features_data else ""
        url = f"https://www.st.com/resource/en/datasheet/{pdf_name}.pdf"
        url_table = f"{url}#page={page}"
        table_number = table_id.replace("table_", "")

        heuristics = raw_json.get("heuristics", {}) or {}
        notes = heuristics.get("_notes", [])
        legend = heuristics.get("_legend", "")

        document = f"{doc_ref} {revision} - {caption}" if doc_ref and revision else caption

        return {
            "table_id": table_id,
            "document": document,
            "rev": revision,
            "table_number": table_number,
            "title": caption,
            "page": page,
            "section": section,
            "section_title": _section_title(section),
            "semantic_type": "",
            "tags": [],
            "url": url_table,
            "url_pdf": url,
            "text_helper": _text_helper(caption, section, headers, len(rows)),
            "table_content": {
                "headers": headers,
                "rows": rows,
                "notes": notes,
                "legend": legend,
                "semantic_type": "",
                "semantic": {},
            },
        }
    except Exception as e:
        logger.error(f"transform_table failed for {raw_json.get('table_id', '?')}: {e}")
        return None


def _build_complete_doc(
    raw_json: dict,
    section: str,
    features_data: dict,
    pdf_name: str,
) -> dict:
    """Format \"document complet\" pour _all_tables.json."""
    table_id = raw_json.get("table_id", "")
    caption = raw_json.get("caption", "")
    merged_pages = raw_json.get("merged_pages", [])
    page = raw_json.get("page", merged_pages[0] if merged_pages else 1)
    headers = raw_json.get("headers", [])
    rows = raw_json.get("rows", [])

    doc_ref = features_data.get("doc_ref", "") if features_data else ""
    revision = features_data.get("revision", "") if features_data else ""
    family = features_data.get("family", "") if features_data else ""
    core = features_data.get("core", "") if features_data else ""
    freq = features_data.get("max_frequency_mhz", "") if features_data else ""
    packages = features_data.get("packages", []) if features_data else []
    package_str = ", ".join(packages)

    url = f"https://www.st.com/resource/en/datasheet/{pdf_name}.pdf"
    url_table = f"{url}#page={page}"
    table_number = table_id.replace("table_", "")

    heuristics = raw_json.get("heuristics", {}) or {}
    notes = heuristics.get("_notes", [])
    legend = heuristics.get("_legend", "")

    document = f"{doc_ref} {revision} - {pdf_name} - {caption}" if doc_ref and revision else f"{pdf_name} - {caption}"

    return {
        "document": document,
        "rev": revision,
        "url_pdf": url,
        "references": doc_ref,
        "package": package_str,
        "family": family,
        "core": core,
        "frequency": str(freq),
        "table_id": table_id,
        "table_number": table_number,
        "title": caption,
        "page": page,
        "section": section,
        "section_title": _section_title(section),
        "semantic_type": "",
        "tags": [],
        "url": url_table,
        "text_helper": _text_helper(caption, section, headers, len(rows)),
        "table_content": {
            "headers": headers,
            "rows": rows,
            "notes": notes,
            "legend": legend,
            "semantic_type": "",
            "semantic": {},
        },
    }


def transform_features(features: dict) -> dict:
    """Transforme features.json au format RAG simplifié."""
    pdf_name = features.get("pdf_name", "")
    family = features.get("family", "")
    
    # Générer url_pdf si vide
    url_pdf = features.get("url_pdf", "")
    if not url_pdf:
        url_pdf = f"https://www.st.com/resource/en/datasheet/{pdf_name}.pdf"
    
    # Construire le text_helper avec TOUTES les données
    parts = [f"pdf_name: {pdf_name}"]
    doc_ref = features.get("doc_ref", "")
    revision = features.get("revision", "")
    date = features.get("date", "")
    if doc_ref:
        parts.append(f"{doc_ref} {revision} ({date})")
    
    title = features.get("title", "")
    if title:
        parts.append(title)
    
    if family:
        parts.append(f"family: {family}")
    if features.get("core"):
        parts.append(f"core: {features['core']}")
    if features.get("max_frequency_mhz"):
        parts.append(f"freq: {features['max_frequency_mhz']} MHz")
    if features.get("flash_kb"):
        parts.append(f"flash: {features['flash_kb']} KB")
    if features.get("ram_kb"):
        parts.append(f"ram: {features['ram_kb']} KB")
    if features.get("voltage_min_v") and features.get("voltage_max_v"):
        parts.append(f"voltage: {features['voltage_min_v']}-{features['voltage_max_v']} V")
    if features.get("packages"):
        pkg_names = [p.split(" (")[0] for p in features["packages"]]
        parts.append(f"packages: {', '.join(pkg_names)}")
    if features.get("operating_temp_c"):
        parts.append(f"temps: {'/'.join(features['operating_temp_c'])}")
    
    text_helper = ". ".join(parts)
    if len(text_helper) > 300:
        text_helper = text_helper[:297] + "..."
    
    return {
        "features": [
            ["pdf_name", pdf_name],
            ["family", family],
            ["page", 1],
            ["merged_pages", [1]],
            ["url", url_pdf],
            ["url_table", f"{url_pdf}#page=1"],
            ["text_helper", text_helper],
        ],
        "features_content": [
            ["doc_ref", doc_ref],
            ["revision", revision],
            ["date", date],
            ["title", title],
            ["core", features.get("core")],
            ["fpu", features.get("fpu", False)],
            ["max_frequency_mhz", features.get("max_frequency_mhz")],
            ["flash_kb", features.get("flash_kb")],
            ["ram_kb", features.get("ram_kb")],
            ["voltage_min_v", features.get("voltage_min_v")],
            ["voltage_max_v", features.get("voltage_max_v")],
            ["operating_temp_c", features.get("operating_temp_c", [])],
            ["packages", features.get("packages", [])],
            ["device_summary", features.get("device_summary")],
        ]
    }


# ═══════════════════════════════════════════════════════════════════════════════════
#  Logging helpers
# ═══════════════════════════════════════════════════════════════════════════════════

def _log_error(msg: str) -> None:
    log_path = RAG_DIR / "_errors.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {msg}\n")


def _log_audit(filename: str, line: str) -> None:
    log_path = RAG_DIR / filename
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {line}\n")


# ═══════════════════════════════════════════════════════════════════════════════════
#  Parcours / écriture
# ═══════════════════════════════════════════════════════════════════════════════════

def process_pdf(
    pdf_name: str,
    family: str,
    out_dir: Path,
    rag_base: Path = RAG_DIR,
    pdf=None,
) -> int:
    src_dir = out_dir
    if not src_dir.exists():
        logger.warning(f"Source directory not found: {src_dir}")
        return 0

    table_files = sorted(
        src_dir.glob("table_*.json"),
        key=lambda p: int(p.stem.split("_")[1])
    )
    if not table_files:
        logger.warning(f"No table_*.json found in {src_dir}")
        return 0

    # Lire features.json depuis outJason/
    features_path = src_dir / "features.json"
    features_data = None
    if features_path.exists():
        try:
            features_data = json.loads(features_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Failed to read features.json: {e}")

    dest_dir = rag_base / family / pdf_name
    dest_dir.mkdir(parents=True, exist_ok=True)

    pdf_path = next(
        (REPO_ROOT / "DataSHEET" / family).glob(f"{pdf_name}.pdf"),
        None,
    )

    # Initialiser les sources + fallback
    section_cache: dict[int, list[tuple[float, str]]] = {}
    bookmarks: list[tuple[int, str]] = []
    toc_entries: list[tuple[int, str]] = []
    table_sections: dict[str, str] = {}
    section_results: dict[str, dict] = {}

    _own_pdf = False
    if pdf_path:
        try:
            if pdf is None:
                pdf = pdfplumber.open(str(pdf_path))
                _own_pdf = True
            try:
                producer = (pdf.metadata or {}).get("Producer", "")
                pdf_type_val = 2 if "antenna" in producer.lower() else 1
            except Exception:
                pdf_type_val = 1
            table_pages = set()
            for f in table_files:
                try:
                    raw = json.loads(f.read_text(encoding="utf-8"))
                    table_pages.add(raw.get("page", 1))
                except Exception:
                    pass
            section_cache = _build_section_cache(pdf, pdf_type_val, table_pages or None)
            bookmarks = get_bookmarks(str(pdf_path))
            toc_entries = get_toc_from_contents_page(pdf)
        except Exception as e:
            logger.warning(f"Failed initialisation for {pdf_path}: {e}")

    # ── Résolution des sections (dual-source + fallback Y) ──────────────────
    for fpath in table_files:
        try:
            raw = json.loads(fpath.read_text(encoding="utf-8"))
            tid = raw.get("table_id", "")
            page_num = raw.get("page", 1)
            caption = raw.get("caption", "")

            result = get_section_dual(
                str(pdf_path) if pdf_path else "",
                page_num,
                caption,
                bookmarks,
                toc_entries,
                pdf.pages if pdf else [],
                section_cache,
            )
            table_sections[tid] = result["section"]
            section_results[tid] = result

            # Logs d'audit par niveau de confiance
            conf = result["confidence"]
            if conf == "conflict":
                _log_audit("_section_conflicts.log",
                           f"{pdf_name} | {tid} | page={page_num} | "
                           f"A={result['section']} | B={result['alt_section']}")
            elif conf == "confirmed_y":
                _log_audit("_section_conflicts.log",
                           f"{pdf_name} | {tid} | page={page_num} | "
                           f"Y={result['section']} | outline/toc={result['alt_section']}")
            elif conf == "y_position_fallback":
                _log_audit("_section_missing.log",
                           f"{pdf_name} | {tid} | page={page_num}")
            elif conf == "single_source":
                _log_audit("_section_single_source.log",
                           f"{pdf_name} | {tid} | page={page_num} | source={result['source']}")
        except Exception as e:
            _log_error(f"section_resolve|{fpath.name}: {e}")
            table_sections[fpath.stem] = ""
            section_results[fpath.stem] = {"section": "", "confidence": "error",
                                            "source": "none", "alt_section": ""}

    # ── Transformation + écriture ──────────────────────────────────────────
    transformed_single = []
    transformed_complete = []
    errors = 0

    for fpath in table_files:
        try:
            raw = json.loads(fpath.read_text(encoding="utf-8"))
            tid = raw.get("table_id", "")
            section = table_sections.get(tid, raw.get("section", ""))

            single = transform_table(raw, section=section, features_data=features_data)
            if single is None:
                errors += 1
                _log_error(f"transform returned None for {fpath.name}")
                continue

            complete = _build_complete_doc(raw, section, features_data, pdf_name)

            doc_ref = features_data.get("doc_ref", "") if features_data else ""
            revision_raw = features_data.get("revision", "") if features_data else ""
            revision_clean = revision_raw.replace(" ", "_")
            name_prefix = f"{doc_ref}_{revision_clean}" if doc_ref and revision_clean else pdf_name
            out_name = f"{name_prefix}_{fpath.stem}.json"
            out_path = dest_dir / out_name
            out_path.write_text(
                json.dumps(single, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            transformed_single.append(single)
            transformed_complete.append(complete)

        except Exception as e:
            errors += 1
            _log_error(f"{fpath.name}: {e}")

    # ── Écrire features.json dans Rag_selective/ ──────────────────────────
    if features_data:
        try:
            transformed_features = transform_features(features_data)
            features_out_path = dest_dir / "features.json"
            features_out_path.write_text(
                json.dumps(transformed_features, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"Failed to write features.json to Rag_selective: {e}")

    # ── _all_tables.json par PDF ────────────────────────────────────────────
    if transformed_complete:
        doc_ref = features_data.get("doc_ref", "") if features_data else ""
        revision_raw = features_data.get("revision", "") if features_data else ""
        revision_clean = revision_raw.replace(" ", "_")
        name_prefix = f"{doc_ref}_{revision_clean}" if doc_ref and revision_clean else pdf_name
        all_name = f"{name_prefix}__all_tables.json"
        all_path = dest_dir / all_name
        all_path.write_text(
            json.dumps(transformed_complete, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ── Rapport section du PDF ──────────────────────────────────────────────
    if section_results:
        _print_section_report(pdf_name, section_results)

    logger.info(
        f"Rag_selective/{family}/{pdf_name}: "
        f"{len(transformed_single)} tables, {errors} errors"
    )

    if _own_pdf and pdf is not None:
        pdf.close()

    return len(transformed_single)


def _print_section_report(pdf_name: str, results: dict[str, dict]) -> None:
    total = len(results)
    counts: dict[str, int] = {}
    for r in results.values():
        counts[r["confidence"]] = counts.get(r["confidence"], 0) + 1

    y_fallback = counts.get("y_position_fallback", 0)
    y_rate = y_fallback / total * 100 if total else 0

    logger.info(
        f"[SECTION] {pdf_name}: "
        f"confirmed_dual={counts.get('confirmed_dual', 0)} "
        f"confirmed_dual_y={counts.get('confirmed_dual_y', 0)} "
        f"single_source={counts.get('single_source', 0)} "
        f"conflict={counts.get('conflict', 0)} "
        f"y_fallback={y_fallback}({y_rate:.1f}%) "
        f"none={counts.get('none', 0)}"
    )


def process_family(family: str, rag_base: Path = RAG_DIR) -> int:
    src_dir = OUTPUT_DIR / family
    if not src_dir.exists():
        logger.warning(f"Family directory not found: {src_dir}")
        return 0

    total = 0
    for pdf_dir in sorted(src_dir.iterdir()):
        if not pdf_dir.is_dir():
            continue
        n = process_pdf(pdf_dir.name, family, pdf_dir, rag_base)
        total += n

    return total


def process_all(rag_base: Path = RAG_DIR) -> int:
    if not OUTPUT_DIR.exists():
        logger.error(f"OUTPUT_DIR not found: {OUTPUT_DIR}")
        return 0

    total = 0
    for family_dir in sorted(OUTPUT_DIR.iterdir()):
        if not family_dir.is_dir():
            continue
        n = process_family(family_dir.name, rag_base)
        total += n

    return total


# ═══════════════════════════════════════════════════════════════════════════════════
#  CLI autonome
# ═══════════════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Build Rag_selective/ from outJason/"
    )
    parser.add_argument("--family", type=str, help="Famille spécifique (ex: C0)")
    parser.add_argument("--pdf-name", type=str, help="PDF spécifique (ex: stm32c011d6)")
    parser.add_argument("--all", action="store_true", help="Traiter tous les PDFs (défaut)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    t0 = time.time()

    if args.pdf_name and args.family:
        n = process_pdf(args.pdf_name, args.family, OUTPUT_DIR / args.family / args.pdf_name)
    elif args.family:
        n = process_family(args.family)
    else:
        n = process_all()

    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"Rapport build_rag_selective:")
    print(f"  Tables transformées : {n}")
    print(f"  Temps : {elapsed:.1f}s")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
