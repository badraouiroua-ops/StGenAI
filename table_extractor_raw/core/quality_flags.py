"""
quality_flags.py — Évaluation de la confiance d'extraction d'une table brute.

Métriques : ratio cellules vides, variance colonnes, nombre de lignes,
détection de fusions verticales suspectes.

Sortie : confidence ("high"/"medium"/"low"/"failed") + warnings textuels.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import MAX_EMPTY_CELL_RATIO, MAX_COL_VARIANCE, MIN_DATA_ROWS


def compute_empty_cell_ratio(rows: list[list[str]]) -> float:
    """Proportion de cellules vides (string vide ou None) dans les rows."""
    if not rows:
        return 1.0
    total = sum(len(row) for row in rows)
    if total == 0:
        return 1.0
    empty = sum(1 for row in rows for cell in row if not cell or not cell.strip())
    return empty / total


def compute_col_variance(headers: list[str], rows: list[list[str]]) -> float:
    """
    Variance normalisée du nombre de colonnes.
    0 = parfaitement homogène, 1 = très incohérent.
    """
    expected = len(headers) if headers else 0
    if expected == 0:
        return 1.0
    col_counts = [len(row) for row in rows] if rows else []
    if not col_counts:
        return 0.0
    deviations = [abs(c - expected) / expected for c in col_counts]
    return sum(deviations) / len(deviations)


def evaluate_table(
    headers: list[str],
    rows: list[list[str]],
    cid_detected: bool = False,
) -> tuple[str, float, float, list[str]]:
    """
    Évalue la qualité d'une table extraite.

    Retourne:
        confidence : "high" | "medium" | "low" | "failed"
        empty_ratio: float
        col_var    : float
        warnings   : list[str]
    """
    warnings: list[str] = []

    # ── Cas échec total ────────────────────────────────────────────────────────
    if not headers and not rows:
        return "failed", 1.0, 1.0, ["no_content_extracted"]

    # ── Métriques ──────────────────────────────────────────────────────────────
    empty_ratio = compute_empty_cell_ratio(rows)
    col_var     = compute_col_variance(headers, rows)
    n_rows      = len(rows)

    # ── Collecte des warnings ──────────────────────────────────────────────────
    if not headers:
        warnings.append("no_headers_detected")

    if n_rows < MIN_DATA_ROWS:
        warnings.append(f"few_data_rows:{n_rows}")

    if empty_ratio > MAX_EMPTY_CELL_RATIO:
        warnings.append(f"high_empty_ratio:{empty_ratio:.2f}")

    if col_var > MAX_COL_VARIANCE:
        warnings.append(f"inconsistent_col_count:variance={col_var:.2f}")

    # Heuristique : si la ligne de header ressemble à des données (toutes courtes)
    if headers and all(len(h) <= 3 for h in headers if h):
        warnings.append("header_row_ambiguous")

    # Heuristique : beaucoup de cellules identiques dans une colonne → fusion verticale probable
    if rows and len(rows) >= 3:
        for col_idx in range(len(rows[0])):
            col_vals = [row[col_idx] for row in rows
                        if col_idx < len(row) and row[col_idx].strip()]
            if len(col_vals) >= 3:
                unique_ratio = len(set(col_vals)) / len(col_vals)
                if unique_ratio < 0.25:  # 75%+ de répétitions dans la colonne
                    warnings.append("vertical_merge_suspected")
                    break

    # Glyphes CID non décodables (police sans ToUnicode) détectés avant nettoyage
    if cid_detected:
        warnings.append("unmapped_cid_glyphs_detected")

    # ── Calcul de la confiance ─────────────────────────────────────────────────
    # Logique de décision (par ordre de priorité) :
    # 1. "failed" : aucun contenu extrait
    # 2. "low" : pas de headers OU empty_ratio > MAX OU variance colonnes > MAX
    # 3. "medium" : empty_ratio modéré (> 60% du max) OU peu de lignes de données
    # 4. "high" : tous les seuils respectés
    #
    # Note : le "has_empty_cells" dans grid_extractor.py est un indicateur
    # séparé (marqueur pour le rapport) — il n'influence PAS la confiance.
    # Un table avec des cellules vides peut être "high" si Fix 8 a tout rempli.
    critical = {"no_content_extracted", "no_headers_detected"}
    if any(w in critical for w in warnings):
        confidence = "failed" if "no_content_extracted" in warnings else "low"
    elif empty_ratio > MAX_EMPTY_CELL_RATIO or col_var > MAX_COL_VARIANCE:
        confidence = "low"
    elif empty_ratio > MAX_EMPTY_CELL_RATIO * 0.6 or n_rows < MIN_DATA_ROWS:
        confidence = "medium"
    else:
        confidence = "high"

    return confidence, empty_ratio, col_var, warnings
