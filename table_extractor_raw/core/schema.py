"""
schema.py — modèles Pydantic pour la sortie JSON brute.
Aucun typage sémantique ici : juste la structure fidèle.
"""
from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field


class RawTable(BaseModel):
    """Une table extraite brute, sans classification de type."""

    # ── Identification ─────────────────────────────────────────────────────────
    table_id:   str = Field(..., description="Ex: 'table_12'")
    caption:    str = Field(..., description="Légende complète telle qu'elle apparaît dans le PDF")
    pdf_name:   str = Field(..., description="Nom du fichier PDF source (sans extension)")
    family:     str = Field(..., description="Famille STM32, ex: 'C0'")

    # ── Localisation ───────────────────────────────────────────────────────────
    page:         int            = Field(..., description="Page de début (1-indexé)")
    merged_pages: list[int]      = Field(default_factory=list, description="Toutes les pages si table multi-pages")

    # ── URLs ──────────────────────────────────────────────────────────────────
    url:       str = Field("", description="URL datasheet sur st.com")
    url_table: str = Field("", description="URL directe vers la page de la table")

    # ── Section ───────────────────────────────────────────────────────────────
    section: str = Field("", description="Titre de la section du datasheet contenant la table")

    # ── Contenu brut ───────────────────────────────────────────────────────────
    headers: list[str]       = Field(default_factory=list, description="Ligne d'en-tête (après correction glyphes)")
    rows:    list[list[str]] = Field(default_factory=list, description="Lignes de données (après correction glyphes)")

    # ── Métadonnées qualité ────────────────────────────────────────────────────
    extraction_method:     Literal["pdfplumber", "pdfplumber_text", "camelot_lattice",
                                   "camelot_stream", "docling", "failed"] = "pdfplumber"
    extraction_confidence: Literal["high", "medium", "low", "failed"]      = "high"
    empty_cell_ratio:      float = Field(0.0, ge=0.0, le=1.0)
    col_count:             int   = Field(0, description="Nombre de colonnes détectées")
    status:                Optional[str] = Field(None, description="Statut d'extraction : 'failed' si table non extractible")

    # ── Drapeaux qualité supplémentaires ─────────────────────────────────────
    has_empty_cells: bool = Field(False, description="True si l'extraction brute avait ≥1 cellule vide (ensuite remplie par propagation)")

    # ── Heuristiques de tracking ───────────────────────────────────────────────
    heuristics: dict = Field(default_factory=dict,
                             description="Traces des heuristiques appliquées pendant l'extraction")

    # ── Données structurées pour non-tables (ordering info, etc.) ──────────
    structured_json: Optional[dict] = Field(None,
        description="Données structurées quand la page n'est pas une grille (ex: ordering information)")

    # ── Avertissements ────────────────────────────────────────────────────────
    warnings: list[str] = Field(default_factory=list,
                                description="Ex: ['header_row_ambiguous', 'vertical_merge_suspected']")

    class Config:
        # Permet la sérialisation propre pour le JSON
        json_encoders = {}
