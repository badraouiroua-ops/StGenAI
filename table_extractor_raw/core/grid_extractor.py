"""
grid_extractor.py — Étape 2 : extraction de la grille brute via pdfplumber.

Fixes intégrés :
  [Fix 1] Texte vertical inversé (upright=False) → reconstruction depuis page.chars
  [Fix 2] Header multi-niveau → détection géométrique + split lignes
  [Fix 3] Sélection de table la plus proche EN DESSOUS de la légende
  [Fix 4] Newlines internes → espace
  [Fix 5] Rowspan/colspan → propagation géométrique via bboxes
  [Fix 6] Propagation descendante des cellules vides (rowspan) avec détection
          de groupe pour les PDFs Type 2
  [Fix 7] Insertion automatique de colonnes page 1 si continuation en a une
          de plus (split géométrique détecté par x0)

Pipeline : _extract_from_page → _expand_spans_and_headers → (continuation)
           → Fix 6 propagation → glyphe → qualité
"""
from __future__ import annotations
import datetime
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Optional

# ── Buffer debug cellules inversées ─────────────────────────────────────────
_reversed_debug_entries: list[dict] = []

def _reset_reversed_debug() -> None:
    _reversed_debug_entries.clear()

def _get_reversed_debug_entries() -> list[dict]:
    return list(_reversed_debug_entries)

import os
import pdfplumber
from pdfplumber.page import Page

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import (
    PDFPLUMBER_TABLE_SETTINGS,
    PDFPLUMBER_TABLE_SETTINGS_FALLBACK,
    PDFPLUMBER_TABLE_SETTINGS_TYPE2,
    PDFPLUMBER_TABLE_SETTINGS_FALLBACK_TYPE2,
    MIN_TABLE_WIDTH,
    DEBUG_IMAGE_DPI,
    DEBUG_EMPTY_ROWS,
    OUTPUT_DIR,
)
from core.toc_detector import TableRef, get_section_at
from core.glyph_fixer import CID_PATTERN, fix_headers, fix_rows
from core.quality_flags import evaluate_table
from core.continuation import find_continuations, _get_col_x0s
from core.ordering import extract_ordering_info

logger = logging.getLogger(__name__)

# ── Constantes pour la détection vectorielle des dashs ──────────────────────────
DASH_COL_KEYWORDS = ("parameter", "conditions", "symbol", "ratings",
                     "min", "typ", "max", "unit", "value")
DASH_CHARS = frozenset({"-", "–", "−", "\u2212", "\uf02d"})

# ══════════════════════════════════════════════════════════════════════════════
# FIX 1 — Texte vertical inversé
# ══════════════════════════════════════════════════════════════════════════════

def _get_rotated_text_map(page: Page) -> dict[tuple, str]:
    """
    Construit une map bbox → texte-correct pour les zones de texte rotatif.

    pdfplumber lit les chars upright=False de bas en haut → texte inversé.
    On regroupe ces chars par zone (même x-range arrondi) et on les re-trie
    par y décroissant (bas → haut dans l'espace PDF = ordre naturel de lecture
    pour texte vertical-upward).

    Retourne {(x0_arrondi, x1_arrondi): texte_corrigé}
    """
    rotated_chars = [c for c in page.chars if not c.get("upright", True)]
    if not rotated_chars:
        return {}

    # Clustering 1D strict par coordonnée X pour éviter de mélanger des
    # colonnes verticales adjacentes (tolérance 2px max).
    rotated_chars.sort(key=lambda c: c["x0"])
    clusters: list[list[dict]] = []
    
    for c in rotated_chars:
        if not clusters:
            clusters.append([c])
            continue
        last_cluster = clusters[-1]
        avg_x0 = sum(ch["x0"] for ch in last_cluster) / len(last_cluster)
        if abs(c["x0"] - avg_x0) <= 2.5:
            last_cluster.append(c)
        else:
            clusters.append([c])

    result = {}
    for chars in clusters:
        # Trier par top décroissant (top grand = en bas de la page)
        # Pour texte "Timers" vertical : le T est en bas (ex: top=446), le s en haut (top=427)
        # → trier du plus grand top au plus petit donne T-i-m-e-r-s ✓
        chars_sorted = sorted(chars, key=lambda c: c["top"], reverse=True)
        text = "".join(c["text"] for c in chars_sorted).strip()
        if text:
            x0 = min(c["x0"] for c in chars)
            x1 = max(c["x1"] for c in chars)
            y0 = min(c["top"] for c in chars)
            y1 = max(c["bottom"] for c in chars)
            # Use a slightly more generous bbox to match the cells later
            result[(round(x0)-2, round(y0)-2, round(x1)+2, round(y1)+2)] = text

    return result


# ══════════════════════════════════════════════════════════════════════════════
# FIX 2 — Header multi-niveau (normalisation des \n dans les cellules)
# ══════════════════════════════════════════════════════════════════════════════

def _normalize_newlines_in_cell(text: str) -> str:
    """Remplace les \n internes d'une cellule de data par un espace."""
    return re.sub(r"\s*\n\s*", " ", text).strip()


def _debug_headers_raw(
    table_id: str,
    table: list[list],
    header_depth: int,
    cols: int,
    final_headers: list[str],
) -> None:
    if header_depth < 2:
        return

    lines = [f"\n=== HEADER_DEBUG [{table_id}] depth={header_depth} cols={cols} rows_in_table={len(table)} ==="]

    for c in range(cols):
        merged = final_headers[c] if c < len(final_headers) else "?"

        parent_raw = ""
        child_raw = ""
        err = ""

        if header_depth >= 1 and len(table) >= 1 and c < len(table[0]):
            v = table[0][c]
            parent_raw = str(v).strip() if v is not None else "(None)"
            if v is None:
                err += "COL0_NONE "

        if header_depth >= 2 and len(table) >= 2 and c < len(table[1]):
            v = table[1][c]
            child_raw = str(v).strip() if v is not None else "(None)"
            if v is None:
                err += "COL1_NONE "

        frag_flag = ""
        if c > 0 and c < cols - 1:
            prev_merged = final_headers[c - 1] if c - 1 < len(final_headers) else ""
            nxt_merged = final_headers[c + 1] if c + 1 < len(final_headers) else ""
            if prev_merged and nxt_merged:
                common = len(os.path.commonprefix([prev_merged, merged, nxt_merged]))
                if common >= 5:
                    frag_flag = " ← FRAGMENTÉ"

        lines.append(
            f"  col {c:>2}: parent={parent_raw!r:45s} child={child_raw!r:25s}"
            f" -> {merged!r}  {err}{frag_flag}"
        )

    lines.append(f"=== END HEADER_DEBUG [{table_id}] ===\n")
    msg = "\n".join(lines)
    import sys as _sys
    _sys.stderr.write(msg + "\n")
    _sys.stderr.flush()


# ══════════════════════════════════════════════════════════════════════════════
# FIX 3 — Sélection de table par proximité sous la légende
# ══════════════════════════════════════════════════════════════════════════════

def _find_caption_y(page: Page, caption: str) -> Optional[float]:
    """
    Cherche la position y (bord bas) de la légende sur la page.
    Extrait les mots de la page et cherche le début de la légende.
    Ne cherche que dans les 75% inférieurs de la page (évite les légendes
    de figure en haut de page qui faussent caption_y → table vide).
    """
    # Extraire les 6 premiers mots de la légende pour la recherche
    caption_words = caption.lower().split()[:5]
    if not caption_words:
        return None

    words = page.extract_words()
    if not words:
        return None

    # Chercher une séquence de mots consécutifs qui matche le début de la légende
    for idx, word in enumerate(words):
        word_clean = word["text"].lower().split("(")[0].rstrip(".,:;!?")
        caption_word_clean = caption_words[0].lower().split("(")[0].rstrip(".,:;!?")
        if word_clean == caption_word_clean:
            # Vérifier les mots suivants
            match_count = 1
            for k in range(1, len(caption_words)):
                if idx + k < len(words):
                    w_clean = words[idx + k]["text"].lower().split("(")[0].rstrip(".,:;!?")
                    c_clean = caption_words[k].lower().split("(")[0].rstrip(".,:;!?")
                    if w_clean == c_clean:
                        match_count += 1
                    else:
                        break
            if match_count >= min(2, len(caption_words)):
                # Ignorer les cross-references "Table N" sans point après le nombre
                # (ex: "Table 16," vs vrai caption "Table 16.")
                if match_count == 2 and len(caption_words) > 3 and word_clean == "table" and "." not in words[idx + 1]["text"]:
                    continue
                return word["bottom"]

    return None


def _table_quality(table: list) -> float:
    """
    Score de 'qualité' d'un tableau réel (vs fragment d'image CID).
    Retourne le nombre de cellules réelles (non-vides, non-CID), ou -1.0 si vide.
    """
    if not table:
        return -1.0
    n_real = 0
    for row in table:
        for c in row:
            s = str(c or "").strip()
            if not s:
                continue
            if CID_PATTERN.search(s):
                continue
            n_real += 1
    return float(n_real) if n_real > 0 else -1.0


def _is_image_table(raw_table: list) -> bool:
    """Détecte si raw_table est un diagramme MCU (dimensions, broches)
    et non un vrai tableau de données. Se base sur le ratio de cellules
    purement numériques et l'absence de mots réels."""
    if not raw_table or len(raw_table) < 2:
        return False
    numeric = 0
    total = 0
    has_words = False
    for row in raw_table:
        for cell in row:
            s = str(cell or "").strip()
            if not s:
                continue
            total += 1
            if re.match(r'^-?[\d.,\s°\'"µ]+$', s):
                numeric += 1
            elif len(s) >= 3:
                has_words = True
    if total > 0 and numeric / total > 0.8 and not has_words:
        return True
    return False


def _pick_best_table(
    page: Page,
    tables: list,
    finder_tables: list,
    caption: str,
) -> tuple[Optional[list], Optional[Any], Optional[tuple]]:
    """
    [Fix 3] Sélectionne la table la plus proche EN DESSOUS de la légende.

    1. Localise la légende par coordonnées y
    2. Parmi les tables dont le bord supérieur est > y_légende, prend la plus proche
    3. Fallback : la plus grande si aucune position trouvée

    Retourne (raw_table, pdfplumber_table_obj, bbox).
    """
    candidates = [(t, ft) for t, ft in zip(tables, finder_tables)
                  if t and len(t) >= 2]
    if not candidates:
        return None, None, None

    caption_y = _find_caption_y(page, caption)

    if caption_y is not None and len(candidates) > 1:
        below = [(t, ft) for t, ft in candidates
                 if ft.bbox[1] > caption_y - 20]
        if below:
            sorted_candidates = sorted(below, key=lambda x: x[1].bbox[1])
        else:
            sorted_candidates = sorted(candidates, key=lambda x: x[1].bbox[1])
    else:
        sorted_candidates = sorted(candidates, key=lambda x: x[1].bbox[1])
        if _table_quality(sorted_candidates[0][0]) < 0.5:
            best_alt = max(candidates, key=lambda x: _table_quality(x[0]))
            if _table_quality(best_alt[0]) > _table_quality(sorted_candidates[0][0]):
                sorted_candidates = [best_alt] + [c for c in sorted_candidates if c != best_alt]

    # Filtrer les fausses tables images (diagrammes MCU)
    rejected_images = 0
    for best_t, best_ft in sorted_candidates:
        if _is_image_table(best_t):
            rejected_images += 1
            continue
        return best_t, best_ft, best_ft.bbox

    # Toutes les tables sont des images → log + retourner la meilleure quand même
    if rejected_images:
        logger.info(f"_pick_best_table: all {rejected_images}/{len(sorted_candidates)} "
                    f"candidates rejected as image tables (returning first anyway)")
    best_t, best_ft = sorted_candidates[0]
    return best_t, best_ft, best_ft.bbox


# ══════════════════════════════════════════════════════════════════════════════
# Extraction principale
# ══════════════════════════════════════════════════════════════════════════════

def _cell_str(cell) -> str:
    """Convertit une cellule pdfplumber (str ou None) en string propre."""
    if cell is None:
        return ""
    # Fix 4 : normaliser les \n internes dans les cellules de data
    return _normalize_newlines_in_cell(str(cell)).strip()


def _detect_vector_dashes(
    raw_table: list[list[str | None]],
    finder_table: Any,
    page: Page,
) -> list[list[str | None]]:
    """
    Détecte les tirets '-' rendus comme traits vectoriels (non capturés par
    pdfplumber) dans les colonnes à dash attendu (Parameter, Conditions,
    Symbol, Ratings, Min, Typ, Max, Unit, Value).

    Pour chaque cellule vide de ces colonnes :
      1. Vérifie page.chars dans la bbox de la cellule → dash-like char ?
      2. Vérifie page.lines dans la bbox → courte horizontale vectorielle ?
    Si une des deux vérifications trouve un dash → la cellule reçoit "-".
    Sinon elle reste "" (vraiment vide).

    Retourne la table corrigée (modifiée in-place).
    """
    if not raw_table or not finder_table or not hasattr(finder_table, 'rows'):
        return raw_table

    headers_raw = raw_table[0] if raw_table else []
    dash_cols = set()
    for i, h in enumerate(headers_raw):
        if h and isinstance(h, str) and any(kw in h.lower() for kw in DASH_COL_KEYWORDS):
            dash_cols.add(i)

    if not dash_cols:
        return raw_table

    n_rows = min(len(raw_table), len(finder_table.rows))

    for r_idx in range(n_rows):
        finder_row = finder_table.rows[r_idx]
        raw_row = raw_table[r_idx]

        if not hasattr(finder_row, 'cells'):
            continue

        n_cols = min(len(raw_row), len(finder_row.cells))

        for c_idx in range(n_cols):
            if c_idx not in dash_cols:
                continue

            cell_val = raw_row[c_idx]
            if cell_val is not None and cell_val != "":
                continue

            cell_bbox = finder_row.cells[c_idx]
            if cell_bbox is None:
                continue
            x0, top, x1, bottom = cell_bbox
            margin = 3

            found = False
            for ch in page.chars:
                if (ch["x0"] >= x0 - margin and ch["x1"] <= x1 + margin
                        and ch["top"] >= top - margin and ch["bottom"] <= bottom + margin
                        and ch["text"] in DASH_CHARS):
                    found = True
                    break

            if not found:
                for line in page.lines:
                    if (line["x0"] >= x0 - margin and line["x1"] <= x1 + margin
                            and line["top"] >= top - margin and line["bottom"] <= bottom + margin
                            and line.get("height", line["bottom"] - line["top"]) <= 4
                            and (line["x1"] - line["x0"]) > 3
                            and (line["x1"] - line["x0"]) < (x1 - x0) * 0.9):
                        found = True
                        break

            if found:
                raw_table[r_idx][c_idx] = "-"

    return raw_table


def _is_isolated_empty(row: list, c: int) -> bool:
    if row[c]:
        return False
    empty_count = sum(1 for cell in row if not cell)
    if empty_count > 1:
        return False
    return c > 0 and bool(row[c - 1]) and c < len(row) - 1 and bool(row[c + 1])

def _build_col_groups(headers: list) -> list[int]:
    groups: list[int] = []
    cur_id = 0
    prev = None
    for h in headers:
        norm = str(h or "").strip()
        if groups and norm and norm == prev:
            groups.append(cur_id)
        else:
            if groups:
                cur_id += 1
            groups.append(cur_id)
        prev = norm
    return groups


def _fill_horizontal(
    rows: list[list[str]],
    col_groups: list[int],
    headers: list[str] | None = None,
) -> None:
    unit_cols: set[int] = set()
    if headers:
        unit_cols = {i for i, h in enumerate(headers) if "unit" in h.lower().strip()}
    for row in rows:
        last_val: str = ""
        last_group: int = -1
        for c in range(len(row)):
            g = col_groups[c] if c < len(col_groups) else -1
            if row[c]:
                last_val = row[c]
                last_group = g
            elif last_val and last_group == g and not _is_isolated_empty(row, c):
                if c not in unit_cols:
                    row[c] = last_val


def _fill_identity_spans(
    rows_raw: list[list[str]],
    col_groups: list[int],
    headers: list[str] | None = None,
) -> None:
    if not rows_raw or not col_groups:
        return
    ncols = min(max(len(r) for r in rows_raw), len(col_groups))
    if ncols <= 1:
        return
    data_start_col = next((c for c, g in enumerate(col_groups) if g != 0), ncols)
    if data_start_col >= ncols:
        return

    unit_cols: set[int] = set()
    if headers:
        unit_cols = {i for i, h in enumerate(headers) if "unit" in h.lower().strip()}

    groups: dict[str, list[int]] = {}
    for r, row in enumerate(rows_raw):
        id_val = str(row[0] or "").strip() if len(row) > 0 else ""
        if id_val not in groups:
            groups[id_val] = []
        groups[id_val].append(r)

    group_sep: dict[str, set[int]] = {}
    for id_val, row_indices in groups.items():
        gsep: set[int] = set()
        for ri in row_indices:
            row = rows_raw[ri]
            for c in range(data_start_col + 1, ncols):
                if col_groups[c] == col_groups[c - 1]:
                    continue
                left = str(row[c - 1] or "").strip() if c - 1 < len(row) else ""
                right = str(row[c] or "").strip() if c < len(row) else ""
                if left and right and left != right:
                    gsep.add(c)
        group_sep[id_val] = gsep

    for row in rows_raw:
        id_val = str(row[0] or "").strip() if len(row) > 0 else ""
        gsep = group_sep.get(id_val, set())
        prev_val = ""
        for c in range(ncols):
            if c in gsep:
                prev_val = ""
            curr = str(row[c] or "").strip() if c < len(row) else ""
            if curr and c >= data_start_col:
                if c not in unit_cols:
                    prev_val = curr
            elif prev_val and c >= data_start_col and c not in unit_cols:
                row[c] = prev_val


def _fill_vertical(
    rows: list[list[str]],
    col_groups: list[int],
    headers: list[str] | None = None,
) -> None:
    if len(rows) < 2:
        return
    n_cols = min(len(rows[0]), max(len(r) for r in rows)) if rows else 0
    unit_cols: set[int] = set()
    if headers:
        unit_cols = {i for i, h in enumerate(headers) if "unit" in h.lower().strip()}
    # Identify the identity columns (contiguous from col 0 sharing the
    # same col_group[0]). When the identity changes between consecutive
    # rows, reset the per-column carry to prevent cross-identity fill.
    id_group = col_groups[0] if col_groups else -1
    id_cols: list[int] = []
    if col_groups and id_group >= 0:
        for cc in range(n_cols):
            if cc < len(col_groups) and col_groups[cc] == id_group:
                id_cols.append(cc)
            else:
                break
    for c in range(n_cols):
        g = col_groups[c] if c < len(col_groups) else -1
        last_val: str = ""
        last_group: int = -1
        for r in range(len(rows)):
            if c < len(rows[r]):
                # Reset carry when identity columns change
                if id_cols and r > 0:
                    id_changed = False
                    for chk_c in id_cols:
                        val_r = (rows[r][chk_c] or "").strip()
                        val_prev = (rows[r-1][chk_c] or "").strip()
                        if val_r and val_r != val_prev:
                            id_changed = True
                            break
                    if id_changed and c not in unit_cols:
                        last_val = ""
                        last_group = -1
                cell = rows[r][c]
                is_unit_dash = c in unit_cols and cell == "-"
                if cell and not is_unit_dash:
                    last_val = cell
                    last_group = g
                elif last_val and last_group == g and (not cell or is_unit_dash):
                    rows[r][c] = last_val


def _ensure_no_empty_cells(
    rows: list[list[str]],
    col_groups: list[int],
    headers: list[str] | None = None,
) -> None:
    unit_cols: set[int] = set()
    if headers:
        unit_cols = {i for i, h in enumerate(headers) if "unit" in h.lower().strip()}
    changed = True
    while changed:
        old = [list(r) for r in rows]
        _fill_horizontal(rows, col_groups, headers=headers)
        _fill_vertical(rows, col_groups, headers=headers)
        for ri, row in enumerate(rows):
            first_val = next((c for c in row if c), "")
            first_col = next((ci for ci, c in enumerate(row) if c), -1)
            if not first_val and ri > 0:
                above = rows[ri - 1]
                for c in range(len(row)):
                    g = col_groups[c] if c < len(col_groups) else -1
                    if c < len(above) and above[c]:
                        row[c] = above[c]
                first_val = next((c for c in row if c), "")
                first_col = next((ci for ci, c in enumerate(row) if c), -1)
            if first_val and first_col >= 0:
                g_first = col_groups[first_col] if first_col < len(col_groups) else -1
                for c in range(len(row)):
                    g = col_groups[c] if c < len(col_groups) else -1
                    if not row[c] and g == g_first and c not in unit_cols:
                        row[c] = first_val
        changed = any(
            old[r][c] != rows[r][c]
            for r in range(len(rows))
            for c in range(len(rows[r]))
        )





def _save_table_crop(
    page: Page,
    table_bbox: Optional[tuple],
    output_base: Path,
    table_id: str,
    family: str,
    pdf_name: str,
) -> str | None:
    """Sauvegarde un crop de la zone de la table dans debug/ et retourne le chemin relatif."""
    try:
        debug_dir = output_base / family / pdf_name / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        img_path = debug_dir / f"{table_id}.png"

        if table_bbox:
            cropped = page.crop(table_bbox)
            img = cropped.to_image(resolution=DEBUG_IMAGE_DPI)
        else:
            img = page.to_image(resolution=DEBUG_IMAGE_DPI)

        img.save(str(img_path))
        return str(img_path.relative_to(output_base.parent))
    except Exception as e:
        logger.warning(f"Could not save table crop: {e}")
        return None


def _save_empty_rows_debug(
    page: Page,
    ref: Any,
    pdf_type: int,
    extraction_method: str,
    raw_table: list | None,
    bbox: tuple | None,
    settings: dict,
    pdf_path: str,
    output_base: Path,
    family: str,
    pdf_name: str,
    caption_y: float | None = None,
    caption_near_bottom: bool = False,
    n_non_empty: int = 0,
    should_try_next: bool = False,
    warnings_list: list | None = None,
    heuristics_dict: dict | None = None,
    pdf: Any = None,
    table_obj: Any = None,
    nxt_attempt: dict | None = None,
) -> None:
    """Sauvegarde un debug complet quand une table a 0 lignes.

    Capture : image pleine page + metadata JSON pour analyse.
    """
    try:
        dbg_dir = output_base / family / pdf_name / "dbg_empty"
        dbg_dir.mkdir(parents=True, exist_ok=True)

        tbl_id = ref.table_id if hasattr(ref, "table_id") else str(ref)
        timestamp = datetime.datetime.now().strftime("%H%M%S")

        # Image pleine page
        img = page.to_image(resolution=DEBUG_IMAGE_DPI)
        img_path = dbg_dir / f"{tbl_id}_page{ref.page}_{timestamp}.png"
        img.save(str(img_path))

        # Texte de la page
        page_text = page.extract_text() or ""

        # Metadata
        meta = {
            "table_id": tbl_id,
            "caption": ref.caption if hasattr(ref, "caption") else "",
            "page": ref.page if hasattr(ref, "page") else 0,
            "section": ref.section if hasattr(ref, "section") else "",
            "pdf_type": pdf_type,
            "extraction_method": extraction_method,
            "bbox": bbox,
            "page_width": page.width,
            "page_height": page.height,
            "settings": settings,
            "caption_y": caption_y,
            "caption_y_ratio": round(caption_y / page.height, 3) if caption_y else None,
            "caption_near_bottom": caption_near_bottom,
            "n_non_empty_raw": n_non_empty,
            "should_try_next": should_try_next,
            "raw_table": raw_table,
            "warnings": warnings_list or [],
            "heuristics": heuristics_dict or {},
            "nxt_attempt": nxt_attempt,
            "page_text_first_1000": page_text[:1000] if page_text else "",
            "pdf_path": pdf_path,
            "pdf_name": pdf_name,
            "family": family,
        }
        meta_path = dbg_dir / f"{tbl_id}_page{ref.page}_{timestamp}.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False, default=str)
        logger.info(f"Empty rows debug saved: {img_path} / {meta_path}")
    except Exception as e:
        logger.warning(f"Could not save empty rows debug: {e}")


def _propagate_spans_type2(
    table: list[list],
    raw_table: list[list],
    rows: int,
    cols: int,
    inserted_rows: int,
    ghost_cols: set[int],
    col_centers: list[Optional[float]],
    table_obj: Any,
) -> None:
    """
    Propagation géométrique des cellules fusionnées pour les PDFs Type 2
    (Antenna House / XML-based).
    
    Stratégie : pour chaque cellule vide, chercher la cellule la plus proche
    (même ligne d'abord, puis en remontant) dont la bbox CONTIENT le centre
    de la colonne cible.
    """
    if not table_obj or not hasattr(table_obj, "rows") or not table_obj.rows:
        return

    for r in range(rows):
        r_phys = r - inserted_rows
        if r_phys < 0:
            for c in range(cols):
                if table[r][c] is None:
                    table[r][c] = table[0][c]
            continue
        if r_phys >= len(table_obj.rows):
            break

        target_row = table_obj.rows[r_phys]
        target_row_top = target_row.bbox[1]

        for c in range(cols):
            if c >= len(table[r]) or table[r][c] is not None:
                continue
            if c in ghost_cols:
                table[r][c] = ""
                continue

            target_cx = col_centers[c]
            if target_cx is None:
                table[r][c] = ""
                continue

            master_val = None

            for r_m in range(r, -1, -1):
                r_m_phys = r_m - inserted_rows
                if r_m_phys < 0 or r_m_phys >= len(table_obj.rows):
                    continue
                for c_m in range(c + 1):
                    if c_m >= len(table_obj.rows[r_m_phys].cells):
                        continue
                    cell_bbox = table_obj.rows[r_m_phys].cells[c_m]
                    if cell_bbox is None:
                        continue

                    covers_row = (r_m == r) or (cell_bbox[3] > target_row_top + 2)
                    covers_col = (cell_bbox[0] <= target_cx <= cell_bbox[2])

                    if covers_row and covers_col:
                        val = raw_table[r_m][c_m] if c_m < len(raw_table[r_m]) else None
                        if val is not None:
                            master_val = val
                            break
                if master_val is not None:
                    break

            table[r][c] = master_val


def _count_header_rows_by_color(page, table_obj) -> int:
    """
    Compte les lignes d'en-tête via fond bleu foncé (Type 2 Antenna House).
    Retourne 0 si pas de bleu détecté (fallback vers heuristiques).
    Vérifie que les Y bleus correspondent à des vraies lignes du tableau,
    puis groupe les Y consécutifs (tolérance 2px). Limite à 150px de hauteur.
    """
    if not hasattr(page, 'rects') or not table_obj or not table_obj.bbox:
        return 0
    if not hasattr(table_obj, 'rows') or not table_obj.rows:
        return 0

    table_bbox = table_obj.bbox

    # Collecter les Y des vraies rangées du tableau
    row_y_bottoms = sorted(set(round(r.bbox[3], 1) for r in table_obj.rows if hasattr(r, 'bbox')))

    # Collecter les rectangles remplis en bleu foncé dans la zone table
    header_y_bottoms = []
    for r in page.rects:
        if (r['x0'] >= table_bbox[0] - 5 and r['x1'] <= table_bbox[2] + 5
                and r['top'] >= table_bbox[1] - 5 and r['bottom'] <= table_bbox[3] + 5):
            fill = r.get('non_stroking_color')
            if fill and len(fill) == 3:
                r_norm, g_norm, b_norm = fill
                if r_norm < 0.15 and g_norm < 0.25 and b_norm > 0.25:
                    if r['bottom'] - table_bbox[1] < 150:
                        header_y_bottoms.append(r['bottom'])

    if not header_y_bottoms:
        return 0

    # Filtrer : ne garder que les Y qui correspondent à des lignes réelles
    matching = sorted(set(
        round(y, 1) for y in header_y_bottoms
        if any(abs(round(y, 1) - ry) < 2.0 for ry in row_y_bottoms)
    ))

    if not matching:
        return 0

    # Grouper les Y consécutifs (tolérance 2px)
    groups = []
    for y in matching:
        if not groups or y - groups[-1][-1] > 2.0:
            groups.append([y])
        else:
            groups[-1].append(y)

    return len(groups)


def _expand_spans_and_headers(
    raw_table: list[list],
    table_obj: Optional[Any] = None,
    page: Optional[Any] = None,
    pdf_type: int = 1,
    table_id: str = "",
) -> tuple[list[str], list[list[str]], list[str]]:
    """
    Propagation géométrique + détection de profondeur d'en-tête + compression.

    Pipeline interne :
      0. Pré-calcul grille géométrique (centres de colonnes via bboxes)
      1. Division spatiale headers compressés (Type 2 : "STM32G081_/_F4")
      2. Détection colonnes fantômes (absentes de toutes les lignes physiques)
      3. Propagation géométrique des cellules fusionnées (rowspan/colspan)
         - Type 1 : parcours left-to-right, remontée verticale
         - Type 2 : recherche bbox qui CONTIENT le centre de colonne cible
      4. Détection profondeur d'en-tête (géométrique + couleur Type 2)
      5. Construction headers finaux (_build_final_headers avec propagation parent)
      6. Extraction lignes de données (hors header_depth)

    Fix complet gérant :
    - rowspan / colspan réels via bboxes
    - colonnes fantômes (structurellement absentes du PDF)
    - cellules encodant les sous-noms via \\n (Table 2 style)
    - en-têtes multi-niveaux avec texte rotatif
    Retourne (headers_compressés, rows_données_brutes, warnings).
    """
    if not raw_table:
        return [], [], ["empty_raw_table"]

    warnings = []
    table = [list(row) for row in raw_table]
    rows = len(table)
    cols = max(len(r) for r in table) if rows > 0 else 0

    # ── 0. Pré-calcul de la grille géométrique des colonnes ──────────────────
    col_centers: list[Optional[float]] = []
    grid_col_centers: list[float] = []
    if table_obj is not None and hasattr(table_obj, "rows") and len(table_obj.rows) > 0:
        # Extraire toutes les coordonnées X uniques pour reconstituer la grille exacte
        x_coords_set = set()
        for r_obj in table_obj.rows:
            for cell in r_obj.cells:
                if cell is not None:
                    x_coords_set.add(cell[0])
                    x_coords_set.add(cell[2])
        x_coords = sorted(list(x_coords_set))
        if len(x_coords) > 1:
            grid_col_centers = [(x_coords[i] + x_coords[i+1]) / 2.0 for i in range(len(x_coords) - 1)]
            col_centers = list(grid_col_centers)
        else:
            col_centers = [None] * cols
    else:
        col_centers = [None] * cols

    # Pad col_centers to match cols if needed
    while len(col_centers) < cols:
        col_centers.append(None)

    # ── 1. Division spatiale des en-têtes compressés (Table 2 style) ────────
    inserted_rows = 0
    rev_fallback = None
    if page is not None and table_obj is not None and hasattr(table_obj, "rows") and len(table_obj.rows) > 0 and len(grid_col_centers) == cols:
        compressed_c = -1
        cell_bbox = None
        span_cols = []
        for c in range(min(cols, len(raw_table[0]))):
            c_cell_bbox = table_obj.rows[0].cells[c] if c < len(table_obj.rows[0].cells) else None
            if c_cell_bbox is not None:
                c_span_cols = []
                for c2, cx in enumerate(grid_col_centers):
                    if c_cell_bbox[0] - 2 <= cx <= c_cell_bbox[2] + 2:
                        c_span_cols.append(c2)
                if len(c_span_cols) > 1:
                    # Vérification géométrique : la cellule a-t-elle du contenu multi-lignes ?
                    words = page.within_bbox(c_cell_bbox).extract_words(x_tolerance=1, y_tolerance=1)
                    lines_words = []
                    for w in words:
                        placed = False
                        for line in lines_words:
                            if abs(line[0]['top'] - w['top']) < 3:
                                line.append(w)
                                placed = True
                                break
                        if not placed:
                            lines_words.append([w])
                    if len(lines_words) >= 2:
                        # Ne pas traiter comme header compressé si c'est du
                        # wrapping de texte (phrase qui continue sur 2 lignes).
                        # Un vrai header compressé a un séparateur (_, /, \)
                        # ou la 2e ligne commence par une Maj.
                        parent_text = " ".join(w['text'] for w in lines_words[0])
                        child_text = " ".join(w['text'] for w in lines_words[1])
                        if parent_text and child_text:
                            last_char = parent_text[-1]
                            is_separator = last_char in ('_', '/', '\\', '-', '|')
                            if not is_separator and child_text[0].islower():
                                continue  # wrapping, pas un header compressé
                        compressed_c = c
                        cell_bbox = c_cell_bbox
                        span_cols = c_span_cols
                        break
                    # Fallback : texte inversé avec parties séparées par espace
                    cell_val = str(table[0][c] or "")
                    if ' ' in cell_val and _is_likely_reversed(cell_val):
                        parts = cell_val.split(' ')
                        if len(parts) >= 2 and len(parts) <= len(c_span_cols) and all(p.strip() for p in parts):
                            rev_fallback = (c, c_cell_bbox, c_span_cols, cell_val, parts)
        
        if compressed_c != -1 and cell_bbox is not None:
            c = compressed_c
                    
            # Extraction spatiale exacte
            words = page.within_bbox(cell_bbox).extract_words(x_tolerance=1, y_tolerance=1)
            
            # Regrouper les mots par ligne (tolérance 3pts)
            lines_words = []
            for w in words:
                placed = False
                for line in lines_words:
                    if abs(line[0]['top'] - w['top']) < 3:
                        line.append(w)
                        placed = True
                        break
                if not placed:
                    lines_words.append([w])
            
            lines_words.sort(key=lambda l: l[0]['top'])
            for line in lines_words:
                line.sort(key=lambda w: w['x0'])
            
            if len(lines_words) >= 2:
                # La 1ère ligne est le parent (ex: STM32G081_)
                parent_words = lines_words[0]
                parent_text = " ".join([w['text'] for w in parent_words])
                
                # Les lignes suivantes sont les enfants, projetés sur les centres
                new_row = [None] * cols
                col_text = {c2: [] for c2 in span_cols}
                
                for line in lines_words[1:]:
                    for w in line:
                        wcx = (w['x0'] + w['x1']) / 2.0
                        closest_c = min(span_cols, key=lambda c2: abs(grid_col_centers[c2] - wcx))
                        col_text[closest_c].append(w['text'])
                        
                for c2 in span_cols:
                    new_row[c2] = " ".join(col_text[c2])
                    
                table[0][c] = parent_text
                raw_table[0][c] = parent_text
                table.insert(1, new_row)
                raw_table.insert(1, new_row)
                rows += 1
                inserted_rows += 1
        
        if rev_fallback is not None:
            c, _, c_span_cols, cell_val, parts = rev_fallback
            rev_parts = [p[::-1].strip() for p in parts]
            parent_text = rev_parts[0]
            new_row = [None] * cols
            for idx, c2 in enumerate(c_span_cols):
                if idx == 0:
                    continue
                new_row[c2] = rev_parts[idx] if idx < len(rev_parts) else ""
            table[0][c] = parent_text
            raw_table[0][c] = parent_text
            table.insert(1, new_row)
            raw_table.insert(1, new_row)
            rows += 1
            inserted_rows += 1

    # ── 2. Identifier les colonnes fantômes ────────────────────────────────────
    ghost_cols: set[int] = set()
    if table_obj is not None and hasattr(table_obj, "rows"):
        for c in range(cols):
            # Si le c est hors limite ou si toutes les lignes physiques ont None
            if all(
                c >= len(table_obj.rows[r].cells) or table_obj.rows[r].cells[c] is None
                for r in range(min(rows - inserted_rows, len(table_obj.rows)))
            ):
                ghost_cols.add(c)

    # ── 3. Propagation géométrique des cellules fusionnées ─────────────────────
    if table_obj is not None and hasattr(table_obj, "rows") and len(table_obj.rows) > 0:
        if pdf_type == 2:
            _propagate_spans_type2(table, raw_table, rows, cols, inserted_rows,
                                   ghost_cols, col_centers, table_obj)
        else:
            for r in range(rows):
                r_phys = r - inserted_rows
                if r_phys < 0:
                    for c in range(cols):
                        if table[r][c] is None:
                            table[r][c] = table[0][c]
                    continue
                if r_phys >= len(table_obj.rows):
                    break
                target_row = table_obj.rows[r_phys]
                for c in range(cols):
                    if c >= len(table[r]) or (table[r][c] is not None and table[r][c] != ""):
                        continue
                    if c in ghost_cols:
                        table[r][c] = ""
                        continue
                    target_cx = col_centers[c]
                    master_val = None
                    for r_m in range(r, -1, -1):
                        r_m_phys = r_m - inserted_rows
                        if r_m_phys < 0 or r_m_phys >= len(table_obj.rows):
                            continue
                        for c_m in range(c, -1, -1):
                            if c_m >= len(table_obj.rows[r_m_phys].cells):
                                continue
                            cell_bbox = table_obj.rows[r_m_phys].cells[c_m]
                            if cell_bbox is None:
                                continue
                            covers_row = (r_m == r) or (cell_bbox[3] > target_row.bbox[1] + 2)
                            covers_col = (target_cx is None) or (cell_bbox[2] > target_cx - 0.5)
                            if covers_row and covers_col:
                                master_val = (raw_table[r_m][c_m]
                                              if c_m < len(raw_table[r_m]) else None)
                                break
                        if master_val is not None:
                            break
                    if master_val is not None:
                        table[r][c] = master_val
    else:
        # Fallback heuristique sans objet géométrique
        warnings.append("no_table_obj_for_spans")
        for r in range(rows):
            for c in range(cols):
                if c < len(table[r]) and table[r][c] is None:
                    left_val = table[r][c-1] if c > 0 else None
                    top_val = table[r-1][c] if r > 0 else None
                    raw_left = raw_table[r][c-1] if c > 0 and c-1 < len(raw_table[r]) else None
                    raw_top = raw_table[r-1][c] if r > 0 and c < len(raw_table[r-1]) else None
                    if left_val is not None and top_val is not None:
                        table[r][c] = top_val if (raw_top is not None and raw_left is None) else left_val
                    elif left_val is not None:
                        table[r][c] = left_val
                    elif top_val is not None:
                        table[r][c] = top_val

    # ── 3b. Fill-down : propager la dernière valeur non-vide vers le bas ───────
    # Ne propage qu'à l'intérieur du même groupe de colonnes défini par les
    # headers adjacents identiques (ex: "Peripheral"+"Peripheral" = groupe [0-1]).
    # Si l'identité hiérarchique change (col 0 + toutes les colonnes partageant
    # son group), on réinitialise le carry pour cette ligne → les cellules
    # vides viennent d'une nouvelle ligne, pas d'un rowspan.
    col_groups_fd = _build_col_groups(table[0] if rows > 0 else [])
    prev_id_vals: list[str] = []
    for c in range(min(cols, len(col_groups_fd))):
        g = col_groups_fd[c]
        carry = None
        carry_group = -1
        for r in range(rows):
            # Réinitialiser le carry si l'identité de cette ligne diffère
            # de la ligne précédente (comparaison des colonnes du groupe d'identité)
            if r > 0 and col_groups_fd and prev_id_vals:
                id_group = col_groups_fd[0]
                cur_id_vals = []
                for chk_c in range(min(cols, len(col_groups_fd))):
                    if col_groups_fd[chk_c] != id_group:
                        break
                    cur_val = table[r][chk_c] if chk_c < len(table[r]) else None
                    cur_id_vals.append(str(cur_val or "").strip())
                if len(cur_id_vals) == len(prev_id_vals):
                    identity_changed = any(
                        cur_id_vals[i] and cur_id_vals[i] != prev_id_vals[i]
                        for i in range(len(cur_id_vals))
                    )
                    if identity_changed:
                        carry = None
                        carry_group = -1
                    prev_id_vals = cur_id_vals
            elif r == 0 and col_groups_fd:
                id_group = col_groups_fd[0]
                cur_id_vals = []
                for chk_c in range(min(cols, len(col_groups_fd))):
                    if col_groups_fd[chk_c] != id_group:
                        break
                    cur_val = table[r][chk_c] if chk_c < len(table[r]) else None
                    cur_id_vals.append(str(cur_val or "").strip())
                prev_id_vals = cur_id_vals

            val = table[r][c] if c < len(table[r]) else None
            if val is not None and str(val).strip():
                carry = val
                carry_group = g
            elif (val is not None and not str(val).strip()) or val is None:
                if carry is not None and carry_group == g:
                    table[r][c] = carry

    # ── 4. Détection géométrique de la profondeur d'en-tête ────────────────────
    header_depth = 1 + inserted_rows
    if table_obj is not None and hasattr(table_obj, "rows") and len(table_obj.rows) > 0:
        for r_idx in range(1 + inserted_rows, min(rows, 5)):
            r_phys = r_idx - inserted_rows
            if r_phys >= len(table_obj.rows):
                break
            target_cy = (table_obj.rows[r_phys].bbox[1] + table_obj.rows[r_phys].bbox[3]) / 2.0
            if any(
                cell is not None and cell[3] > target_cy
                for cell in table_obj.rows[0].cells
            ):
                header_depth = r_idx + 1
            else:
                break
    else:
        for r in range(1, min(rows, 5)):
            is_hdr = any(
                c < len(raw_table[r]) and raw_table[r][c] is None
                and c < len(raw_table[r-1]) and raw_table[r-1][c] is not None
                for c in range(cols)
            )
            if is_hdr:
                header_depth = r + 1
            else:
                break

    # ── Détection couleur des lignes d'en-tête (Type 2 Antenna House) ─────
    if pdf_type == 2 and rows >= 2:
        hdr_rows = _count_header_rows_by_color(page, table_obj)
        if hdr_rows > 0:
            header_depth = max(header_depth, hdr_rows)

        # Fallback heuristique si la couleur seule ne suffit pas
        if header_depth == 1:
            non_empty_0 = sum(1 for c in raw_table[0] if c is not None and str(c).strip())
            if non_empty_0 < cols * 0.3:
                header_depth = 2
            elif non_empty_0 > 0:
                empty_prefix = 0
                for c in range(min(3, cols)):
                    val_0 = raw_table[0][c] if c < len(raw_table[0]) else ''
                    val_1 = raw_table[1][c] if c < len(raw_table[1]) else ''
                    empty_0 = val_0 is None or str(val_0).strip() == ''
                    filled_1 = val_1 is not None and str(val_1).strip() != ''
                    if empty_0 and filled_1:
                        empty_prefix += 1
                if empty_prefix >= 1:
                    header_depth = 2

            # Détection des en-têtes spanning via répétitions adjacentes dans Row 0
            # Ex: [A, B, C,C,C, D,D,D,D,D, E,E,E,E,E, F] → header_depth=2
            # On compare uniquement la 1ère ligne (avant \n) car les cellules
            # peuvent contenir des sous-étiquettes (ex: "Conditions\nVDD=1.62V").
            if header_depth == 1 and rows >= 2:
                identical = 0
                for c in range(1, min(cols, len(raw_table[0]))):
                    v0 = (str(raw_table[0][c] or "").strip()).split("\n")[0]
                    v1 = (str(raw_table[0][c-1] or "").strip()).split("\n")[0]
                    if v0 and v0 == v1:
                        identical += 1
                if identical >= cols * 0.25:
                    header_depth = 2

    if header_depth > 1:
        warnings.append(f"dynamic_header_depth:{header_depth}")
    # Guard : si header_depth > 2 ET que les lignes restantes < 2,
    # forcer header_depth = 1 (sur-détection header → perte données)
    if header_depth > 2 and (rows - header_depth) < 2:
        header_depth = 1
        warnings.append("header_depth_forced:1")

    # ── 5. Construction des en-têtes finaux ────────────────────────────────────
    final_headers = _build_final_headers(table, cols, header_depth, pdf_type, table_id=table_id)    # ── 6. Lignes de données ───────────────────────────────────────────────────
    final_rows = [
        [_normalize_newlines_in_cell(str(cell or "")) for cell in table[r]]
        for r in range(header_depth, rows)
    ]

    return final_headers, final_rows, warnings



def _build_final_headers(
    table: list[list],
    cols: int,
    header_depth: int,
    pdf_type: int = 1,
    table_id: str = "",
) -> list[str]:
    """
    Construit la liste finale des en-têtes.

    - header_depth == 1 : lecture directe de la ligne 0.
    - header_depth >= 2 : fusion « Parent / Enfant » sur les lignes d'en-tête.

    RÈGLE DE PROPAGATION :
    - None (cellule fusionnée) → réutiliser last_parent
    - '' (cellule vide, pas fusionnée) → pas de parent
    - '(N)' (footnote marker) → ignoré comme parent
    """
    if header_depth == 1:
        return [
            _normalize_newlines_in_cell(str(table[0][c] or "")).strip()
            for c in range(cols)
        ]

    final: list[str] = []
    last_parent: str = ""

    for c in range(cols):
        parts: list[str] = []
        for r in range(header_depth):
            original = table[r][c]
            val = str(original or "").strip()
            if "\n" in val:
                val = val.replace("\n", " ").strip()
            val = _normalize_newlines_in_cell(val).strip()

            if r == 0:
                if original is None:
                    effective = last_parent
                elif val and not re.match(r'^\(\d+\)$', val):
                    last_parent = val
                    effective = val
                else:
                    effective = ""
            else:
                effective = val

            if effective and (not parts or parts[-1] != effective):
                parts.append(effective)

        final.append(" / ".join(parts))

    # ── Post-traitement : refusion headers fragmentés ─────────────────────────
    # Quand une cellule parent multi-lignes est éclatée par pdfplumber, le texte
    # des lignes 2+ fuit dans row1. On détecte ça par :
    #   - header_depth >= 3
    #   - row0[c] = None, row1[c] ≠ None et commence par minuscule ou '('
    #   - 2+ colonnes consécutives avec ce motif
    # On reconstruit le parent complet et on supprime les fragments de row1.
    if header_depth >= 3:
        c = 0
        while c < cols:
            start = None
            # Chercher début de groupe : row0[c] != None (parent) et row0[c+1] == None (fusion)
            if (c + 1 < cols
                and table[0][c] is not None and str(table[0][c]).strip()
                and table[0][c + 1] is None
                and table[1][c + 1] is not None):
                nxt = str(table[1][c + 1]).strip()
                if nxt and (nxt[0].islower() or nxt[0] == '('):
                    start = c
                    end = c + 1
                    while end + 1 < cols:
                        if (table[0][end + 1] is None
                            and table[1][end + 1] is not None
                            and str(table[1][end + 1]).strip()):
                            end += 1
                        else:
                            break

            if start is not None:
                parent_start = str(table[0][start]).strip()
                fragments = []
                for fc in range(start, end + 1):
                    frag = str(table[1][fc]).strip()
                    if frag and frag != parent_start:
                        fragments.append(frag)
                reconstructed = parent_start + " " + " ".join(fragments)

                for fc in range(start, end + 1):
                    child_val = str(table[2][fc]).strip() if header_depth > 2 and fc < len(table[2]) else ""
                    if child_val:
                        final[fc] = reconstructed + " / " + child_val
                    else:
                        final[fc] = reconstructed

                c = end + 1
            else:
                c += 1

    _debug_headers_raw(table_id, table, header_depth, cols, final)
    return final



def extract_table_grid(
    pdf_path: str,
    ref: TableRef,
    family: str,
    pdf_name: str,
    output_base: Path,
    all_refs: list[TableRef] = None,
    pdf_type: int = 1,
    pdf=None,
) -> dict:
    """
    Extrait la grille d'une table identifiée par `ref`.
    Retourne un dict conforme au schéma RawTable (sérialisable en JSON).

    Pipeline interne (dans l'ordre) :
      1. _extract_from_page → grille brute pdfplumber (bordures) ou pdfplumber_text
      2. Filtrage lignes au-dessus de la légende (si bleed)
      3. Si table vide → ré-extraction depuis page suivante (body sur page N+1)
      4. Troncature si bleed (texte de table suivante dans les données)
      5. _expand_spans_and_headers : Fix 2 (headers compressés), Fix 3 (ghost cols),
         Fix 5 (rowspan/colspan géométrique), détection profondeur header
      6. Fix 4 : continuation multi-pages (colonnes, ordre, correction headers)
      7. Fix 7 : insertion colonnes page 1 si continuation en a plus (x0 géométrique)
      8. Fix 6 : propagation descendante cellules vides (rowspan)
      9. Fix 8 : garantie zéro cellule vide (Type 2 uniquement)
     10. Détection has_empty_cells (APRÈS Fix 8 — seul le vrai artefact compte)
     11. Correction glyphes (fix_headers, fix_rows)
     12. Dedup lignes consécutives + suppression lignes totalement vides
     13. Fusion colonnes adjacentes identiques (_merge_identical_adjacent_columns)
     14. Fusion colonnes fragmentées (_merge_fragmented_columns) — SEULEMENT pdfplumber_text
     15. Évaluation qualité (confidence, empty_ratio)
     16. Image de debug si nécessaire
    """
    result = {
        "table_id":              ref.table_id,
        "caption":               ref.caption,
        "pdf_name":              pdf_name,
        "family":                family,
        "url":       f"https://www.st.com/resource/en/datasheet/{pdf_name}.pdf",
        "url_table": f"https://www.st.com/resource/en/datasheet/{pdf_name}.pdf#page={ref.page}",
        "section":               ref.section,
        "page":                  ref.page,
        "merged_pages":          [ref.page],
        "headers":               [],
        "rows":                  [],
        "extraction_method":     "pdfplumber",
        "extraction_confidence": "failed",
        "empty_cell_ratio":      1.0,
        "col_count":             0,
        "warnings":              [],
    }

    _own_pdf = pdf is None
    if _own_pdf:
        pdf = pdfplumber.open(pdf_path)
    try:
        if ref.page < 1 or ref.page > len(pdf.pages):
            result["warnings"].append(f"page_out_of_range:{ref.page}")
            return result

        page = pdf.pages[ref.page - 1]  # pdfplumber est 0-indexé

        # ── Fix 1 : préparer la map des textes rotatifs ────────────────────────
        rotated_map = _get_rotated_text_map(page)

        # ── Extraire la grille ─────────────────────────────────────────────────
        raw_table, table_obj, method, bbox = _extract_from_page(page, ref, rotated_map, pdf_type)

        # ── Raffinement section par position Y ────────────────────────────────
        # Utilise le cache Y-position construit par _assign_sections pour
        # associer la table à la section précise (même page avec plusieurs sections)
        caption_y = _find_caption_y(page, ref.caption)
        if caption_y is not None:
            y_section = get_section_at(pdf_path, ref.page, caption_y)
            if y_section:
                result["section"] = y_section

        if raw_table is None:
            result["warnings"].append("no_table_found_on_page")
            logger.warning(f"{ref.table_id}: no table found on page {ref.page}")
            crop_path = _save_table_crop(page, bbox, output_base, ref.table_id, family, pdf_name)
            if crop_path:
                result.setdefault("debug", {})["crop_path"] = crop_path
            if DEBUG_EMPTY_ROWS:
                _settings = PDFPLUMBER_TABLE_SETTINGS_TYPE2 if pdf_type == 2 else PDFPLUMBER_TABLE_SETTINGS
                _save_empty_rows_debug(
                    page=page, ref=ref, pdf_type=pdf_type,
                    extraction_method=method, raw_table=None, bbox=None,
                    settings=_settings, pdf_path=pdf_path,
                    output_base=output_base, family=family, pdf_name=pdf_name,
                    warnings_list=result["warnings"],
                )
            return result

        result["extraction_method"] = method

        # ── Sauvegarder le nb de colonnes original (avant filtrage caption_y) ─
        orig_col_count = max(len(r) for r in raw_table) if raw_table else 0

        # ── Non-tables : Ordering information (pas une grille) ──────────────
        # Ces entrées de la TOC sont des listes textuelles, pas des tableaux.
        # On garde l'entrée mais avec rows vides + capture image.
        # Pour Type 1, on utilise extract_ordering_info pour parser le texte.
        if "ordering information" in ref.caption.lower():
            result["headers"] = []
            result["rows"] = []
            result["empty_cell_ratio"] = 1.0
            result["col_count"] = 0
            if pdf_type == 1:
                page_text = page.extract_text() or ""
                oi = extract_ordering_info(
                    page_text=page_text,
                    doc_id=pdf_name,
                    table_id=int(ref.table_id.split("_")[1]),
                    page=ref.page,
                )
                if oi["structured_json"].get("type") == "ordering_information":
                    result["structured_json"] = oi["structured_json"]

                    result["extraction_confidence"] = "high"
                    result["empty_cell_ratio"] = 0.0
                    result["warnings"] = ["non_table:ordering_information"]
                else:
                    result["extraction_confidence"] = "low"
                    result["warnings"] = ["non_table_captured:ordering_information"]
            else:
                result["extraction_confidence"] = "low"
                result["warnings"] = ["non_table_captured:ordering_information"]
            crop_path = _save_table_crop(page, bbox, output_base, ref.table_id, family, pdf_name)
            if crop_path:
                result.setdefault("debug", {})["crop_path"] = crop_path
            logger.info(f"{ref.table_id}: non-table ordering info, captured")
            return result

        # ── Heuristiques de tracking ─────────────────────────────────────────
        heuristics = {}

        # ── Repérer la position y de la légende ─────────────────────────────
        caption_y = None
        if raw_table:
            caption_y = _find_caption_y(page, ref.caption)

        # ── [Fix Général] Filtrer les lignes au-dessus de la légende ─────────
        # La stratégie text merge les tables adjacentes (ex: Table 8 I2C + Table 9 USART).
        # On utilise les coordonnées y du finder pour ne garder que les lignes
        # sous la légende. table_obj → None car ses coordonnées ne correspondent
        # plus au raw_table filtré (l'expansion spatiale est moins critique pour
        # les tables text-strategy sans bordures).
        if raw_table and table_obj and hasattr(table_obj, 'rows'):
            if caption_y is not None and len(table_obj.rows) == len(raw_table):
                ys = [r.bbox[1] for r in table_obj.rows]
                keep = [i for i, y in enumerate(ys) if y >= caption_y - 5]
                if keep and len(keep) < len(raw_table):
                    raw_table = [raw_table[i] for i in keep]
                    table_obj = None
                    logger.info(f"{ref.table_id}: filtered {len(keep)}/{len(ys)} rows below caption (y>{caption_y:.0f})")
                elif not keep:
                    # Toutes les lignes sont au-dessus de la légende → ré-extraire
                    # sous la légende sur la MÊME page (pas la page suivante).
                    # Cas typique : 2 tables par page, pdfplumber a fusionné les deux.
                    re_extracted = False
                    if caption_y is not None and len(page.bbox) == 4:
                        crop = (page.bbox[0], caption_y - 5, page.bbox[2], page.bbox[3])
                        if crop[1] < crop[3]:
                            p_cropped = page.within_bbox(crop)
                            r_cropped = _get_rotated_text_map(p_cropped)
                            nxt_raw, nxt_obj, nxt_method, nxt_bbox = _extract_from_page(
                                p_cropped, ref, r_cropped, pdf_type, ""
                            )
                            if nxt_raw and len(nxt_raw) >= 2:
                                raw_table, table_obj, method, bbox = nxt_raw, nxt_obj, nxt_method, nxt_bbox
                                logger.info(f"{ref.table_id}: re-extracted below caption on same page "
                                            f"({len(raw_table)} rows, {nxt_method})")
                                re_extracted = True
                    if not re_extracted:
                        raw_table = []
                        table_obj = None
                        logger.info(f"{ref.table_id}: all {len(ys)} rows above caption, treated as empty")

        # ── Détecter si la légende est en bas de page (→ table page suivante) ──
        # Si la légende est dans les 25% inférieurs de la page, la table body
        # commence probablement en haut de la page suivante. On forcera la
        # vérification même si qq lignes résiduelles (footer, etc.) sont présentes.
        caption_near_bottom = False
        if caption_y is not None:
            if caption_y > page.height * 0.75:
                caption_near_bottom = True
                heuristics["caption_near_bottom"] = True
                heuristics["caption_y_ratio"] = round(caption_y / page.height, 3)

        # ── [Fix] Si vide ou légende en bas → extraire depuis la page suivante ──
        # Quand la table body est sur la page d'après (titre page N, corps page N+1),
        # le filtrage ne laisse que 0-1 lignes. On vérifie la page immédiatement
        # suivante uniquement (offset=1) — chercher plus loin risque de capturer
        # une table complètement différente.
        # Le mot-clé de légende donne un bonus modéré (+20) pour départager,
        # sans dominer la qualité de base.
        # Seuil n_non_empty < 3 : les cas à 3-4 lignes sont des faux positifs
        # (légende mi-page avec extraction partielle). 0-2 = vraie page suivante.
        start_page = ref.page
        n_non_empty = sum(1 for row in raw_table if any(str(c).strip() for c in row)) if raw_table else 0
        caption_near_bottom = (
            caption_y is not None
            and caption_y > page.height * 0.85
        )
        if raw_table and caption_near_bottom and n_non_empty > 0:
            total_cells = len(raw_table) * max(len(r) for r in raw_table)
            filled_cells = sum(1 for row in raw_table for c in row if str(c).strip()) if total_cells else 0
            sparse_below = total_cells == 0 or filled_cells / total_cells < 0.15
        else:
            sparse_below = False
        should_try_next = (n_non_empty == 0) or (caption_near_bottom and sparse_below)
        saved_pre_body = []
        if should_try_next and ref.page < len(pdf.pages):
                caption_keyword = ""
                if ref.caption:
                    parts = ref.caption.split(".")
                    if len(parts) >= 2:
                        cw = parts[-1].strip().split()
                        if cw:
                            caption_keyword = cw[0].lower()
                pg_idx = ref.page  # page suivante (déjà +1 ci-dessous)
                if pg_idx < len(pdf.pages):
                    p = pdf.pages[pg_idx]
                    # Le body du tableau commence en haut de la page suivante.
                    # Le bas de page (footer, autre contenu) est exclu pour
                    # éviter les colonnes parasites (14+ au lieu de 7).
                    crop = (p.bbox[0], p.bbox[1], p.bbox[2], p.height * 0.7)
                    p_cropped = p.within_bbox(crop)
                    r = _get_rotated_text_map(p_cropped)
                    nxt_raw, nxt_obj, nxt_method, nxt_bbox = _extract_from_page(p_cropped, ref, r, pdf_type, "")
                    if nxt_raw:
                        nq = _table_quality(nxt_raw)
                        keyword_bonus = 0
                        if caption_keyword:
                            for row in nxt_raw:
                                for c in row:
                                    if c and caption_keyword in c.lower():
                                        keyword_bonus = 20
                                        break
                                if keyword_bonus:
                                    break
                        nq_boosted = nq + keyword_bonus
                        if nq_boosted >= 10.0:
                            # ── Garde-fou : page suivante contient une AUTRE table ? ──
                            # Vérifier dans les lignes extraites (nxt_raw) puis dans le
                            # texte complet de la page (capte les captions hors crop).
                            nxt_text = " ".join(str(c) for row in nxt_raw for c in row).lower()
                            cur_num = int(ref.table_id.split("_")[1])
                            other_tables = re.findall(r"table\s+(\d+)", nxt_text)
                            other_nums = [int(n) for n in other_tables if n.isdigit()]
                            rejected = False
                            if other_nums and cur_num not in other_nums:
                                rejected = True
                            else:
                                # [Fix D] Scanner aussi le texte complet de la page
                                full_text = p.extract_text().lower()
                                full_nums = re.findall(r"table\s+(\d+)", full_text)
                                full_nums_i = [int(n) for n in full_nums if n.isdigit()]
                                if full_nums_i and cur_num not in full_nums_i:
                                    # Vérifier que la table différente est BIEN DANS la zone crop
                                    # (sinon c'est une table plus bas sur la page, pas un problème)
                                    crop_bottom = p.height * 0.7
                                    for w in p.extract_words():
                                        wt = w["text"].lower()
                                        if re.match(r"table\s+\d+", wt):
                                            nums_in = re.findall(r"\d+", wt)
                                            if nums_in and int(nums_in[0]) in full_nums_i and int(nums_in[0]) != cur_num:
                                                if w["top"] < crop_bottom:
                                                    rejected = True
                                                    break
                            if rejected:
                                logger.info(f"{ref.table_id}: body_on_next_page rejected "
                                            f"(page {pg_idx + 1} has a different table)")
                                nq_boosted = -1
                            else:
                                nxt_cols = max(len(r) for r in nxt_raw) if nxt_raw else 0
                                if orig_col_count > 0 and nxt_cols > orig_col_count * 2 + 5:
                                    logger.info(f"{ref.table_id}: body_on_next_page rejected "
                                                f"(cols {orig_col_count}→{nxt_cols})")
                                    nq_boosted = -1
                                else:
                                    # Sauvegarde des lignes de la page N avant remplacement
                                    saved_pre_body = raw_table
                                    raw_table, table_obj, method, bbox = nxt_raw, nxt_obj, nxt_method, nxt_bbox
                                    page = pdf.pages[pg_idx]
                                    start_page = ref.page + 1
                                    result["extraction_method"] = method
                                    heuristics["body_on_next_page"] = True
                                    heuristics["next_page_q_boosted"] = nq_boosted
                                    logger.info(f"{ref.table_id}: body on page {start_page}, re-extracted ({len(raw_table)} rows, {nxt_method}, q={nq_boosted:.0f})")

        # ── Capturer le nombre de colonnes extraites ─────────────────────────
        if raw_table:
            heuristics["cols_extracted"] = max(len(r) for r in raw_table)

        # ── [Fix] Suppression des lignes de titre débordant dans la grille ──
        # Le titre "Table N. ... (continued)" peut apparaître n'importe où
        # dans raw_table (pas seulement ligne 0) quand body_on_next_page crop
        # 70 % ou que pdfplumber_text éclate la page. On scanne TOUTES les
        # lignes et on supprime toute ligne dont UNE cellule matche
        # "Table N." avec le bon numéro de table.
        if raw_table:
            cur_id = int(re.findall(r'\d+', str(ref.table_id))[0])
            before = len(raw_table)
            cleaned = []
            for ri, row in enumerate(raw_table):
                is_bleed = False
                # Vérification cellule par cellule
                for ci, cell in enumerate(row):
                    cell_s = str(cell).strip()
                    m = re.match(r"(?:Table|Tableau)\s+(\d+)[\.:]", cell_s, re.IGNORECASE)
                    if m and int(m.group(1)) == cur_id:
                        is_bleed = True
                        break
                    # Cas fragmenté : "Table 11" / "8." / "LQFP176..." sur 3 cellules
                    if re.match(r"(?:Table|Tableau)\s+\d+\s*$", cell_s, re.IGNORECASE):
                        combined = cell_s
                        for k in range(1, 4):
                            if ci + k >= len(row):
                                break
                            combined += str(row[ci + k]).strip()
                            m2 = re.match(r"(?:Table|Tableau)\s+(\d+)[\.:]", combined, re.IGNORECASE)
                            if m2:
                                if int(m2.group(1)) == cur_id:
                                    is_bleed = True
                                break
                        if is_bleed:
                            break
                if not is_bleed:
                    cleaned.append(row)
            if len(cleaned) < before:
                raw_table = cleaned
                logger.info(f"{ref.table_id}: removed {before - len(cleaned)} caption bleed rows ({len(raw_table)} rows remaining)")

        # ── [Fix] Troncature des tables suivantes (bleed) ────────────────────
        # On détecte les marqueurs de transition (ex: "Table 51." dans les lignes
        # de la Table 50) et on coupe raw_table à la première ligne concernée.
        # Applicable à toutes les méthodes d'extraction.
        if raw_table:
            rows_before = len(raw_table)
            raw_table = _truncate_at_next_table(raw_table, ref.table_id)
            if len(raw_table) < rows_before:
                heuristics["truncated_rows"] = rows_before - len(raw_table)

        # ── [Fix 4b] Suppression lignes de bleed page header/footer ────────
        # pdfplumber_text éclate les titres de section (ex: "Electrical
        # characteristics") en 14+ colonnes. On supprime ces lignes avant
        # _expand_spans_and_headers pour que l'expansion + _merge_fragmented_columns
        # (étape 14) fonctionnent sur les vrais headers de la table.
        if method == "pdfplumber_text" and raw_table:
            rows_before = len(raw_table)
            raw_table = _remove_bleed_rows(raw_table, method)
            if len(raw_table) < rows_before:
                heuristics["bleed_rows_removed"] = rows_before - len(raw_table)

        # ── Vérification finale : table vide ──────────────────────────────────
        if not raw_table:
            result["warnings"].append("empty_raw_table")
            crop_path = _save_table_crop(page, bbox, output_base, ref.table_id, family, pdf_name)
            if crop_path:
                result.setdefault("debug", {})["crop_path"] = crop_path
            return result

        # ── Fix 2 & 5 : Headers structurels et Propagation globale ─────────────
        headers, rows_raw, span_warnings = _expand_spans_and_headers(raw_table, table_obj, page, pdf_type, table_id=ref.table_id)
        result["warnings"].extend(span_warnings)

        # ── [Fix] Fusion des lignes de la page N avec la page N+1 ─────────────
        # Quand body_on_next_page remplace raw_table, les lignes de la page
        # originale (page N) sont perdues. On les réinsère ici en utilisant
        # le même header_depth que la page N+1 (même structure de table).
        if saved_pre_body:
            hd = len(raw_table) - len(rows_raw)
            extra = saved_pre_body[hd:]
            nxt_cols = max(len(r) for r in rows_raw) if rows_raw else 0
            if extra:
                extra_cols = max(len(r) for r in extra) if extra else 0
                if extra_cols == 0 or nxt_cols == 0 or abs(extra_cols - nxt_cols) / max(nxt_cols, 1) > 0.5:
                    logger.debug(f"body_on_next_page: skip merge ({extra_cols} cols != {nxt_cols} cols)")
                else:
                    rows_raw = extra + rows_raw
                    logger.info(f"body_on_next_page: merged {len(extra)} rows from page {ref.page}")

        # ── Fix 4 : Continuation multi-pages ──────────────────────────────────
        merged_pages = [start_page]
        n_insert = 0
        if all_refs is not None:
            header_depth = len(raw_table) - len(rows_raw)
            first_cell_text = str(raw_table[0][0] or "").strip() if raw_table else ""
            base_x0s = _get_col_x0s(table_obj) if table_obj else []
            base_bottom = table_obj.bbox[3] if table_obj else None
            c_pages, c_rows, target_cols, cont_x0s_list, b7_triggered = find_continuations(
                pdf,
                start_page,
                len(headers),
                all_refs,
                ref.table_id,
                header_depth,
                first_cell_text,
                pdf_type=pdf_type,
                base_col_x0s=base_x0s,
                base_header=headers,
                base_bbox_bottom=base_bottom,
            )
            if c_pages and len(c_pages) > 1:
                merged_pages = c_pages

                # Fix 7 : élargir la page 1 si la continuation a scindé une colonne
                # Ex: page 1 a ["Name"] mais page N a ["Name", "Name sub"]
                # L'insertion se fait à la position dictée par la géométrie x0 :
                # les x0 présents dans la continuation mais absents de la page 1
                # (tolérance 5pt) reçoivent une colonne vide.
                # Le nouveau header est copié depuis le voisin de gauche.
                n_insert = target_cols - len(headers)
                if n_insert > 0:
                    p1_x0s = _get_col_x0s(table_obj) if table_obj else []
                    merged_cont_x0s = sorted(set(
                        round(x, 1) for lst in cont_x0s_list for x in lst
                    ))
                    surplus_x0s = []
                    for cx in merged_cont_x0s:
                        if not any(abs(cx - px) < 5 for px in p1_x0s):
                            surplus_x0s.append(cx)

                    if not p1_x0s:
                        insert_positions = list(range(len(headers), len(headers) + n_insert))
                    else:
                        insert_positions = []
                        for sx in surplus_x0s:
                            pos = sum(1 for px in p1_x0s if px < sx)
                            insert_positions.append(pos)

                    insert_positions = sorted(set(insert_positions))[:n_insert]

                    # Insertion droite→gauche pour préserver les indices
                    # La valeur insérée est copiée depuis le voisin gauche
                    # (même logique que le header). Si pos=0 (pas de voisin
                    # gauche), copier la valeur de l'ancienne colonne 0
                    # (avant insertion de Fix 7).
                    for pos in sorted(insert_positions, reverse=True):
                        header_val = headers[pos - 1] if pos > 0 else headers[0]
                        headers = headers[:pos] + [header_val] + headers[pos:]
                        for r in range(len(rows_raw)):
                            if pos > 0 and pos - 1 < len(rows_raw[r]):
                                neighbor_val = rows_raw[r][pos - 1]
                            elif pos == 0 and rows_raw[r]:
                                neighbor_val = rows_raw[r][0]
                            else:
                                neighbor_val = ""
                            rows_raw[r] = rows_raw[r][:pos] + [neighbor_val] + rows_raw[r][pos:]

                rows_raw.extend([[_cell_str(c) for c in row] for row in c_rows])
                result["warnings"].append(f"multi_page_merged:{len(merged_pages)}")

                # ── Heuristics : debug continuation ──────────────────────────
                dup_positions = [i for i in range(1, len(headers)) if headers[i] == headers[i-1]]
                heuristics["continuation"] = {
                    "base_cols": len(headers),
                    "cont_cols_raw": len(c_rows[0]) if c_rows else 0,
                    "target_cols": target_cols,
                    "base_has_dup_headers": bool(dup_positions),
                    "dup_positions": dup_positions,
                    "b7_detected": b7_triggered,
                    "b7_expanded": b7_triggered and len(headers) > (len(c_rows[0]) if c_rows else 0),
                    "cont_pages": len(merged_pages),
                }

                # Tronquer les lignes de la table suivante dans les données de continuation
                # (ex: page 88 contient fin table 50 + début table 51)
                before = len(rows_raw)
                rows_raw = _truncate_at_next_table(rows_raw, ref.table_id)
                if len(rows_raw) < before:
                    heuristics["truncated_rows"] = heuristics.get("truncated_rows", 0) + before - len(rows_raw)

                # Fix 8 : remplir horizontalement les lignes de continuation
                # (les cellules fusionnées horizontalement ne sont pas propagées
                # dans les pages de continuation, contrairement à la page 1).
                col_groups = _build_col_groups(headers)
                _fill_horizontal(rows_raw, col_groups, headers=headers)

                # Propager col 0 vers le bas pour les lignes de continuation.
                # Les rowspan verticaux de col 0 (ex: "Peripherals") arrivent
                # avec "" sur la page de continuation. Sans cette propagation,
                # _fill_identity_spans regrouperait ces lignes sous "" au lieu
                # du bon identifiant.
                if col_groups:
                    id_group = col_groups[0]
                    id_cols: list[int] = []
                    for cc in range(len(col_groups)):
                        if col_groups[cc] == id_group:
                            id_cols.append(cc)
                        else:
                            break
                    for r in range(1, len(rows_raw)):
                        identity_changed = False
                        for chk_c in id_cols:
                            if chk_c >= len(rows_raw[r]) or chk_c >= len(rows_raw[r-1]):
                                break
                            val_r = (rows_raw[r][chk_c] or "").strip()
                            val_prev = (rows_raw[r-1][chk_c] or "").strip()
                            if val_r and val_r != val_prev:
                                identity_changed = True
                                break
                        if not identity_changed and not rows_raw[r][0] and rows_raw[r-1][0]:
                            rows_raw[r][0] = rows_raw[r-1][0]

                _fill_identity_spans(rows_raw, col_groups, headers=headers)
        headers, rows_raw = _merge_identical_adjacent_columns(headers, rows_raw, skip_first_n=n_insert)

        # ── Fix 6 : Propagation descendante des cellules vides ─────────────
        # Les cellules fusionnées verticalement (rowspan) apparaissent comme ""
        # dans les lignes de continuation et parfois même sur la 1ère page.
        # Règle : si une cellule est "", on copie la valeur de la ligne du dessus.
        # Exception : si la 1ère colonne change vers une valeur jamais vue
        # → nouveau groupe → ne pas propager (ex: table_6 I/O → Notes évite
        # la propagation erronée entre groupes différents).
        # Note : inactif pour pdfplumber_text (les cellules vides sont des
        # trous structurels, pas des rowspan — Fix 6 les dupliquerait).
        if rows_raw and result["extraction_method"] != "pdfplumber_text":
            col_groups = _build_col_groups(headers)
            n_cols = len(headers)
            for r in range(1, len(rows_raw)):
                first_changed = False
                seen_before = False
                cur_first = (rows_raw[r][0] or "").strip() if len(rows_raw[r]) > 0 else ""
                prev_first = (rows_raw[r-1][0] or "").strip() if len(rows_raw[r-1]) > 0 else ""
                first_changed = cur_first != prev_first
                if first_changed:
                    seen_before = any(
                        (rows_raw[r2][0] or "").strip() == cur_first
                        for r2 in range(r)
                    )
                # Vérifier si l'identité hiérarchique a changé :
                # comparer toutes les colonnes partageant le même col_group que
                # col 0 (identité = Peripheral/Type/Category, pas les packages).
                # Si une colonne d'identité a une valeur non-vide différente
                # entre r et r-1, c'est une nouvelle ligne, pas un rowspan.
                identity_changed = False
                if col_groups:
                    id_group = col_groups[0]
                    for chk_c in range(min(n_cols, len(rows_raw[r]), len(rows_raw[r-1]))):
                        if col_groups[chk_c] != id_group:
                            break
                        val_r = (rows_raw[r][chk_c] or "").strip()
                        val_prev = (rows_raw[r-1][chk_c] or "").strip()
                        if val_r and val_r != val_prev:
                            identity_changed = True
                            break

                for c in range(min(n_cols, len(rows_raw[r]))):
                    if rows_raw[r][c] == "" and c < len(rows_raw[r-1]) and rows_raw[r-1][c] != "":
                        if identity_changed:
                            continue
                        if cur_first and first_changed and not seen_before:
                            continue
                        if _is_isolated_empty(rows_raw[r], c):
                            continue
                        rows_raw[r][c] = rows_raw[r-1][c]

        # ── Fix 8 : garantie zéro cellule vide ──────────────────────────
        # Remplit horizontalement puis verticalement toute cellule résiduelle.
        # Même les cellules vraiment vides dans le PDF reçoivent le dernier
        # voisin connu (règle "remete le père").
        col_groups = _build_col_groups(headers)
        _ensure_no_empty_cells(rows_raw, col_groups, headers=headers)

        # ── Correction des glyphes ─────────────────────────────────────────────
        headers    = fix_headers(headers)
        for i in range(len(headers)):
            if _is_likely_reversed(headers[i]):
                if ' ' in headers[i]:
                    headers[i] = ' '.join(p[::-1].strip() for p in headers[i].split(' '))
                else:
                    headers[i] = headers[i][::-1].strip()
        rows_fixed = fix_rows(rows_raw)

        rows_fixed = _deduplicate_rows(rows_fixed)

        # ── [Fix] Suppression des lignes totalement vides ───────────────────
        # Les artefacts pdfplumber_text créent parfois des lignes où toutes
        # les cellules sont "" (ex: ligne séparatrice header/body mal capturée).
        rows_fixed = [row for row in rows_fixed if any(c for c in row)]

        # ── [Fix] Correction texte inversé — row0 + col0 ─
        if rows_fixed:
            rows_fixed[0] = _fix_reversed_cells(
                [rows_fixed[0]], table_id=f"{ref.table_id}_row0"
            )[0]
        for row in rows_fixed[1:]:
            if row and isinstance(row[0], str) and len(row[0]) >= 5:
                clean = row[0].replace("\n", "")
                if _is_likely_reversed(clean):
                    row[0] = clean[::-1].strip()

        # ── [Fix] Suppression des lignes header résiduelles dans les données ─
        # Cas rare : _expand_spans_and_headers peut laisser des lignes header
        # dans rows_fixed (ex: footnote "(1)" ou header dupliqué après merge).
        while rows_fixed and headers:
            if rows_fixed[0] == headers:
                rows_fixed.pop(0)
            elif len(rows_fixed[0]) == len(headers) and all(
                rows_fixed[0][c] == headers[c] for c in range(1, len(headers))
            ):
                rows_fixed.pop(0)
            elif rows_fixed[0] and rows_fixed[0][0].strip().lower() in {'symbol', 'parameter'}:
                rows_fixed.pop(0)
            else:
                break
        # Scan aussi les lignes au milieu (ex: en-tête secondaire après merge de sous-tables)
        rows_fixed = [r for r in rows_fixed if not (r and r[0].strip().lower() in {'symbol', 'parameter'})]
        # Supprime les résidus de sous-en-tête (même 1ère cellule que la ligne précédente,
        # une seule cellule différente contenant 'Max'/'Typ'/'Min'/'Unit')
        cleaned = []
        for r in rows_fixed:
            if cleaned and len(r) == len(cleaned[-1]) and r[0] == cleaned[-1][0]:
                diffs = [c for c in range(len(r)) if r[c] != cleaned[-1][c]]
                if len(diffs) == 1 and r[diffs[0]].strip().lower() in {'max', 'typ', 'min', 'unit'}:
                    continue
            cleaned.append(r)
        rows_fixed = cleaned
        headers, rows_fixed = _merge_identical_adjacent_columns(headers, rows_fixed, skip_first_n=n_insert)

        # ── Phase 3 : Détection spanning perdu (continuation) ────────────────
        # Vérifie si le spanning d'en-tête (ex: "Peripherals" x2) a été perdu
        # pendant l'extraction ou le merge continuation. Si détecté, loggue
        # les conditions dans heuristics pour debug.
        cont_info = heuristics.get("continuation", {})
        if cont_info.get("b7_detected"):
            has_dup = len(headers) >= 2 and headers[0] == headers[1]
            if not has_dup:
                # Le spanning a été perdu — checker par les données
                category_repeats = 0
                for r in range(1, len(rows_fixed)):
                    if (rows_fixed[r] and rows_fixed[r-1]
                        and len(rows_fixed[r]) > 0 and len(rows_fixed[r-1]) > 0
                        and rows_fixed[r][0] == rows_fixed[r-1][0]):
                        category_repeats += 1
                heuristics["spanning_check"] = {
                    "header_has_dup": has_dup,
                    "category_repeats": category_repeats,
                    "likely_missing_span": category_repeats > 0,
                }

        # ── [Fix] Fusion des colonnes fragmentées (pdfplumber_text) ─────────
        # S'applique à TOUS les pdfplumber_text (Type 1 et Type 2) car
        # l'extraction sans bordures fragmente toujours les mots, quel que
        # soit le type de PDF. Les 4 heuristiques (H1-H4) protègent les
        # vraies colonnes ("Min", "Typ", "Max", "Unit").
        if result["extraction_method"] == "pdfplumber_text":
            cols_before = len(headers)
            headers, rows_fixed, merged = _merge_fragmented_columns(headers, rows_fixed)
            if merged > 0:
                heuristics["columns_initial"] = cols_before
                heuristics["columns_merged"] = merged
                heuristics["columns_final"] = len(headers)

        # ── [Fix] Fusion header vide → droite (pdfplumber_text) ───────────
        # S'applique à TOUS les pdfplumber_text : les colonnes-pont à
        # header vide apparaissent dans tous les types de PDF quand
        # l'extraction texte est utilisée (pas de bordures pour délimiter).
        if result["extraction_method"] == "pdfplumber_text":
            cols_before_h = len(headers)
            headers, rows_fixed, merged_h = _merge_empty_header_rightward(headers, rows_fixed)
            if merged_h > 0:
                heuristics["columns_initial"] = heuristics.get("columns_initial", cols_before_h)
                heuristics["columns_merged"] = heuristics.get("columns_merged", 0) + merged_h
                heuristics["columns_final"] = len(headers)

        # ── [Fix] Suppression des footnotes trailing (pdfplumber_text) ──────
        # Les notes de bas de tableau apparaissent en fin de données avec
        # une 1ère cellule au format "N." (ex: "1.", "2.", "6.").
        # Ne supprime qu'un bloc contigu à la fin.
        if result["extraction_method"] == "pdfplumber_text" and rows_fixed:
            rows_before_fn = len(rows_fixed)
            rows_fixed, fn_removed = _remove_trailing_footnotes(rows_fixed)
            if fn_removed > 0:
                heuristics["footnote_rows_removed"] = fn_removed

        # ── [Fix] Suppression des lignes de fuite post-merge ──────────────
        # Après fusion des colonnes, les références à d'autres tables
        # deviennent visibles (ex: "Table26", "Table 23:"). On refait
        # une passe de nettoyage pour les lignes résiduelles qui :
        # 1. Référencent une autre table (même sans espace/point)
        # 2. Commencent par une minuscule (continuation parasite)
        if result["extraction_method"] == "pdfplumber_text" and rows_fixed:
            rows_before_bleed = len(rows_fixed)
            rows_fixed, bleed_removed = _remove_bleed_rows_bottom(rows_fixed, ref.table_id)
            if bleed_removed > 0:
                heuristics["post_merge_bleed_removed"] = bleed_removed

        # ── [Fix] Suppression des lignes footnote (N) résiduelles ──────────
        # Après les merges, certaines notes de bas de tableau comme "(1)"
        # peuvent rester sous forme de ligne complète (1ère cellule = "(1)").
        # _remove_trailing_footnotes les rate car elles utilisent (N) au lieu
        # de N., et _remove_bleed_rows_bottom les rate car une ligne de données
        # réelle (ex: "I DD(BOR)") bloque le scan bottom-up avant d'atteindre
        # la ligne footnote (1). Ce filtre parcourt TOUTES les lignes (pas
        # seulement celles de fin) et supprime toute ligne dont la 1ère cellule
        # est exactement (N) — format typique des notes de bas de datasheet.
        if result["extraction_method"] == "pdfplumber_text" and rows_fixed:
            before = len(rows_fixed)
            rows_fixed = [r for r in rows_fixed
                          if not (re.match(r'^\(\d+\)$', str(r[0]).strip())
                                  and all(not str(c).strip() for c in r[1:]))]
            fn_removed = before - len(rows_fixed)
            if fn_removed > 0:
                heuristics["footnote_paren_removed"] = fn_removed
                logger.info(f"Removed {fn_removed} footnote (N) rows")

        # ── [Fix] Validateur post-merge anti-destruction ────────────────────
        # Si après tous les merges, un header contient "Table" + un chiffre,
        # c'est que le merge a collé la légende dans une cellule réelle.
        # evaluate_table voit un ratio vide=0.0 et marque "high" → faux positif.
        # On force low + warning pour signaler la corruption.
        corrupted = False
        if result["extraction_method"] == "pdfplumber_text":
            for h in headers:
                if re.search(r"Table\s+\d+", str(h), re.IGNORECASE):
                    corrupted = True
                    break
            if not corrupted:
                for row in rows_fixed[:5]:
                    for c in row:
                        if re.search(r"Table\s+\d+", str(c), re.IGNORECASE):
                            corrupted = True
                            break
                    if corrupted:
                        break

        # ── Évaluation qualité ─────────────────────────────────────────────────
        confidence, empty_ratio, _, warnings_eval = evaluate_table(headers, rows_fixed)
        if corrupted:
            warnings_eval.append("post_merge_corrupted")
            confidence = "low"
        result["warnings"].extend(warnings_eval)

        # ── Marquage des tables avec cellules vides ────────────────────────────
        # Détecte les vrais vides dans la sortie FINALE (rows_fixed) après
        # tous les Fix. Les cellules vides temporaires des colonnes fragmentées
        # (merge, header leak, propagation) ne comptent pas.
        has_empty_final = bool(rows_fixed) and any(
            not cell or not str(cell).strip()
            for row in rows_fixed
            for cell in row
        )
        if has_empty_final:
            result["warnings"].append("has_empty_cells")
            result["has_empty_cells"] = True

        # ── Expansion Device summary : chaque part number sur sa propre ligne ──
        if "device summary" in ref.caption.lower():
            new_rows = []
            for row in rows_fixed:
                if len(row) >= 2:
                    ref_val = row[0]
                    parts = [p.strip() for p in re.split(r'[,;]\s*|\n+', row[1]) if p.strip()]
                    for p in parts:
                        new_row = list(row)
                        new_row[1] = p
                        new_rows.append(new_row)
                else:
                    new_rows.append(row)
            rows_fixed = new_rows

        # ── Suppression des lignes de texte de section parasites ──
        rows_fixed, sec_removed = _remove_section_bleed_rows(rows_fixed, ref.table_id)

        # ── Nettoyage des lignes d'en-tête résiduelles et split inter-tables ──
        # Certaines pages ont des sous-tableaux fusionnés avec leur propre
        # ligne d'en-tête (ex: table_40). Si d'autres tables partagent la même
        # page, la première ligne d'en-tête résiduelle marque la frontière entre
        # deux tables : on garde les lignes AVANT (table courante) et on ignore
        # les lignes APRÈS (qui appartiennent à la table suivante).
        # Sinon on supprime simplement toutes les lignes d'en-tête.
        _HEADER_KW = {"symbol", "parameter", "conditions", "condition", "min", "max", "typ", "unit", "value", "speed", "features"}
        other_tables = [r for r in all_refs if r.page == ref.page and r.table_id != ref.table_id] if all_refs else []
        split_idx = None
        if other_tables:
            for i, row in enumerate(rows_fixed):
                if len(row) >= 3:
                    all_header = True
                    for cell in row:
                        txt = str(cell).strip().lower()
                        if not txt or not any(kw in txt for kw in _HEADER_KW):
                            all_header = False
                            break
                    if all_header:
                        split_idx = i
                        break
        if split_idx is not None:
            if split_idx == 0:
                cleaned: list[list[str]] = []
                for row in rows_fixed:
                    if len(row) >= 3:
                        all_header = True
                        for cell in row:
                            txt = str(cell).strip().lower()
                            if not txt or not any(kw in txt for kw in _HEADER_KW):
                                all_header = False
                                break
                        if all_header:
                            continue
                    cleaned.append(row)
                rows_fixed = cleaned
            else:
                rows_fixed = rows_fixed[:split_idx]
        else:
            cleaned: list[list[str]] = []
            for row in rows_fixed:
                if len(row) >= 3:
                    all_header = True
                    for cell in row:
                        txt = str(cell).strip().lower()
                        if not txt or not any(kw in txt for kw in _HEADER_KW):
                            all_header = False
                            break
                    if all_header:
                        continue
                cleaned.append(row)
            rows_fixed = cleaned

        # ── [Fix] Padding de toutes les lignes au nombre de colonnes des headers ──
        # Certaines lignes arrivent avec moins d'items que len(headers) à cause des
        # cellules fusionnées (colspan) dans le PDF. Ce padding normalise l'output
        # pour garantir un nombre de colonnes cohérent.
        hc = len(headers)
        for i, row in enumerate(rows_fixed):
            while len(row) < hc:
                row.append("")

        # ── [Fix] Remplacer codes commande par noms STM32 dans headers ──────
        # Certains datasheets mettent des codes commande (Q3H0X546N) au lieu
        # des noms de devices STM32 dans la ligne d'en-tête. On pioche le nom
        # depuis la légende de la table.
        import re as _re
        _oc_pat = _re.compile(r'Q3H0[A-Z0-9]+')
        _stm32_pat = _re.compile(r'(STM32[A-Za-z0-9]+)')
        _stm32_m = _stm32_pat.search(ref.caption) if ref.caption else None
        if _stm32_m:
            _stm32_name = _stm32_m.group(1)
            for _i, _h in enumerate(headers):
                if _h and _oc_pat.search(_h):
                    _oc = _oc_pat.search(_h).group()
                    headers[_i] = f"{_stm32_name} ({_oc})"

        # ── Remplissage du résultat ────────────────────────────────────────────
        if heuristics:
            result["heuristics"] = heuristics
        result.update({
            "headers":               headers,
            "rows":                  rows_fixed,
            "merged_pages":          merged_pages,
            "extraction_confidence": confidence,
            "empty_cell_ratio":      round(empty_ratio, 4),
            "col_count":             len(headers),
        })

        # ── Image crop debug (seulement si qualité ≠ high) ────────────────────
        if confidence != "high":
            crop_path = _save_table_crop(page, bbox, output_base, ref.table_id, family, pdf_name)
            if crop_path:
                result.setdefault("debug", {})["crop_path"] = crop_path

        # ── Debug table vide ─────────────────────────────────────────────────
        if DEBUG_EMPTY_ROWS and not rows_fixed:
            _settings = PDFPLUMBER_TABLE_SETTINGS_TYPE2 if pdf_type == 2 else PDFPLUMBER_TABLE_SETTINGS
            _save_empty_rows_debug(
                page=page, ref=ref, pdf_type=pdf_type,
                extraction_method=method, raw_table=raw_table, bbox=bbox,
                settings=_settings, pdf_path=pdf_path,
                output_base=output_base, family=family, pdf_name=pdf_name,
                caption_y=caption_y, caption_near_bottom=caption_near_bottom,
                n_non_empty=n_non_empty, should_try_next=should_try_next,
                warnings_list=result["warnings"], heuristics_dict=heuristics,
            )

    finally:
        if _own_pdf:
            pdf.close()

    logger.info(
        f"{ref.table_id} | page={ref.page} | method={method} "
        f"| confidence={confidence} | rows={len(result['rows'])} "
        f"| empty={result['empty_cell_ratio']:.2f}"
    )
    return result


def _is_likely_reversed(cell: str) -> bool:
    """True si le texte semble être lu à l'envers (vertical dans le PDF).

    Gère le texte avec newlines (ex: "A\\ntroP" = "PortA" inversé).
    Compare la distribution des majuscules entre l'original et l'inversé
    pour détecter les textes inversés même quand l'original commence
    par une majuscule (ex: "A troP" → "Port A").

    Approche : mesure la dérive de casse (uppercase drift) — si les
    majuscules sont clusterisées au début du texte, le texte a de fortes
    chances d'être inversé, même si la version corrigée commence par
    minuscule (ex: "ISL f" → "f LSI", correct = "f LSI").
    """
    clean = cell.replace("\n", "")
    if len(clean) < 5:
        return False

    rev = clean[::-1]
    if not (rev and len(rev) > 1):
        return False

    # ── Dérive de casse (uppercase drift) ────────────────────────────
    # Si les majuscules sont clusterisées au début (drift < 0),
    # le texte est probablement inversé.
    # Ex: "ISL f" → upper [0,1,2], lower [4] → drift = 1.0-4.0 = -3.0
    # Ex: "f LSI" → upper [2,3,4], lower [0] → drift = 3.0-0.0 = 3.0
    upper_pos = [i for i, c in enumerate(clean) if c.isupper()]
    lower_pos = [i for i, c in enumerate(clean) if c.islower()]
    drift = 0.0
    if upper_pos and lower_pos:
        avg_upper = sum(upper_pos) / len(upper_pos)
        avg_lower = sum(lower_pos) / len(lower_pos)
        drift = avg_upper - avg_lower

    # Si drift >= 0 (majuscules PAS clusterisées au début) ET l'inversé
    # ne commence pas par Maj/chiffre → texte normal, pas inversé.
    # Remplace l'ancien guard fixe rev[0].isupper() qui bloquait
    # les cas où la version correcte commence par minuscule.
    if drift >= 0 and not (rev[0].isupper() or rev[0].isdigit()):
        return False

    # ── Heuristiques rapides ─────────────────────────────────────────
    # "ot" inversé → "to" (ex: "6.3 ot 0.2")
    if re.search(r'\d+[.\s]*ot\s+\d+', clean):
        return True
    # "C°" inversé → "°C" (ex: "031 C°")
    if re.search(r'\d+\s*C\s*°(?!C)', clean):
        return True

    def _mid_word_uppers(text: str) -> int:
        """Compte les majuscules en milieu de mot (ni position 0, ni après espace)."""
        count = 0
        for i, c in enumerate(text):
            if c.isupper() and i > 0 and not text[i - 1].isspace():
                count += 1
        return count

    def _initial_upper_run(text: str) -> int:
        """Longueur de la séquence majuscule au début du texte.
        Ex: "STM32..." → 3, "Port A" → 1, "Timers" → 1"""
        count = 0
        for c in text:
            if c.isupper():
                count += 1
            else:
                break
        return count

    # La version inversée doit avoir au moins autant de lettres
    cell_alpha = sum(1 for c in clean if c.isalpha())
    rev_alpha = sum(1 for c in rev if c.isalpha())
    if rev_alpha < cell_alpha:
        return False

    clean_mid = _mid_word_uppers(clean)
    rev_mid = _mid_word_uppers(rev)
    clean_init = _initial_upper_run(clean)
    rev_init = _initial_upper_run(rev)

    # La version inversée ne doit pas avoir PLUS de majuscules en milieu de mot
    if rev_mid > clean_mid:
        return False

    # Si le texte original a déjà "(N)" correctement orienté → pas inversé
    # Ex: "V (1) IL" est déjà correct
    if re.search(r'\(\d+\)', clean):
        return False

    # Si le texte contient déjà "to NN" ou "°C" correct → pas inversé
    if re.search(r'\d+[.\s]*to\s+\d+', clean) or re.search(r'°\s*C', clean):
        return False

    # ── Motivations communes de faux positifs ──────────────────────────
    # Acronyme suivi d'une parenthèse (ex: "SRAM (Kbytes)")
    if re.search(r'\([A-Za-z]+\)', clean):
        return False
    # Plage de tension (ex: "2.7 V - 3.6 V")
    if re.search(r'\d+\.\d+\s*V\s*[-–]\s*\d+\.\d+\s*V', clean):
        return False
    # Bit-width suivi d'acronyme (ex: "12-bit ADC channels", "32-bit timer")
    if re.search(r'\d+-bit\s+[A-Z]', clean):
        return False
    # Acronyme avec trait d'union (ex: "Octo-SPI interface", "I2C-bus")
    if re.search(r'[A-Za-z]+-[A-Z]{2,}', clean):
        return False
    # Acronyme suivi d'un mot d'au moins 2 lettres (ex: "DMA support",
    # "DHUK and BHK key selection"). Exclut les textes courts type
    # "ISL f" (1 seule lettre apres l'espace) qui sont vraiment inverses.
    if len(clean) >= 8 and re.match(r'^[A-Z]{2,}\s+[a-z]{2,}', clean):
        return False
    # Paramètre courte : une lettre + espace + mot en majuscules (ex: "V HSEH", "R AIN", "C ADC")
    if re.match(r'^[A-Z]\s+[A-Z]', clean):
        return False
    # Port pattern : une lettre + espace + mot finissant par majuscule (ex: "G troP" → "Port G")
    if re.match(r'^[A-Z]\s+[a-z]*[A-Z]$', clean):
        return True
    # Paramètre courte : une lettre + espace + mot en minuscules (ex: "V rising", "V falling")
    if re.match(r'^[A-Z]\s+[a-z]', clean):
        return False
    # Date (ex: "02-Dec-2024")
    if re.match(r'\d{1,2}-[A-Z][a-z]{2}-\d{4}', clean):
        return False
    # Symbole : une lettre + espace + signe/unité (ex: "V = 3 V DD")
    if re.match(r'^[A-Z]\s*[=<>]', clean):
        return False
    # Symbole : [lettre] [space] [lettre] (ex: "T STG", "S EMI", "N END", "V DDA")
    if re.match(r'^[A-Z]\s+[A-Z]{2,4}$', clean):
        return False
    # Symbole : [lettre] [space] [lettre/num] (ex: "I INJ", "V FTB", "V FESD")
    if re.match(r'^[A-Z]\s+[A-Z0-9/_]+\s*$', clean):
        return False
    if re.match(r'^[A-Z]\s*[=<>]', clean):
        return False
    # Symbole : [lettre] [space] [lettre] (ex: "T STG", "S EMI", "N END", "V DDA")
    if re.match(r'^[A-Z]\s+[A-Z]{2,4}$', clean):
        return False
    # Symbole : [lettre] [space] [lettre/num] (ex: "I INJ", "V FTB", "V FESD")
    if re.match(r'^[A-Z]\s+[A-Z0-9/_]+\s*$', clean):
        return False
    # Identifiant technique 100% majuscules + chiffres + underscore
    # (ex: "I3C1_SDA", "PWR_CSTOP", "RTC_REFIN").
    # Un vrai texte inversé contiendrait des minuscules.
    if re.match(r'^[A-Z0-9_]+$', clean):
        return False
    # Deux acronymes consécutifs suivis d'un mot minuscule
    # (ex: "HDR DDR message"). Texte normal, pas inversé.
    if re.match(r'^[A-Z]{2,}\s+[A-Z]{2,}\s+[a-z]', clean):
        return False
    # Acronyme CamelCase suivi d'au moins un autre mot
    # (ex: "IrDA SIR ENDEC block" → IrD = CamelCase).
    # Un vrai texte inversé ne commencerait pas par Maj+min+Maj.
    # Exception : si l'inversé a une séquence majuscule initiale plus longue
    # suivie d'un chiffre (ex: "TxC3A5C23MTS" → "STM32C5A3CxT"),
    # c'est un part number inversé, pas un acronyme CamelCase.
    if re.match(r'^[A-Z][a-z]+[A-Z]', clean):
        if rev_init > clean_init + 1 and rev_init < len(rev) and rev[rev_init].isdigit():
            pass  # part number inversé (ex: "TxC3A5C23MTS" → "STM32C5A3CxT")
        else:
            return False
    # Acronyme court + mot minuscule (ex: "LIN mode", "HSE startup").
    # Requiert len < 15 car les textes longs passent déjà par le guard
    # ^[A-Z]{2,}\s+[a-z]{2,} ci-dessus.
    if len(clean) < 15 and re.match(r'^[A-Z]{2,4}\s+[a-z]{2,}', clean):
        return False
    # Commence par un caractère non-alphanumérique (ex: ΣIVDD, ~0.5 LSB).
    # Ces cellules sont des artefacts d'extraction, pas du texte inversé.
    if clean and not clean[0].isalnum():
        return False
    # "I/O" (Input/Output) suivi d'un espace → pas inversé
    if re.match(r'^I/O\s', clean):
        return False
    # Phrase en casse normale : Maj + minuscules + espace (ex: "Propagation delay...",
    # "With 50 kHz..."). Un texte inversé ne commencerait pas par Maj+min+espace.
    if re.match(r'^[A-Z][a-z]{2,}\s', clean):
        return False
    # Paramètre technique (majuscules/chiffres/underscore) suivi d'au moins
    # 2 lettres minuscules (ex: "OSC_IN input pin low-level voltage").
    # Exclut "ISL f" (1 seule lettre minuscule après l'espace) qui est vraiment inversé.
    if re.match(r'^[A-Z0-9_]+[\s:]+[a-z]{2,}', clean):
        return False
    # Paramètre en casse mixte avec underscore (ex: "Vhyst_POR_PDR", "Vhyst_PVD")
    if re.match(r'^[A-Z][a-z]+_[A-Z]', clean):
        return False
    # Paramètre technique avec 2+ underscores (ex: "VHSE_ext_PP")
    # Structure typique : MAJ_min_MAJ, improbable dans un texte inversé.
    if clean.count('_') >= 2 and re.search(r'[a-z]', clean):
        return False
    # Préfixe "f" minuscule (fréquence) suivi d'un acronyme (ex: "fHSE_ext", "fLSE_ext")
    if re.match(r'^[a-z][A-Z]{2,}', clean):
        return False
    # Chevrons avec paramètre (ex: "MODE<2:0>_V12=111 BUFFER OFF")
    if re.search(r'<[A-Za-z0-9:]+>', clean):
        return False
    # Opérateur arithmétique (ex: "315*Ton/ (Ton+Toff)")
    if re.search(r'[\*\+]\(?[A-Za-z]', clean):
        return False
    # Inégalité de tension (ex: "2.7 V < VDD < 3.6 V")
    if re.search(r'<\s*[A-Z]+\s*<', clean):
        return False
    # Deux mots en majuscules séparés par un espace (ex: "TRIMOFFSETP TRIMLPOFFSETP")
    if re.match(r'^[A-Z0-9]{2,}\s+[A-Z0-9]{2,}$', clean):
        return False
    # Paramètre suivi d'un tiret et d'une valeur numérique (ex: "VDDA -100 mV")
    if re.match(r'^[A-Z0-9_]+\s+[-–]\s*\d+', clean):
        return False
    # Acronyme avec parenthèse multi-mot (ex: "PKA (ECDSA signature verification)")
    if re.match(r'^[A-Z]{2,}\s+\([A-Za-z]+\s+', clean):
        return False
    # Code de commande/package avec slash (ex: "Bx/8x170C23MTS", "C092xB/xC")
    if re.search(r'[A-Z][a-z]/[A-Za-z0-9]', clean) or re.search(r'[A-Z]\d+[a-z][A-Z]/', clean):
        return False
    # Les deux versions commencent par minuscule → symétrique → pas inversé
    if clean and rev and clean[0].islower() and rev[0].islower():
        return False
    # Parenthèse multi-mot (ex: "(parity error)")
    if re.search(r'\([A-Za-z]+\s+[A-Za-z]+\)', clean):
        return False
    # Séparateur " / " (ex: "RTC / RNG / AES / VREFBUF")
    if re.search(r'\s/\s', clean):
        return False
    # Trois mots ou plus en majuscules (ex: "USER TRIM COVERAGE")
    if re.match(r'^[A-Z][A-Z0-9\s]{5,}$', clean):
        return False
    # "A/D", "I/O" etc (acronyme avec slash)
    if re.search(r'A/[A-Z]', clean):
        return False
    # Formule "f = f ..." (ex: "f = f HCLK HSI48/HSIDIV")
    if re.search(r'f\s*=\s*f\s', clean):
        return False
    # "V . CORE" pattern (lettre + espace + point + espace + lettre)
    if re.search(r'\b[A-Z]\s\.\s[A-Z]', clean):
        return False
    # Texte se terminant par un point (ex: "g to I/O control registers.")
    if clean.endswith('.'):
        return False
    # Nom de pin avec tiret (ex: "PF2-NRST")
    if re.match(r'^[A-Z0-9]+-[A-Z0-9]+$', clean):
        return False
    # Valeur numérique avec unité (ex: "48MHz", "16MHz")
    if re.match(r'^\d+[A-Za-z]+$', clean):
        return False
    # Plage de tension avec "to" (ex: "2.0 V to 3.6 V")
    if re.search(r'\d+\.\d+\s*V\s+to\s', clean):
        return False
    # Majuscule + minuscule + espace (ex: "Hz internal RC (LSI")
    if re.match(r'^[A-Z][a-z]{1,}\s', clean):
        return False
    # Parenthèse ouvrante suivie de minuscule (ex: "CD (binar")
    if re.search(r'\([a-z]', clean):
        return False
    # Chiffre + V + minuscule (ex: "6 V operatin", "2.0 V corr")
    if re.match(r'^\d+(\.\d+)?\s+V\s+[a-z]', clean):
        return False
    # Formule avec nombre décimal + lettre + espace + majuscule (ex: "0.39x D 2 or")
    if re.search(r'\d+\.\d+[a-z]\s+[A-Z]', clean):
        return False
    # Formule avec nombre décimal + lettre + majuscule collée (ex: "0.3xVdd")
    if re.search(r'\d+\.\d+[a-z][A-Z]', clean):
        return False

    # Texte inversé : une seule majuscule en toute dernière position + l'inversé commence
    # par une majuscule + minuscules (ex: "sremiT" → "Timers", "secafretni.mmoC" → "Comm.interfaces")
    if len(upper_pos) == 1 and upper_pos[0] == len(clean) - 1:
        if rev[0].isupper() and len(rev) > 1 and rev[1].islower():
            return True

    # Texte inversé tout-minuscules + l'inversé commence par Maj+min (ex: "gnirotinom" → "Monitoring")
    if not upper_pos and rev[0].isupper() and len(rev) > 1 and rev[1].islower():
        return True

    # Si l'inversé a une plus longue séquence majuscule au début → inversé
    # Sauf si l'original commence par minuscule ou chiffre
    # (ex: "f LSI" → l'original commence bien par minuscule)
    # Requiert diff >= 2 pour éviter les faux positifs "V rising DD" (diff=1)
    if rev_init > clean_init + 1 and not clean[0].islower() and not clean[0].isdigit():
        return True

    # Parenthèses inversées ")N(" → inversé
    if re.search(r'\(\d+\)', rev) and re.search(r'\)\d+\(', clean):
        return True

    # Drift négatif : majuscules clusterisées au début → inversé
    # Capture les cas où l'inversé commence par minuscule
    # (ex: "ISL f" → "f LSI", drift = -3.0).
    # Garde-fou : les part numbers STM32 ("STM32C5A3KxT", drift = -5.0)
    # ont aussi un drift négatif mais ne sont PAS inversés.
    if drift < -0.5 and not re.match(r'^[A-Z]+\d+[A-Z]', clean):
        return True

    # Si l'original commence par majuscule et les deux versions ont autant
    # de majuscules en milieu de mot → symétrique → pas inversé
    # Sauf si drift < 0 (déjà capturé ci-dessus)
    if not clean[0].islower() and rev_mid >= clean_mid:
        return False

    return False


def _fix_reversed_cells(rows: list[list], table_id: str = "") -> list[list]:
    SPACE_AROUND_PARENS = re.compile(r'\s*([()])\s*')

    if not rows:
        return rows
    fixed = []
    for row_idx, row in enumerate(rows):
        fixed_row = []
        for col_idx, cell in enumerate(row):
            if isinstance(cell, str) and len(cell) >= 5:
                clean = cell.replace("\n", "")
                is_rev = _is_likely_reversed(clean)
                corrected = None
                if is_rev:
                    corrected = clean[::-1]
                    corrected = SPACE_AROUND_PARENS.sub(r'\1', corrected)
                    cell = corrected
                upper_pos = [i for i, c in enumerate(clean) if c.isupper()]
                lower_pos = [i for i, c in enumerate(clean) if c.islower()]
                drift_v = 0.0
                if upper_pos and lower_pos:
                    drift_v = (sum(upper_pos) / len(upper_pos)) - (sum(lower_pos) / len(lower_pos))
                _reversed_debug_entries.append({
                    "table_id": table_id,
                    "col": col_idx,
                    "row": row_idx,
                    "original": clean,
                    "reversed": is_rev,
                    "drift": round(drift_v, 2),
                    "corrected": corrected,
                })
            fixed_row.append(cell)
        fixed.append(fixed_row)
    return fixed


def _deduplicate_rows(rows: list[list[str]]) -> list[list[str]]:
    """
    Supprime les lignes consécutives strictement identiques.
    Fixe les doublons créés par Fix 6 quand des lignes vides artificielles
    (pdfplumber_text) reçoivent la valeur de la ligne au-dessus.
    """
    if not rows:
        return rows
    result = [rows[0]]
    for row in rows[1:]:
        if row != result[-1]:
            result.append(row)
    return result


def _remove_bleed_rows(raw_table: list, method: str) -> list:
    """
    Supprime les lignes de bleed page header/footer en haut de raw_table.
    Uniquement pour pdfplumber_text, 2 heuristiques séquentielles :
      1. Sparsité (< 30% de remplissage) — supprime les lignes creuses
         (ex: "Electrical characteristics" éclaté en 3/14 colonnes)
      2. Préfixe (>80% des cellules partagent le même préfixe 4-car.)
         (ex: "Elect" x14)
    Appliqué avant _expand_spans_and_headers pour que l'expansion travaille
    sur les vrais headers (Symbol/Parameter/...) et que l'étape 14 existante
    (_merge_fragmented_columns) fusionne correctement les fragments.
    """
    if method != "pdfplumber_text" or not raw_table:
        return raw_table
    result = list(raw_table)
    for _ in range(min(10, len(result))):
        if not result or not result[0]:
            break
        row = result[0]
        total = len(row)
        non_empty = [str(c).strip() for c in row if c and str(c).strip()]
        n_filled = len(non_empty)
        fill_ratio = n_filled / total if total > 0 else 0
        # Heuristique 1 : ligne vide ou très creuse (< 30% remplie)
        if fill_ratio < 0.3:
            logger.info(f"_remove_bleed_rows: sparse row removed ({n_filled}/{total} filled)")
            result.pop(0)
            continue
        # Heuristique 2 : >80% des cellules partagent le même préfixe
        if n_filled >= 3:
            pref_counts = {}
            for c in non_empty:
                p = c[:4].lower() if len(c) >= 4 else c.lower()
                pref_counts[p] = pref_counts.get(p, 0) + 1
            max_p, max_n = max(pref_counts.items(), key=lambda x: x[1])
            if max_n / n_filled > 0.8:
                logger.info(f"_remove_bleed_rows: prefix bleed removed ({max_p} x{max_n}/{n_filled})")
                result.pop(0)
                continue
        break
    return result


def _remove_bleed_rows_bottom(rows: list[list[str]], table_id: str) -> tuple[list[list[str]], int]:
    """
    Supprime les lignes de fuite en BAS du tableau APRÈS la fusion des
    colonnes. Détecte les lignes qui référencent une autre table ou qui
    commencent par une minuscule (continuation de texte parasite).
    Également : 1ère cellule non-alphanumérique (ex: ".4 Embe", "(1)")
    → continuation ou footnote parasite. Exclut '+' (symboles comme +3.3V).
    Également exclut '/', '.', '-' (Prescaler /4, decimal values .5, dash -40).

    Pour Type 2 (Antenna House), les Prescaler values commencent par "/",
    les valeurs negatives par "-", et les unites comme ".5" par ".".
    Retourne (rows_nettoyées, nb_supprimées).
    """
    if not rows:
        return rows, 0
    cut = len(rows)
    ALLOWED_NONALNUM = frozenset({'+', '/', '.', '-', '(', '_'})
    for i in range(len(rows) - 1, -1, -1):
        text = "".join(str(c or "") for c in rows[i])
        if not text.strip():
            cut = i
            continue
        # Référence à une autre table (ex: "Table 26", "Table26")
        m = re.search(r'\bTable\s*(\d+)', text, re.IGNORECASE)
        if m:
            nums = re.findall(r'\d+', str(table_id))
            cur_id = int(nums[0]) if nums else 0
            if int(m.group(1)) != cur_id:
                cut = i
                logger.info(f"_remove_bleed_rows_bottom: cut at row {i} (ref Table {m.group(1)}, cur={cur_id})")
                continue
        # Première cellule commence par minuscule → continuation parasite
        # (ex: "pecified by design." → 'p' minuscule = fragment de "Specified")
        # Exclut les motifs Type 2 valides comme "x = 0 to 7"
        first = str(rows[i][0]).strip() if rows[i] else ""
        if first and first[0].islower():
            if re.search(r'=', first):
                pass  # valeur d'affectation (ex: "x = 0 to 7")
            elif len(first) <= 4:
                pass  # trop court (ex: "aaa", "tVDD")
            elif re.search(r'\d', first):
                pass  # contient chiffre (ex: "fCK" est rare, mais OK)
            else:
                cut = i
                logger.info(f"_remove_bleed_rows_bottom: cut at row {i} (lowercase first cell '{first[:20]}')")
                continue
        # Première cellule commence par un caractère non-alphanumérique
        # (ex: ".4 Embe", "(1)") → continuation ou footnote parasite
        # Exclut '+' (symboles comme +3.3V), '/' (Prescaler /4, /8),
        # '.' (decimal values .5, .25), '-' (negative -40, ranges)
        if first and not first[0].isalnum() and first[0] not in ALLOWED_NONALNUM:
            cut = i
            logger.info(f"_remove_bleed_rows_bottom: cut at row {i} (non-alnum first cell '{first[:20]}')")
            continue
        break
    removed = len(rows) - cut
    if removed > 0:
        logger.info(f"_remove_bleed_rows_bottom: removed {removed} trailing bleed rows")
    return rows[:cut], removed


def _remove_section_bleed_rows(rows: list[list[str]], table_id: str) -> tuple[list[list[str]], int]:
    """Supprime les lignes de texte de section parasites sous le tableau.

    Les datasheets ont du texte de section (3.5 Boot mode, 3.6 CRC, ...)
    qui suit le tableau sur la meme page. pdfplumber les capture comme
    des lignes de tableau.

    Detection (3 signaux combines pour une couverture radicale) :
      1. 1ere cellule = numero de section (ex: "3.5", "1.1.2")
      2. 1ere cellule commence par minuscule (continuation de paragraphe)
      3. Cellule col 1+ commence par minuscule (fragment de prose,
         ex: "verview" pour "Functional overview")

    Pour Type 2 (Antenna House), P2 et P3 sont trop agressifs car les
    unites (ms, mA), les parametres (tVDD) et les Prescaler (x = 0 to 7)
    commencent aussi par minuscule. On exclut :
      - P2 : si le texte semble etre une valeur (chiffre, operateur)
      - P3 : sauf pour la derniere colonne (unite, toujours minuscule)
    """
    if not rows:
        return rows, 0
    # Section bleed n'apparait JAMAIS dans les premieres lignes (toujours en queue)
    MIN_CUT = min(3, len(rows) // 4)
    cut = len(rows)
    for i, row in enumerate(rows):
        if i < MIN_CUT:
            continue  # ignorer les premieres lignes (header + donnees)
        first = str(row[0]).strip() if row else ""
        # Pattern 1: section number (ex: "3.5.1", "3.5 Boot mode")
        # Ne pas confondre avec les valeurs décimales (ex: "1.5", "0.11")
        if re.match(r'^\d+\.\d+\.\d+', first) or re.match(r'^\d+\.\d+\s+[A-Za-z]', first):
            cut = i
            logger.info(f"_remove_section_bleed_rows: cut at row {i} ('{first}')")
            break
        # Pattern 2: first cell starts lowercase (paragraph continuation)
        # Exclut les motifs Type 2 valides : valeur avec espace/chiffre/apostrophe
        if first and re.match(r'^[a-z]', first):
            # Exclure si c'est une donnee Type 2 typique :
            # - contient "= " (ex: "x = 0 to 7")
            # - contient un chiffre (ex: "tVDD" non-chiffree OK, "x = 0 to 7" OK)
            # - contient "'" suivi de lettres (ex: "re-design", "specified by")
            # - contient "(" → nom de parametre (ex: "twu(Sleep)", "tres(TIM)")
            # - contient "_" → nom de parametre (ex: "fHSE_ext", "fLSE_ext")
            # - contient " " + majuscule → newline→space artifact (ex: "t\nWUSLEEP")
            if re.search(r'=', first):
                pass  # valeur d'affectation, PAS une prose
            elif len(first) <= 6:
                pass  # trop court pour etre une prose (ex: "tprog", "aaa")
            elif re.search(r'[a-z][A-Z]', first):
                pass  # camelCase technique (ex: "trLSE", "tfLSE", "tVDD")
            elif re.search(r'\d', first):
                pass  # contient un chiffre = valeur technique, pas prose
            elif re.search(r'[(_]', first):
                pass  # contient ( ou _ = nom de parametre, pas prose
            elif re.match(r'^[a-z]\s+[A-Z]', first):
                pass  # newline→space artifact (ex: "t\nWUSLEEP" → "t WUSLEEP")
            else:
                cut = i
                logger.info(f"_remove_section_bleed_rows: cut at row {i} (lowercase first)")
                break
        # Pattern 3: any cell in col 1+ starts lowercase (prose fragment)
        # Exclut la DERNIERE colonne (toujours l'unite, toujours minuscule: ms, mA, V, °C)
        # Exclut les lignes de continuation (1ere cellule identique a la ligne precedente)
        # Exclut aussi si la 1ere cellule est vide (continuation row) ou commence par
        # majuscule → ligne header/donnees
        # Section bleed n'apparait JAMAIS dans les premieres lignes (toujours en queue)
        if len(row) >= 2 and first and not re.match(r'^[A-Z]', first):
            # Ne pas couper les premieres lignes (section bleed est toujours en fin de table)
            if i < 3:
                continue
            # Ne pas couper si la 1ere cellule contient \n (param technique: "f\nCK")
            if '\n' in first:
                continue
            # Ligne de continuation apres merge vertical → pas du section bleed
            is_continuation = (
                i > 0 and rows[i-1] and len(rows[i-1]) > 0
                and str(rows[i-1][0]).strip() == first
            )
            if not is_continuation:
                found = False
                for ci, cell in enumerate(row[1:], start=1):
                    # Ignorer la derniere colonne (unite)
                    if ci == len(row) - 1:
                        continue
                    stripped = str(cell).strip()
                    if stripped and re.match(r'^[a-z]', stripped):
                        if re.search(r'[(_\d]', stripped):
                            continue
                        if re.search(r'[/\-°]', stripped):
                            continue
                        # Paramètres techniques techniques courts (fCK, tv(TX)...)
                        # ne sont jamais de la prose de section
                        if len(stripped) <= 10:
                            continue
                        found = True
                        break
                if found:
                    cut = i
                    logger.info(f"_remove_section_bleed_rows: cut at row {i} (lowercase col 1+)")
                    break
    removed = len(rows) - cut
    if removed > 0:
        logger.info(f"_remove_section_bleed_rows: removed {removed} trailing section bleed rows")
    return rows[:cut], removed


def _truncate_at_next_table(
    raw_table: list[list],
    table_id,
) -> list[list]:
    """
    [Fix] Tronque raw_table dès qu'une ligne contient une table suivante
    (ex: "Table 66." dans les données de la Table 65) ou un pied de page.
    """
    if not raw_table:
        return raw_table
    # Extraire le numéro de table (supporte "table_65", "65", 65)
    if isinstance(table_id, int):
        cur_id = table_id
    else:
        nums = re.findall(r'\d+', str(table_id))
        cur_id = int(nums[0]) if nums else 0
    cut_idx = len(raw_table)
    for i, row in enumerate(raw_table):
        text = "".join(str(c or "") for c in row)
        # Couper si "Table N.", "Table N:", ou "TableN" (sans espace)
        # avec N > table_id actuelle. Le pattern large \bTable\s*(\d+)
        # capture aussi "Table26" (fréquent dans les PDFs scannés).
        m = re.search(r'\bTable\s*(\d+)', text)
        if m and int(m.group(1)) > cur_id:
            cut_idx = i
            logger.info(f"_truncate_at_next_table: cut at row {i} (Table {m.group(1)})")
            break
        if re.search(r'\bpage\s+\d+/\d+\b', text):
            cut_idx = i
            logger.info(f"_truncate_at_next_table: cut at row {i} (footer)")
            break
        if re.search(r'\bDS\s+\d+\s*-\s*Rev\b', text):
            cut_idx = i
            logger.info(f"_truncate_at_next_table: cut at row {i} (DS footer)")
            break
    return raw_table[:cut_idx]


def _merge_identical_adjacent_columns(
    headers: list[str],
    rows: list[list[str]],
    skip_first_n: int = 0,
) -> tuple[list[str], list[list[str]]]:
    if not headers or not rows:
        return headers, rows
    cols = len(headers)
    keep = [True] * cols
    for c in range(cols - 1, skip_first_n, -1):
        if c < len(headers) and headers[c] == headers[c-1]:
            same = True
            for r in range(len(rows)):
                v1 = rows[r][c] if c < len(rows[r]) else ""
                v2 = rows[r][c-1] if c-1 < len(rows[r]) else ""
                if v1 != v2:
                    same = False
                    break
            if same:
                keep[c] = False
                logger.info(f"_merge_identical_adjacent_columns: merged col {c} into {c-1}")
    new_headers = [h for i, h in enumerate(headers) if keep[i]]
    new_rows = [[cell for i, cell in enumerate(row) if keep[i]] for row in rows]
    new_cols = len(new_headers)
    if new_cols < cols:
        logger.info(f"_merge_identical_adjacent_columns: {cols} → {new_cols} cols")
    return new_headers, new_rows


def _merge_fragmented_columns(
    headers: list[str],
    rows: list[list[str]],
) -> tuple[list[str], list[list[str]], int]:
    """
    [Fix] Fusionne les colonnes fragmentées (pdfplumber_text) dans leur voisine
    de gauche. Détecte les fragments par 4 heuristiques complémentaires :
    1. Colonne vide/tres creuse (<=40% non-vide)
    2. Header commence en minuscule = fragment de mot (ex: "ax" dans "M"/"ax")
    3. 1ere cellule donnee commence en minuscule = continuation de mot
       Guard : si le header droit est >=3 car. avec 1ère majuscule (ex: "Unit"),
       c'est une colonne réelle → ne pas fusionner.
    4. Cellules tres courtes (< 4 car.) — consecutif limite a 4
       Guard identique à H3 pour protéger les colonnes réelles.

    Détection des header leaks : pendant la fusion des données, toute cellule
    identique à l'en-tête original de sa colonne est considérée comme un
    "header leak" (fragment d'en-tête qui a coulé dans les données) et n'est
    PAS fusionnée. Ex: colonne "s" (suffixe de "Conditions") avec données "s"
    dans toutes les lignes → ignorée.

    Espace intelligent : lowercase+lowercase → pas d'espace (fragments de mot,
    ex: "ris"+"ing" = "rising"). Sinon → espace (ex: "V DD"+"rising" = "V DD rising").

    IMPORTANT : cette fonction est uniquement appelée pour les tables
    pdfplumber_text (extraction sans bordures). Pour les tables pdfplumber
    (bordures), les colonnes sont déjà correctement détectées et il ne
    faut PAS les fusionner — sinon les tables mécaniques (7 colonnes
    Symbol/mm/inches) sont écrasées en 2 colonnes (H2 traite "millimeters"
    comme un fragment). Voir le garde-fou à la ligne 1084.
    Retourne (headers, rows, merged_count).
    """
    if not headers or not rows:
        return headers, rows, 0
    cols = len(headers)
    keep = [True] * cols
    # Sauvegarde des headers originaux pour la détection des header leaks
    # pendant la fusion. Sans cette copie, les fusions successives (droite→
    # gauche) modifient headers[] et faussent la comparaison donnée==header.
    orig_headers = list(headers)
    consec_merged = 0
    for c in range(cols - 1, 0, -1):
        if not keep[c]:
            consec_merged += 1
            continue
        if consec_merged >= 4:
            # Si la colonne courante est un fragment clair (header minuscule),
            # on permet la fusion même après 4 fusions consécutives de colonnes
            # vides. Sans ce passe-droit, "itionsMinTypMax" (col 3, header 'i'
            # minuscule) serait sauté après les 4 colonnes vides 7,6,5,4.
            if c < len(headers) and headers[c] and headers[c].strip() and headers[c].strip()[0].islower():
                pass
            else:
                consec_merged = 0
                continue
        # Compter les valeurs non-vides dans cette colonne
        n_non_empty = 0
        first_non_empty = None
        max_len = 0
        for r in rows:
            if c < len(r) and r[c] and str(r[c]).strip():
                v = str(r[c]).strip()
                n_non_empty += 1
                if first_non_empty is None:
                    first_non_empty = v
                max_len = max(max_len, len(v))
        total = len(rows)
        ratio = n_non_empty / total if total > 0 else 0
        # Heuristique 1 : colonne vide/creuse
        if ratio < 0.4:
            merge = True
        # Heuristique 2 : header commence en minuscule = fragment de mot
        # (ex: header "ax" dans "M"/"ax" → fusionner avec "M" → "Max")
        # Garde : ne PAS fusionner les colonnes d'unité connues (mA, mV, MHz, °C...)
        elif c < len(headers) and headers[c] and headers[c].strip() and headers[c].strip()[0].islower():
            _UNIT_H = frozenset({"ma","mv","ua","khz","mhz","ghz","hz",
                                 "ns","us","ms","s","mw","uw","db","dbm",
                                 "°c","c","k","v","a","unit","units"})
            if headers[c].strip().lower() in _UNIT_H:
                merge = False
            else:
                merge = True
        # Heuristique 3 : donnee commence en minuscule = continuation de mot
        # Ex: 1ère cellule "v" (continuation de "~10 V") → fusionner
        # Protection : si le header droit est ≥2 car. avec majuscule, c'est
        # une colonne réelle (ex: "Unit", "Un") → NE PAS fusionner. Sans ce
        # garde-fou, "Un" (fragment de "Unit") serait collé à "Max" → "MaxUn".
        elif first_non_empty and first_non_empty[0].islower():
            if c < len(headers) and headers[c] and headers[c].strip():
                if len(headers[c].strip()) >= 2 and headers[c].strip()[0].isupper():
                    merge = False
                else:
                    merge = True
            else:
                merge = True
        # Heuristique 4 : cellules tres courtes (< 4 car.)
        # Ex: données "mA", "V" — trop courtes pour être une colonne réelle
        # Même garde-fou que H3 : header "Unit", "Un" protégé (≥2 car., majuscule)
        elif max_len < 4:
            if c < len(headers) and headers[c] and headers[c].strip():
                if len(headers[c].strip()) >= 2 and headers[c].strip()[0].isupper():
                    merge = False
                else:
                    merge = True
            else:
                merge = True
        else:
            merge = False
        if merge:
            left_h = headers[c-1] if c-1 < len(headers) else ""
            right_h = headers[c] if c < len(headers) else ""
            # Utilise orig_headers pour la détection header leak, car headers
            # est modifié par les fusions des colonnes de droite déjà traitées.
            # Ex: col 5 "s" + col 6 "Min" → headers[5]="sMin", mais le header
            # original de la col 5 était "s" → droit_h_str="s" pour la détection.
            left_h_str = (orig_headers[c-1] or "").strip() if c-1 < len(orig_headers) else ""
            right_h_str = (orig_headers[c] or "").strip() if c < len(orig_headers) else ""
            if right_h and right_h.strip():
                if left_h and left_h.strip():
                    headers[c-1] = left_h + right_h
                else:
                    headers[c-1] = right_h
            # Fusion des données AVEC détection des header leaks
            # Header leak = donnée identique à l'en-tête de sa colonne
            # (ex: colonne "s" contient "s" dans toutes les lignes = pas une vraie donnée)
            # Quand un header leak est détecté, la cellule est ignorée lors de la fusion.
            # ESPACE INTELLIGENT : lowercase+lowercase → pas d'espace (fragment de mot),
            # sinon → espace (mots séparés, ex: "V DD"+"rising" → "V DD rising")
            for r in range(len(rows)):
                if c < len(rows[r]):
                    left_cell = rows[r][c-1] if c-1 < len(rows[r]) else ""
                    right_cell = rows[r][c] if c < len(rows[r]) else ""
                    if right_cell and right_cell.strip():
                        is_right_leak = right_cell.strip() == right_h_str
                        is_left_leak = left_cell.strip() == left_h_str
                        if not is_right_leak:
                            if left_cell and left_cell.strip() and not is_left_leak and left_cell != right_cell:
                                if left_cell[-1].islower() and right_cell[0].islower():
                                    rows[r][c-1] = left_cell + right_cell
                                else:
                                    rows[r][c-1] = left_cell + " " + right_cell
                            else:
                                rows[r][c-1] = right_cell
            keep[c] = False
            consec_merged += 1
            logger.info(f"_merge_fragmented_columns: merged col {c} into {c-1} (r={ratio:.2f}, first='{first_non_empty}')")
        else:
            consec_merged = 0
    merged = sum(1 for k in keep if not k)
    new_headers = [h for i, h in enumerate(headers) if keep[i]]
    new_rows = [[cell for i, cell in enumerate(row) if keep[i]] for row in rows]
    new_cols = len(new_headers)
    if new_cols < cols:
        logger.info(f"_merge_fragmented_columns: {cols} → {new_cols} cols")
    return new_headers, new_rows, merged


def _filter_narrow_tables(
    tables: list, finder_tables: list
) -> tuple[list, list]:
    """Filtre les tables trop étroites (< MIN_TABLE_WIDTH) = bandeaux décoratifs."""
    if not tables or not finder_tables:
        return tables or [], finder_tables or []
    filtered = [
        (t, ft) for t, ft in zip(tables, finder_tables)
        if ft.bbox[2] - ft.bbox[0] >= MIN_TABLE_WIDTH
    ]
    if not filtered:
        return tables, finder_tables
    return [t for t, ft in filtered], [ft for t, ft in filtered]


def _has_keyword(raw_table: list[list], keyword: str) -> bool:
    """Vérifie si un mot-clé apparaît dans les cellules d'une table extraite."""
    if not keyword or not raw_table:
        return False
    kw = keyword.lower()
    return any(
        kw in str(c).lower()
        for row in raw_table for c in row if c is not None
    )


def _extract_from_page(
    page: Page,
    ref: TableRef,
    rotated_map: dict,
    pdf_type: int = 1,
    caption_keyword: str = "",
) -> tuple[Optional[list], Optional[Any], str, Optional[tuple]]:
    """
    Extrait la grille brute depuis une page via pdfplumber.

    Stratégie d'extraction (3 essais) :
      1. "lines" (bordures réelles) → méthode pdfplumber
         - Filtrage Type 2 : rejet des bandeaux < MIN_TABLE_WIDTH
         - Sélection via _pick_best_table (proximité sous légende)
         - Correction Fix 1 (texte rotatif)
      2. "text" (fallback interne) → méthode pdfplumber_text
         - Même sélection + filtrage
         - Utilisé quand les bordures ne sont pas détectées
      3. Sans finder (PDF sans bordures) → bbox = page entière
         - Dernier recours pour les tables sans structure visible

    caption_keyword : si fourni, la stratégie lines n'est acceptée que si
                      ce mot-clé apparaît dans les cellules extraites
                      (évite de capturer la mauvaise table).

    Retourne (raw_table, table_obj, method_name, bbox) ou (None, None, ..., None) si échec.
    """
    settings = PDFPLUMBER_TABLE_SETTINGS_TYPE2 if pdf_type == 2 else PDFPLUMBER_TABLE_SETTINGS
    fallback = PDFPLUMBER_TABLE_SETTINGS_FALLBACK_TYPE2 if pdf_type == 2 else PDFPLUMBER_TABLE_SETTINGS_FALLBACK

    # Essai 1 : stratégie lignes
    tables = page.extract_tables(settings)
    finder = page.debug_tablefinder(settings)
    best1 = best_ft1 = bbox1 = None
    if tables and finder.tables:
        if pdf_type == 2:
            tables, finder.tables = _filter_narrow_tables(tables, finder.tables)
        best1, best_ft1, bbox1 = _pick_best_table(page, tables, finder.tables, ref.caption)
        if best1 is not None and _is_image_table(best1):
            best1 = best_ft1 = bbox1 = None
        if best1 is not None:
            best1 = _apply_rotated_fix(page, best1, rotated_map, best_ft1)
            best1 = _detect_vector_dashes(best1, best_ft1, page)
            best1, best_ft1 = _merge_compatible_tables(best1, best_ft1, tables, finder.tables, page=page, table_id=ref.table_id)

    q1 = _table_quality(best1) if best1 else -1.0
    if q1 >= 2.0:
        if not caption_keyword or _has_keyword(best1, caption_keyword):
            return best1, best_ft1, "pdfplumber", bbox1

    # Essai 2 : stratégie texte
    tables_text = page.extract_tables(fallback)
    finder_text = page.debug_tablefinder(fallback)
    best2 = best_ft2 = bbox2 = None
    if tables_text and finder_text.tables:
        if pdf_type == 2:
            tables_text, finder_text.tables = _filter_narrow_tables(tables_text, finder_text.tables)
        best2, best_ft2, bbox2 = _pick_best_table(page, tables_text, finder_text.tables, ref.caption)
        if best2 is not None and _is_image_table(best2):
            best2 = best_ft2 = bbox2 = None
        if best2 is not None:
            best2 = _apply_rotated_fix(page, best2, rotated_map, best_ft2)
            best2 = _detect_vector_dashes(best2, best_ft2, page)
            best2, best_ft2 = _merge_compatible_tables(best2, best_ft2, tables_text, finder_text.tables, page=page, table_id=ref.table_id)

            # Le filtrage des lignes au-dessus de la légende est fait
            # dans extract_table_grid (après _extract_from_page) pour que
            # la continuation multi-pages fonctionne correctement.

    q2 = _table_quality(best2) if best2 else -1.0

    def _kw_ok(raw):
        return not caption_keyword or _has_keyword(raw, caption_keyword)

    if best2 is not None and q2 >= q1 and _kw_ok(best2):
        return best2, best_ft2, "pdfplumber_text", bbox2
    if best1 is not None and _kw_ok(best1):
        return best1, best_ft1, "pdfplumber", bbox1
    if best2 is not None and _kw_ok(best2):
        return best2, best_ft2, "pdfplumber_text", bbox2

    # Essai 3 : tables sans finder (PDF sans bordures nettes → bbox=page entière)
    if tables:
        ft_dummy = [type("T", (), {"bbox": (0, 0, page.width, page.height)})()]
        best, best_ft, _ = _pick_best_table(page, tables, ft_dummy, ref.caption)
        if best is not None and (_is_image_table(best) or not _kw_ok(best)):
            best = None
        if best is not None:
            return best, best_ft, "pdfplumber", None

    return None, None, "pdfplumber", None


def _merge_compatible_tables(
    raw_table: list,
    table_obj: Any,
    all_tables: list,
    all_finder: list,
    page: Any = None,
    table_id: str = "",
) -> tuple[list, Any]:
    """
    Fusionne les tables pdfplumber adjacentes compatibles.

    Pdfplumber peut diviser une table logique (ex: "Table 2. Device features")
    en plusieurs tables physiques suite à des bordures interrompues,
    changements de fond, ou sauts de ligne horizontale.
    Cette fonction réassemble les fragments qui ont le même nombre de colonnes
    et sont situés directement l'un en-dessous de l'autre.

    [Fix B] Vérifie le texte entre les fragments : si "Table N" (N différent)
    est présent, les fragments appartiennent à des tables distinctes → ne pas merger.
    """
    if not raw_table or not table_obj or not all_tables or not all_finder:
        return raw_table, table_obj

    ncols = len(raw_table[0])

    try:
        idx = next(i for i, ft in enumerate(all_finder) if ft is table_obj)
    except StopIteration:
        return raw_table, table_obj

    cur_num = 0
    if table_id:
        nums = re.findall(r'\d+', str(table_id))
        cur_num = int(nums[0]) if nums else 0

    merged = list(raw_table)
    current_ft = table_obj
    current_bottom = current_ft.bbox[3]

    for i in range(idx + 1, len(all_finder)):
        ft = all_finder[i]
        t = all_tables[i]
        if t is None or len(t) < 2:
            break
        if len(t[0]) != ncols:
            break
        gap = ft.bbox[1] - current_bottom
        if gap < 0 or gap > 50:
            break
        if abs(ft.bbox[0] - current_ft.bbox[0]) > 15:
            break

        # [Fix B] Vérifier si le gap contient "Table N" avec un numéro différent
        if page is not None and cur_num > 0:
            gap_bbox = (
                min(ft.bbox[0], current_ft.bbox[0]),
                current_bottom,
                max(ft.bbox[2], current_ft.bbox[2]),
                ft.bbox[1],
            )
            gap_words = page.within_bbox(gap_bbox).extract_words()
            gap_text = " ".join(w["text"] for w in gap_words)
            gap_nums = re.findall(r'[Tt]able\s*(\d+)', gap_text)
            if gap_nums and any(int(n) != cur_num for n in gap_nums):
                logger.info(
                    f"_merge_compatible_tables: gap contains table {gap_nums}, "
                    f"≠ current {cur_num}, not merging"
                )
                break

        rows_to_add = list(t)
        if rows_to_add and merged and rows_to_add[0] == merged[-1]:
            rows_to_add = rows_to_add[1:]
        merged.extend(rows_to_add)
        current_ft = ft
        current_bottom = ft.bbox[3]

    return merged, current_ft


def _apply_rotated_fix(
    page: Page,
    raw_table: list,
    rotated_map: dict,
    finder_table: Any,
) -> list:
    """
    [Fix 1] Pour chaque cellule, si elle est dans une zone de texte rotatif,
    remplacer son texte par la version correctement ordonnée.

    Utilise le bbox du finder_table pour la validation spatiale :
    - N'applique la correction que si la zone de texte rotatif CHEVAUCHE
      physiquement la cellule cible.
    - Requiert au moins 3 caractères de correspondance pour éviter les
      faux positifs (ex: "C" ne doit pas matcher "C230MTS").
    """
    if not rotated_map or finder_table is None:
        return raw_table

    fixed_table = []
    for row_idx, row in enumerate(raw_table):
        fixed_row = []
        for col_idx, cell in enumerate(row):
            if cell and isinstance(cell, str):
                cell_clean = re.sub(r'[^a-zA-Z0-9]', '', cell[::-1])
                if len(cell_clean) < 3:
                    fixed_row.append(cell)
                    continue

                cell_bbox = None
                try:
                    if (row_idx < len(finder_table.rows) and
                            col_idx < len(finder_table.rows[row_idx].cells)):
                        cell_bbox = finder_table.rows[row_idx].cells[col_idx]
                except (IndexError, AttributeError):
                    pass

                for (rx0, ry0, rx1, ry1), corrected in rotated_map.items():
                    if cell_bbox is not None:
                        cx0, cy0, cx1, cy1 = cell_bbox
                        overlap_x = max(0, min(rx1, cx1) - max(rx0, cx0))
                        overlap_y = max(0, min(ry1, cy1) - max(ry0, cy0))
                        if overlap_x <= 0 or overlap_y <= 0:
                            continue

                    corrected_clean = re.sub(r'[^a-zA-Z0-9]', '', corrected)
                    if corrected_clean == cell_clean or corrected_clean.startswith(cell_clean):
                        cell = corrected
                        break
            fixed_row.append(cell)
        fixed_table.append(fixed_row)

    return fixed_table


def _remove_trailing_footnotes(rows: list[list]) -> tuple[list[list], int]:
    """
    Supprime les lignes de footnotes en fin de tableau.
    Heuristique : lignes trailing consécutives dont la 1ère cellule = N.
    (ex: "1.", "2.", "6.") — motif typique des notes de bas de datasheet.
    Ne supprime qu'un bloc contigu à la fin, minimum 2 lignes pour éviter
    de toucher aux lignes de données légitimes (ex: "1." isolé = donnée).
    """
    if not rows:
        return rows, 0
    remove = 0
    for i in range(len(rows) - 1, -1, -1):
        first = str(rows[i][0]).strip() if rows[i] else ""
        if re.match(r'^\d+\.', first):  # fix: sans $ pour matcher "1.The pull-up" et "6.3.16"
            remove += 1
        else:
            break
    if remove < 2:
        return rows, 0
    cleaned = rows[:-remove]
    logger.info(f"_remove_trailing_footnotes: removed {remove} trailing footnote rows")
    return cleaned, remove


def _merge_empty_header_rightward(
    headers: list[str],
    rows: list[list[str]],
) -> tuple[list[str], list[list[str]], int]:
    """
    Fusionne les colonnes à header vide vers la colonne réelle de droite.
    Les colonnes à header vide entre deux colonnes réelles contiennent
    des fragments de texte (ex: "NRST i" dans l'espace Symbol↔Parameter).
    On les fusionne vers la droite pour reconstituer le texte complet.
    Processed right-to-left pour préserver les indices.
    """
    if not headers or not rows:
        return headers, rows, 0
    cols = len(headers)
    keep = [True] * cols
    merged_count = 0

    for c in range(cols - 1, -1, -1):
        if not keep[c]:
            continue
        h_c = headers[c].strip() if c < len(headers) else ""
        if h_c:
            continue
        # Trouver le voisin réel à droite
        right_real = None
        for t in range(c + 1, cols):
            if keep[t] and t < len(headers) and headers[t] and headers[t].strip():
                right_real = t
                break
        if right_real is None:
            continue
        # Fusionner : prepend les données de c dans right_real
        for ri in range(len(rows)):
            if c < len(rows[ri]) and rows[ri][c] and str(rows[ri][c]).strip():
                val = str(rows[ri][c]).strip()
                if right_real < len(rows[ri]):
                    target = rows[ri][right_real] or ""
                    if val != target:  # fix: evite duplication (ex: "Symbol"+"Symbol")
                        if target:
                            # Ajouter un espace si la jointure n'est pas un
                            # fragment de mot. Règle : si val finit par une
                            # lettre minuscule ET target commence par une
                            # lettre minuscule, c'est un fragment de mot
                            # (ex: "res"+"et"→"reset") → pas d'espace.
                            if val[-1].isalpha() and target[0].isalpha() and val[-1].islower() and target[0].islower():
                                rows[ri][right_real] = val + target
                            else:
                                rows[ri][right_real] = val + " " + target
                        else:
                            rows[ri][right_real] = val
        keep[c] = False
        merged_count += 1
        logger.info(
            f"_merge_empty_header_rightward: col {c} (h='') → col {right_real} "
            f"(h='{headers[right_real]}') [{merged_count}]"
        )

    new_headers = [h for i, h in enumerate(headers) if keep[i]]
    new_rows = [[cell for i, cell in enumerate(row) if keep[i]] for row in rows]
    if merged_count > 0:
        logger.info(
            f"_merge_empty_header_rightward: {cols} → {len(new_headers)} cols "
            f"({merged_count} merged)"
        )
    return new_headers, new_rows, merged_count


def extract_footnotes_from_pages(
    rows: list[list],
    headers: list[str],
    page_text_cache: dict[int, str],
    pages: list[int],
) -> list[str]:
    """Extrait les notes de bas de tableau (1. ..., 2. ...) correspondant aux
    marqueurs (N) dans les cellules ET les headers.
    Utilise page_text_cache (dict page_num->text) au lieu d'ouvrir le PDF.
    Retourne ['1. X = supported.', '2. Wake-up supported from Stop mode.', ...]."""
    markers: set[str] = set()
    for cell in headers:
        for m in re.finditer(r'\((\d+)\)', str(cell)):
            markers.add(m.group(1))
    for row in rows:
        for cell in row:
            for m in re.finditer(r'\((\d+)\)', str(cell)):
                markers.add(m.group(1))
    if not markers:
        return []

    scan_pages = set(pages)
    if pages:
        scan_pages.add(max(pages) + 1)

    notes: dict[str, str] = {}
    SECTION_RE = re.compile(r'^\d+(?:\.\d+){1,3}\s')
    for pg_num in scan_pages:
        text = page_text_cache.get(pg_num)
        if not text:
            continue
        lines = text.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            m = re.match(r'^(\d+)\.\s+(.+)', line)
            if m and m.group(1) in markers:
                num = m.group(1)
                note_parts = [m.group(2)]
                if i + 1 < len(lines):
                    nxt = lines[i + 1].strip()
                    if (nxt and not re.match(r'^\d+\.\s+', nxt)
                            and not SECTION_RE.match(nxt)
                            and not re.match(r'^Table\s+\d+', nxt, re.I)
                            and not re.match(r'^Figure\s+\d+', nxt, re.I)
                            and len(nxt) < 150):
                        note_parts.append(nxt)
                        i += 1
                full = " ".join(note_parts)
                if num not in notes:
                    notes[num] = f"{num}. {full}"
            i += 1

    return [notes[k] for k in sorted(notes, key=int)]


def extract_legend_from_page(
    caption: str,
    headers: list[str],
    page_text_cache: dict[int, str],
    pages: list[int],
) -> list[str]:
    """Extrait la légende : texte entre le titre du tableau (caption) et la
    première ligne d'en-têtes (headers). Ne contient jamais de numérotation.
    Retourne ['Specified by design...', 'Clocked by HSI...', ...]."""
    if not caption or not headers:
        return []

    # Extraire le numéro de table depuis le caption
    m_table = re.match(r'Table\s+(\d+)', caption, re.I)
    if not m_table:
        return []
    table_num = m_table.group(1)

    # Premier header pour repérer la limite basse
    first_header_words = headers[0].strip().split()[:2]
    if not first_header_words:
        return []

    legend_lines: list[str] = []
    NOTE_RE = re.compile(r'^\d+\.\s')
    SECTION_RE = re.compile(r'^\d+(?:\.\d+){1,3}\s')

    for pg_num in pages:
        text = page_text_cache.get(pg_num)
        if not text:
            continue
        lines = text.split("\n")
        # Trouver la ligne du caption "Table X."
        caption_idx = None
        for idx, line in enumerate(lines):
            if re.match(rf'^Table\s+{table_num}\b', line.strip(), re.I):
                caption_idx = idx
                break
        if caption_idx is None:
            continue

        # Trouver la ligne du premier header
        header_idx = None
        for idx in range(caption_idx + 1, len(lines)):
            line = lines[idx].strip()
            if all(w in line for w in first_header_words):
                header_idx = idx
                break

        if header_idx is None or header_idx <= caption_idx + 1:
            continue

        # Construire l'ensemble des mots significatifs des headers
        # pour détecter les fragments d'en-têtes multi-lignes
        header_words: set[str] = set()
        for h in headers:
            for w in h.split():
                if len(w) >= 2:
                    header_words.add(w.lower().strip("(),./"))

        # Tout ce qui est entre caption et header, sans numérotation
        for idx in range(caption_idx + 1, header_idx):
            line = lines[idx].strip()
            if not line:
                continue
            if NOTE_RE.match(line):
                continue
            if SECTION_RE.match(line):
                continue
            if re.match(r'^Table\s+\d+', line, re.I):
                continue
            if re.match(r'^Figure\s+\d+', line, re.I):
                continue

            # Filtre 1 : fragment d'en-tête — tous les mots apparaissent dans les headers
            # ex: "I3C I3C" (I3C est dans le header), "SPI2S1, SPI2S2, SPI2S3"
            words = line.split()
            sig_words = [w.lower().strip("(),./") for w in words if len(w) >= 2]
            if sig_words and all(w in header_words for w in sig_words):
                continue

            # Filtre 2 : titre court — ≤5 mots, tous commencent par majuscule
            # ex: "Max LDO", "Conditions", "HSE HCLK", "Level/", "Operating"
            if len(words) <= 5 and all(w[0].isupper() for w in words if w):
                continue

            # Filtre 3 : identifiants majuscules — tous les tokens sont des
            # identifiants techniques (AF0, I3C, HSE, HCLK, etc.)
            # ex: "AF0 AF1 AF2 AF3 AF4 AF5 AF6 AF7 AF8 AF9 AF10 AF11 AF12 AF13 AF14 AF15"
            if all(re.match(r'^[A-Z][A-Z0-9]*[,./()]?$', w.strip("(),./"))
                   for w in words if len(w) >= 2):
                continue

            legend_lines.append(line)

    return legend_lines


def extract_notes_type1(
    rows: list[list],
    caption: str,
    page_text_cache: dict[int, str],
    pages: list[int],
) -> list[str]:
    """Extrait les notes pour Type 1 sans marqueurs (N) dans les cellules.

    Deux stratégies :
    1. Cherche un heading 'Notes:' ou 'Note:' dans le texte de page,
       puis collecte les lignes N. ... consécutives en dessous.
    2. Fallback: lignes N. ... isolées en fin de page.

    Retourne ['1. ...', '2. ...'] ou [] si aucune note trouvée.
    """
    if not rows or not caption:
        return []

    scan_pages = set(pages)
    if pages:
        scan_pages.add(max(pages) + 1)

    notes: dict[str, str] = {}
    NOTES_HEADING_RE = re.compile(r'^Notes?\s*:?\s*$', re.I)
    NOTE_LINE_RE = re.compile(r'^(\d+)\.\s+(.+)')
    SECTION_RE = re.compile(r'^\d+(?:\.\d+){1,3}\s')

    for pg_num in sorted(scan_pages):
        text = page_text_cache.get(pg_num)
        if not text:
            continue
        lines = text.split("\n")

        # Stratégie 1: chercher "Notes:" heading
        found_heading = False
        for i, line in enumerate(lines):
            if NOTES_HEADING_RE.match(line.strip()):
                found_heading = True
                # Collecter les lignes N. ... après le heading
                j = i + 1
                while j < len(lines):
                    nxt = lines[j].strip()
                    m = NOTE_LINE_RE.match(nxt)
                    if m:
                        num = m.group(1)
                        note_parts = [m.group(2)]
                        if j + 1 < len(lines):
                            nxt2 = lines[j + 1].strip()
                            if (nxt2 and not NOTE_LINE_RE.match(nxt2)
                                    and not SECTION_RE.match(nxt2)
                                    and not re.match(r'^Table\s+\d+', nxt2, re.I)
                                    and not re.match(r'^Figure\s+\d+', nxt2, re.I)
                                    and len(nxt2) < 150):
                                note_parts.append(nxt2)
                                j += 1
                        full = " ".join(note_parts)
                        if num not in notes:
                            notes[num] = f"{num}. {full}"
                        j += 1
                    else:
                        break

        # Stratégie 2: lignes N. ... en fin de page (fallback)
        # Cherche dans les 20 dernières lignes de la page
        if not found_heading:
            start = max(0, len(lines) - 20)
            for i in range(start, len(lines)):
                line = lines[i].strip()
                m = NOTE_LINE_RE.match(line)
                if m and int(m.group(1)) <= 30:  # notes numérotées jusqu'à 30
                    num = m.group(1)
                    note_parts = [m.group(2)]
                    if i + 1 < len(lines):
                        nxt = lines[i + 1].strip()
                        if (nxt and not NOTE_LINE_RE.match(nxt)
                                and not SECTION_RE.match(nxt)
                                and not re.match(r'^Table\s+\d+', nxt, re.I)
                                and len(nxt) < 150):
                            note_parts.append(nxt)
                    full = " ".join(note_parts)
                    if num not in notes:
                        notes[num] = f"{num}. {full}"

    return [notes[k] for k in sorted(notes, key=int)]
