# Rapport d'architecture — Pipeline RAG STM32

> Projet : `C:\Users\user\Desktop\rag1\`
> Objectif : extraire, corriger et indexer les tableaux techniques des datasheets STM32 pour un système RAG (Retrieval-Augmented Generation).

---

## 1. Vue d'ensemble

Le projet est un pipeline en **4 phases** qui transforme des PDFs de datasheets STM32 (familles C0, C5, N6, …) en JSON de tables structurés, prêts pour du RAG.

```
DataSHEET/*.pdf
      │
      ▼   PHASE 1 — Extraction (pdfplumber + règles custom, ~90 % précision)
table_extractor_raw/main.py
      │  → outJason/           (JSON bruts par table + features.json + _all_tables.json)
      │  → capt/               (PNG des pages, vérité terrain visuelle pour le LLM)
      ▼
table_extractor_raw/build_rag_selective.py
      │  → Rag_selective/      (JSON au format RAG sélectif : table_*.json + _all_tables.json + features.json)
      ▼   PHASE 2 — Correction LLM (Gemini Flash multimodal, ~99 %+)
BatchLLMValidation.py
      │  → Correction/         (JSON de corrections + review_summary.json)
      ▼   PHASE 3 — Application des corrections (fusion locale)
ApplyCorrections.py
      │  → correction_Rag/     (JSON finaux corrigés)
      ▼   PHASE 4 — Agrégation finale (après retouches manuelles)
UpdateAllTables.py
      →  *_all_tables.json
```

> **Noms réels du projet** : les dossiers s'appellent `outJason` (et non `outputjson`) et `Rag_selective` (et non `RagSelective`). Le reste de ce rapport utilise les noms réels du terrain.

---

## 2. Arborescence du dépôt

```
rag1/
├── app.py                    # Wrapper Phase 1 → table_extractor_raw/main.py
├── BatchLLMValidation.py     # Phase 2 : validation/correction LLM par batch
├── ApiManager.py             # Gestionnaire intelligent des clés API Gemini (pools)
├── ApplyCorrections.py       # Phase 3 : application des corrections JSON
├── UpdateAllTables.py        # Phase 4 : recrée les *_all_tables.json
├── api_config.json           # Configuration des pools de clés API (A → I, 84 clés max)
├── .env                      # Clés API Gemini (privé, ignoré par git)
├── run_all_families.ps1      # Orchestrateur PowerShell : extraction + RAG par famille
│
├── table_extractor_raw/      # Moteur d'extraction pdfplumber (Phase 1)
│   ├── main.py               # CLI + process_pdf + dédup inter-table
│   ├── config.py             # Seuils qualité + chemins + réglages pdfplumber
│   ├── build_rag_selective.py# outJason/ → Rag_selective/ (sections dual-source)
│   ├── requirements.txt
│   └── core/                 # Modules du moteur
│       ├── toc_detector.py   # Étape 1 : détection des tables (TOC / annotations / scan)
│       ├── grid_extractor.py # Étape 2 : extraction de la grille (3215 lignes)
│       ├── continuation.py   # Tables multi-pages (Table X … (continued))
│       ├── page1_features.py # Features page 1 (packages, device summary, PDF type)
│       ├── glyph_fixer.py    # Correction des glyphes mal encodés
│       ├── quality_flags.py  # Confiance high/medium/low/failed + warnings
│       ├── ordering.py       # Pages "Ordering information scheme" (structured_json)
│       └── schema.py         # Modèle Pydantic RawTable
│
├── DataSHEET/<famille>/…pdf  # PDFs originaux (185 PDFs, ~643 Mo)
├── outJason/<famille>/<ds>/  # JSON bruts (table_N.json, features.json, _all_tables.json, _run_report.json, _reversed_debug.json)
├── capt/<famille>/<ds>/tableau_N/page_P.png  # Images pages (150 DPI) pour le LLM
├── Rag_selective/<famille>/<ds>/  # JSON format RAG sélectif + _section_conflicts.log
├── Correction/<famille>/<ds>/     # JSON de corrections LLM
├── correction_Rag/<famille>/<ds>/ # JSON finaux corrigés + *_all_tables.json
├── ApiLog/api_state.json    # État persistant des clés API (généré au 1er run)
│
├── README.md                # Guide d'utilisation complet (commandes, formats)
├── ARCHITECTURE.md          # Documentation technique détaillée (file:line)
├── ProbSol.md               # Problèmes résolus (fausses continuations, etc.)
├── TEST_PROTOCOL.md         # Protocole de test de masse (dedup inter-table)
├── Rapport_Validation_C5_Complet.md  # Rapport d'exécution LLM famille C5
└── Rapport.md               # Ce rapport
```

---

## 3. Phase 1 — Extraction (`table_extractor_raw/`)

### 3.1 `main.py` (594 lignes) — CLI + orchestration

Point d'entrée du moteur. Arguments : `--pdf`, `--family`, `--all`, `--random N` (mutuellement exclusifs), `--workers`, `--tables 2,5,10,11` (avec `--pdf` uniquement).

Fonctions clés :
- `process_pdf(pdf_path, family, table_ids)` (ligne 183) — pipeline complet par PDF :
  1. `detect_pdf_type` → 1 (Acrobat) ou 2 (Antenna House) selon le Producer PDF.
  2. `detect_tables` (TOC/annotations/scan) → liste de `TableRef`.
  3. **Features page 1** (`features.json`) — indépendant, ne bloque pas l'extraction.
  4. Pour chaque table : `extract_table_grid` → garde-fou dessin mécanique → `correct_footer_in_table` → `_fix_missing_dashes` → notes/légendes → validation Pydantic `RawTable` → écriture `outJason/<fam>/<ds>/table_N.json`.
  5. **Capture PNG** des pages (dpi 150) dans `capt/…/tableau_N/page_P.png`.
  6. `_deduplicate_table_boundaries` — suppression des lignes en doublon entre tables adjacentes (N vs N+1/N+2).
  7. Écrit `_all_tables.json` (features en premier, puis tables).
  8. `build_rag_selective.process_pdf` → génère `Rag_selective/`.
  9. Écrit `_reversed_debug.json` et `_run_report.json`.
- `_deduplicate_table_boundaries` (ligne 107) — pour N6, saute les 6 premières tables ; ne supprime jamais si le résultat devient vide ; met à jour `datasheet_metaData.rows_count`.
- `_fix_missing_dashes` (ligne 54) — remplit par `-` les cellules vides des colonnes à tiret attendu (`parameter`, `conditions`, `symbol`, `min`, `typ`, `max`, `unit`, `value`).
- `_run_parallel` (ligne 499) — `ProcessPoolExecutor` ; commentaire de code : workers=16 provoque un OOM sur les gros PDFs (200+ pages), **workers=8 max stable**.

Notes :
- Force l'UTF-8 sur stdout/stderr Windows (cp1252 ne supporte pas µ, Ω, ✓, →).
- `REPO_ROOT` = racine du dépôt (insérée dans `sys.path` pour `build_rag_selective`).

### 3.2 `config.py` (82 lignes) — Seuils et réglages

| Paramètre | Valeur | Rôle |
|---|---|---|
| `MIN_DATA_ROWS` | 1 | une seule ligne de données est valide (ex : Calibration values) |
| `MAX_EMPTY_CELL_RATIO` | 0.50 | jusqu'à 50 % de vide autorisé (tables Pinout/Features creuses) |
| `MAX_COL_VARIANCE` | 0.40 | tolérance pour les sous-lignes fusionnées |
| `MAX_CONTINUATION_PAGES` | 30 | sécurité anti-boucle infinie |
| `MAX_CONT_COL_DRIFT` | 50 px | vrai continuation < 31 px, fausse > 55 px (200 était trop permissif) |
| `MIN_TABLE_WIDTH` | 20 | Type 2 : en dessous = bandeau décoratif → rejeté |
| `SAVE_DEBUG_IMAGES` | True | crop image à côté du JSON |
| `SAVE_IMAGES_ONLY_ON_ISSUE` | True | image seulement si confidence ≠ high |
| `DEBUG_IMAGE_DPI` | 150 | résolution des crops |
| `DEBUG_EMPTY_ROWS` | True | debug détaillé si 0 ligne |
| `PDFPLUMBER_TABLE_SETTINGS` | lines, snap/join/tol 3 | réglage standard Type 1 |
| `PDFPLUMBER_TABLE_SETTINGS_FALLBACK` | text, tol 3 | fallback interne pdfplumber |
| `PDFPLUMBER_TABLE_SETTINGS_TYPE2` | lines, snap/join/tol 5 | Type 2 Antenna House |
| `PDFPLUMBER_TABLE_SETTINGS_FALLBACK_TYPE2` | text, tol 5 | fallback Type 2 |

Chemins : `OUTPUT_DIR = outJason`, `RAG_DIR = Rag_selective`, `LOG_DIR = table_extractor_raw/logs`.

### 3.3 `core/toc_detector.py` (722 lignes) — Détection des tables

Stratégie : **H2.0 (annotations PDF)** → **H2.1-H2.4 (regex TOC)** → **H2.5 (scan inline)**.

- `TableRef` (dataclass) : `table_id` ("table_12"), `caption`, `page`, `dest_y`, `section`.
- `detect_tables` (l.164) : fusionne les sources (déduplication par `table_id`), trie par page, appelle `_assign_sections`.
- `_st_to_actual_page` (l.52) : convertit les numéros de page imprimés "X/TOTAL" en index PDF réels (mapping complet, fallback offset puis interpolation linéaire). Cache global `_ST_MAPPING_CACHE`.
- `_from_toc_links` (l.466) : annotations `/Link → /GoTo` résolues via `pypdf` (objid → page), texte sous l'annotation via pdfplumber coords, capture `dest_y` (conversion PDF → pdfplumber).
- `_from_toc` (l.553) : TOC multi-pages (max 10), entrées complètes et multi-lignes, pattern `TOC_ENTRY_PATTERN`.
- `_from_toc_reverse` (l.685) : Type 2 — TOC en fin de document (dernières 30 pages), fallback tout le document.
- `_from_inline_scan` (l.701) : scan de chaque page à la recherche de légendes `Table N. …` (complète les manquantes).
- `_assign_sections` (l.232) : construit `_SECTION_CACHE[pdf_path][page] = [(y_top, label)]` via `SECTION_HEADING_PATTERN` + whitelist TOC (`_extract_toc_section_numbers`) ; assigne chaque table à sa section (fallback page-level, `MAX_PAGE_GAP = 20` → "General purpose / Overview").
- `get_section_at` (l.23) : retourne la section la plus proche au-dessus de `y_top`, remonte max 5 pages en arrière.

### 3.4 `core/grid_extractor.py` (3215 lignes) — Extraction de la grille

C'est le module le plus gros et le plus riche en heuristiques. Points d'entrée principaux :
- `extract_table_grid(...)` — pipeline complet d'une table.
- `_extract_from_page` — 3 tentatives : (1) `lines` (settings standard), (2) `text` (fallback), (3) sans finder (bbox = page entière) ; sinon échec.
- `_find_caption_y` — localise la légende (seuil de mots réduit de 3 à 2, voir ProbSol).
- `_expand_spans_and_headers` — reconstituer les colonnes fusionnées.
- `_build_col_groups`, `_fill_horizontal`, `_fill_vertical`, `_fill_identity_spans`, `_ensure_no_empty_cells` — propagation des cellules fusionnées.
- `_fix_reversed_cells` (via `_is_likely_reversed`, `_mid_word_uppers`, `_initial_upper_run`) — texte RTL inversé (ex : `kcolc UPC` → `CPU clock`).
- `_detect_vector_dashes` — détection vectorielle des tirets (chars + lines).
- `_remove_bleed_rows(_bottom)`, `_remove_section_bleed_rows`, `_truncate_at_next_table`, `_remove_trailing_footnotes` — nettoyage des débordements.
- `_merge_identical_adjacent_columns`, `_merge_fragmented_columns`, `_filter_narrow_tables`, `_merge_compatible_tables` — fusion/consolidation de colonnes.
- `_count_header_rows_by_color` — Type 2 : détection du nombre de lignes d'en-tête par fond coloré.
- `_apply_rotated_fix` — texte pivoté à 90° (map `_get_rotated_text_map`).
- `_save_table_crop`, `_save_empty_rows_debug` — captures debug.
- `extract_footnotes_from_pages`, `extract_legend_from_page`, `extract_notes_type1` — notes `(N)` et légendes.
- Debug cellules inversées : `_reset_reversed_debug`, `_get_reversed_debug_entries` (→ `_reversed_debug.json`).

### 3.5 `core/continuation.py` (873 lignes) — Tables multi-pages

- `find_continuations(...)` — chercher les pages suivantes contenant la suite de la table (retourne `merged_pages`, lignes supplémentaires, `target_cols`, `all_col_x0s`, `b7_triggered`).
- `_is_continuation_page` — détection "Table X … (continued)" (`_CONTINUED_RE`) + heuristique de position (bbox en haut de page) + vérification structure des colonnes.
- `_pick_best_continuation` — préférence `lines` (diff cols ≤ 4) > `text` > `text_grid`.
- `_build_text_grid` — fallback texte : regroupement par `top`, saut du titre continued, saut des notes (25 % inférieurs), clustering des x0.
- Expansion/réduction au bon nombre de colonnes : `_expand_cont_row` (distribution uniforme des valeurs réelles), `_expand_cont_row_by_header_spans` (répétition selon runs d'en-têtes), `_expand_cont_row_by_x0s` (détection des colonnes fusionnées par matching x0), `_reduce_cont_row` (essai de toutes les combinaisons de colonnes à dropper — **attention O(C(n,k))**, 184 756 combinaisons pour C(20,10)).
- `_headers_differ` — comparaison ensembliste (Jaccard) des en-têtes, robuste aux abréviations/substrings.
- `_min_drift` — drift minimum entre x0 en sautant chaque position fantôme.
- Fix B7 (l.833) : alignement quand `base_header` a des colonnes dupliquées ("Conditions" ×6 → ×1 en continuation) — `_remove_adjacent_duplicates` + `dup_insert_positions`.
- Fix EOT (fin de table) : troncature à la rencontre d'un titre "Table X." dans les données, gestion des row_ys.
- `MAX_CONT_COL_DRIFT = 50` : les vraies continuations ont des drifts < 31 px, les fausses > 55 px.

### 3.6 `core/page1_features.py` (561 lignes) — Features page 1

- `_detect_pdf_type` — type PDF (1 Acrobat / 2 Antenna House).
- Modèles Pydantic : `ExtractionMeta`, `DeviceFeatures`.
- `extract_features_page_range` — extraction complète : `doc_ref`, `revision`, `date`, `title`, `family`, `core`, `fpu`, `max_frequency_mhz`, `flash_kb`, `ram_kb`, `voltage_min/max_v`, `operating_temp_c`, `packages`, `device_summary`, `extraction_meta.source_pages`.
- `_parse_header_footer` — parse le bandeau header/footer selon `pdf_type`.
- `_parse_packages` — liste des packages (validation dimension via `_is_valid_dim`/`_normalize_dim`, exclusion si le dim appartient à un autre package).
- `_parse_device_summary` / `_parse_device_summary_type2` — table de synthèse du device.
- `_parse_features_bullets` — puces "Features" (flash, ram, freq, voltage, temp).
- `_flash_val`, `_extract_packages_from_pdf`, `_page_text_from_words`, `_get_page_text`, `_extract_text_for_pages`.

### 3.7 `core/glyph_fixer.py` (127 lignes) — Glyphes mal encodés

- `GLYPH_MAP` : table de correspondance empirique (µ, Ω, °, ², ³, tirets, guillemets, flèches ↑ ↓ →).
- `CID_PATTERN` : nettoyage des glyphes `(cid:N)` non décodables (polices sans ToUnicode).
- `FOOTER_PATTERN` : `DSxxxx - Rev x page xx/xx` (contamination de pied de page).
- `fix_text`, `fix_headers`, `fix_rows`, `correct_footer_in_table`.

### 3.8 `core/quality_flags.py` (120 lignes) — Confiance et warnings

- `compute_empty_cell_ratio` — proportion de cellules vides.
- `compute_col_variance` — variance normalisée du nb de colonnes.
- `evaluate_table` — décision : `failed` (aucun contenu) → `low` (pas de headers / empty > max / variance > max) → `medium` (empty > 60 % du max / peu de lignes) → `high`.
- Warnings : `no_content_extracted`, `no_headers_detected`, `few_data_rows`, `high_empty_ratio`, `inconsistent_col_count`, `header_row_ambiguous`, `vertical_merge_suspected`, `unmapped_cid_glyphs_detected`.
- Note : `has_empty_cells` n'influence PAS la confiance (indicateur séparé pour le rapport).

### 3.9 `core/ordering.py` (221 lignes) — Pages "Ordering information scheme"

Pages non-grille qui décomposent le code produit STM32 en segments.
- `extract_ordering_info(page_text, doc_id, table_id, page)` → `{"structured_json": {...}, "rag_chunks": [...]}`.
- Parsing catégories (lignes sans `=`), options (`code = meaning`), exemple de code (`Example:`), footers ignorés.
- Génération de chunks RAG : un chunk "exemple global" + un chunk par catégorie.

### 3.10 `core/schema.py` (59 lignes) — Modèle Pydantic `RawTable`

Champs : `table_id`, `caption`, `pdf_name`, `family`, `page`, `merged_pages`, `url`, `url_table`, `section`, `headers`, `rows`, `extraction_method` (`pdfplumber`/`pdfplumber_text`/`camelot_lattice`/`camelot_stream`/`docling`/`failed`), `extraction_confidence` (`high`/`medium`/`low`/`failed`), `empty_cell_ratio`, `col_count`, `status`, `has_empty_cells`, `heuristics`, `structured_json`, `warnings`.

### 3.11 `build_rag_selective.py` (846 lignes) — outJason → Rag_selective

- **Détection de section double source** (`get_section_dual`) : priorité Y-position → outline pypdf (`get_bookmarks`) + page Contents (`get_toc_from_contents_page`) en cross-validation.
- Niveaux de confiance section : `confirmed_dual` > `confirmed_y` > `single_source` > `conflict` (prend outline) > `y_position_fallback` > `none`.
- Logs d'audit dans `Rag_selective/` : `_section_conflicts.log`, `_section_missing.log`, `_section_single_source.log`, `_errors.log`.
- `transform_table` → format "une seule table" (table_id, document, rev, table_number, title, page, section, section_title, semantic_type, tags, url, url_pdf, text_helper, table_content{headers, rows, notes, legend}).
- `_build_complete_doc` → format "document complet" (ajoute doc_ref, package, family, core, frequency) pour `*__all_tables.json`.
- `transform_features` → `features.json` au format RAG simplifié (`features` paires + `features_content` paires).
- Nommage sortie : `DSxxxxx_Rev_N_table_X.json` / `DSxxxxx_Rev_N__all_tables.json` (préfixe doc_ref + revision si dispo, sinon `pdf_name`).
- `_text_helper` : caption + section + aperçu des 5 premiers headers + "(table vide)" si 0 ligne, tronqué à 300 chars.
- CLI autonome : `--family F`, `--pdf-name NAME`, `--all`.

---

## 4. Phase 2 — Correction LLM (`BatchLLMValidation.py`, 496 lignes)

### Flux
1. Pour chaque datasheet de `Rag_selective/<fam>/<ds>/`, liste les `*_table_*.json` (hors all_tables), triés par numéro.
2. Reprise automatique : un fichier déjà présent dans `Correction/` est ignoré ("Déjà traité").
3. Pour chaque table : charge les images `capt/<fam>/<ds>/tableau_N/page_P.png`, le texte PDF positionné (`mots@x0` par ligne), et le JSON source (`table_content` + `warnings` injectés).
4. Envoi à Gemini (`gemini-flash-latest`, température 0, `response_mime_type="application/json"`, `thinking_budget=8000`) avec **3 sources** : image(s) + texte positionné + JSON brut.
5. Parsing JSON + filtrage par les BOUCLIERS + sauvegarde dans `Correction/<fam>/<ds>/`.
6. Génère `review_summary.json` (tables MANUAL_REVIEW_NEEDED / ERRORS_FOUND) par datasheet.
7. Écrit `Rapport_Validation_<Famille>_Complet.md`.

### Gestion API
- `max_retries = 30` par table.
- 429/503/RESOURCE_EXHAUSTED → `report_rate_limit` (60 s).
- quota/daily → `report_exhausted` (24 h).
- erreur permanente → `report_permanent_error` (24 h).
- `time.sleep(60)` toutes les 6 tables soumises (par datasheet).

### BOUCLIERS de sécurité (côté Python)
- **BOUCLIER-1** : interdit de détruire un vrai texte (`valeur_originale_json` informatif → remplacé par vide/tiret).
- **BOUCLIER-2** : remplissage d'une case vide/tiret → confiance ≥ 90 obligatoire.
- **BOUCLIER-3** : suppression > 20 % des lignes (ou > 5) → passage en MANUAL_REVIEW_NEEDED.
- **BOUCLIER-4** : toute correction de confiance < 80 → passage en MANUAL_REVIEW_NEEDED (les propositions sont conservées dans le fichier).
- Sécurisation du schéma : `erreurs_corrigees` doit être une liste de dicts (sinon ignoré).

### Statuts LLM possibles
| Statut | Signification | Comportement aval |
|---|---|---|
| `OK` | Aucune erreur détectée, table parfaite | `ApplyCorrections` ne modifie rien |
| `ERRORS_FOUND` | Erreurs détectées et corrigées | `ApplyCorrections` applique `erreurs_corrigees` + `lignes_en_trop_supprimees` + `lignes_manquantes_ajoutees` |
| `MANUAL_REVIEW_NEEDED` | Structure trop complexe / confiance < seuils | `ApplyCorrections` ne fait rien sauf si `confirmation == "OK"` ; table copiée telle quelle |
| `ERROR` | Erreur critique LLM (parse) | statistique `error`, non sauvegardé |

---

## 5. Phase 3 — Application des corrections (`ApplyCorrections.py`, 356 lignes)

Fusion `Rag_selective/` + `Correction/` → `correction_Rag/`. 100 % local (`ThreadPoolExecutor`, workers=6 par défaut).

Ordre d'application (`apply_correction`) :
1. **Suppressions** (`lignes_en_trop_supprimees`) : indices triés décroissant ; **sécurité** : la ligne supprimée doit matcher ≥ 2 cellules d'en-tête, sinon rejet (index halluciné par le LLM).
2. **Insertions** (`lignes_manquantes_ajoutees`) : tri croissant avec compensation d'offset.
3. **Corrections de cellules** (`erreurs_corrigees`) : sur la ligne ciblée (`row_index`), puis **propagation globale** à toutes les lignes de la même colonne ayant la même valeur (avec strip).
4. Sécurité : **jamais** toucher une case vide `""` ; `[WARN-MISMATCH]` si la valeur originale attendue ne correspond pas.

Comportement par statut :
- `ERRORS_FOUND` → applique.
- `MANUAL_REVIEW_NEEDED` + `confirmation == "OK"` → applique (validation manuelle faite).
- `MANUAL_REVIEW_NEEDED` (sans OK) → copie l'original.
- pas de fichier de correction → copie l'original (`no_corr`).

Sortie : `DSxxxxx_Rev_N_table_X.json` + agrégat `DSxxxxx_Rev_N_all_tables.json` par datasheet.

---

## 6. Phase 4 — Agrégation (`UpdateAllTables.py`, 61 lignes)

Recrée `*_all_tables.json` à partir des tables individuelles de `correction_Rag/<fam>/<ds>/` (tri par `table_N`). À utiliser après correction manuelle de tables `MANUAL_REVIEW_NEEDED` directement dans `correction_Rag/`.

---

## 7. Gestion des clés API (`ApiManager.py`, 234 lignes)

- `api_config.json` : 9 pools (A : clés 1-10, B : 11-18, C : 19-25, D : 26-34, E : 35-44, F : 45-54, G : 55-64, H : 65-74, I : 75-84). Index **1-based**.
- Rotation : **Round-Robin inter-pools** (curseur global), puis dans chaque pool la clé au `last_used` le plus ancien parmi les disponibles.
- Persistance : `ApiLog/api_state.json` (status, unblock_time, last_used, nb_used, nb_errors, tokens_in, tokens_out) — généré automatiquement au premier run.
- Si toutes les clés sont bloquées : attend jusqu'au prochain déblocage (lock libéré pendant le sleep).
- Durées : `BLOCK_1MIN = 60`, `BLOCK_24H = 86400`.

---

## 8. Schéma des données

### 8.1 `outJason/<fam>/<ds>/table_N.json` (brut)
```json
{
  "table_id": "table_12",
  "caption": "Table 12. I2C characteristics",
  "pdf_name": "stm32c011d6",
  "family": "C0",
  "page": 29,
  "merged_pages": [29],
  "url": "", "url_table": "",
  "section": "5.3.6 Supply current characteristics",
  "headers": ["Symbol", "Parameter", "Min", "Typ", "Max", "Unit"],
  "rows": [["...", "..."]],
  "extraction_method": "pdfplumber",
  "extraction_confidence": "high",
  "empty_cell_ratio": 0.0,
  "col_count": 6,
  "status": null,
  "has_empty_cells": false,
  "heuristics": { "_notes": [], "_legend": "" },
  "warnings": [],
  "datasheet_metaData": {
    "pdf_name": "...", "table_id": "table_12", "is_continued": false,
    "pages": [29], "rows_count": 10, "cols_count": 6,
    "confidence": "high", "empty_cell_ratio": 0.0
  }
}
```

### 8.2 `Rag_selective/<fam>/<ds>/DSxxxx_Rev_N_table_1.json` (RAG sélectif)
```json
{
  "table_id": "table_1", "document": "DS13866 Rev 5 - Table 1. ...",
  "rev": "Rev 5", "table_number": "1", "title": "Table 1. ...",
  "page": 1, "section": "4.1 ...", "section_title": "...",
  "semantic_type": "", "tags": [],
  "url": "https://www.st.com/resource/en/datasheet/<pdf>.pdf#page=1",
  "url_pdf": "https://www.st.com/resource/en/datasheet/<pdf>.pdf",
  "text_helper": "Table 1. ... — section 4.1 ....",
  "table_content": { "headers": [...], "rows": [...], "notes": [], "legend": "", "semantic_type": "", "semantic": {} }
}
```

### 8.3 `Correction/<fam>/<ds>/table_12.json`
```json
{
  "table_id": "table_12",
  "status": "ERRORS_FOUND",
  "logs": [],
  "erreurs_corrigees": [
    {
      "ligne": "PC15- OSCX_OUT",
      "row_index": 1,
      "analyse_visuelle": "Espace parasite après PC15-",
      "corrections": [
        { "colonne_index": 4, "valeur_originale_json": "PC15- OSCX_OUT (PC15)", "nouvelle_valeur": "PC15-OSCX_OUT (PC15)", "confiance": 98 }
      ]
    }
  ],
  "lignes_manquantes_ajoutees": [],
  "lignes_en_trop_supprimees": [ { "row_index": 6, "raison": "Ligne d'en-tête répétée (saut de page)" } ]
}
```

---

## 9. Outillage et vérification

- `run_all_families.ps1` : boucle sur chaque famille — `main.py --family X --workers N` (workers selon nb PDFs : 4/8/12/16) puis `build_rag_selective.py --family X` ; logging horodaté dans `run_all_families.log`.
- `TEST_PROTOCOL.md` : protocole de test du dedup inter-table (test unitaire C5, scan famille, scan 185 PDFs, vérifs de non-régression).
- `Rapport_Validation_C5_Complet.md` : exemple de rapport LLM (famille C5, 88 tables — toutes "Déjà traitées/ignorées").

---

## 10. Git / GitHub (état actuel)

- Remote : `https://github.com/MaleBenAttia/StGenAI.git` (HTTPS).
- Branche `malek` : poussée propre et vérifiée (`git ls-remote` → `04400b7a… refs/heads/malek` ; `git status` → up to date). Commit racine `04400b7a`, ~1548 fichiers.
- Branche locale `malek_full_history` : conserve l'ancien historique (3 Go, 35 025 modifs) — non poussée.
- SSH : clé `id_rsa` non autorisée sur GitHub → abandon (PAT sans `admin:public_key`).
- `.gitignore` : ignore `venv/`, `__pycache__/`, `outJason/`, `DataSHEET/`, `Correction/`, `capt/`, `ApiLog/`, `table_extractor_raw/logs/`, `*.log`, `.env`, secrets, etc.

---

## 11. Points de vigilance / limites connues

1. **Workers mémoire** : workers > 8 → OOM sur les gros PDFs (200+ pages, famille H5). Plafond stable = 8.
2. **`_reduce_cont_row`** : complexité combinatoire O(C(n,k)) sur les continuations larges (à surveiller).
3. **Reprise LLM** : `BatchLLMValidation` ignore les fichiers déjà présents dans `Correction/` → ne pas supprimer la famille de Correction pour un retraitement complet sans re-générer.
4. **`MANUAL_REVIEW_NEEDED`** : nécessite une validation manuelle (`confirmation: "OK"`) avant application ; sinon copie de l'original.
5. **En-têtes dupliqués ("Conditions" ×N)** : gérés par les Fix B7 / expansion x0s / `_remove_adjacent_duplicates`.
6. **`has_empty_cells` ≠ confiance** : une table `high` peut contenir des cellules vides (remplies par propagation).
7. **Déduction famille** : pour `--pdf`, la famille est déduite du nom du dossier parent (ex : `DataSHEET/C0/stm32c011d6.pdf` → C0).
8. **Encoding** : les scripts forcent l'UTF-8 (console Windows) ; les JSON sont écrits en `ensure_ascii=False, indent=2`.
9. **Scripts évoqués dans les docs mais non présents à la racine** : `check_quality.py`, `aggregate_stats.py`, `table_extractor_raw/generate_debug_report.py`, `rag_transformer.py` (mentionné dans ARCHITECTURE.md et commentaire `main.py:41`). Vérifier leur présence avant usage.
