# GUIDE COMPLET DE L'ARCHITECTURE & DES COMMANDES — RAG1 STM32

Ce document est le **guide de référence** pour tout développeur ou **Agent IA** devant comprendre, exécuter, déboguer ou faire évoluer le projet `rag1`.

---

## 1. Vue d'Ensemble & Pipeline RAG

Le projet `rag1` extrait, valide, corrige et indexe la totalité des **tableaux techniques** et **figures de brochage (pinout/ballout)** des datasheets microcontrôleurs STM32 pour alimenter une base de connaissances vectorielle / RAG haute fidélité.

```
                  ┌───────────────────────────────┐
                  │    Input/PDFs/<FAMILLE>/...   │ (Datasheets bruts)
                  └───────────────┬───────────────┘
                                  │
      ┌───────────────────────────┴───────────────────────────┐
      │ (Extraction Tableaux)                                 │ (Extraction Pinout/Ballout)
      ▼                                                       ▼
┌───────────────────────────┐                           ┌───────────────────────────┐
│ app.py                    │                           │ PipelineViaLLM/Figure.py  │
│ (table_extractor_raw)     │                           │ (Multimodal Gemini Vision)│
└─────────────┬─────────────┘                           └─────────────┬─────────────┘
              │                                                       │
              ├──────────────────────────────────────┐                │
              ▼                                      ▼                ▼
┌───────────────────────────┐          ┌───────────────────────────┐  ┌───────────────────────────┐
│ Output/Json/Raw_Extracted │          │ Output/Images/            │  │ Output/Images/            │
│ (JSON bruts pdfplumber)   │          │ Tables_Screenshots        │  │ Figures_Screenshots       │
└─────────────┬─────────────┘          └─────────────┬─────────────┘  └─────────────┬─────────────┘
              │ (build_rag_selective.py)             │                              │
              ▼                                      │                              ▼
┌───────────────────────────┐                        │                ┌───────────────────────────┐
│ Output/Json/              │                        │                │ Output/Json/              │
│ Selective_Tables          │                        │                │ Final_Figures             │
└─────────────┬─────────────┘                        │                │ (Brochages formatés RAG)  │
              │                                      │                └───────────────────────────┘
              └───────────────────┬──────────────────┘
                                  │ (BatchLLMValidation.py)
                                  ▼
                    ┌───────────────────────────┐
                    │ Output/Json/              │
                    │ LLM_Corrections           │
                    └─────────────┬─────────────┘
                                  │
                 ┌────────────────┴────────────────┐
                 │ (Tables OK / Corrigées)         │ (Status: MANUAL_REVIEW_NEEDED)
                 ▼                                 ▼
┌───────────────────────────┐        ┌───────────────────────────┐
│ ApplyCorrections.py       │        │ PipelineViaLLM/           │
│                           │        │ MANUAL_REVIEW.py          │
└─────────────┬─────────────┘        └─────────────┬─────────────┘
              │                                    │
              │                                    ▼
              │                      ┌───────────────────────────┐
              │                      │ Output/Json/Manual_Review │
              │                      └─────────────┬─────────────┘
              │                                    │
              └───────────────────┬────────────────┘
                                  │ (ApplyCorrections.py / UpdateAllTables.py)
                                  ▼
                    ┌───────────────────────────┐
                    │ Output/Json/Final_Tables  │
                    │ (*_all_tables.json)       │
                    └───────────────────────────┘
```

---

## 2. Dictionnaire des Dossiers & Fichiers

### 📁 `Input/`
Dossier racine contenant l'ensemble des données d'entrée brutes (non modifiables).
- **`Input/PDFs/<FAMILLE>/<datasheet>.pdf`** :
  - **Rôle** : Les fichiers PDF officiels de STMicroelectronics classés par famille (ex: `C0/`, `C5/`, `F1/`, `N6/`...).
  - **Exemple** : `Input/PDFs/C0/stm32c011d6.pdf`

---

### 📁 `Output/`
Dossier racine regroupant tous les artefacts générés par le pipeline.
- **`Output/Images/`** :
  - **`Tables_Screenshots/<FAMILLE>/<datasheet>/tableau_<N>/page_<P>.png & .pdf`** : Captures d'écran haute définition et pages unitaires PDF des tableaux extraits (utilisées par le LLM pour valider/corriger la structure).
  - **`Figures_Screenshots/<FAMILLE>/<datasheet>/page_<P>.png & .pdf`** : Captures 600 DPI avec filtres de netteté/contraste des schémas de Pinout & Ballout.
- **`Output/Json/`** :
  - **`Raw_Extracted/<FAMILLE>/<datasheet>/`** : JSON bruts initiaux produits par `pdfplumber` (contenant cellules, lignes, méta).
  - **`Selective_Tables/<FAMILLE>/<datasheet>/`** : Tables nettoyées et converties au schéma officiel RAG avant passage LLM.
  - **`LLM_Corrections/<FAMILLE>/<datasheet>/`** : JSON validés/corrigés par Gemini Flash (`status`: `OK`, `CORRECTED`, ou `MANUAL_REVIEW_NEEDED`).
  - **`Manual_Review/<FAMILLE>/<datasheet>/`** : Tables complexes re-corrigées automatiquement par `MANUAL_REVIEW.py` avec le prompt expert.
  - **`Final_Tables/<FAMILLE>/<datasheet>/`** : JSON finaux unifiés après application des corrections + fichier d'agrégation `DSxxxx_Rev_x_all_tables.json`.
  - **`Final_Figures/<FAMILLE>/<datasheet>/`** : JSON finaux des figures de Pinout / Ballout avec liste exhaustive des broches (sans matrice creuse).
- **`Output/Reports/`** :
  - **Rôle** : Documentation technique, rapports de validation de famille (`Rapport.md`, `Rapport_Validation_C5_Complet.md`), protocoles de tests (`TEST_PROTOCOL.md`), notes d'architecture (`ARCHITECTURE.md`) et résolutions de problèmes (`ProbSol.md`).

---

### 📁 `PipelineViaLLM/`
Regroupe tous les scripts Python exploitant les modèles LLM multimodaux Gemini & l'orchestration des clés API.
- **`ApiManager.py`** : Moteur de répartition de charge multi-clés Gemini (Round-Robin inter-pools, gestion des 429 RPM/TPM avec backoff exponentiel, détection et mise en quarantaine 24h immédiate des clés 401 invalides).
- **`api_config.json`** : Configuration des pools de clés API (Pool A, B, C...).
- **`BatchLLMValidation.py`** : Validation et correction Phase 2 (envoie prompt + JSON existant + image + page PDF à Gemini).
- **`MANUAL_REVIEW.py`** : Re-traitement ciblé Phase 2-bis pour toutes les tables ayant reçu le statut `MANUAL_REVIEW_NEEDED`.
- **`Figure.py`** : Détecte les schémas de boîtiers (TOC/bookmarks) et extrait chaque brochage/ballout en JSON standardisé RAG.
- **`ApplyCorrections.py`** : Fusionne les tables sources avec les corrections LLM pour produire les tables finales.
- **`UpdateAllTables.py`** : Fusionne toutes les tables individuelles d'un datasheet en un fichier unique `*_all_tables.json`.
- **`Prompt_Tables.txt`** : Prompt expert d'ingénierie inversée pour la correction de tableaux STM32 complexes.

---

### 📁 `table_extractor_raw/`
Moteur heuristique d'extraction de tableaux (Phase 1).
- **`main.py`** : Orchestrateur d'extraction PDF (`pdfplumber`, `PyMuPDF/fitz`).
- **`build_rag_selective.py`** : Convertit les tables de `Raw_Extracted/` vers `Selective_Tables/`.
- **`config.py`** : Seuils de tolérance, détection Type 1 (Acrobat) / Type 2 (Antenna House).
- **`core/`** : Modules d'extraction de grilles (`grid_extractor.py`), détection TOC (`toc_detector.py`), capture des features en page 1 (`page1_features.py`), et validation de schéma (`schema.py`).

---

### 📁 `ApiLog/`
- **`api_state.json`** : État dynamique persisté des clés API (nombre d'appels, erreurs, tokens consommés, statut de blocage).

---

### 📄 Fichiers Racine
- **`app.py`** : Point d'entrée racine simplifié pour lancer l'extraction Phase 1.
- **`run_all_families.ps1`** : Script PowerShell automatisant le passage de Phase 1 sur toutes les familles présentes dans `Input/PDFs`.
- **`.env`** : Clés d'API Gemini (chargées automatiquement par `ApiManager.py`).
- **`README.md`** : Vue d'ensemble du projet.
- **`AGENT_GUIDE.md`** : Ce document.

---

## 3. Guide des Commandes (CLI Reference)

Toutes les commandes doivent être exécutées depuis la **racine du projet (`rag1/`)**.

### 🔹 Étape 1 : Extraction Brute des Tableaux (pdfplumber)
```bash
# Traiter un seul PDF :
python app.py --pdf Input/PDFs/C0/stm32c011d6.pdf

# Traiter toute une famille de microcontrôleurs :
python app.py --family C0 --workers 8

# Traiter l'ensemble des familles du dossier Input/PDFs :
python app.py --all --workers 16

# Convertir les JSON bruts en tables RAG structurées :
python table_extractor_raw/build_rag_selective.py --family C0
```

---

### 🔹 Étape 2 : Validation & Correction par Gemini LLM
```bash
# Lancer la validation LLM sur une famille entière :
python PipelineViaLLM/BatchLLMValidation.py --family C5 --workers 4

# Lancer la validation sur un seul datasheet :
python PipelineViaLLM/BatchLLMValidation.py --family C5 --datasheet stm32c532cb --workers 2

# Optionnel : Forcer le re-traitement même si déjà en cache :
python PipelineViaLLM/BatchLLMValidation.py --family C5 --force
```

---

### 🔹 Étape 2-Bis : Re-traitement Automatique des Tables en Revue Manuelle
```bash
# Traiter toutes les tables marquées "MANUAL_REVIEW_NEEDED" d'une famille :
python PipelineViaLLM/MANUAL_REVIEW.py --family C5 --workers 2

# Traiter les tables en revue manuelle d'un seul datasheet :
python PipelineViaLLM/MANUAL_REVIEW.py --family C5 --pdf stm32c591ce --workers 1
```

---

### 🔹 Étape 3 : Extraction des Figures de Brochage (Pinout & Ballout)
```bash
# Extraire les figures d'un seul PDF :
python PipelineViaLLM/Figure.py --pdf Input/PDFs/C0/stm32c011d6.pdf

# Extraire les figures de toute une famille :
python PipelineViaLLM/Figure.py --family C0 --workers 2
```

---

### 🔹 Étape 4 : Application des Corrections & Fusion Finale
```bash
# Appliquer les corrections validées pour générer Final_Tables/ :
python PipelineViaLLM/ApplyCorrections.py --family C5 --workers 4

# Appliquer sur un seul datasheet :
python PipelineViaLLM/ApplyCorrections.py --family C5 --datasheet stm32c532cb

# Recréer le fichier combiné *_all_tables.json après retouches :
python PipelineViaLLM/UpdateAllTables.py --family C5
```

---

### 🔹 Automatisation Globale Multi-Familles (PowerShell)
```powershell
# Exécuter les étapes d'extraction sur toutes les familles séquentiellement :
.\run_all_families.ps1
```

---

## 4. Règles d'Or pour les Agents IA

1. **Chemins d'accès** : Toujours utiliser les chemins normalisés (`Input/PDFs`, `Output/Json/...`, `Output/Images/...`, `PipelineViaLLM/`). Ne jamais recréer les anciens dossiers dépréciés (`DataSHEET`, `capt`, `outJason`, `Rag_selective`, `Correction`, `correction_Rag`).
2. **Gestion des clés API** : `ApiManager.py` gère automatiquement la rotation, les quotas et l'exclusion des clés 401. Ne jamais modifier manuellement `ApiLog/api_state.json` pendant un run.
3. **Parallélisme & Workers** : Pour les appels LLM (`BatchLLMValidation.py`, `MANUAL_REVIEW.py`), privilégier 2 à 4 workers afin d'éviter la saturation simultanée des quotas RPM des pools de clés.
