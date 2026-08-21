"""
config.py — seuils, chemins, flags globaux
Tous les paramètres ajustables ici, jamais en dur dans le code.

Types de PDF supportés :
- Type 1 (Acrobat) : PDFs générés par Adobe Acrobat, tables avec bordures
  nettes. Stratégie "lines" fonctionne bien.
- Type 2 (Antenna House) : PDFs générés par Antenna House (XML/XSL-FO),
  souvent sans bordures visibles mais avec fond coloré pour les headers.
  Tolérances plus larges (snap/join = 5 vs 3).
"""
from pathlib import Path

# ── Chemins ───────────────────────────────────────────────────────────────────
ROOT_DIR   = Path(__file__).parent
OUTPUT_DIR = ROOT_DIR.parent / "Output" / "Json" / "Raw_Extracted"
LOG_DIR    = ROOT_DIR / "logs"
RAG_DIR    = ROOT_DIR.parent / "Output" / "Json" / "Selective_Tables"

# ── Seuils qualité (déclenchent le fallback ou le flag) ───────────────────────
MIN_DATA_ROWS          = 1     # une seule ligne de données est valide (ex: Calibration values)
MAX_EMPTY_CELL_RATIO   = 0.50  # jusqu'à 50% de vide autorisé (ex: tables Pinout/Features très creuses)
MAX_COL_VARIANCE       = 0.40  # tolérance accrue pour les sous-lignes fusionnées (ex: "Master mode" au milieu d'une table)

# ── Continuation multi-pages ──────────────────────────────────────────────────
MAX_CONTINUATION_PAGES = 30    # sécurité anti-boucle infinie
MAX_CONT_COL_DRIFT    = 50    # seuil de dérive x0 (px) pour accepter la suite d'une table
                               # 50 : les fausses continuations (table différente) ont des drifts
                               # > 55px ; les vraies continuations (même table) ont des drifts < 31px.
                               # 200 était trop permissif (captait des tables adjacentes).

# ── Type 2 (Antenna House / XML-based) ────────────────────────────────────────
MIN_TABLE_WIDTH = 20      # en dessous = bandeau décoratif / marge → rejeter

# ── Debug / images ────────────────────────────────────────────────────────────
SAVE_DEBUG_IMAGES         = True   # crop image à côté du JSON
SAVE_IMAGES_ONLY_ON_ISSUE = True   # si True : image seulement si confidence != "high"
DEBUG_IMAGE_DPI           = 150    # résolution des crops (compromis taille/lisibilité)
DEBUG_EMPTY_ROWS          = True   # capture debug détaillée si la table a 0 lignes

# ── pdfplumber — réglages grille ──────────────────────────────────────────────
PDFPLUMBER_TABLE_SETTINGS = {
    "vertical_strategy":   "lines",
    "horizontal_strategy": "lines",
    "snap_tolerance":      3,
    "join_tolerance":      3,
    "edge_min_length":     3,
    "min_words_vertical":  3,
    "min_words_horizontal": 1,
    "intersection_tolerance": 3,
    "text_tolerance":      3,
}

PDFPLUMBER_TABLE_SETTINGS_FALLBACK = {
    # fallback interne pdfplumber : stratégie texte si "lines" donne rien
    "vertical_strategy":   "text",
    "horizontal_strategy": "text",
    "snap_tolerance":      3,
    "join_tolerance":      3,
    "text_tolerance":      3,
}

# ── pdfplumber — réglages Type 2 (Antenna House / XML) ───────────────────────
PDFPLUMBER_TABLE_SETTINGS_TYPE2 = {
    "vertical_strategy":      "lines",
    "horizontal_strategy":    "lines",
    "snap_tolerance":         5,
    "join_tolerance":         5,
    "edge_min_length":         5,
    "min_words_vertical":     3,
    "min_words_horizontal":   1,
    "intersection_tolerance": 5,
    "text_tolerance":         5,
}

PDFPLUMBER_TABLE_SETTINGS_FALLBACK_TYPE2 = {
    "vertical_strategy":   "text",
    "horizontal_strategy": "text",
    "snap_tolerance":      5,
    "join_tolerance":      5,
    "text_tolerance":      5,
}
