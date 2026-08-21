#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ApplyCorrections.py
───────────────────
Lit les tables brutes depuis  Rag_selective/<family>/<datasheet>/
Lit les corrections LLM depuis Correction/<family>/<datasheet>/
Produit les tables corrigées dans correction_Rag/<family>/<datasheet>/
  + un fichier agrégé  DSxxxx_Rev_x_all_tables.json

Format attendu des fichiers de correction :
{
  "table_id": "table_12",
  "status": "ERRORS_FOUND" | "OK",
  "erreurs_corrigees": [
    {
      "row_index": 1,
      "corrections": [
        { "colonne_index": 4, "nouvelle_valeur": "..." }
      ]
    }
  ],
  "lignes_en_trop_supprimees": [
    6,            ← entier direct (format LLM réel)
    { "row_index": 6 }  ← OU objet (les deux formats sont acceptés)
  ],
  "lignes_manquantes_ajoutees": [
    { "row_index": 5, "contenu": ["a","b","c"] }
  ]
}

Exemple d'utilisation :
  python ApplyCorrections.py --family C0 --datasheet stm32c031c4 --workers 6
"""

import argparse
import json
import os
import sys
import time
import re
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy

# ---------------------------------------------------------------------------
#  Fix Windows console encoding
# ---------------------------------------------------------------------------
if os.name == 'nt':
    import ctypes
    ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# ---------------------------------------------------------------------------
#  Utilitaires thread-safe
# ---------------------------------------------------------------------------
print_lock = threading.Lock()

def safe_print(*args, **kwargs):
    with print_lock:
        try:
            print(*args, **kwargs)
        except UnicodeEncodeError:
            # Fallback: replace problematic chars
            msg = ' '.join(str(a) for a in args)
            print(msg.encode('ascii', errors='replace').decode('ascii'), **kwargs)

# ---------------------------------------------------------------------------
#  Fonctions de lecture/écriture JSON
# ---------------------------------------------------------------------------
def load_json(p: Path):
    try:
        with p.open('r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        safe_print(f"[ERROR] Cannot read {p}: {e}")
        return {}

def dump_json(data, p: Path):
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open('w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        safe_print(f"[ERROR] Cannot write {p}: {e}")

# ---------------------------------------------------------------------------
#  Fusion des corrections (format LLM réel)
# ---------------------------------------------------------------------------
def apply_correction(original: dict, correction: dict) -> dict:
    """Applique les corrections LLM sur la table originale.

    Règles de sécurité :
      - NE JAMAIS vider une case contenant du texte.
      - NE JAMAIS regrouper plusieurs références sur une ligne.
      - NE TOUCHE JAMAIS AUX CASES VIDES ("").
    """
    result = deepcopy(original)

    # Vérifier que le status indique des erreurs à corriger
    status = correction.get('status', 'OK')
    if status == 'OK':
        return result  # Rien à corriger

    rows = None
    if 'table_content' in result and 'rows' in result['table_content']:
        rows = result['table_content']['rows']
    elif 'rows' in result:
        rows = result['rows']

    if rows is None:
        safe_print(f"[WARN] No 'rows' found in table {result.get('table_id', '?')}, skipping corrections.")
        return result

    # ── 1. Supprimer les lignes en trop (du plus grand index au plus petit) ──
    lignes_sup = correction.get('lignes_en_trop_supprimees', [])
    headers = result.get("table_content", {}).get("headers", [])

    if lignes_sup:
        # Accepter les deux formats :
        #   - liste d'entiers :  [14, 27, 38]           (format réel LLM)
        #   - liste d'objets  :  [{"row_index": 14}, …]  (format docstring)
        raw_indices = []
        for entry in lignes_sup:
            if isinstance(entry, int):
                raw_indices.append(entry)
            elif isinstance(entry, dict) and 'row_index' in entry:
                raw_indices.append(entry['row_index'])
            # Ignorer les entrées invalides silencieusement

        indices_to_remove = sorted(set(raw_indices), reverse=True)

        for idx in indices_to_remove:
            if 0 <= idx < len(rows):
                row_to_delete = rows[idx]

                # SÉCURITÉ : Vérifier que c'est bien un en-tête répété
                # On accepte dès qu'au moins 1 cellule correspond à un en-tête connu
                # (les headers de saut de page peuvent avoir peu de cellules visibles)
                matches = sum(1 for cell in row_to_delete if cell and cell in headers)

                if matches >= 1:
                    safe_print(f"  [DEL] Row {idx} removed (Header match: {matches})")
                    rows.pop(idx)
                else:
                    safe_print(f"  [DEL-REJECT] Row {idx} not removed: does not look like a header (matches={matches}). Possible LLM hallucination.")
            else:
                safe_print(f"  [WARN] Row index {idx} out of range (len={len(rows)})")

    # ── 2. Ajouter les lignes manquantes ──
    lignes_add = correction.get('lignes_manquantes_ajoutees', [])
    if lignes_add:
        # Trier par row_index croissant pour insérer dans le bon ordre
        lignes_add_sorted = sorted(
            [e for e in lignes_add if 'row_index' in e and ('contenu' in e or 'valeurs' in e)],
            key=lambda x: x['row_index']
        )
        offset = 0  # Décalage accumulé après chaque insertion
        for entry in lignes_add_sorted:
            idx = entry['row_index'] + offset
            contenu = entry.get('contenu') or entry.get('valeurs')
            if idx <= len(rows):
                rows.insert(idx, contenu)
                safe_print(f"  [ADD] Row inserted at index {idx}")
                offset += 1
            else:
                rows.append(contenu)
                safe_print(f"  [ADD] Row appended (index {idx} > len)")
                offset += 1

    # ── 3. Appliquer les corrections de cellules ──
    erreurs = correction.get('erreurs_corrigees', [])
    for err_block in erreurs:
        row_idx = err_block.get('row_index')
        corrections_list = err_block.get('corrections', [])
        
        for corr in corrections_list:
            col_idx = corr.get('colonne_index')
            new_val = corr.get('nouvelle_valeur')
            original_val = corr.get('valeur_originale_json')
            
            if col_idx is None or new_val is None:
                continue
                
            # 1. Traitement des En-têtes (HEADER) si row_index est null/None
            if row_idx is None:
                # Récupérer la liste des headers (soit dans table_content, soit à la racine)
                headers_list = result.get("table_content", {}).get("headers", [])
                if not headers_list and "headers" in result:
                    headers_list = result["headers"]
                    
                if headers_list and 0 <= col_idx < len(headers_list):
                    old_val = headers_list[col_idx]
                    
                    # Log mismatch
                    if original_val is not None and str(old_val).strip() != str(original_val).strip():
                        safe_print(f"  [WARN-MISMATCH] HEADER Col {col_idx}: attendu '{original_val}', trouvé '{old_val}' (Force appliquée)")
                        
                    if old_val == "" and new_val != "":
                        safe_print(f"  [SKIP] HEADER Col {col_idx}: case vide, on ne touche pas")
                    else:
                        headers_list[col_idx] = new_val
                        safe_print(f"  [FIX-HEADER] Col {col_idx}: '{old_val}' -> '{new_val}'")
                        
                        # Mettre à jour aussi le text_helper si la chaîne s'y trouve
                        if "text_helper" in result and isinstance(result["text_helper"], str):
                            if old_val in result["text_helper"]:
                                result["text_helper"] = result["text_helper"].replace(old_val, new_val)
                                safe_print(f"  [FIX-HELPER] Remplacement de l'en-tête dans text_helper")
                                
                else:
                    safe_print(f"  [WARN] HEADER Col {col_idx} out of range ou pas de headers")
                    
                continue # On passe à la correction suivante, on ne propage pas globalement un header
                
            # 2. Appliquer à la ligne ciblée (données)
            if 0 <= row_idx < len(rows):
                row = rows[row_idx]
                if 0 <= col_idx < len(row):
                    old_val = row[col_idx]
                    
                    # Vérifier s'il y a un décalage de nommage (logging seulement)
                    if original_val is not None and str(old_val).strip() != str(original_val).strip():
                        safe_print(f"  [WARN-MISMATCH] Row {row_idx}, Col {col_idx}: attendu '{original_val}', trouvé '{old_val}' (Correction forcée appliquée)")
                        
                    if old_val == "" and new_val != "":
                        safe_print(f"  [SKIP] Row {row_idx}, Col {col_idx}: case vide, on ne touche pas")
                    else:
                        row[col_idx] = new_val
                        safe_print(f"  [FIX] Row {row_idx}, Col {col_idx}: '{old_val}' -> '{new_val}'")
                else:
                    safe_print(f"  [WARN] Col index {col_idx} out of range")
                    
            # 2. Propagation globale (auto-correction intelligente pour le reste de la colonne)
            if original_val and len(str(original_val)) > 1: # On ne propage pas les tirets ou cases vides
                clean_original_val = str(original_val).strip()
                for current_row_idx, r in enumerate(rows):
                    if current_row_idx == row_idx:
                        continue # Déjà traité
                    if 0 <= col_idx < len(r):
                        # Tolérance aux espaces avec strip()
                        if str(r[col_idx]).strip() == clean_original_val:
                            r[col_idx] = new_val
                            safe_print(f"  [FIX-GLOBAL] Row {current_row_idx}, Col {col_idx}: '{r[col_idx]}' -> '{new_val}'")

    return result

# ---------------------------------------------------------------------------
#  Traitement d'un datasheet
# ---------------------------------------------------------------------------
def process_datasheet(family: str, ds: str, src_root: Path, corr_root: Path, manual_root: Path, out_root: Path):
    src_dir = src_root / family / ds
    corr_dir = corr_root / family / ds
    manual_dir = manual_root / family / ds
    out_dir = out_root / family / ds
    out_dir.mkdir(parents=True, exist_ok=True)

    if not src_dir.is_dir():
        safe_print(f"[ERROR] Source directory not found: {src_dir}")
        return
    if not corr_dir.is_dir() and not manual_dir.is_dir():
        safe_print(f"[WARN] No correction directories found for {ds}, copying originals as-is.")

    # Trouver toutes les tables (format DSxxxx_Rev_x_table_N.json)
    table_files = sorted(
        [p for p in src_dir.iterdir() if p.is_file() and '_table_' in p.name and p.suffix == '.json'],
        key=lambda x: int(re.search(r'_table_(\d+)', x.name).group(1))
    )

    if not table_files:
        safe_print(f"[WARN] No table files found in {src_dir}")
        return

    safe_print(f"\n{'='*60}")
    safe_print(f"[START] {ds} — {len(table_files)} tables to process")
    safe_print(f"{'='*60}")

    corrected_paths = []
    processed_count = 0
    stats = {'fixed': 0, 'ok': 0, 'no_corr': 0}

    for src_path in table_files:
        try:
            table_name = src_path.name
            manual_path = manual_dir / table_name if manual_dir.is_dir() else None
            corr_path = corr_dir / table_name if corr_dir.is_dir() else None
            out_path = out_dir / table_name

            original = load_json(src_path)
            if not original:
                safe_print(f"[ERROR] Empty/invalid source: {src_path}")
                continue

            # Choix de la correction : Manual_Review en priorité
            correction_file = None
            is_manual = False
            if manual_path and manual_path.is_file():
                correction_file = manual_path
                is_manual = True
            elif corr_path and corr_path.is_file():
                correction_file = corr_path

            if correction_file:
                correction = load_json(correction_file)
                if correction:
                    if is_manual:
                        safe_print(f"  [INFO] Utilisation de la correction MANUAL_REVIEW pour {table_name}")
                        
                    status = correction.get('status')
                    conf = correction.get('confirmation')
                    
                    if status == 'ERRORS_FOUND' or (status == 'MANUAL_REVIEW_NEEDED' and conf == 'OK') or (status == 'MANUAL_REVIEW_NEEDED' and is_manual):
                        # Note: Si c'est un patch venant de Manual_Review, on l'applique d'office (même si le status est resté MANUAL_REVIEW_NEEDED)
                        safe_print(f"\n[FIXING] {table_name}")
                        merged = apply_correction(original, correction)
                        stats['fixed'] += 1
                    elif status == 'MANUAL_REVIEW_NEEDED':
                        safe_print(f"  [SKIP] {table_name}: MANUAL_REVIEW_NEEDED (Attente de 'confirmation': 'OK' ou correction manuelle)")
                        merged = original
                        stats['ok'] += 1
                    else:
                        merged = original
                        stats['ok'] += 1
                else:
                    merged = original
                    stats['ok'] += 1
            else:
                merged = original
                stats['no_corr'] += 1

            dump_json(merged, out_path)
            corrected_paths.append(out_path)
            processed_count += 1
        except Exception as e:
            safe_print(f"[ERROR] Failed on {src_path.name}: {e}")
            # Copy original as fallback
            try:
                dump_json(load_json(src_path), out_dir / src_path.name)
                corrected_paths.append(out_dir / src_path.name)
            except Exception:
                pass
            processed_count += 1
            continue

    # Agrégation finale → DSxxxx_Rev_x_all_tables.json
    if corrected_paths:
        sample_name = corrected_paths[0].name
        base = sample_name.split('_table_')[0]
        agg_path = out_dir / f"{base}_all_tables.json"
        aggregated = []
        for p in corrected_paths:
            data = load_json(p)
            if data:
                aggregated.append(data)
        dump_json(aggregated, agg_path)
        safe_print(f"\n[DONE] {ds}: {len(aggregated)} tables aggregated -> {agg_path}")

    safe_print(f"[STATS] {ds}: {stats['fixed']} fixed, {stats['ok']} OK, {stats['no_corr']} no correction file")

# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description='Apply LLM corrections and rebuild all_tables files.')
    fam_group = parser.add_mutually_exclusive_group(required=True)
    fam_group.add_argument('--family', help='Family name (e.g., C0, N6)')
    fam_group.add_argument('--an',     help='Dossier Application Notes (ex: 41)')
    parser.add_argument('--datasheet', help='Specific datasheet (e.g., stm32c031c4 or AN1709). If omitted, all in the family.')
    parser.add_argument('--workers', type=int, default=6, help='Number of parallel workers')
    parser.add_argument('--src-root', default='Output/Json/Selective_Tables', help='Root of raw tables')
    parser.add_argument('--corr-root', default='Output/Json/LLM_Corrections', help='Root of correction tables')
    parser.add_argument('--manual-root', default='Output/Json/Manual_Review', help='Root of manual review tables (Priority 1)')
    parser.add_argument('--out-root', default='Output/Json/Final_RAG', help='Where corrected tables go')
    args = parser.parse_args()

    # Résoudre le nom de famille (–-family ou --an)
    resolved_family = args.family if args.family else args.an

    src_root = Path(args.src_root).resolve()
    corr_root = Path(args.corr_root).resolve()
    manual_root = Path(args.manual_root).resolve()
    out_root = Path(args.out_root).resolve()

    family_dir = src_root / resolved_family
    if not family_dir.is_dir():
        safe_print(f"[ERROR] Family directory not found: {family_dir}")
        sys.exit(1)

    if args.datasheet:
        datasheets = [args.datasheet]
    else:
        datasheets = sorted([d.name for d in family_dir.iterdir() if d.is_dir()])

    safe_print(f"[INFO] Processing {len(datasheets)} datasheet(s): {', '.join(datasheets)}")
    safe_print(f"[INFO] Source: {src_root}")
    safe_print(f"[INFO] Corrections: {corr_root}")
    safe_print(f"[INFO] Manual Review (Priority): {manual_root}")
    safe_print(f"[INFO] Output: {out_root}")

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = []
        for ds in datasheets:
            futures.append(executor.submit(process_datasheet, resolved_family, ds, src_root, corr_root, manual_root, out_root))
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                safe_print(f"[ERROR] Exception: {e}")

    safe_print(f"\n{'='*60}")
    safe_print("=== ALL PROCESSING COMPLETE ===")
    safe_print(f"{'='*60}")

if __name__ == '__main__':
    main()
