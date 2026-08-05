"""
page1_features.py — Extracts Features/Summary data from pages 1..N of STM32 datasheets.
Output: outJason/<family>/<pdf_name>/features.json

100% independent from the table extraction pipeline.
"""
from __future__ import annotations
import json
import logging
import re
from pathlib import Path
from typing import Literal, Optional
import pdfplumber
from pydantic import BaseModel, Field

logger = logging.getLogger("page1_features")

# ── Regex patterns ──────────────────────────────────────────────────────────

# Packages: captures name + optional inline dimensions (e.g. "TSSOP20 (6.4 x 4.4 mm)")
PACKAGE_RE = re.compile(
    r'(?P<name>(?:LQFP|UFQFPN|TSSOP|UFBGA|WLCSP|VFBGA|SO\d*N?|TFBGA|UQFN|LFBGA|QFN)\d*)'
    r'(?:\s*\(([^)]+)\))?',
    re.IGNORECASE
)
# Exclude false positives like SO7816 (from ISO7816 interface) — SO package ends with N or digit < 4 chars
_PACKAGE_SO_FP = re.compile(r'^SO\d{4,}$', re.IGNORECASE)
# Dimensions on separate line (e.g. "(4.9x6 mm)") — matched by position to package names
_DIMENSION_RE = re.compile(r'\((\d+\.?\d*\s*[×x]\s*\d+\.?\d*\s*mm)\)')

# Footer patterns for doc ref, revision and date
TYPE1_FOOTER_RE = re.compile(
    r'(\w+\s+\d{4})\s+(DS\d+)\s+Rev\s+(\d+)\s+\d+/\d+\s*$', re.MULTILINE
)
TYPE2_FOOTER_RE = re.compile(
    r'(DS\d+)\s*-\s*Rev\s+(\d+)\s*-\s*(\w+\s+\d{4})', re.MULTILINE
)

# Page range detection: stop scanning when these markers appear
END_MARKERS = [
    re.compile(r'^Contents$', re.IGNORECASE),
    re.compile(r'^Table of Contents$', re.IGNORECASE),
    re.compile(r'^1\s+Introduction', re.IGNORECASE),
    re.compile(r'^1\s+About this document', re.IGNORECASE),
    re.compile(r'^1\.\s+\w'),
]

# Full-text regexes — search entire text for these specs
CORE_RE = re.compile(r'Cortex[®\s]*-([A-Z0-9+]+)')
FPU_RE = re.compile(r'(?:with\s+)?FPU|floating\s+point\s+unit', re.IGNORECASE)
FREQ_RE = re.compile(r'(?:frequency\s+up\s+to|up\s+to)\s+(\d+)\s*MHz', re.IGNORECASE)
FLASH_RE = re.compile(r'(\d+(?:\.\d+)?)\s*-?\s*(?P<unit>[KMk])bytes?\s.*?flash', re.IGNORECASE)
RAM_RE = re.compile(r'(\d+(?:\.\d+)?)\s*-?\s*(?P<unit>[KMk])bytes?\s.*?SRAM', re.IGNORECASE)
VOLTAGE_RE = re.compile(r'(\d+\.?\d*)\s*(?:V\s*)?(?:to|-)\s*(\d+\.?\d*)\s*V')
TEMP_RE = re.compile(
    r'(-?\d+)\s*°C?\s*to\s*(-?\d+)(?:\s*°C?)?(?:\s*/\s*(-?\d+)(?:\s*°C?)?)?(?:\s*/\s*(-?\d+)(?:\s*°C?)?)?(?:\s*/\s*(-?\d+)(?:\s*°C?)?)?',
    re.IGNORECASE
)
DMA_RE = re.compile(r'(?:(\d+)\s*-?\s*(?:channel|channels)\s+(?:DMA|LPDMA)|(?:DMA|LPDMA).*?(\d+)\s*(?:channel|channels))', re.IGNORECASE)

# Part number fallback: used when pdfplumber table extraction fails
PART_RE = re.compile(r'STM32[A-Z0-9]{6,}')

# (TIMER_LINE_RE, ADC_LINE_RE, COMM_INTF_RE, SECURITY_KW_RE supprimés — inutilisés)

MAX_SCAN_PAGES = 10


def _detect_pdf_type(pdf_path: str, pdf=None) -> int:
    """Détecte le type de PDF : 1 = Acrobat, 2 = Antenna House."""
    try:
        if pdf is not None:
            producer = (pdf.metadata or {}).get("Producer", "")
        else:
            with pdfplumber.open(pdf_path) as _pdf:
                producer = (_pdf.metadata or {}).get("Producer", "")
        return 2 if "antenna" in producer.lower() else 1
    except Exception:
        return 1


# ── Pydantic models ─────────────────────────────────────────────────────────

class ExtractionMeta(BaseModel):
    source_pages: list[int]
    extraction_method: str
    text_extraction_reliable: bool = True
    packages_source: Literal["text", "image_label", "uncertain"] = "text"
    confidence: Literal["high", "medium", "low"] = "high"
    missing_fields: list[str] = []
    low_confidence_fields: list[str] = []


class DeviceFeatures(BaseModel):
    pdf_name: str
    family: str
    pdf_type: int
    doc_ref: Optional[str] = None
    revision: Optional[str] = None
    date: Optional[str] = None
    title: Optional[str] = None
    url_pdf: Optional[str] = None
    page: Optional[int] = None
    core: Optional[str] = None
    fpu: bool = False
    max_frequency_mhz: Optional[int] = None
    flash_kb: Optional[int] = None
    ram_kb: Optional[int] = None
    voltage_min_v: Optional[float] = None
    voltage_max_v: Optional[float] = None
    operating_temp_c: list[str] = []
    packages: list[str] = []
    device_summary: Optional[dict] = None
    extraction_meta: ExtractionMeta


# ── Helpers ─────────────────────────────────────────────────────────────────

def _expand_device_summary_rows(ds: dict | None) -> dict | None:
    """Explose les part numbers sur leur propre ligne.

    Ex: ["STM32C011x4", "STM32C011F4, STM32C011J4"]
      → ["STM32C011x4", "STM32C011F4"]
        ["STM32C011x4", "STM32C011J4"]
    """
    if not ds or "rows" not in ds or not ds["rows"]:
        return ds
    headers = ds.get("headers", [])
    expanded = []
    for row in ds["rows"]:
        if len(row) < 2:
            expanded.append(row)
            continue
        ref = row[0]
        pns_raw = row[1]
        parts = [p.strip() for p in re.split(r'[,;]\s*|\n+', pns_raw) if p.strip()]
        for p in parts:
            expanded.append([ref, p])
    return {"headers": headers, "rows": expanded}


def _get_pdf_page_count(pdf_path: str, pdf=None) -> int:
    try:
        if pdf is not None:
            return len(pdf.pages)
        with pdfplumber.open(pdf_path) as _pdf:
            return len(_pdf.pages)
    except Exception:
        return 999


def _get_page_text(pdf_path: str, page: int, pdf=None) -> str:
    try:
        if pdf is not None:
            if page < 1 or page > len(pdf.pages):
                return ""
            return pdf.pages[page - 1].extract_text() or ""
        with pdfplumber.open(pdf_path) as _pdf:
            if page < 1 or page > len(_pdf.pages):
                return ""
            return _pdf.pages[page - 1].extract_text() or ""
    except Exception as e:
        logger.warning(f"pdfplumber page {page} failed: {e}")
        return ""


# ── Page range detection (100% dynamic, no type-based rules) ──────────────

def detect_features_page_range(pdf_path: str, pdf=None) -> list[int]:
    """Scanne les premières pages jusqu'à trouver Contents/Introduction."""
    total = _get_pdf_page_count(pdf_path, pdf=pdf)
    for p in range(1, min(total, MAX_SCAN_PAGES) + 1):
        text = _get_page_text(pdf_path, p, pdf=pdf)
        if not text:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if any(m.match(stripped) for m in END_MARKERS):
                return list(range(1, p)) if p > 1 else [1]
    return [1]


def _extract_text_for_pages(pdf_path: str, pages: list[int], pdf=None) -> str:
    """Concatène le texte pdfplumber des pages demandées."""
    if not pages:
        return ""
    try:
        if pdf is not None:
            parts = []
            for p in pages:
                if 1 <= p <= len(pdf.pages):
                    text = pdf.pages[p - 1].extract_text()
                    if text:
                        parts.append(text)
            return "\n\n".join(parts)
        with pdfplumber.open(pdf_path) as _pdf:
            parts = []
            for p in pages:
                if 1 <= p <= len(_pdf.pages):
                    text = _pdf.pages[p - 1].extract_text()
                    if text:
                        parts.append(text)
            return "\n\n".join(parts)
    except Exception as e:
        logger.warning(f"pdfplumber pages {pages} failed: {e}")
        return ""


# ── Parsers ─────────────────────────────────────────────────────────────────

def _parse_header_footer(text: str, pdf_type: int) -> dict:
    """Extrait doc_ref, revision, date et title depuis le footer du PDF."""
    result = {"doc_ref": None, "revision": None, "date": None, "title": None}

    if pdf_type == 1:
        m = TYPE1_FOOTER_RE.search(text)
        if m:
            result["date"] = m.group(1).strip()
            result["doc_ref"] = m.group(2)
            result["revision"] = f"Rev {m.group(3)}"
    else:
        m = TYPE2_FOOTER_RE.search(text)
        if m:
            result["doc_ref"] = m.group(1)
            result["revision"] = f"Rev {m.group(2)}"
            result["date"] = m.group(3).strip()

    # Title: first substantial line(s) not matching noise patterns
    # Handle multi-line titles (e.g. "Arm Cortex -M0+ ... RAM ," on line 1, "2 x USART ... V" on line 2)
    title_lines = []
    title_started = False
    for line in text.splitlines():
        line = line.strip()
        if not title_started:
            # Look for the actual title (contains product description keywords)
            if any(kw in line for kw in ("Arm", "Cortex", "MCU", "MPU")):
                title_started = True
                title_lines.append(line)
            continue
        # Title started, collect subsequent lines until noise
        if len(line) < 5:
            break
        if any(x in line for x in ("Datasheet", "DS", "Rev", "page")):
            break
        if line.startswith(("•", "-", "–", "Table", "Product")):
            break
        if line == "Features":
            break
        title_lines.append(line)
        if len(title_lines) >= 3:
            break
    if title_lines:
        result["title"] = " ".join(title_lines)

    return result


# Pattern pour dimensions sans parenthèses (ex: "14 x 14 mm")
_PLAIN_DIMENSION_RE = re.compile(r'(\d+\.?\d*\s*[×x]\s*\d+\.?\d*\s*mm)')


def _parse_packages(text: str) -> list[str]:
    """
    Extrait les noms de packages et leurs dimensions associées.

    Pour chaque nom de package trouvé :
    1. Si des dimensions inline sont capturées dans PACKAGE_RE, les utiliser
    2. Sinon, chercher la dimension la plus proche après le nom (≤ 200 car.)
       - D'abord les dimensions parenthésées (format standard)
       - Puis les dimensions sans parenthèses (ex: "14 x 14 mm")
    3. Déduplication par nom de package
    """
    seen = set()
    packages = []

    dims_all = [(m.start(), m.group(1)) for m in _DIMENSION_RE.finditer(text)]
    plain_dims = [(m.start(), m.group(1)) for m in _PLAIN_DIMENSION_RE.finditer(text)]

    def _is_valid_dim(text: str) -> bool:
        """Vérifie si un texte ressemble à une dimension (contient x et mm)."""
        return bool(re.search(r'\d+\s*[×x]\s*\d+\s*mm', text, re.IGNORECASE))

    def _normalize_dim(dim: str) -> str:
        """Normalise '14x14mm' -> '14 x 14 mm', '5x5mm' -> '5 x 5 mm', etc."""
        m = re.match(r'(\d+\.?\d*)\s*[×x]\s*(\d+\.?\d*)\s*mm', dim, re.IGNORECASE)
        if m:
            return f"{m.group(1)} x {m.group(2)} mm"
        return dim

    pkg_positions = [(m.start(), m.end()) for m in PACKAGE_RE.finditer(text)
                     if len(m.group("name")) >= 4
                     and any(c.isdigit() for c in m.group("name"))]

    for m in PACKAGE_RE.finditer(text):
        name = m.group("name")
        if not name or len(name) < 4:
            continue
        if _PACKAGE_SO_FP.match(name):
            continue
        if not any(c.isdigit() for c in name):
            continue
        key = name.upper()
        if key in seen:
            continue
        seen.add(key)

        end = m.end()
        inline = m.group(2)
        if inline and _is_valid_dim(inline):
            packages.append(f"{name} ({_normalize_dim(inline)})")
            continue

        def _dim_not_owned_by_other_pkg(dpos: int) -> bool:
            for ps, pe in pkg_positions:
                if ps > end and ps < dpos:
                    return False
            return True

        best_dims = None
        for dpos, dims in dims_all:
            if dpos >= end and dpos - end <= 200 and _dim_not_owned_by_other_pkg(dpos):
                best_dims = _normalize_dim(dims)
                break
        if not best_dims:
            for dpos, dims in plain_dims:
                if dpos >= end and dpos - end <= 200 and _dim_not_owned_by_other_pkg(dpos):
                    best_dims = _normalize_dim(dims)
                    break

        if best_dims:
            packages.append(f"{name} ({best_dims})")
        else:
            packages.append(name)

    return packages


def _parse_device_summary(pdf_path: str, text: str, pdf=None) -> dict | None:
    """
    Extrait la table Device summary (Reference / Part number) depuis la page 1.

    Type 1 (Acrobat) : headers contiennent "Reference" / "Part number".
    Type 2 (Antenna House) : bandeau header (Product summary / Device summary),
        pas de headers explicites — 2 colonnes visuelles (Reference | Part number).
    """
    try:
        _local_pdf = pdf
        if _local_pdf is None:
            import pdfplumber as _pplib
            _local_pdf = _pplib.open(pdf_path)
            _close_local = True
        else:
            _close_local = False
        try:
            if len(_local_pdf.pages) < 1:
                raise ValueError("no pages")
            p = _local_pdf.pages[0]
            tables = p.extract_tables()
            for table in tables:
                if not table or len(table) < 2:
                    continue
                headers = table[0]
                if not headers:
                    continue
                h_text = " ".join(h.strip().lower() for h in headers if h)
                h_low = [h.strip().lower() if h else "" for h in headers]

                # Type 1 : headers contiennent "reference" / "part number"
                if "reference" in h_low or "part number" in h_low:
                    clean_headers = [h.strip() if h else "" for h in headers]
                    clean_rows = []
                    for row in table[1:]:
                        clean_row = [(c or "").replace("\n", " ").strip() for c in row]
                        if any(c for c in clean_row):
                            clean_rows.append(clean_row)
                    if clean_rows:
                        return _expand_device_summary_rows({"headers": clean_headers, "rows": clean_rows})

                # Type 2 : bandeau header (Product summary / Device summary)
                type2_keywords = ("product summary", "device summary", "product status")
                if any(kw in h_text for kw in type2_keywords):
                    return _parse_device_summary_type2(table)
        finally:
            if _close_local:
                _local_pdf.close()
    except Exception:
        pass

    # Fallback regex
    seen = set()
    parts = []
    for m in PART_RE.finditer(text):
        pn = m.group(0)
        if "x" in pn.lower() or len(pn) < 9:
            continue
        if pn not in seen:
            seen.add(pn)
            parts.append(pn)
    if parts:
        parts = sorted(parts)
        return {"headers": ["Part number"], "rows": [[p] for p in parts]}
    return None


def _parse_device_summary_type2(table: list) -> dict:
    """
    Parse un tableau device_summary Type 2 (Antenna House).

    Structure :
    - Row 0 : Bandeau header (Product summary / Device summary) — ignoré
    - Row 1+ : [reference_artefacts, part_numbers_multi_lignes]

    Les artefacts (caractères "S" du bandeau bleu) sont nettoyés.
    Les part numbers multi-lignes sont regroupés par référence.
    """
    headers = ["Reference", "Part number"]
    rows = []

    for row in table[1:]:  # Skip header band
        if not row or len(row) < 2:
            continue
        ref_raw = (row[0] or "").strip()
        pns_raw = (row[1] or "").strip()

        if not ref_raw or not pns_raw:
            continue

        # Nettoyer la référence : supprimer artefacts "S" isolés du bandeau
        ref_lines = [l.strip() for l in ref_raw.split("\n") if l.strip()]
        ref_clean = " ".join(ref_lines)
        # Supprimer les "S" isolés (artefacts du bandeau bleu)
        ref_clean = re.sub(r'\bS\b', '', ref_clean).strip()
        ref_clean = re.sub(r'\s+', ' ', ref_clean).strip()

        if not ref_clean:
            continue

        # Nettoyer les part numbers : collapse whitespace, remplacer \n par ", "
        pns_clean = " ".join(pns_raw.split())
        pns_clean = pns_clean.replace("\n", ", ")
        # Supprimer artefacts "S" isolés au début ou après virgule
        pns_clean = re.sub(r'(?:^|,\s*)S\b', lambda m: m.group(0).replace('S', ''), pns_clean)
        pns_clean = re.sub(r'\s+', ' ', pns_clean).strip(", ")
        # Nettoyer virgules en double
        pns_clean = re.sub(r',\s*,', ',', pns_clean).strip(", ")

        # Corriger les part numbers qui manquent le préfixe "STM"
        # Si la référence commence par "STM32", les part numbers doivent aussi
        if ref_clean.startswith("STM32") and pns_clean and not pns_clean.startswith("STM"):
            pns_clean = re.sub(r'(?<!\w)TM32', 'STM32', pns_clean)

        if ref_clean and pns_clean:
            rows.append([ref_clean, pns_clean])

    return _expand_device_summary_rows({"headers": headers, "rows": rows})


def _parse_features_bullets(text: str) -> dict:
    """Extrait core, freq, flash, ram, voltage, temperature depuis le texte."""
    result = {
        "core": None, "fpu": False,
        "max_frequency_mhz": None, "flash_kb": None, "ram_kb": None,
        "voltage_min_v": None, "voltage_max_v": None,
        "operating_temp_c": [],
    }

    m = CORE_RE.search(text)
    if m:
        result["core"] = f"Cortex-{m.group(1)}"

    if FPU_RE.search(text):
        result["fpu"] = True

    m = FREQ_RE.search(text)
    if m:
        result["max_frequency_mhz"] = int(m.group(1))

    # flash: plusieurs matchs possibles (cache + flash) → on prend le max
    flash_matches = list(FLASH_RE.finditer(text))
    if flash_matches:
        def _flash_val(m):
            v = float(m.group(1))
            u = m.group("unit") or "K"
            return int(round(v * 1024)) if u.upper() == "M" else int(v)
        best = max(flash_matches, key=_flash_val)
        result["flash_kb"] = _flash_val(best)

    m = RAM_RE.search(text)
    if m:
        val = float(m.group(1))
        unit = m.group("unit") or "K"
        result["ram_kb"] = int(round(val * 1024)) if unit.upper() == "M" else int(val)

    m = VOLTAGE_RE.search(text)
    if m:
        result["voltage_min_v"] = float(m.group(1))
        result["voltage_max_v"] = float(m.group(2))

    # Temperature: supporte -40°C to 85°C/105°C/125°C et -40 °C to 85/125 °C
    for m in TEMP_RE.finditer(text):
        groups = [m.group(i) for i in range(1, 6) if m.group(i)]
        if len(groups) >= 2:
            low = groups[0]
            temps = [f"{low}°C to {t}°C" for t in groups[1:]]
        else:
            temps = []
        result["operating_temp_c"] = temps
        break

    return result


# ── Main entry point ────────────────────────────────────────────────────────

def _extract_packages_from_pdf(pdf_path: str, pdf=None) -> list[str]:
    """
    Extrait les packages de TOUTES les pages en utilisant extract_words()
    pour reconstituer correctement le texte même si chaque caractère est
    un objet texte positionné individuellement.

    Fusion : si un package apparaît sur plusieurs pages, on garde la
    version AVEC dimensions (même si trouvée plus tard).
    """
    def _has_real_dim(entry: str) -> bool:
        return bool(re.search(r'\d+\s*[×x]\s*\d+\s*mm', entry, re.IGNORECASE))

    def _page_text_from_words(page) -> str:
        """Reconstitue le texte d'une page à partir des mots/charactères.
        Si une ligne contient surtout des caractères isolés (mot d'1 lettre),
        on les joint sans espaces pour reconstituer les vrais mots.
        """
        words = page.extract_words()
        if not words:
            return ""

        # Grouper par Y arrondi
        lines: dict[int, list[tuple[float, str]]] = {}
        for w in words:
            y_key = round(w["top"], 0)
            lines.setdefault(y_key, []).append((w["x0"], w["text"]))

        text_lines = []
        for y_pos in sorted(lines):
            chars = sorted(lines[y_pos], key=lambda x: x[0])
            words_on_line = [c[1] for c in chars]

            # Si la plupart des mots sont des caractères uniques → joindre
            single_chars = sum(1 for w in words_on_line if len(w) == 1)
            if single_chars >= len(words_on_line) * 0.6 and len(words_on_line) > 3:
                line_text = "".join(words_on_line)
            else:
                line_text = " ".join(words_on_line)
            text_lines.append(line_text)

        return "\n".join(text_lines)

    seen: dict[str, str] = {}
    page_of: dict[str, int] = {}

    try:
        _local_pdf = pdf
        if _local_pdf is None:
            import pdfplumber as _pplib
            _local_pdf = _pplib.open(pdf_path)
            _close_local = True
        else:
            _close_local = False
        try:
            for pg_idx, page in enumerate(_local_pdf.pages):
                text = _page_text_from_words(page)
                if not text:
                    continue
                pkgs_on_page = _parse_packages(text)
                for entry in pkgs_on_page:
                    name = entry.split(" (")[0].upper()
                    has_dim = _has_real_dim(entry)
                    if name not in seen:
                        seen[name] = entry
                        page_of[name] = pg_idx + 1
                    elif has_dim and not _has_real_dim(seen[name]):
                        seen[name] = entry
                        page_of[name] = pg_idx + 1
        finally:
            if _close_local:
                _local_pdf.close()
    except Exception as e:
        logger.warning(f"Package extraction from PDF failed: {e}")

    sorted_pkgs = sorted(seen.items(), key=lambda x: (page_of[x[0]], x[0]))
    return [entry for _, entry in sorted_pkgs]


def extract_features_page_range(pdf_path: str, pdf=None) -> dict:
    """
    Point d'entrée principal.

    1. Détecte les pages de features (page 1..N avant Contents)
    2. Extrait le texte via pdfplumber
    3. Parse header/footer, packages, device_summary, features bullets
    4. Valide et retourne le dict DeviceFeatures
    """
    p = Path(pdf_path)
    pdf_name = p.stem
    family = p.parent.name
    pdf_type_val = _detect_pdf_type(str(p), pdf=pdf)

    pages = detect_features_page_range(str(p), pdf=pdf)
    full_text = _extract_text_for_pages(str(p), pages, pdf=pdf)

    header = _parse_header_footer(full_text, pdf_type_val)
    # Packages : scanner TOUTES les pages page par page
    pkgs = _extract_packages_from_pdf(str(p), pdf=pdf)
    device_summary = _parse_device_summary(str(p), full_text, pdf=pdf)
    features = _parse_features_bullets(full_text)

    extraction_method = f"regex_type{pdf_type_val}"

    missing_fields = []
    for field in ["core", "max_frequency_mhz", "flash_kb", "ram_kb",
                   "voltage_min_v", "voltage_max_v"]:
        if features.get(field) is None:
            missing_fields.append(field)

    low_confidence_fields = []
    if not features.get("operating_temp_c"):
        missing_fields.append("operating_temp_c")

    meta = ExtractionMeta(
        source_pages=pages,
        extraction_method=extraction_method,
        confidence="high" if len(missing_fields) <= 1 else "medium",
        missing_fields=missing_fields,
        low_confidence_fields=low_confidence_fields,
        text_extraction_reliable=True,
        packages_source="text",
    )

    result = DeviceFeatures(
        pdf_name=pdf_name,
        family=family,
        pdf_type=pdf_type_val,
        doc_ref=header.get("doc_ref"),
        revision=header.get("revision"),
        date=header.get("date"),
        title=header.get("title"),
        url_pdf=f"https://www.st.com/resource/en/datasheet/{pdf_name}.pdf",
        page=pages[0] if pages else 1,
        core=features["core"],
        fpu=features["fpu"],
        max_frequency_mhz=features["max_frequency_mhz"],
        flash_kb=features["flash_kb"],
        ram_kb=features["ram_kb"],
        voltage_min_v=features["voltage_min_v"],
        voltage_max_v=features["voltage_max_v"],
        operating_temp_c=features["operating_temp_c"],
        packages=pkgs,
        device_summary=device_summary,
        extraction_meta=meta,
    )

    return result.model_dump()


