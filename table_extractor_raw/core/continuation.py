"""
continuation.py — Gestion des tables multi-pages.

Détecte la suite d'une table sur les pages suivantes en reconnaissant
les titres "Table X. ... (continued)", filtre les fausses pages de titre,
et adapte le nombre de colonnes quand une colonne se scinde en sous-colonnes.

Exporte : find_continuations(), _get_col_x0s()
"""
from __future__ import annotations
import logging
import re
from typing import Any, Optional

import pdfplumber
from pdfplumber.page import Page

from core.toc_detector import TableRef
from config import (
    PDFPLUMBER_TABLE_SETTINGS,
    PDFPLUMBER_TABLE_SETTINGS_TYPE2,
    PDFPLUMBER_TABLE_SETTINGS_FALLBACK,
    PDFPLUMBER_TABLE_SETTINGS_FALLBACK_TYPE2,
    MIN_TABLE_WIDTH,
    MAX_CONTINUATION_PAGES,
    MAX_CONT_COL_DRIFT,
)

logger = logging.getLogger(__name__)

_CONTINUED_RE = re.compile(
    r"(?:continued|cont['’]?d|cont\.|\(suite\))",
    re.IGNORECASE,
)
_HEADER_KEYWORDS = {
    "symbol", "parameter", "pin", "name", "peripheral",
    "features", "condition", "conditions", "min", "max",
    "unit", "typ", "value", "speed",
}


def _get_col_x0s(table_obj) -> list[float]:
    """
    Retourne les abscisses gauches (x0) médianes de chaque colonne,
    dans l'ordre des colonnes du finder table pdfplumber.
    """
    if not hasattr(table_obj, 'rows') or not table_obj.rows:
        return []
    maxc = max(len(r.cells) for r in table_obj.rows) if table_obj.rows else 0
    col_x0s: dict[int, list[float]] = {j: [] for j in range(maxc)}
    for r in table_obj.rows:
        for j, c in enumerate(r.cells):
            if c:
                col_x0s[j].append(round(c[0], 1))
    result = []
    for j in sorted(col_x0s.keys()):
        vals = col_x0s[j]
        if vals:
            vals.sort()
            result.append(vals[len(vals) // 2])
    return result


def _build_text_grid(
    words: list[dict],
    page: Page,
    current_table_num: str,
) -> Optional[list[list[str]]]:
    """Fallback texte quand pdfplumber ne détecte pas de lignes de tableau.

    1. Groupe les mots par position verticale (ligne).
    2. Saute la ligne "Table X ... (continued)".
    3. Saute les notes de bas de page (25% inférieurs).
    4. Détermine les colonnes par clustering des x0.
    5. Assigne chaque mot à sa colonne et retourne une grille.
    """
    # Clustering 1D avec tolérance 3px pour regrouper les mots d'une même ligne visuelle
    words_sorted = sorted(words, key=lambda w: w["top"])
    clusters: list[list[dict]] = []
    for w in words_sorted:
        if not clusters:
            clusters.append([w])
            continue
        last_cluster = clusters[-1]
        avg_top = sum(cw["top"] for cw in last_cluster) / len(last_cluster)
        if abs(w["top"] - avg_top) <= 3.0:
            last_cluster.append(w)
        else:
            clusters.append([w])

    data_lines: list[list[dict]] = []
    for line_words in clusters:
        y = sum(cw["top"] for cw in line_words) / len(line_words)
        line_words = sorted(line_words, key=lambda w: w["x0"])
        line_text = " ".join(w["text"] for w in line_words).lower()
        if current_table_num and f"table {current_table_num}" in line_text:
            if _CONTINUED_RE.search(line_text):
                continue
        if y > page.height * 0.75:
            continue
        data_lines.append(line_words)

    if len(data_lines) < 2:
        return None

    all_x0s = sorted({round(w["x0"], 0) for line in data_lines for w in line})
    cols: list[float] = []
    for x in all_x0s:
        if cols and abs(x - cols[-1]) < 12:
            continue
        cols.append(x)

    if len(cols) < 2:
        return None

    grid: list[list[str]] = []
    for line_words in data_lines:
        row = [""] * len(cols)
        for w in line_words:
            col_idx = min(range(len(cols)), key=lambda i: abs(w["x0"] - cols[i]))
            if row[col_idx]:
                row[col_idx] += " " + w["text"]
            else:
                row[col_idx] = w["text"]
        grid.append(row)

    return grid


def _pick_best_continuation(
    candidates: list[tuple[list[list], Any, str, Optional[list[float]]]],
    expected_col_count: int,
) -> Optional[tuple[list[list], Any, str, Optional[list[float]]]]:
    """Parmi les stratégies ayant réussi, choisir la meilleure.

    Ordre de préférence :
    1. lines (géométrique) si le nb de colonnes est raisonnable (diff ≤ 4)
    2. text (fallback texte pdfplumber)
    3. text_grid (grille construite depuis les mots)
    """
    if not candidates:
        return None
    _METHOD_RANK = {"lines": 0, "text": 1, "text_grid": 2}
    scored = []
    for table_data, top_ft, method, cont_x0s in candidates:
        col_count = max(len(r) for r in table_data) if table_data else 0
        col_diff = abs(col_count - expected_col_count)
        if method == "lines" and col_diff <= 4:
            if len(table_data) <= 1 and any(
                c[2] != "lines" and len(c[0]) > 1 for c in candidates
            ):
                rank = 3
            else:
                rank = 0
        else:
            rank = _METHOD_RANK.get(method, 9)
        scored.append((rank, col_diff, -len(table_data), table_data, top_ft, method, cont_x0s))
    scored.sort()
    _, _, _, table_data, top_ft, method, cont_x0s = scored[0]
    return table_data, top_ft, method, cont_x0s


def _remove_adjacent_duplicates(headers: list[str]) -> list[str]:
    result: list[str] = []
    for h in headers:
        h_norm = str(h).strip().lower()
        if not result or result[-1] != h_norm:
            result.append(h_norm)
    return result


def _headers_differ(
    cont_first_row: list | None,
    base_header: list[str] | None,
    threshold: float = 0.33,
) -> bool:
    if not base_header or not cont_first_row:
        return False
    # Normaliser les deux côtés : supprimer les doublons adjacents
    # (ex: ["Conditions","Conditions"] → ["Conditions"])
    # pour gérer les colonnes scindées avec le même en-tête.
    base_norm = set(_remove_adjacent_duplicates(base_header))
    cont_norm = set(_remove_adjacent_duplicates(cont_first_row))
    # Comparaison ensembliste (Jaccard) : intersection / union.
    # Plus robuste que l'alignement index par index : deux tables
    # différentes avec des colonnes en commun (ex: Symbol, Parameter)
    # mais des structures distinctes ne sont pas confondues.
    intersection = base_norm & cont_norm
    union = base_norm | cont_norm
    if len(union) < 2:
        return False
    dissimilarity = 1.0 - len(intersection) / len(union)
    if dissimilarity <= threshold:
        return False
    # Fallback : préfixe/substring — si chaque header de continuation
    # est contenu dans un header base, les tables sont structurellement
    # identiques malgré les abréviations (ex: "AF0" vs "AF0 / SYS_AF").
    if cont_norm and base_norm:
        cont_clean = {c for c in cont_norm if c.strip()}
        base_clean = {b for b in base_norm if b.strip()}
        matches = sum(1 for ch in cont_clean if any(ch in bh or bh in ch for bh in base_clean))
        ratio = matches / max(len(cont_clean), 1)
        return ratio < 0.5
    return True


def _is_continuation_page(
    page: Page,
    expected_col_count: int,
    current_table_id: str,
    pdf_type: int = 1,
    base_header: list[str] | None = None,
) -> tuple[bool, Optional[list], Optional[list[float]], Optional[Any], bool]:
    current_table_num = current_table_id.split("_")[1] if "_" in current_table_id else ""

    words = page.extract_words()
    page_text = " ".join(w["text"] for w in words).lower()

    # ── Détection "Table X ... (continued)" par regex ─────────────────────────
    has_continued_title = (
        current_table_num
        and f"table {current_table_num}" in page_text
        and bool(_CONTINUED_RE.search(page_text))
    )

    settings = PDFPLUMBER_TABLE_SETTINGS_TYPE2 if pdf_type == 2 else PDFPLUMBER_TABLE_SETTINGS
    fallback_settings = PDFPLUMBER_TABLE_SETTINGS_FALLBACK_TYPE2 if pdf_type == 2 else PDFPLUMBER_TABLE_SETTINGS_FALLBACK

    def _extract(settings_dict) -> tuple[Optional[list], Optional[Any]]:
        tt = page.extract_tables(settings_dict)
        ff = page.debug_tablefinder(settings_dict)
        if not tt or not ff.tables:
            return None, None
        if pdf_type == 2:
            filtered = [
                (t, ft) for t, ft in zip(tt, ff.tables)
                if ft.bbox[2] - ft.bbox[0] >= MIN_TABLE_WIDTH
            ]
            if filtered:
                tt = [t for t, ft in filtered]
                ff.tables = [ft for t, ft in filtered]
        candidates_list = [(t, ft) for t, ft in zip(tt, ff.tables) if t and len(t) >= 1]
        if not candidates_list:
            return None, None
        top_table, top_ft = min(candidates_list, key=lambda x: x[1].bbox[1])
        return top_table, top_ft

    # ── Collecter tous les résultats valides ──────────────────────────────────
    good: list[tuple[list[list], Any, str, Optional[list[float]]]] = []

    # Essai 1 : settings principaux (stratégie lignes)
    top_table, top_ft = _extract(settings)
    if top_table and top_ft:
        good.append((top_table, top_ft, "lines", _get_col_x0s(top_ft)))

    # Essai 2 : settings fallback (stratégie texte)
    top_table2, top_ft2 = _extract(fallback_settings)
    if top_table2 and top_ft2:
        good.append((top_table2, top_ft2, "text", _get_col_x0s(top_ft2)))

    # Essai 3 : grille construite depuis les mots (fallback ultime)
    if has_continued_title:
        text_grid = _build_text_grid(words, page, current_table_num)
        if text_grid:
            good.append((text_grid, None, "text_grid", None))

    # Essai 4 : fallback texte-grille même sans "(continued)"
    if not good and len(words) > 10:
        text_grid = _build_text_grid(words, page, current_table_num)
        if text_grid:
            good.append((text_grid, None, "text_grid", None))

    if not good:
        return False, None, None, None, has_continued_title

    # ── Si "(continued)" détecté : pivoter ────────────────────────────────────
    if has_continued_title:
        best = _pick_best_continuation(good, expected_col_count)
        if best:
            table_data, ft, method, x0s = best
            col_count = max(len(r) for r in table_data) if table_data else 0
            if col_count < max(2, expected_col_count // 2):
                logger.info(
                    f"  continuation page {page.page_number}: col_count={col_count} "
                    f"too low (expected ~{expected_col_count}), skipping"
                )
                return False, None, None, None, True
            logger.info(
                f"  continuation page {page.page_number}: {method} strategy, "
                f"{len(table_data)} rows, {col_count} cols"
            )
            return True, table_data, x0s, ft, True

    # ── Sans "(continued)" : heuristique de position ──────────────────────────
    # Prendre le premier résultat des stratégies lignes/texte
    if not good:
        return False, None, None, None, False
    table_data, top_ft, method, x0s = good[0]

    if top_ft is not None and top_ft.bbox[1] > page.height * 0.75:
        return False, None, None, None, False

    for w in words:
        if "Table" in w["text"] and w["top"] < top_ft.bbox[1]:
            line_words = [ow for ow in words if abs(ow["top"] - w["top"]) < 3]
            line_x0s = [ow["x0"] for ow in line_words]
            # Ignorer les cross-references : "Table N" au milieu d'une phrase
            # (mot précédé d'autres mots sur la même ligne, x0 > min de la ligne)
            min_x0 = min(line_x0s)
            if w["x0"] > min_x0 + 3:
                continue
            line_text = " ".join(ow["text"] for ow in line_words).lower()
            if current_table_num and f"table {current_table_num}" in line_text:
                pass  # table courante trouvée → continuer
            elif current_table_num:
                nums = re.findall(r'table\s*(\d+)', line_text)
                if nums and nums[0] != current_table_num:
                    logger.info(
                        f"  page {page.page_number}: found \"{line_text.strip()}\" "
                        f"(table {nums[0]} != current {current_table_num}), "
                        f"will truncate downstream"
                    )
                    continue

    col_count = max(len(r) for r in table_data) if table_data else 0
    if abs(col_count - expected_col_count) > 2:
        # pdfplumber fusionne parfois des colonnes identiques adjacentes
        # dans la page de continuation (ex: 6× "Conditions" → 1× "Conditions").
        # Si les ensembles d'en-têtes (après dédup) sont identiques, la
        # table est bien une continuation — accepter malgré le diff.
        if base_header and table_data:
            base_set = set(_remove_adjacent_duplicates(base_header))
            cont_set = set(_remove_adjacent_duplicates(table_data[0]))
            if base_set == cont_set:
                logger.info(
                    f"  page {page.page_number}: col_count={col_count} vs "
                    f"expected={expected_col_count}, but headers match, accepting"
                )
            else:
                return False, None, None, None, False
        else:
            return False, None, None, None, False

    # ── Vérification du contenu de l'en-tête ───────────────────────────────
    # Si la page N+1 a un en-tête différent de la table de base, c'est que
    # la table trouvée n'est pas la continuation mais une table différente.
    if base_header and table_data:
        from core.grid_extractor import _is_likely_reversed
        header_row = table_data[0]
        if header_row:
            # Essai 1 : renversement cellule par cellule (comportement actuel)
            candidate = [
                str(c)[::-1] if c and _is_likely_reversed(str(c)) else (str(c) if c is not None else "")
                for c in header_row
            ]
            # Essai 2 : si ça ne matche pas, essayer le renversement complet
            # de toutes les cellules (ex: noms de device tout-en-majuscules
            # inversés que _is_likely_reversed ne détecte pas)
            if _headers_differ(candidate, base_header):
                full_rev = [
                    str(c)[::-1] if isinstance(c, str) and len(c) >= 3 else (str(c) if c is not None else "")
                    for c in header_row
                ]
                if not _headers_differ(full_rev, base_header):
                    candidate = full_rev
            header_row = candidate
        if _headers_differ(header_row, base_header):
            logger.info(
                f"  page {page.page_number}: header mismatch with base table, "
                f"rejecting continuation"
            )
            return False, None, None, None, False

    return True, table_data, x0s, top_ft, False


def _get_header_spans(base_header: list[str]) -> list[int]:
    run_lengths: list[int] = []
    i = 0
    while i < len(base_header):
        j = i
        while j < len(base_header) and base_header[j] == base_header[i]:
            j += 1
        run_lengths.append(j - i)
        i = j
    return run_lengths


def _expand_cont_row_by_header_spans(
    row: list,
    target_cols: int,
    base_header: list[str],
) -> list:
    spans = _get_header_spans(base_header)
    if len(row) != len(spans):
        return _expand_cont_row(row, target_cols)
    result: list[str] = []
    for i, val in enumerate(row):
        s = spans[i] if i < len(spans) else 1
        if val is None:
            result.extend([""] * s)
        else:
            result.extend([str(val)] * s)
    return result[:target_cols]


def _expand_cont_row(row: list, expected_col_count: int) -> list:
    """
    Expands a row from a continuation page that has fewer physical columns
    (due to merged/spanned cells) to match the expected column count.

    Strategy: distribute None/empty cells after each real value so that the
    total reaches expected_col_count. Real values with None neighbours are
    assumed to span all remaining columns uniformly.

    Example: ['Bootloader', 'USART1, I2C1', None, None, None] (5 cols physical)
             → ['Bootloader', 'Bootloader', 'USART1, I2C1', 'USART1, I2C1', ...]
               padded out to 10 cols.
    """
    if len(row) >= expected_col_count:
        return row  # Already the right size

    # Count real (non-None) segments and None spans
    # Simple approach: repeat each real value to fill the gap
    real_values = []
    for cell in row:
        if cell is not None:
            real_values.append(str(cell))
        # None = merged from left → will be filled by propagation later

    if not real_values:
        return [""] * expected_col_count

    # Distribute real values as evenly as possible across expected_col_count
    result = []
    slots_per_value = expected_col_count // len(real_values)
    remainder = expected_col_count % len(real_values)

    for i, val in enumerate(real_values):
        count = slots_per_value + (1 if i < remainder else 0)
        result.extend([val] * count)

    return result[:expected_col_count]


def _expand_cont_row_by_x0s(
    row: list,
    expected_col_count: int,
    base_x0s: list[float],
    cont_x0s: list[float],
) -> list:
    """
    Expand a row by matching continuation column x0s to base column x0s.
    When pdfplumber merges adjacent identical columns (e.g. 6× "Conditions"
    → 1× "Conditions") in the continuation, the merged column's x0 range
    covers multiple base column x0s. This function detects such merges and
    duplicates the value across the correct number of base columns.

    Example:
      base_x0s = [42, 98, 185, 215, 245, 275, 305, 335, 420, 450, 480, 510]
      cont_x0s = [42, 98, 185, 420, 450, 480, 510]
      → span counts: [1, 1, 6, 1, 1, 1, 1]
      → row len 7 → expanded to 12
    """
    if len(row) >= expected_col_count:
        return row

    next_cx0s = list(cont_x0s[1:]) + [float("inf")]
    span_counts: list[int] = []
    for cx0, nxt in zip(cont_x0s, next_cx0s):
        count = sum(1 for bx0 in base_x0s if cx0 - 1 <= bx0 < nxt)
        span_counts.append(max(count, 1))

    result: list[str] = []
    for ci, count in enumerate(span_counts):
        val = str(row[ci]) if ci < len(row) and row[ci] is not None else ""
        result.extend([val] * count)

    return result[:expected_col_count]


def _reduce_cont_row(row: list, expected_col_count: int) -> list:
    """
    Reduce a row to expected_col_count by dropping columns that are likely
    pdfplumber-injected separators (mostly None/empty).

    Strategy: try all combinations of columns to drop; pick the one that
    preserves the most real values, then favours dropping None/empty
    columns (no data loss), then columns near the center (separator
        heuristic), then rightmost columns.

    ATTENTION : O(C(n,k)) — pour une ligne de 20 colonnes avec n_drop=10,
    cela évalue C(20,10) = 184 756 combinaisons. Appels répétés par ligne
    de continuation. Monitorer si ralentissement sur tables larges.
    """
    from itertools import combinations

    n_drop = len(row) - expected_col_count
    center = (len(row) - 1) / 2

    best_criteria = None
    best_row = None

    for drop_set in combinations(range(len(row)), n_drop):
        kept = [row[i] for i in range(len(row)) if i not in drop_set]
        real_count = sum(
            1 for c in kept if c is not None and str(c).strip() != ""
        )
        none_dropped = sum(
            1
            for i in drop_set
            if row[i] is None or str(row[i]).strip() == ""
        )
        avg_dist = sum(abs(i - center) for i in drop_set) / n_drop

        crit = (real_count, none_dropped, -avg_dist, sum(drop_set))
        if best_criteria is None or crit > best_criteria:
            best_criteria = crit
            best_row = kept

    return best_row


def _min_drift(cols_a: list[float], cols_b: list[float]) -> float:
    """
    Calcule le drift minimum entre deux listes de x0 de colonnes en essayant
    d'ignorer toute position dans la plus longue liste. Gère les colonnes
    fantômes à n'importe quelle position (début, milieu ou fin).

    Ex: base=[42.5,185.1,233.2,...] cont=[42.5,116.7,185.1,233.2,...]
        skip position 1 (116.7) → base aligné avec cont privé de l'index 1
        → drift=0.0
    """
    shorter, longer = sorted([cols_a, cols_b], key=len)
    n_diff = len(longer) - len(shorter)
    if n_diff == 0:
        return max(abs(longer[i] - shorter[i]) for i in range(len(shorter)))
    best = float("inf")
    for skip in range(len(longer)):
        aligned = list(longer[:skip]) + list(longer[skip+1:])
        if len(aligned) != len(shorter):
            continue
        drift = max(abs(aligned[i] - shorter[i]) for i in range(len(shorter)))
        if drift < best:
            best = drift
    return best


def _find_other_table_titles(
    page: Page,
    current_table_num: str,
) -> list[tuple[float, str]]:
    words = page.extract_words()
    lines: dict[float, list[dict]] = {}
    for w in words:
        key = round(w["top"], 0)
        lines.setdefault(key, []).append(w)
    results: list[tuple[float, str]] = []
    for y, line in lines.items():
        sorted_line = sorted(line, key=lambda w: w["x0"])
        line_text = " ".join(w["text"] for w in sorted_line).lower()
        m = re.search(r'table\s+(\d+)\s*\.', line_text)
        if m and m.group(1) != current_table_num:
            min_x0 = sorted_line[0]["x0"]
            table_word = next((w for w in sorted_line if "table" in w["text"].lower()), None)
            if table_word and table_word["x0"] > min_x0 + 3:
                continue
            results.append((y, line_text.strip()))
    results.sort(key=lambda x: x[0])
    return results

def find_continuations(
    pdf: pdfplumber.PDF,
    start_page_num: int,
    expected_col_count: int,
    all_refs: list[TableRef],
    current_table_id: str = "",
    header_depth: int = 1,
    first_cell_text: str = "",
    max_pages: int = MAX_CONTINUATION_PAGES,
    pdf_type: int = 1,
    base_col_x0s: list[float] | None = None,
    base_header: list[str] | None = None,
    base_bbox_bottom: float | None = None,
) -> tuple[list[int], list[list[str]], int, list[list[float]]]:
    """
    Cherche les pages suivantes contenant la suite de la table.

    Stratégie :
    1. Pour chaque page suivante, vérifier si c'est une continuation via
       _is_continuation_page (titre "Table X (continued)" ou position en haut)
    2. Vérifier la dérive géométrique des colonnes (drift des x0)
    3. Réduire/étendre les lignes de continuation au nombre de colonnes cible
    4. Arrêter si : page de la table suivante atteinte, ou max_pages, ou
       colonnes structurellement différentes

    Retourne (pages_fusionnees, lignes_supplementaires, target_cols, all_col_x0s, b7_triggered).
    target_cols = max(expected_col_count, max cols trouvées dans les continuations).
    """
    merged_pages = [start_page_num]
    all_data_rows: list[list] = []
    all_col_x0s: list[list[float]] = []
    max_cont_cols = 0

    current_page = start_page_num + 1

    # Trouver la prochaine légende de table différente (limite de scan)
    # Inclure les tables sur la même page que la table courante pour éviter
    # de continuer au-delà d'une table distincte (ex: Table 27 + Table 28 page 69).
    next_refs = [r for r in all_refs if r.page > start_page_num and r.table_id != current_table_id]
    next_ref = min(next_refs, key=lambda r: r.page) if next_refs else None
    next_table_page = next_ref.page if next_ref else float('inf')

    cont_header_rows: list[list[str]] = []
    while current_page <= len(pdf.pages) and len(merged_pages) < max_pages:
        # Ne pas dépasser la page de la table suivante
        if current_page > next_table_page:
            break

        # Vérifier si une table différente commence en haut de page
        # (ex: page 91 a "Table 66" en haut → skip, page 88 a "Table 51"
        # en bas après les données continuation → accept)
        # Type 2 : pas de marqueur (continued), on laisse _is_continuation_page
        # décider via la structure des colonnes.
        if pdf_type != 2:
            cur_nums = re.findall(r'\d+', current_table_id)
            cur_num_str = cur_nums[0] if cur_nums else ""
            if cur_num_str:
                pg = pdf.pages[current_page - 1]
                words = pg.extract_words()
                top30 = sorted(words, key=lambda w: w["top"])[:30]
                top_text = " ".join(w["text"] for w in top30).lower()
                if _CONTINUED_RE.search(top_text) and f"table {cur_num_str}" in top_text:
                    pass  # "(continued)" présent → accepter
                else:
                    # Ne considérer que les vrais titres "Table N." (avec point)
                    # pour éviter les cross-references ("see Table 2 for details").
                    # Vérifier aussi que le titre est dans les 100 premiers pts
                    # pour ignorer les tables qui commencent plus bas sur la page.
                    all_in_top = re.findall(r'table\s*(\d+)\s*\.', top_text)
                    top_nums = set(all_in_top)
                    if top_nums and cur_num_str not in top_nums:
                        title_at_top = any(
                            re.match(r'table\s+\d+\s*\.', w["text"].lower())
                            and w["top"] < 100
                            for w in top30
                        )
                        if title_at_top:
                            logger.info(
                                f"  -> page {current_page}: different table(s) {top_nums} at top "
                                f"(no Table {cur_num_str}), skipping continuation"
                            )
                            break

        is_cont, table_data, cont_x0s, top_ft, has_title = _is_continuation_page(
            pdf.pages[current_page - 1], expected_col_count, current_table_id,
            pdf_type, base_header=base_header,
        )
        if not is_cont:
            break

        # ── Capturer les positions Y de chaque ligne ────────────────────────────
        row_ys: list[float] = []
        if top_ft is not None and hasattr(top_ft, 'rows'):
            for i in range(min(len(table_data), len(top_ft.rows))):
                row_ys.append(top_ft.rows[i].bbox[1])
        # ── Vérification de la dérive des colonnes ─────────────────────────────
        # Ignorée quand le titre "(continued)" est présent sur la page : le titre
        # est une preuve suffisante que c'est la même table, même si la géométrie
        # des colonnes diffère (ex : lignes de bordures manquantes en continuation
        # → pdfplumber détecte moins de colonnes, mais _expand_spans_and_headers
        # les reconstituera à partir de la bbox des cellules fusionnées).
        if not has_title and base_col_x0s and cont_x0s and len(base_col_x0s) >= 2:
            if abs(len(cont_x0s) - len(base_col_x0s)) <= 1:
                drift = _min_drift(cont_x0s, base_col_x0s)
            else:
                drift = float("inf")

            if drift > MAX_CONT_COL_DRIFT:
                # Si les en-têtes correspondent, le drift géométrique peut être
                # causé par des colonnes scindées/fusionnées (ex: "Conditions"
                # dédoublé). Tolérer le drift quand les en-têtes matchent.
                headers_match = (
                    base_header is not None and table_data is not None
                    and table_data
                    and not _headers_differ(table_data[0], base_header, threshold=0.5)
                )
                if not headers_match:
                    logger.warning(
                        f"  -> page {current_page}: column drift {drift:.1f}px "
                        f"({len(cont_x0s)} cols vs {len(base_col_x0s)} base), skipping"
                    )
                    break

        merged_pages.append(current_page)
        logger.info(f"    -> found continuation on page {current_page} ({len(table_data)} rows)")

        # ── Détection vectorielle des dashs sur la page de continuation ──────
        from core.grid_extractor import _detect_vector_dashes
        table_data = _detect_vector_dashes(table_data, top_ft, pdf.pages[current_page - 1])

        # Collecter les x0s des colonnes de cette page de continuation
        if cont_x0s:
            all_col_x0s.append(cont_x0s)

        # ── Supprimer l'en-tête répété ────────────────────────────────────────
        if len(table_data) > 0:
            cont_header_rows.append([str(c) if c is not None else "" for c in table_data[0]])
        if table_data:
            skip = 0
            if base_header:
                dedup_base = set(_remove_adjacent_duplicates(base_header))
                for row in table_data:
                    row_norm = set(_remove_adjacent_duplicates([str(c or "").lower() for c in row]))
                    intersection = dedup_base & row_norm
                    union = dedup_base | row_norm
                    if len(union) > 0 and (len(intersection) / len(union) >= 0.5):
                        skip += 1
                    elif len(row_norm) > 0 and sum(1 for ch in row_norm if any(ch in bh or bh in ch for bh in dedup_base)) / len(row_norm) >= 0.5:
                        skip += 1
                    else:
                        break
            
            # Fallback si base_header non fourni ou ne matche pas
            if skip == 0:
                row0_cell0 = str(table_data[0][0] or "").strip()
                if row0_cell0 and first_cell_text and row0_cell0 == first_cell_text:
                    for row in table_data:
                        c0 = str(row[0] or "").strip()
                        if c0 == first_cell_text or c0 == "":
                            skip += 1
                        else:
                            break
                else:
                    row0 = [str(c or "").lower() for c in table_data[0]]
                    if any(any(kw in cell for kw in _HEADER_KEYWORDS) for cell in row0):
                        skip = 1

            data_rows = table_data[skip:]
        else:
            data_rows = []

        if data_rows:
            # Supprimer les lignes parasites : même 1ère cellule que la ligne
            # précédente ET toutes les autres cellules vides/identiques.
            cleaned = []
            for row in data_rows:
                if cleaned and row and cleaned[-1] and row[0] == cleaned[-1][0]:
                    is_dup = True
                    for j in range(1, len(row)):
                        if j < len(cleaned[-1]) and str(row[j] or "").strip() and str(row[j] or "").strip() != str(cleaned[-1][j] or "").strip():
                            is_dup = False
                            break
                    if is_dup:
                        logger.info(f"  skipped duplicate row: {[str(c)[:20] for c in row]}")
                        continue
                cleaned.append(row)
            data_rows = cleaned

            # ── [Fix EOT] Détection fin de table : titre "Table X." dans les données ──
            if data_rows and not has_title:
                pg = pdf.pages[current_page - 1]
                cur_num_str = re.findall(r'\d+', current_table_id)[0] if re.findall(r'\d+', current_table_id) else ""
                cutoff_y: float | None = None
                cutoff_reason = ""
                if cur_num_str and row_ys:
                    titles = _find_other_table_titles(pg, cur_num_str)
                    data_min_y = min(row_ys)
                    data_max_y = max(row_ys)
                    for title_y, title_text in titles:
                        if title_y <= data_max_y:
                            cutoff_y = title_y - 3
                            cutoff_reason = f"Table title at y={title_y:.0f}"
                            logger.info(f"  -> page {current_page}: EOT table title y={title_y:.0f}")
                            break
                    if cutoff_y is None:
                        logger.info(f"  -> page {current_page}: EOT no table title in data")
                # Appliquer la troncature
                if cutoff_y is not None and row_ys:
                    data_ys = row_ys[-len(data_rows):] if len(row_ys) >= len(data_rows) else []
                    if len(data_ys) == len(data_rows):
                        tronq = []
                        for ri, row in enumerate(data_rows):
                            if ri < len(data_ys) and data_ys[ri] > cutoff_y:
                                logger.info(f"  -> page {current_page}: truncated row {ri} y={data_ys[ri]:.0f}>{cutoff_y:.0f} ({cutoff_reason})")
                                break
                            tronq.append(row)
                        if len(tronq) < len(data_rows):
                            data_rows = tronq
                    else:
                        logger.info(f"  -> page {current_page}: EOT row_ys mismatch {len(data_ys)} vs {len(data_rows)} (total row_ys={len(row_ys)})")
                elif cutoff_y is not None:
                    logger.info(f"  -> page {current_page}: EOT cutoff_y={cutoff_y:.0f} but row_ys empty")
                elif row_ys:
                    logger.info(f"  -> page {current_page}: EOT no cutoff but row_ys={len(row_ys)}")

            # Tronquer dès qu'une table suivante apparaît dans les données
            # (ex: page 91 a "Table 66. SPI characteristics" en première ligne)
            cur_num = int(re.findall(r'\d+', current_table_id)[0]) if re.findall(r'\d+', current_table_id) else 0
            truncated = []
            for row in data_rows:
                text = "".join(str(c or "") for c in row)
                m = re.search(r'\b[Tt]able\s*(\d+)', text)
                if m and int(m.group(1)) != cur_num:
                    logger.info(f"  -> page {current_page}: truncating at row {len(truncated)} (Table {m.group(1)})")
                    break
                truncated.append(row)
            data_rows = truncated

            if data_rows:
                for row in data_rows:
                    max_cont_cols = max(max_cont_cols, len(row))
                all_data_rows.extend(data_rows)
            else:
                merged_pages.pop()
                logger.info(f"  -> page {current_page}: all rows truncated, removed from merged_pages")

        current_page += 1

    # ── Traiter toutes les lignes collectées avec le même target_cols ────
    target_cols = max(expected_col_count, min(max_cont_cols, expected_col_count + 1))
    # Si la continuation a plus de colonnes que la base, vérifier si c'est dû
    # à une colonne dupliquée dans l'en-tête continuation (ex: "Conditions"
    # en double dans continuation mais pas dans base). Dans ce cas, utiliser
    # expected_col_count comme cible plutôt que d'insérer une colonne vide.
    if target_cols > expected_col_count and base_header and cont_header_rows:
        last_cont_header = cont_header_rows[-1]
        # Si la continuation a une colonne de plus que la base et que cette
        # colonne supplémentaire est "None" (spanning artifact), ignorer.
        # Ne pas ignorer si la colonne contient des données réelles dans les
        # lignes de continuation (c'est un spanning scindé, ex: "Peripherals"
        # scindé en 2 colonnes par un bord visuel page N mais pas page 1).
        if len(last_cont_header) == expected_col_count + 1:
            none_positions = [i for i, c in enumerate(last_cont_header) if str(c).strip().lower() in ("none", "", "")]
            base_without_none = list(last_cont_header)
            for pos in sorted(none_positions, reverse=True):
                if pos < len(base_without_none):
                    base_without_none.pop(pos)
            if len(base_without_none) == expected_col_count:
                has_real_data = any(
                    all_data_rows and pos < len(all_data_rows[r]) and str(all_data_rows[r][pos] or "").strip()
                    for pos in none_positions
                    for r in range(len(all_data_rows))
                )
                if has_real_data:
                    logger.info(
                        f"  cont has {max_cont_cols} cols vs base {expected_col_count}: "
                        f"None column at {none_positions} has data, keeping target={target_cols}"
                    )
                else:
                    logger.info(
                        f"  cont has {max_cont_cols} cols vs base {expected_col_count}: "
                        f"extra None column detected, capping target to {expected_col_count}"
                    )
                    target_cols = expected_col_count

    # ── [Fix B7] Alignement continuation quand base_header a des colonnes dupliquées ──
    # Ex: base [Symbol, Symbol, Parameter,...] (8 cols) mais continuation
    #     [Symbol, Parameter,...] (7 cols, Symbol unique). Sans ce fix,
    #     les lignes continuation sont réduites 8→7 en droppant la mauvaise
    #     colonne → décalage de 1 colonne vers la droite.
    needs_dup_expand = False
    b7_triggered = False
    dup_insert_positions: list[int] = []
    cont_header_norm: list[str] = []
    if base_header and cont_header_rows and all_data_rows and len(all_data_rows[0]) < target_cols <= len(base_header):
        b7_triggered = True
        dedup_base = _remove_adjacent_duplicates(base_header)
        cont_header_norm = [str(c).strip().lower() for c in cont_header_rows[-1][:len(dedup_base)]]
        if dedup_base == cont_header_norm:
            needs_dup_expand = True
            target_cols = len(base_header)
            for i in range(1, len(base_header)):
                if base_header[i] == base_header[i-1]:
                    dup_insert_positions.append(i)
            logger.info(
                f"  continuation dup-expand: base={len(base_header)} dedup={len(dedup_base)} "
                f"insert_positions={dup_insert_positions}"
            )

    extra_rows = []
    for row in all_data_rows:
        if needs_dup_expand:
            missing = target_cols - len(row)
            row = list(row) + [""] * missing
        elif len(row) < target_cols:
            if base_header and len(_get_header_spans(base_header)) == len(row):
                row = _expand_cont_row_by_header_spans(row, target_cols, base_header)
            elif base_col_x0s and all_col_x0s:
                row = _expand_cont_row_by_x0s(row, target_cols, base_col_x0s, all_col_x0s[0])
            else:
                row = _expand_cont_row(row, target_cols)
        elif len(row) > target_cols:
            row = _reduce_cont_row(row, target_cols)
        extra_rows.append(row)

    return merged_pages, extra_rows, target_cols, all_col_x0s, b7_triggered
