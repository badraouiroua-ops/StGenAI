"""
glyph_fixer.py — Correction des glyphes mal encodés dans les PDFs STM32.

Principe : table de correspondance pure, zéro logique conditionnelle par
type de table. S'applique uniformément sur tous les textes (headers + rows).

La table est construite empiriquement sur le corpus réel. Compléter au fur
et à mesure des cas observés.
"""
from __future__ import annotations
import re
import unicodedata

# ── Table de correspondance glyphe → Unicode ──────────────────────────────────
# Clés : caractères ou séquences qui apparaissent mal encodés dans les PDFs STM32
# Valeurs : leur équivalent Unicode correct
GLYPH_MAP: dict[str, str] = {
    # Unités courantes mal encodées
    "\uf06d":  "µ",   # mu (micro) - police Symbol/custom STM32
    "\uf0b5":  "µ",   # variante mu
    "\u00b5":  "µ",   # MICRO SIGN → GREEK SMALL LETTER MU (normalisation)
    "\uf057":  "Ω",   # Omega (ohm) - police custom
    "\uf0b0":  "°",   # degré
    "\u00b0":  "°",   # degré (déjà correct mais normalisé)
    "\uf0b2":  "²",   # exposant 2
    "\uf0b3":  "³",   # exposant 3
    "\uf032":  "²",   # variante exposant 2
    "\uf033":  "³",   # variante exposant 3
    # Tirets / espaces
    "\u2013":  "–",   # EN DASH (garder, juste normaliser)
    "\u2212":  "-",   # MINUS SIGN → tiret standard
    "\uf02d":  "-",   # tiret custom
    # Guillemets
    "\u201c":  '"',
    "\u201d":  '"',
    "\u2018":  "'",
    "\u2019":  "'",
    "\uf0d8":  "↑",
    "\uf0da":  "↓",
    "\uf0e0":  "→",
    # Exposants numériques inline (ex: "10-6" encodé en glyphes)
    # Traités séparément par _fix_superscripts()
}

# ── Patterns de détection CID / pieds de page ──────────────────────────────────

# Séquences regex à corriger APRÈS la table de glyphes
_REGEX_FIXES: list[tuple[str, str]] = [
    # Espaces multiples → espace simple
    (r"  +", " "),
    # Retours à la ligne internes → espace
    (r"[\r\n]+", " "),
]

# Pattern de détection des glyphes CID non décodables (police sans ToUnicode)
# Note : certains tokens sont malformés (parenthèse fermante manquante, ex: (cid:8)
CID_PATTERN = re.compile(r"\(cid:[\d ]*\)?")

# Pattern de pied de page DSxxxx - Rev x page xx/xx (contamination fréquente)
FOOTER_PATTERN = re.compile(
    r"\bDS\d{3,6}\s*-\s*Rev\s*\d+.*?page\s*\d+/\d+",
    re.IGNORECASE,
)


def fix_text(text: str) -> str:
    """
    Applique la correction de glyphes sur un texte brut.
    1. Remplacement glyphe par glyphe
    2. NFC normalization
    3. Corrections regex
    4. Strip
    """
    if not text:
        return text

    # Étape 1 : remplacement glyphe → Unicode
    for src, dst in GLYPH_MAP.items():
        text = text.replace(src, dst)

    # Étape 2 : normalisation Unicode NFC
    text = unicodedata.normalize("NFC", text)

    # Étape 3 : corrections regex
    for pattern, replacement in _REGEX_FIXES:
        text = re.sub(pattern, replacement, text)

    # Étape 4 : nettoyage des glyphes CID non décodables (police sans ToUnicode)
    if CID_PATTERN.search(text):
        text = CID_PATTERN.sub("", text).strip()
        if text in ("-", "–", "−", "\u2212"):
            text = "-"
        elif len(text) < 2:
            text = ""

    return text.strip()


def fix_headers(headers: list[str]) -> list[str]:
    """Applique fix_text sur chaque header."""
    return [fix_text(h) for h in headers]


def fix_rows(rows: list[list[str | None]]) -> list[list[str]]:
    """
    Applique fix_text sur chaque cellule.
    None → "" (cellule vide explicite, pas d'ambiguïté).
    """
    return [
        [fix_text(cell) if cell is not None else "" for cell in row]
        for row in rows
    ]


def correct_footer_in_table(table: dict) -> dict:
    """
    Retire les pieds de page (DSxxxx - Rev x page xx/xx) des headers et rows
    d'un dictionnaire de table brute.  Appelé depuis main.py APRÈS le garde-fou
    dessin mécanique, pour ne pas marquer failed les tables qui n'avaient que
    du footer dans leur en-tête.
    """
    table["headers"] = [FOOTER_PATTERN.sub("", h).strip() for h in table.get("headers", [])]
    table["rows"] = [
        [FOOTER_PATTERN.sub("", c).strip() for c in row]
        for row in table.get("rows", [])
    ]
    return table
