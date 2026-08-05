# Plan: Fix table boundary detection (6 correctifs)

## Problèmes
1. **Table 27 ↔ 28** (même page 69) : lignes de Table 28 dans table_27.json
2. **Table 50 ↔ 51** (pages 87→88) : lignes de Table 51 dans table_50.json

## Correctifs

### Fix A — `_truncate_at_next_table()` pour TOUTES les méthodes
**Fichier :** `grid_extractor.py` ligne 1368
**Changement :** Supprimer `if method == "pdfplumber_text"` du guard.
```python
# AVANT
if method == "pdfplumber_text" and raw_table:

# APRÈS
if raw_table:
```
**Effet :** Les lignes contenant "Table N" avec N > table courante sont coupées quelle que soit la méthode d'extraction.

---

### Fix B — `_merge_compatible_tables()` vérifie le texte entre fragments
**Fichier :** `grid_extractor.py` fonction `_merge_compatible_tables()` (ligne 2721)
**Changement :**
1. Ajouter paramètres `page: Page, table_id: str`
2. Avant d'ajouter `rows_to_add`, extraire le texte du gap entre `current_ft.bbox[3]` et `ft.bbox[1]`
3. Chercher "Table N" avec N différent du numéro courant
4. Si trouvé, break (ne pas merger)

```python
def _merge_compatible_tables(
    raw_table: list, table_obj: Any, all_tables: list,
    all_finder: list, page=None, table_id=None,
) -> tuple[list, Any]:
```
Ajouter dans la boucle (après ligne 2761) :
```python
        if page is not None and table_id is not None:
            gap_text = page.extract_text({
                "x0": ft.bbox[0], "top": current_bottom,
                "x1": ft.bbox[2], "bottom": ft.bbox[1],
            })
            cur_num = int(re.findall(r'\d+', table_id)[0]) if re.findall(r'\d+', table_id) else 0
            gap_nums = re.findall(r'[Tt]able\s*(\d+)', gap_text)
            if gap_nums and any(int(n) != cur_num for n in gap_nums):
                break
```

**Appels modifiés** (lignes 2671, 2691) :
```python
best1, best_ft1 = _merge_compatible_tables(best1, best_ft1, tables, finder.tables, page=page, table_id=ref.table_id)
best2, best_ft2 = _merge_compatible_tables(best2, best_ft2, tables_text, finder_text.tables, page=page, table_id=ref.table_id)
```

---

### Fix C — `_is_continuation_page()` cherche "Table N" sur toute la page
**Fichier :** `continuation.py` ligne 294-312
**Changement :** Remplacer la fenêtre 50px par tous les mots au-dessus du bbox.

```python
    # AVANT
    for w in words:
        if "Table" in w["text"] and top_ft.bbox[1] - 50 < w["top"] < top_ft.bbox[1]:

    # APRÈS
    for w in words:
        if "Table" in w["text"] and w["top"] < top_ft.bbox[1]:
```

---

### Fix D — `body_on_next_page` scanne le texte complet de la page suivante
**Fichier :** `grid_extractor.py` lignes 1291-1303
**Changement :** Après avoir scanné les lignes extraites, extraire le texte complet de la page et le scanner aussi.

```python
                            # ── Garde-fou : page suivante contient une AUTRE table ? ──
                            # Vérifier d'abord dans les lignes extraites, puis dans
                            # le texte complet de la page (capte les captions hors crop).
                            nxt_text = " ".join(str(c) for row in nxt_raw for c in row).lower()
                            cur_num = int(ref.table_id.split("_")[1])
                            other_tables = re.findall(r"table\s+(\d+)", nxt_text)
                            # Vérifier aussi le texte complet de la page
                            full_page_text = p.extract_text().lower()
                            full_page_tables = re.findall(r"table\s+(\d+)", full_page_text)
                            other_nums = [int(n) for n in other_tables if n.isdigit()]
                            full_nums = [int(n) for n in full_page_tables if n.isdigit()]
                            # Si une table différente est trouvée DANS ou HORS extraction
                            if (other_nums and cur_num not in other_nums) or \
                               (full_nums and cur_num not in full_nums):
```

---

### Fix E — `find_continuations()` inclut les tables sur la page courante
**Fichier :** `continuation.py` ligne 531-534
**Changement :** Inclure `start_page_num` dans la recherche (au lieu de `current_page`).

```python
    # AVANT
    next_refs = [r for r in all_refs if r.page >= current_page and r.table_id != current_table_id]

    # APRÈS
    next_refs = [r for r in all_refs if r.page >= start_page_num and r.table_id != current_table_id]
```

---

### Fix F — Détection de discontinuité de contenu
**Fichier :** `grid_extractor.py` lignes 1704-1746
**Changement :** Après le split par `_HEADER_KW`, si aucun split trouvé mais `other_tables` existe, scanner les lignes pour une discontinuité : 1ère colonne change de motif ET ce changement persiste sur ≥ 2 lignes.

```python
        # ── [Fix F] Détection de discontinuité de contenu entre tables fusionnées ──
        # Si pdfplumber a fusionné deux tables distinctes (même structure de colonnes),
        # la frontière n'est pas détectable par _HEADER_KW (en-têtes déjà extraits).
        # On détecte un changement brusque et persistant dans la 1ère colonne.
        if split_idx is None and other_tables and len(rows_fixed) >= 4:
            for i in range(1, len(rows_fixed) - 1):
                prev_val = str(rows_fixed[i-1][0]).strip() if rows_fixed[i-1] else ""
                curr_val = str(rows_fixed[i][0]).strip() if rows_fixed[i] else ""
                next_val = str(rows_fixed[i+1][0]).strip() if rows_fixed[i+1] else ""
                if prev_val and curr_val and next_val:
                    if curr_val != prev_val and curr_val == next_val:
                        # Changement persistant sur ≥ 2 lignes → frontière
                        split_idx = i
                        logger.info(
                            f"{ref.table_id}: content discontinuity at row {i} "
                            f"('{prev_val}' → '{curr_val}'), splitting"
                        )
                        break
        if split_idx is not None and split_idx > 0:
            rows_fixed = rows_fixed[:split_idx]
```

**Ajouter** après la fin du bloc `_HEADER_KW` existant (après ligne 1746).

---

## Validation
1. Extraction test stm32c5a3cg tables 27, 28, 50, 51
2. Vérifier :
   - table_27.json : 6 rows (STOP modes uniquement, pas HSIKERON)
   - table_28.json : 2 rows (HSIKERON uniquement)
   - table_50.json : 4 rows (VOL/VOH uniquement, pas Fmax/tr/tf)
   - table_51.json : données AC complètes (Fmax/tr/tf avec speed grades)
3. Full scan famille C5 → 0 régression vs état actuel
