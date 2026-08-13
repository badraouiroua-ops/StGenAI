"""
main.py — CLI : traite un PDF ou un dossier entier.

Usage:
    python main.py --pdf DataSHEET/C0/stm32c011d6.pdf
    python main.py --family C0
    python main.py --family C0 --workers 4
    python main.py --all --workers 8
    python main.py --random 20 --workers 8
    python main.py --pdf DataSHEET/C5/stm32c532cb.pdf --tables 2,5,10,11
"""
from __future__ import annotations
import argparse
import json
import logging
import re
import sys
import io
import os
import random as random_module
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

# Force UTF-8 sur stdout Windows (cp1252 ne supporte pas µ, Ω, ✓, →, etc.)
try:
    if sys.stdout.encoding != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass
try:
    if sys.stderr.encoding != "utf-8":
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass
import time

# ── Setup path ─────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

# ── Chemin racine du dépôt (pour rag_transformer.py) ──────────────────────────
REPO_ROOT = Path(__file__).parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import ROOT_DIR, OUTPUT_DIR, LOG_DIR, RAG_DIR
from core.toc_detector import detect_tables
from core.grid_extractor import extract_table_grid, extract_footnotes_from_pages, extract_legend_from_page, extract_notes_type1, _reset_reversed_debug, _get_reversed_debug_entries
from core.glyph_fixer import correct_footer_in_table
import pdfplumber
import fitz
from core.schema import RawTable
from build_rag_selective import process_pdf as build_rag_pdf

def _fix_missing_dashes(table: dict) -> dict:
    """Remplace les cellules vides par '-' dans les colonnes à dash attendu.

    La détection vectorielle (page.chars + page.lines) a déjà été faite dans
    grid_extractor._detect_vector_dashes.  Ce fallback ne touche que les
    cellules encore vides, après cette détection.
    """
    headers = table.get("headers", [])
    rows = table.get("rows", [])
    if not headers or not rows:
        return table

    # Mêmes mots-clés que _detect_vector_dashes dans grid_extractor
    dash_col_keywords = ("parameter", "conditions", "symbol", "ratings",
                         "min", "typ", "max", "unit", "value")
    dash_cols = {
        i for i, h in enumerate(headers)
        if any(kw in h.lower() for kw in dash_col_keywords)
    }

    if not dash_cols:
        return table

    for row in rows:
        for ci in dash_cols:
            if ci < len(row) and row[ci] == "":
                row[ci] = "-"

    return table


def detect_pdf_type(pdf_path: str) -> int:
    """Détecte le type de PDF : 1 = Acrobat, 2 = Antenna House (XML)."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            producer = (pdf.metadata or {}).get("Producer", "")
        return 2 if "antenna" in producer.lower() else 1
    except Exception:
        return 1

# ── Logging JSON simple ────────────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("main")


def _deduplicate_table_boundaries(all_tables: list[dict], out_dir: Path, family: str = "") -> int:
    sorted_tables = sorted(
        all_tables,
        key=lambda t: int(re.findall(r'\d+', t.get("table_id", "0"))[0])
    )

    # N6 : sauter les 6 premières tables
    if family == "N6":
        sorted_tables = sorted_tables[6:]
        if not sorted_tables:
            return 0

    removed_rows = 0
    modified_ids: set[str] = set()

    for i in range(len(sorted_tables)):
        cur = sorted_tables[i]
        cur_rows = cur.get("rows", [])
        if not cur_rows:
            continue

        next_rows: list[list] = []
        for j in range(1, 3):
            if i + j < len(sorted_tables):
                next_rows.extend(sorted_tables[i + j].get("rows", []))

        if not next_rows:
            continue

        next_set = {json.dumps(row, ensure_ascii=False) for row in next_rows}
        before = len(cur_rows)
        removed_indices: list[int] = []
        removed_rows_data: list[list] = []
        kept: list[list] = []
        for ri, row in enumerate(cur_rows):
            if json.dumps(row, ensure_ascii=False) in next_set:
                removed_indices.append(ri)
                removed_rows_data.append(row)
            else:
                kept.append(row)

        # Ne pas appliquer si le résultat est vide
        if len(kept) == 0:
            logger.info(f"  [dedup] {cur['table_id']}: would become empty, skipped")
            continue

        if before != len(kept):
            n_removed = before - len(kept)
            removed_rows += n_removed
            modified_ids.add(cur["table_id"])
            heuristics = cur.setdefault("heuristics", {})
            heuristics["_dedup_rows_removed"] = n_removed
            heuristics["_dedup_removed_indices"] = removed_indices
            cur["rows"] = kept

    total = removed_rows
    if total > 0:
        parts = []
        if removed_rows:
            parts.append(f"{removed_rows} duplicate rows")
        logger.info(f"  [dedup] removed {' and '.join(parts)} across table boundaries")
        for table_json in all_tables:
            tid = table_json["table_id"]
            if tid not in modified_ids:
                continue
            new_count = len(table_json["rows"])
            if new_count != table_json["datasheet_metaData"]["rows_count"]:
                table_json["datasheet_metaData"]["rows_count"] = new_count
            out_file = out_dir / f"{tid}.json"
            out_file.write_text(
                json.dumps(table_json, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
    return removed_rows


def process_pdf(pdf_path: Path, family: str, table_ids: list[int] | None = None) -> dict:
    """
    Traite un PDF complet :
    1. Détecte le type (Acrobat vs Antenna House) via detect_pdf_type
    2. Détecte les tables (TOC ou scan) via detect_tables
    3. Filtre par table_ids si spécifié (ex: [2, 5, 10, 11])
    4. Extrait chaque table via extract_table_grid (pipeline complet)
    5. Valide via Pydantic (RawTable schema)
    6. Sauvegarde en JSON + image debug si nécessaire
    7. Génère le chunks RAG (optionnel)

    Retourne un résumé du run pour ce PDF.
    """
    pdf_name = pdf_path.stem
    logger.info(f"=== START {family}/{pdf_name} ===")
    t0 = time.time()

    out_dir = OUTPUT_DIR / family / pdf_name
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "pdf": str(pdf_path),
        "family": family,
        "pdf_name": pdf_name,
        "tables_found": 0,
        "tables_extracted": 0,
        "high": 0,
        "medium": 0,
        "low": 0,
        "failed": 0,
        "errors": [],
        "worst_tables": [],  # trié par empty_cell_ratio desc
    }

    # ── Détection du type de PDF ───────────────────────────────────────────────
    pdf_type = detect_pdf_type(str(pdf_path))
    logger.info(f"PDF type: {pdf_type} ({'Antenna House' if pdf_type == 2 else 'Acrobat'})")

    # ── Ouverture unique du PDF ────────────────────────────────────────────────
    pdf = pdfplumber.open(str(pdf_path))
    doc_fitz = fitz.open(str(pdf_path))

    # ── Étape 1 : détection ────────────────────────────────────────────────────
    try:
        refs = detect_tables(str(pdf_path), pdf_type=pdf_type, pdf=pdf)
    except Exception as e:
        logger.error(f"TOC detection failed: {e}")
        summary["errors"].append(f"toc_detection:{e}")
        return summary

    summary["tables_found"] = len(refs)
    logger.info(f"Detected {len(refs)} tables")

    # ── Filtrage par IDs de tables spécifiques (--tables) ────────────────────────
    if table_ids is not None:
        allowed = {f"table_{tid}" for tid in table_ids}
        refs = [r for r in refs if r.table_id in allowed]
        logger.info(f"  --tables filter: kept {len(refs)}/{summary['tables_found']} "
                    f"({', '.join(r.table_id for r in refs)})")
        summary["tables_found"] = len(refs)

    # ── Étape 1b : Features extraction (indépendante, n'affecte pas les tables) ──
    features_path = out_dir / "features.json"
    if not features_path.exists():
        try:
            from core.page1_features import extract_features_page_range
            features = extract_features_page_range(str(pdf_path), pdf=pdf)
            features_path.write_text(
                json.dumps(features, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            logger.info(f"Features saved: {features_path.name} "
                        f"({len(features.get('packages', []))} pkgs, "
                        f"device_summary={'yes' if features.get('device_summary') else 'no'}, "
                        f"pages={features.get('extraction_meta', {}).get('source_pages', [])})")
        except Exception as e:
            logger.warning(f"Features extraction skipped: {e}")



    if not refs:
        logger.warning("No tables detected — PDF may have no table index")
        return summary

    # ── Cache de textes (pour notes/légendes, tous types) ─────────────────────
    page_text_cache: dict[int, str] = {}
    for i in range(len(pdf.pages)):
        page_text_cache[i + 1] = pdf.pages[i].extract_text() or ""

    # ── Reset buffer debug cellules inversées ──────────────────────────────────
    _reset_reversed_debug()

    # ── Étape 2 : extraction ───────────────────────────────────────────────────
    extracted_tables = []
    all_tables_json = []
    for ref in refs:
        try:
            raw_dict = extract_table_grid(
                pdf_path=str(pdf_path),
                ref=ref,
                family=family,
                pdf_name=pdf_name,
                output_base=OUTPUT_DIR,
                all_refs=refs,
                pdf_type=pdf_type,
                pdf=pdf,
            )

            # ── Garde-fou : dessins mécaniques/PCB détectés comme tableaux vides ──
            if (raw_dict.get("empty_cell_ratio", 0) >= 0.90
                    and not any(h.strip() for h in raw_dict.get("headers", []))):
                raw_dict["status"] = "failed"
                raw_dict.setdefault("warnings", []).append("unresolved:likely_mechanical_drawing_not_extractable_as_text")

            # ── Nettoyage des pieds de page (après le garde-fou) ─────────────────
            raw_dict = correct_footer_in_table(raw_dict)

            # ── Correction des tirets manquants dans les colonnes Min/Max/Typ ────
            raw_dict = _fix_missing_dashes(raw_dict)

            # ── Notes et légendes (tous types) ───────────────────────────────────
            if page_text_cache:
                pages = raw_dict.get("merged_pages", [raw_dict.get("page", 1)])
                rows = raw_dict.get("rows", [])
                headers = raw_dict.get("headers", [])
                caption = raw_dict.get("caption", "")

                # Pattern A: marqueurs (N) dans les cellules (Type 1 & 2)
                notes = extract_footnotes_from_pages(rows, headers, page_text_cache, pages)

                # Pattern B: Notes: heading ou notes sans marqueurs (Type 1)
                if not notes:
                    notes = extract_notes_type1(rows, caption, page_text_cache, pages)

                if notes:
                    # Format: préfixe doc_ref/revision si disponible
                    doc_ref = raw_dict.get("doc_ref", "")
                    revision = raw_dict.get("revision", "")
                    prefix = f"[{doc_ref} {revision}] " if doc_ref and revision else ""
                    raw_dict.setdefault("heuristics", {})["_notes"] = [
                        f"{prefix}{n}" if prefix else n for n in notes
                    ]

                legend = extract_legend_from_page(caption, headers, page_text_cache, pages)
                if legend:
                    raw_dict.setdefault("heuristics", {})["_legend"] = legend

            # Validation Pydantic
            table_obj = RawTable(**raw_dict)
            table_json = table_obj.model_dump()
            
            # Ajout des métadonnées spécifiques demandées à la toute fin du JSON
            table_json["datasheet_metaData"] = {
                "pdf_name": table_json["pdf_name"],
                "table_id": table_json["table_id"],
                "is_continued": len(table_json["merged_pages"]) > 1,
                "pages": table_json["merged_pages"],
                "rows_count": len(table_json["rows"]),
                "cols_count": len(table_json["headers"]),
                "confidence": table_json["extraction_confidence"],
                "empty_cell_ratio": round(table_json["empty_cell_ratio"], 4)
            }

            # Debug crop path (non inclus dans le schéma RawTable)
            if "debug" in raw_dict:
                table_json["debug"] = raw_dict["debug"]

            # Sauvegarde JSON individuelle
            out_file = out_dir / f"{ref.table_id}.json"
            out_file.write_text(
                json.dumps(table_json, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

            # ── Capture images des pages de la table (capt/<famille>/<datasheet>/tableau_N/) ──
            capt_dir = ROOT_DIR.parent / "capt" / family / pdf_name
            merged_pages = raw_dict.get("merged_pages", [raw_dict.get("page", 1)])
            if merged_pages:
                table_num = int(re.search(r'\d+', ref.table_id).group())
                table_capt_dir = capt_dir / f"tableau_{table_num}"
                table_capt_dir.mkdir(parents=True, exist_ok=True)
                for pg in merged_pages:
                    # ── Screenshot PNG (150 dpi) ──
                    fz_page = doc_fitz[pg - 1]
                    pix = fz_page.get_pixmap(dpi=150)
                    pix.save(str(table_capt_dir / f"page_{pg}.png"))
                    # ── Page extraite en PDF (page individuelle) ──
                    try:
                        import pypdf as _pypdf
                        _reader = _pypdf.PdfReader(str(pdf_path))
                        _writer = _pypdf.PdfWriter()
                        if 1 <= pg <= len(_reader.pages):
                            _writer.add_page(_reader.pages[pg - 1])
                            _pdf_out = table_capt_dir / f"page_{pg}.pdf"
                            with open(str(_pdf_out), "wb") as _f:
                                _writer.write(_f)
                    except Exception as _pdf_err:
                        logger.warning(f"  Impossible d'extraire la page PDF {pg}: {_pdf_err}")

            all_tables_json.append(table_json)

            # Stats
            conf = table_json["extraction_confidence"]
            summary[conf] = summary.get(conf, 0) + 1
            summary["tables_extracted"] += 1

            extracted_tables.append({
                "table_id": ref.table_id,
                "confidence": conf,
                "empty_cell_ratio": table_json["empty_cell_ratio"],
                "has_empty_cells": table_json.get("has_empty_cells", False),
                "warnings": table_json.get("warnings", []),
                "extraction_method": table_json.get("extraction_method", ""),
                "status": table_json.get("status"),
                "caption": table_json.get("caption", "")[:120],
                "page": table_json.get("page", 0),
                "merged_pages": table_json.get("merged_pages", []),
                "col_count": table_json.get("col_count", 0),
                "headers_preview": json.dumps(table_json.get("headers", []), ensure_ascii=False)[:200],
                "rows_count": len(table_json.get("rows", [])),
                "heuristics": table_json.get("heuristics", {}),
            })

            logger.info(f"  [OK] {ref.table_id} -> {out_file.name} [{conf}]")

        except Exception as e:
            logger.error(f"  ✗ {ref.table_id}: {e}", exc_info=True)
            summary["errors"].append(f"{ref.table_id}:{e}")
            summary["failed"] += 1

    # ── [Dedup] Suppression des rows dupliquées entre tables adjacentes ────────
    dedup_rows = _deduplicate_table_boundaries(all_tables_json, out_dir, family=family)
    summary["dedup_rows_removed"] = dedup_rows

    # ── Sauvegarde du fichier global _all_tables.json ──────────────────────────
    # Construire le contenu avec features en premier
    features_data = None
    features_path = out_dir / "features.json"
    if features_path.exists():
        try:
            features_data = json.loads(features_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    
    all_output = []
    if features_data:
        all_output.append({"features": features_data})
    all_output.extend(all_tables_json)
    
    all_path = out_dir / "_all_tables.json"
    all_path.write_text(
        json.dumps(all_output, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    # ── Génération Rag_selective (schéma allégé, text_helper) ──────────────
    try:
        n_selective = build_rag_pdf(pdf_name, family, out_dir, RAG_DIR, pdf=pdf)
        if n_selective > 0:
            logger.info(f"Rag_selective: {n_selective} tables -> Rag_selective/{family}/{pdf_name}/")
    except Exception as e:
        logger.error(f"Rag_selective generation failed: {e}")

    # ── Fermeture du PDF ────────────────────────────────────────────────────────
    doc_fitz.close()
    pdf.close()

    # ── Rapport de synthèse ────────────────────────────────────────────────────
    # Toutes les tables problématiques : non-high + high avec cellules vides
    worst = sorted(
        [t for t in extracted_tables
         if t["confidence"] != "high"
         or t.get("has_empty_cells")
         or t.get("warnings")],
        key=lambda t: t["empty_cell_ratio"],
        reverse=True
    )
    summary["worst_tables"] = worst

    # ── Classification des échecs : dessin-mécanique vs bug ──────────────────
    mech_keywords = ["mechanical data", "outline", "package outline"]
    n_mech = n_bug = 0
    for t in extracted_tables:
        if t.get("status") in ("failed", "review_needed") or t.get("confidence") == "low":
            cap = (t.get("caption") or "").lower()
            status = t.get("status") or ""
            if any(kw in cap for kw in mech_keywords) and status in ("failed", "review_needed"):
                n_mech += 1
            else:
                n_bug += 1
    summary["drawing_failed"] = n_mech
    summary["bug_suspected"] = n_bug

    elapsed = time.time() - t0
    logger.info(
        f"=== DONE {pdf_name} | {summary['tables_extracted']}/{summary['tables_found']} tables "
        f"| high={summary['high']} medium={summary['medium']} "
        f"low={summary['low']} failed={summary['failed']} "
        f"| drawings={n_mech} bugs?={n_bug} "
        f"| {elapsed:.1f}s ==="
    )

    # ── Sauvegarde du debug des cellules inversées ──────────────────────────
    entries = _get_reversed_debug_entries()
    if entries:
        debug_data = {
            "pdf_name": pdf_name,
            "family": family,
            "tables_checked": len({e["table_id"] for e in entries}),
            "cells_checked": len(entries),
            "cells_reversed": sum(1 for e in entries if e["reversed"]),
            "entries": entries,
        }
        debug_path = out_dir / "_reversed_debug.json"
        debug_path.write_text(
            json.dumps(debug_data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    # Sauvegarde du rapport de synthèse
    report_path = out_dir / "_run_report.json"
    report_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    return summary


def _run_parallel(
    items: list[Path],
    get_family: callable,
    workers: int,
) -> None:
    """Exécute process_pdf en parallèle via ProcessPoolExecutor.

    Mémoire : chaque processus charge un pdfplumber complet en RAM.
    Sur 32 Go, workers=16 OOM sur PDFs de 200+ pages. workers=8 est
    le max stable pour les gros PDFs de la famille H5 (200+ pages).
    """
    t_start = time.time()
    ok = failed = 0
    total = len(items)

    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(process_pdf, pdf, get_family(pdf)[1]): pdf
            for pdf in items
        }
        for future in as_completed(futures):
            pdf = futures[future]
            try:
                summary = future.result()
                name = summary["pdf_name"]
                if summary["failed"] == 0 and summary["tables_extracted"] == summary["tables_found"]:
                    ok += 1
                else:
                    failed += 1
                    n_fail = summary["failed"]
                    n_tot = summary["tables_found"]
                    logger.warning(f"⚠ {name}: {n_fail}/{n_tot} tables failed")
            except Exception as e:
                failed += 1
                logger.error(f"✗ {pdf.name}: {e}")

    elapsed = time.time() - t_start
    logger.info(f"=== PARALLEL DONE: {ok} OK / {failed} FAILED / {total} total | {elapsed:.0f}s ===")


def main():
    parser = argparse.ArgumentParser(
        description="STM32 PDF Table Extractor — sortie JSON brute sans classification"
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pdf",    type=Path, help="Chemin vers un PDF spécifique")
    group.add_argument("--family", type=str,  help="Ex: C0 → traite tous les PDFs de cette famille")
    group.add_argument("--all",    action="store_true", help="Traite tous les PDFs du projet")
    group.add_argument("--random", type=int, metavar="N", help="Traite N PDFs aléatoires")

    parser.add_argument("--workers", type=int, default=None,
                        help="Nombre de workers parallèles (défaut = nombre de cœurs CPU)")
    parser.add_argument("--tables", type=str, default=None,
                        help="IDs de tables spécifiques, ex: 2,5,10,11 (avec --pdf uniquement)")

    # Chemin racine des PDFs — sous-dossier DataSHEET du repo
    DATASHEETS_ROOT = Path(__file__).parent.parent / "DataSHEET"

    args = parser.parse_args()
    workers = args.workers or os.cpu_count() or 1
    table_ids = [int(x.strip()) for x in args.tables.split(",")] if args.tables else None

    if args.tables and not args.pdf:
        logger.warning("--tables est ignoré sans --pdf (utilisable uniquement avec un seul PDF)")

    if args.pdf:
        # Un seul PDF — déduire la famille depuis le dossier parent
        pdf_path = args.pdf.resolve()
        family = pdf_path.parent.name
        process_pdf(pdf_path, family, table_ids=table_ids)

    elif args.family:
        family_dir = DATASHEETS_ROOT / args.family
        if not family_dir.exists():
            logger.error(f"Family directory not found: {family_dir}")
            sys.exit(1)
        pdfs = sorted(family_dir.glob("*.pdf"))
        logger.info(f"Processing {len(pdfs)} PDFs from family {args.family} ({workers} workers)")
        _run_parallel(pdfs, lambda p: (p, args.family), workers)

    elif args.random:
        all_pdfs = sorted(DATASHEETS_ROOT.glob("*/*.pdf"))
        n = min(args.random, len(all_pdfs))
        selected = random_module.sample(all_pdfs, n)
        logger.info(f"Processing {n} random PDFs ({workers} workers)")
        _run_parallel(selected, lambda p: (p, p.parent.name), workers)

    elif args.all:
        all_pdfs = sorted(DATASHEETS_ROOT.glob("*/*.pdf"))
        logger.info(f"Processing {len(all_pdfs)} PDFs total ({workers} workers)")
        _run_parallel(all_pdfs, lambda p: (p, p.parent.name), workers)


if __name__ == "__main__":
    main()
