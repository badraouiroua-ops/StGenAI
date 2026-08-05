# Plan de correction — Continuations multi-pages

## Résumé

**4 bugs** identifiés dans la détection des continuations de table (`continuation.py`).  
**~40 tables (Problème B) + ~290 tables (Problème A)** = **~330 tables** affectées au total.

---

## Problème A : `same_page_others` bloque la continuation

### Cause
`continuation.py:499-511` : si la page de base contient **d'autres tables** ET que la table courante ne remplit pas 85 % de la page, la continuation est totalement sautée.

```python
same_page_others = [r for r in all_refs if r.page == start_page_num
                    and r.table_id != current_table_id]
if same_page_others:
    if base_bbox_bottom < page_height * 0.85:
        return merged_pages, [], expected_col_count, []  # ← skip
```

Exemple : page 31 a **Table 11** (legend, L1) + **Table 12** (pinout, L28).  
Table 12 ne remplit pas 85% → continuation skip. Pourtant pages 32+ ont la suite.

### Tables concernées
**~290 tables** dans 19 familles (cols ≥ 8, rows ≤ 4, confiance haute/moyenne, pas "Alternate function").

Filtre appliqué : tables de 1-4 lignes et ≥ 8 colonnes — forte probabilité de continuation manquée.

| Famille | Nb tables |
|---------|-----------|
| C0 | 7 |
| C5 | 18 |
| F0 | 23 |
| F1 | 1 |
| F2 | 4 |
| F3 | 37 |
| F4 | 41 |
| F7 | 5 |
| G0 | 5 |
| H5 | 12 |
| H7 | 50 |
| L0 | 6 |
| L1 | 11 |
| L4 | 38 |
| L5 | 4 |
| N6 | 5 |
| U0 | 6 |
| U3 | 6 |
| U5 | 14 |

**Familles sans tables filtrées :** G4

Liste complète exportée vers `problem_a_tables.csv` (4 394 entrées, toutes colonnes ≥ 5).

Cas typiques :
- Tables de caractéristiques électriques cohabitant avec d'autres tables
- "Current consumption" tables
- Toutes les tables "Pin/ball definition" qui partagent une page avec une légende

---

## Problème B : `_headers_differ` rejette les headers abrégés

### Cause
`continuation.py:158-179` : comparaison Jaccard sur les chaînes exactes.
Page de continuation : headers **abrégés** `"AF0"`  
Page de base : headers **complets** `"AF0 / SYS_AF"`  
→ intersection = ∅ → dissimilarité = 1.0 → rejet

### Tables concernées (40 tables, 12 familles)

| Famille | PDF | Table | Lignes | Cols | Légende |
|---------|-----|-------|--------|------|---------|
| C0 | stm32c011d6 | table_12 | 6 | 10 | Pin assignment and description |
| C0 | stm32c051c6 | table_12 | 5 | 11 | Pin assignment and description |
| C0 | stm32c091kb | table_12 | 5 | 13 | Pin assignment and description |
| F0 | stm32f038c6 | table_11 | 4 | 11 | Pin definitions |
| F0 | stm32f072c8 | table_14 | 6 | 12 | STM32F072x8/xB pin definitions |
| F0 | stm32f078cb | table_13 | 5 | 12 | STM32F078CB/RB/VB pin definitions |
| F3 | stm32f302cb | table_13 | 6 | 10 | STM32F302xB/C pin definitions |
| F3 | stm32f303cb | table_13 | 6 | 10 | STM32F303xB/C pin definitions |
| F4 | stm32f411cc | table_8 | 4 | 11 | STM32F411xC/xE pin definitions |
| F4 | stm32f427ag | table_10 | 3 | 14 | STM32F427xx/429xx pin and ball definitions |
| F4 | stm32f437ai | table_10 | 3 | 14 | STM32F437xx/439xx pin and ball definitions |
| F4 | stm32f446mc | table_10 | 4 | 11 | STM32F446xx pin and ball descriptions |
| F7 | stm32f730i8 | table_10 | 3 | 10 | STM32F730x8 pin and ball definition |
| F7 | stm32f745ie | table_10 | 3 | 14 | STM32F745xx/746xx pin and ball definition |
| F7 | stm32f756bg | table_10 | 3 | 14 | STM32F756xx pin and ball definition |
| F7 | stm32f777bi | table_11 | 2 | 17 | STM32F777xx/778Ax/779xx pin and ball definitions |
| G0 | stm32g071c8 | table_12 | 3 | 14 | Pin assignment and description |
| G0 | stm32g081cb | table_12 | 3 | 14 | Pin assignment and description |
| H7 | stm32h7b3ai | table_7 | 4 | 20 | STM32H7B3xI pin/ball definition |
| L0 | stm32l062c8 | table_15 | 5 | 10 | STM32L062x8 pin definitions |
| L0 | stm32l071c8 | table_15 | 6 | 15 | STM32L071xxx pin definition |
| L0 | stm32l071c8 | table_21 | 2 | 10 | Alternate functions port H |
| L0 | stm32l072cb | table_22 | 2 | 10 | Alternate functions port H |
| L0 | stm32l073cb | table_16 | 4 | 12 | STM32L073xx pin definition |
| L0 | stm32l073cb | table_22 | 2 | 10 | Alternate functions port H |
| L0 | stm32l082cz | table_15 | 5 | 10 | STM32L082xx pin definition |
| L0 | stm32l082cz | table_19 | 2 | 10 | Alternate functions port H |
| L0 | stm32l083cb | table_22 | 2 | 10 | Alternate functions port H |
| L1 | stm32l151qc | table_8 | 6 | 11 | STM32L151xC/C-A and STM32L152xC/C-A pin definitions |
| L1 | stm32l151qd | table_8 | 6 | 11 | STM32L151xD and STM32L152xD pin definitions |
| L1 | stm32l162qc | table_8 | 6 | 11 | STM32L162xC/C-A pin definitions |
| L4 | stm32l451cc | table_16 | 3 | 12 | STM32L451xx pin definitions |
| L4 | stm32l462ce | table_15 | 2 | 12 | STM32L462xx pin definitions |
| L4 | stm32l471qe | table_16 | 3 | 11 | STM32L471xx pin definitions |
| U0 | stm32u031c6 | table_17 | 4 | 18 | Port F alternate functions |
| U0 | stm32u073c8 | table_12 | 4 | 14 | STM32U073x8/B/C pin/ball definition |
| U0 | stm32u073c8 | table_13 | 1 | 18 | Port A alternate functions |
| U0 | stm32u073c8 | table_18 | 4 | 18 | Port F alternate functions |
| U0 | stm32u083cc | table_12 | 4 | 14 | STM32U083xC pin/ball definition |
| U0 | stm32u083cc | table_18 | 4 | 18 | Port F alternate functions |

---

## Problème C : `_build_text_grid` inaccessible sans label "(continued)"

### Cause
`continuation.py:237-240` : `_build_text_grid` (fallback texte) n'est appelé que dans le bloc `if has_continued_title:`. Les pages de continuation SANS le mot "(continued)" n'ont pas accès à ce fallback.

### Impact
Quand pdfplumber ne détecte pas de bordures de table (ex: tableaux sans filets), `_extract` retourne `None` → `good` vide → continuation rejetée, même si `_build_text_grid` aurait extrait les données correctement.

---

## Problème D : Cellules inversées dans les headers

### Cause
Certains PDF STM32 utilisent du texte **vertical** dans les en-têtes (ex: "niP" au lieu de "Pin", "epyt" au lieu de "type").  
pdfplumber extrait ces cellules dans l'ordre de lecture colonne → la chaîne est inversée.

```python
# Page 32 extrait :
L3: 'epyt'       # devrait être 'type'
L4: 'niP'        # devrait être 'Pin'
L5: 'erutcurts'  # devrait être 'structure'
```

`_headers_differ` compare `"niP"` avec `"Pin type"` → pas de match → rejet.

### Tables concernées
~5-10 tables "Pin/ball definition" supplémentaires où les en-têtes sont en orientation verticale.

---

## Corrections

### Fix A : `_headers_differ` — Fallback substring
**Fichier :** `continuation.py` ligne 178  
**Avant :**
```python
return dissimilarity > threshold  # False si Jaccard > 0.33
```
**Après :**
```python
# 1) Jaccard strict existant
if dissimilarity <= threshold:
    return False

# 2) Fallback : préfixe/substring
# Si chaque header de continuation est contenu dans un header base,
# les tables sont structurellement identiques → accepter
if cont_norm and base_norm:
    matches = 0
    total = len(cont_norm)
    # Nettoyer les chaînes vides
    cont_clean = {c for c in cont_norm if c.strip()}
    base_clean = {b for b in base_norm if b.strip()}
    for ch in cont_clean:
        if any(ch in bh or bh in ch for bh in base_clean):
            matches += 1
    ratio = matches / max(len(cont_clean), 1)
    return ratio < 0.5
return True
```

### Fix B : `same_page_others` — Exclure tables précédentes
**Fichier :** `continuation.py` ligne 502  
**Avant :**
```python
same_page_others = [r for r in all_refs if r.page == start_page_num
                    and r.table_id != current_table_id]
```
**Après :**
```python
def _table_num(table_id: str) -> int:
    nums = re.findall(r'\d+', table_id)
    return int(nums[0]) if nums else 0

same_page_others = [r for r in all_refs if r.page == start_page_num
                    and r.table_id != current_table_id
                    and _table_num(r.table_id) > _table_num(current_table_id)]
```
Ne bloque que si une table **postérieure** partage la même page.  
Une table antérieure (ex: Table 11 avant Table 12) n'interrompt pas la continuation.

### Fix C : `_build_text_grid` — Fallback universel
**Fichier :** `continuation.py` lignes 237-263  
**Avant :** `_build_text_grid` dans un bloc `if has_continued_title:`  
**Après :** Toujours tenter `_build_text_grid` si `good` est vide et que la page contient suffisamment de mots :

```python
# Après _extract(settings) et _extract(fallback_settings)
# Essai 3 : texte-grille (même sans "(continued)")
if not good and len(words) > 10:
    text_grid = _build_text_grid(words, page, current_table_num)
    if text_grid:
        good.append((text_grid, None, "text_grid", None))
```

### Fix D : Normaliser les cellules inversées
**Fichier :** `continuation.py` ligne 306  
**Avant :**
```python
if _headers_differ(table_data[0], base_header):
    return False, None, None, None, False
```
**Après :**
```python
from core.grid_extractor import _is_likely_reversed

# Normaliser les cellules inversées avant comparaison
if table_data[0]:
    header_row = [str(c[::-1]) if c and _is_likely_reversed(str(c)) else str(c) for c in table_data[0]]
    if _headers_differ(header_row, base_header):
        return False, None, None, None, False
else:
    if _headers_differ(table_data[0], base_header):
        return False, None, None, None, False
```

---

## Plan de déroulement

### Phase 1 : Appliquer les 4 fixes
1. Appliquer Fix A (`_headers_differ` substring)
2. Appliquer Fix B (`same_page_others`)
3. Appliquer Fix C (`_build_text_grid` universal)
4. Appliquer Fix D (cellules inversées)

### Phase 2 : Tests sur échantillon
Tester les 5 cas connus :
| Table | PDF | Problème | Résultat attendu |
|-------|-----|----------|------------------|
| U0 table_13 | stm32u073c8 | B | 16+ rows (PA0-PA15) |
| U0 table_12 | stm32u073c8 | A+B | 40+ rows (pinout complet) |
| F4 table_13 | stm32f469ae | B | 16+ rows (Port A) |
| N6 table_8 | stm32n655 | B+D | 50+ rows (pinout) |
| F4 table_10 | stm32f427ag | B | 50+ rows (pinout) |

### Phase 3 : Run complet
Lancer `main.py` + `build_rag_selective.py` sur **C0, C5, F0, F3, F4, N6, U0, U5**  
Vérifier :
- Nombre de tables "failed" = 0
- Row counts des tables "Pin/Alternate" augmentés significativement
- Pas de régressions sur les tables déjà correctes

### Phase 4 : Validation finale
Comparer avant/après pour les ~35 tables ciblées :
- Table 13 (U0) : rows 1 → 16+
- Table 12 (U0) : rows 4 → 40+
- Table 10 (F4) : rows 3 → 50+
- etc.

---

## Risques

| Risque | Mitigation |
|--------|-----------|
| Fix A (substring) trop permissif → faux positifs | Seuil à 0.5 (50%) testé sur les cas connus |
| Fix B (exclure tables précédentes) → table_12 récupère page 32 OK | Vérifier que `_is_continuation_page` valide bien les headers |
| Fix C (`_build_text_grid`) crée des grilles bruitées | Limiter aux pages avec ≥10 mots et ≥2 lignes |
| Fix D (inversion) corrige à tort des cellules non inversées | `_is_likely_reversed` déjà testé (4464/4464 OK) |

---

## Fichiers modifiés

1. `table_extractor_raw/core/continuation.py` — 4 modifications, ~20 lignes ajoutées au total
