# Problèmes et Solutions — Extraction de tables STM32

## 1. Fausses continuations (table_42/43)

**Problème :** `_headers_differ` comparait les en-têtes index par index.
Quand une colonne était fusionnée (colspan) dans la page de continuation,
le décalage d'index faisait détecter une différence (ex: colonne "Conditions"
manquante → toutes les colonnes suivantes décalées → fausse non-correspondance
→ table rejetée).

**Solution :** Comparaison ensembliste (Jaccard) au lieu d'index par index.
On calcule `|A ∩ B| / |A ∪ B|` sur les ensembles de textes d'en-têtes dédupliqués.
Seuil : `jaccard < 0.50` → headers différents, sinon identiques.

**Fichier :** `continuation.py:_headers_differ`

---

## 2. Continuation rejetée malgré en-têtes identiques (table_58)

**Problème :** `_is_continuation_page` rejetait la continuation quand
`|col_count - expected| > 2`, même si les en-têtes étaient identiques.
Table_58 passait de 7 à 12 colonnes (colonnes fusionnées qui "s'ouvrent"
sur la page de continuation) → rejetée.

**Solution :** Quand les ensembles d'en-têtes (dédupliqués) sont strictement
identiques entre `raw_header` et `base_header`, on accepte la continuation
quel que soit l'écart de `col_count`.

**Fichier :** `continuation.py:_is_continuation_page`

---

## 3. Colonnes fusionnées mal expansées dans la continuation

**Problème :** `find_continuations` utilisait `_expand_cont_row` qui répétait
la dernière valeur connue pour remplir les cellules vides. Pour les colonnes
fusionnées horizontalement (ex: 6× "Conditions"), la cellule unique n'était
pas propagée aux 6 colonnes → données manquantes.

**Solution :** Nouvelle fonction `_expand_cont_row_by_x0s` qui utilise les
coordonnées x0 des colonnes de la page de base (`base_x0s`) pour décider
combien de fois répéter chaque valeur dans la page de continuation.
Remplace `_expand_cont_row` quand `base_x0s` et `cont_x0s` sont disponibles.

**Fichier :** `continuation.py:_expand_cont_row_by_x0s`

---

## 4. Lignes au-dessus de la légende (5 bugs F4+U0)

**Problème :** Quand 2 tables cohabitent sur la même page, pdfplumber les
fusionne en une seule `raw_table`. Le filtre caption supprime les lignes
de l'AUTRE table (au-dessus de la légende cible), mais ne trouve AUCUNE
ligne pour la table cible → `raw_table = []`. Puis `body_on_next_page`
tente la page suivante mais échoue aussi (autre table).

**Cas concrets :**
- F4 stm32f469ae / stm32f479ag : table_76 (Ethernet MAC MII) page 155
  avec table_75 (RMII) → corps de table_76 sur page 156 mais rejeté car
  table_77 aussi présente
- U0 stm32u073c8 / stm32u083cc : table_67 (NRST pin) page 86 avec
  continuation de table_66 → corps sur page 87
- U0 stm32u031c6 : table_65 (NRST pin) page 80 → corps sur page 81

**Solution 4a — Re-extraction sous caption_y (même page) :**
Quand toutes les lignes sont au-dessus de `caption_y`, on recadre la page
à partir de `caption_y - 5` et on ré-appelle `_extract_from_page` sur
cette zone recadrée (permet de capturer la table cible sans l'autre table).

**Solution 4b — Guard body_on_next_page relaxé :**
L'ancien guard rejetait la page suivante si **n'importe quel** autre numéro
de table était présent (`any(n != cur_num for n in other_nums)`).
Nouveau guard : on rejette seulement si la table cible **n'est pas** présente
(`other_nums and cur_num not in other_nums`). Ceci permet à table_76 d'être
extraite de la page 156 même si table_77 y apparaît aussi.

**Fichier :** `grid_extractor.py`

---

## 5. Headers avec codes commande au lieu de noms STM32

**Problème :** Certains datasheets (N6 stm32n645a0 table_2) utilisent des
codes commande `Q3H0X546N 23MTS` comme en-têtes de colonnes, sans le nom
du device STM32. Le code `23MTS` (date code) polluait l'en-tête.

**Solution :** Après la détection des en-têtes, on applique une regex
`Q3H0[A-Z0-9]+` pour détecter les codes commande. On extrait le nom STM32
depuis la légende via `STM32[A-Za-z0-9]+`. Les headers sont remplacés par
`"STM32N645xx (Q3H0X546N)"`.

**Fichier :** `grid_extractor.py`

---

---

## 6. `_find_caption_y` — Seuil de mots réduit de 3 à 2

**Problème :** `_find_caption_y` cherchait une séquence de **3 mots consécutifs**
de la légende dans le texte de la page. Quand le PDF scindait un mot (ex:
`"Current"` → `"C"` + `"urrent"` sur deux zones de texte distinctes), le
matching échouait car seul 1 mot sur 3 était trouvé → `caption_y = None`
→ pas de filtrage caption → lignes d'une autre table au-dessus conservées
→ **13 tables vides** (table_21 L0, table_36 F7, etc.).

**Solution :** Seuil passé à `min(2, len(caption_words))`. Pour une légende
de 7 mots, on accepte 2 matchs au lieu de 3. Ceci tolère un seul mot scindé
tout en évitant les faux positifs (2 mots sont difficilement visibles par
hasard dans le texte de la page). Le fix `page_height * 0.25` (min_y)
testé précédemment a été abandonné car il bloquait la détection de
légendes situées dans le quart supérieur de la page.

**Fichier :** `grid_extractor.py:_find_caption_y`

---

## 7. Table C5/table_65 — Colonnes "Conditions" dupliquées (guard `cols ≤ 10`)

**Problème :** `_merge_identical_adjacent_columns` avait un guard
`if cols <= 10: return headers, rows` qui empêchait la fusion des colonnes
"Conditions" dupliquées dans les tables ≤10 colonnes. Résultat : headers
à 8 colonnes (dont `"Conditions"` et `"Conditions"` côte à côte) au lieu
de 7.

**Solution :** Le guard a été supprimé. La fusion s'applique maintenant
quel que soit le nombre de colonnes. De plus, l'appel à
`_merge_identical_adjacent_columns` a été **déplacé AVANT** la recherche
de continuation (ligne ~1407 de `grid_extractor.py`) pour que
`find_continuations()` utilise un header sans doublon.

**Fichier :** `grid_extractor.py:_merge_identical_adjacent_columns` (ligne ~2402)

---

## 8. Continuation — Colonne `None` en trop + lignes vides dupliquées

**Problème :** Deux artefacts dans la fusion continuation :

1. **Colonne `None` en trop :** quand une colonne spanning est scindée sur
   la page de continuation, pdfplumber insère parfois une colonne `None`
   supplémentaire (`col_count > expected_col_count`). La boucle d'expansion
   ajoutait alors une colonne vide supplémentaire dans le résultat final.
2. **Lignes vides dupliquées :** la page de continuation répétait parfois la
   première ligne de données (même première cellule, autres cellules vides
   ou identiques), créant des doublons.

**Solution :**
1. Quand `col_count > expected_col_count`, on cale `target_cols` sur
   `expected_col_count` au lieu de `col_count` (la colonne `None` est
   ignorée).
2. Avant l'insertion, on saute toute ligne dont la première cellule est
   identique à la ligne précédente ET dont au moins une autre cellule est
   vide (signe de doublon de continuation).

**Fichier :** `continuation.py:find_continuations`

---

## Résultat final

| Métrique | Valeur |
|----------|--------|
| Datasheets | 185 (20 familles) |
| Tables extraites | 18 694 |
| Tables vides | **0** (88 ordering_info OK, 4 dessins mécaniques préexistants) |
| Crédibilité | 180 high / 5 crashs préexistants |
| Temps full scan | 1656s (27 min) — 16 workers |
| Régressions | **0** ✅ |
