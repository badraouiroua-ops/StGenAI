"""
ordering.py — Extraction des pages "Ordering information scheme".

Ces pages ne sont PAS des tableaux en grille : elles décomposent le code
produit STM32 en segments (Device family, Pin count, Package, etc.)
avec pour chaque segment les codes possibles et leur signification.

Fonction unique : extract_ordering_info(page_text, doc_id, table_id, page)
→ {"structured_json": {...}, "rag_chunks": [...]}
"""
from __future__ import annotations
import re
import logging

logger = logging.getLogger(__name__)

TABLE_TITLE_PATTERN = re.compile(
    r'(?:Table|Tableau)\s+(\d+)[.:]\s*(.+?)(?:\s+scheme)?\s*$',
    re.IGNORECASE
)
EXAMPLE_PATTERN = re.compile(
    r'(?:Example|Exemple)\s*:\s*(.+)',
    re.IGNORECASE
)
CATEGORY_LINE_PATTERN = re.compile(r'^[A-ZÉ][A-Za-zéèêëàâäùûüôöîïç\s\-]+$')
TRAILING_FOOTER_PATTERNS = [
    re.compile(r'^\d+/\d+\s+DS'),         # "198/202 DS13268 Rev 4"
    re.compile(r'^For\s+a\s+list\s+of\s+available'),  # "For a list of..."
    re.compile(r'^DS\d+\s+Rev'),          # "DS13268 Rev 4"
]


def _is_category_line(line: str) -> bool:
    """Une ligne catégorie : pas de '=', < 40 car., commence par majuscule."""
    s = line.strip()
    if not s:
        return False
    if '=' in s:
        return False
    if len(s) >= 40:
        return False
    if not s[0].isupper():
        return False
    # Exclure les lignes qui ressemblent à des titres de sections numérotés
    if re.match(r'^\d+\.\d+', s):
        return False
    return True


def _is_option_line(line: str) -> bool:
    """Une ligne d'option contient '=' (code = signification)."""
    return '=' in line.strip()


def _is_footer(line: str) -> bool:
    """Vérifie si la ligne est un pied de page à ignorer."""
    s = line.strip()
    if not s:
        return True
    for pat in TRAILING_FOOTER_PATTERNS:
        if pat.match(s):
            return True
    return False


def extract_ordering_info(
    page_text: str,
    doc_id: str = "",
    table_id: int = 0,
    page: int = 0,
) -> dict:
    """
    Extrait, structure et génère les chunks RAG d'une page
    "Ordering information scheme".

    Retourne :
    {
        "structured_json": { ... },
        "rag_chunks": [ ... ]
    }
    ou en cas d'échec :
    {
        "structured_json": {"success": False, "raw_text": "<texte>"},
        "rag_chunks": []
    }
    """
    lines = page_text.split('\n')

    # ── Étape 1 : détection du titre et de l'exemple ─────────────────────
    table_title = ""
    table_num = table_id
    example_parts: list[str] = []
    start_idx = 0

    for i, line in enumerate(lines):
        m = TABLE_TITLE_PATTERN.search(line.strip())
        if m:
            table_num = int(m.group(1))
            table_title = line.strip()
            start_idx = i + 1
            break

    for i in range(start_idx, len(lines)):
        m = EXAMPLE_PATTERN.match(lines[i].strip())
        if m:
            raw = m.group(1).strip()
            example_parts = raw.split()
            start_idx = i + 1
            break

    # ── Étape 2 : parsing des catégories et options ─────────────────────
    categories: list[dict] = []
    current_cat: str | None = None
    current_options: list[dict] = []

    for i in range(start_idx, len(lines)):
        line = lines[i].strip()
        if not line:
            continue
        if _is_footer(line):
            continue

        if _is_category_line(line):
            # Finaliser la catégorie précédente
            if current_cat is not None and current_options:
                categories.append({
                    "category": current_cat,
                    "options": current_options,
                })
            current_cat = line
            current_options = []
        elif _is_option_line(line):
            # Découper au premier '='
            eq_pos = line.index('=')
            code = line[:eq_pos].strip()
            meaning = line[eq_pos + 1:].strip()
            if current_cat is not None:
                current_options.append({
                    "code": code,
                    "meaning": meaning,
                })

    # Finaliser la dernière catégorie
    if current_cat is not None and current_options:
        categories.append({
            "category": current_cat,
            "options": current_options,
        })

    # ── Vérification ────────────────────────────────────────────────────
    if not categories:
        logger.warning("extract_ordering_info: no categories found")
        return {
            "structured_json": {"success": False, "raw_text": page_text},
            "rag_chunks": [],
        }

    # ── Étape 3 : construction du JSON structuré ────────────────────────
    structured = {
        "doc_id": doc_id,
        "table_id": table_num,
        "page": page,
        "type": "ordering_information",
        "table_title": table_title,
        "example_code": example_parts if example_parts else [],
        "categories": categories,
    }

    # ── Étape 4 : génération des chunks RAG ─────────────────────────────
    rag_chunks = []

    # Chunk exemple global
    if example_parts:
        cat_names = [c["category"] for c in categories]
        segments = []
        for idx, seg in enumerate(example_parts):
            cat_name = cat_names[idx] if idx < len(cat_names) else f"segment_{idx}"
            segments.append(f"{cat_name} = {seg}")
        example_text = (
            f"Document: {doc_id}, {table_title}, page {page}.\n"
            f"Format du code produit: {' '.join(example_parts)}\n"
            f"Ce code se décompose en : {', '.join(segments)}."
        )
        rag_chunks.append({
            "chunk_text": example_text,
            "metadata": {
                "doc_id": doc_id,
                "table_id": table_num,
                "page": page,
                "type": "ordering_information",
                "category": None,
            },
        })

    # Chunks par catégorie
    for cat in categories:
        options_lines = [
            f"- {opt['code']} = {opt['meaning']}"
            for opt in cat["options"]
        ]
        chunk_text = (
            f"Document: {doc_id}, {table_title}, page {page}.\n"
            f"Catégorie: {cat['category']}\n"
            f"Codes disponibles:\n" + "\n".join(options_lines)
        )
        rag_chunks.append({
            "chunk_text": chunk_text,
            "metadata": {
                "doc_id": doc_id,
                "table_id": table_num,
                "page": page,
                "type": "ordering_information",
                "category": cat["category"],
            },
        })

    # ── Étape 5 : retour final ──────────────────────────────────────────
    return {
        "structured_json": structured,
        "rag_chunks": rag_chunks,
    }
