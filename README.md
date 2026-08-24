# RAG STM32 — Pipeline d'extraction et correction des tableaux de datasheets

Pipeline complet pour extraire, corriger et indexer les tableaux techniques des datasheets STM32 (familles C0, N6, etc.) pour un système RAG (Retrieval-Augmented Generation).

---

## Architecture du pipeline

```
PDF Datasheet STM32
      │
      ▼
┌─────────────────────────────────────┐
│  PHASE 1 — Extraction               │  app.py → table_extractor_raw/
│  Outil : pdfplumber + règles custom │  Précision : ~90%
│  Sortie : Output/Json/Selective_Tables/  │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│  PHASE 2 — Correction LLM           │  BatchLLMValidation.py + Gemini Flash
│  Outil : Gemini Flash multimodal    │  Précision : ~99%+
│  Sortie : Output/Json/LLM_Corrections/  │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│  PHASE 3 — Application              │  ApplyCorrections.py
│  Outil : Script Python              │  Fusion source + corrections LLM
│  Sortie : Output/Json/Final_Tables/ │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│  PHASE 4 — Agrégation finale        │  UpdateAllTables.py
│  Sortie : *_all_tables.json         │  Après retouches manuelles
└─────────────────────────────────────┘
```

---

## Structure des dossiers

```
rag1/
├── requirements.txt          # Dépendances Python du projet complet
├── app.py                    # Point d'entrée Phase 1 (extraction PDF)
├── run_all_families.ps1      # Automatisation multi-familles
├── .env                      # Clés API Gemini (privé, non versionné)
│
├── table_extractor_raw/      # Moteur d'extraction pdfplumber (Phase 1)
│
├── PipelineViaLLM/           # Scripts de validation, correction & enrichissement LLM
│   ├── ApiManager.py         # Gestionnaire intelligent des clés API (pools)
│   ├── BatchLLMValidation.py # Phase 2 : Correction LLM par batch
│   ├── MANUAL_REVIEW.py      # Re-correction automatique des tables complexes
│   ├── ApplyCorrections.py   # Phase 3 : Application des corrections JSON
│   ├── UpdateAllTables.py    # Phase 4 : Recrée all_tables.json après retouches
│   ├── Figure.py             # Extraction des figures Pinout/Ballout
│   ├── Prompt_Tables.txt     # Prompt expert pour la correction manuelle
│   └── api_config.json       # Configuration des pools de clés API
│
├── Input/
│   └── PDFs/                 # PDFs originaux STM32, organisés par famille
│       └── C0/
│           └── stm32c011d6.pdf
│
├── Output/
│   ├── Images/
│   │   ├── Tables_Screenshots/   # Images PNG des pages PDF (pour le LLM)
│   │   │   └── C0/stm32c011d6/tableau_12/page_29.png
│   │   └── Figures_Screenshots/  # Images PNG des figures Pinout/Ballout
│   │       └── C0/stm32c011d6/page_27.png
│   │
│   ├── Json/
│   │   ├── Raw_Extracted/        # JSON bruts extraits par pdfplumber
│   │   ├── Selective_Tables/     # JSON filtrés et structurés (Phase 1 output)
│   │   │   └── C0/stm32c011d6/DS13866_Rev_5_table_1.json
│   │   ├── LLM_Corrections/      # JSON de corrections LLM (Phase 2 output)
│   │   │   └── C0/stm32c011d6/DS13866_Rev_5_table_12.json
│   │   ├── Final_Tables/         # JSON finaux corrigés (Phase 3 output)
│   │   │   └── C0/stm32c011d6/DS13866_Rev_5_all_tables.json
│   │   ├── Final_Figures/        # JSON des figures Pinout/Ballout
│   │   └── Manual_Review/        # JSON re-corrigés par MANUAL_REVIEW.py
│   │
│   └── Reports/                  # Documentation et rapports
│       ├── ARCHITECTURE.md
│       ├── Rapport.md
│       └── ...
│
└── ApiLog/
    └── api_state.json        # État persistant des clés API (généré automatiquement)
```

---

## ⚙️ Installation & Initialisation

### 1. Prérequis
- **Python** 3.10 ou supérieur
- **PowerShell** (Windows) ou **Bash** (Linux/macOS)

### 2. Création de l'environnement virtuel (`venv`)

À la racine du projet (`rag1/`) :

```bash
# Créer le dossier venv
python -m venv venv
```

### 3. Activation de l'environnement virtuel

- **Sur Windows (PowerShell) :**
  ```powershell
  .\venv\Scripts\Activate.ps1
  ```
  *(En cas d'erreur de restriction de script PowerShell, exécuter d'abord : `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process`)*

- **Sur Windows (Invite de commandes CMD) :**
  ```cmd
  .\venv\Scripts\activate.bat
  ```

- **Sur Linux / macOS (Bash/Zsh) :**
  ```bash
  source venv/bin/activate
  ```

### 4. Installation des dépendances

Une fois l'environnement virtuel activé (le préfixe `(venv)` apparaît dans votre terminal) :

```bash
# Mettre à jour pip (recommandé)
python -m pip install --upgrade pip

# Installer toutes les dépendances requises
pip install -r requirements.txt
```

### 5. Configuration du fichier `.env`

Copiez l'exemple de configuration et renseignez vos clés API Gemini :

```bash
# Sous Windows (PowerShell)
Copy-Item .env.example .env

# Sous Linux/macOS
cp .env.example .env
```

Éditez ensuite `.env` pour insérer vos clés API (`GEMINI_API_KEY1=...`).

---

## Phase 1 — Extraction PDF (`app.py`)

Wrapper du moteur d'extraction `table_extractor_raw/`. Lit les PDFs avec `pdfplumber`, détecte et sérialise les tableaux en JSON structuré.

### Usage
```bash
python app.py --pdf Input/PDFs/C0/stm32c011d6.pdf   # Un seul PDF
python app.py --family C0                             # Toute une famille
python app.py --all                                   # Tous les PDFs
```

### Format JSON produit
```json
{
  "table_id": "table_12",
  "document": "DS13866 Rev 5",
  "page": 29,
  "table_content": {
    "headers": ["Pin / SO8N", "Pin name (function upon reset)", "..."],
    "rows": [
      ["1", "PC14-OSCX_IN (PC14)", "I/O", "..."],
      ["8", "PC15-OSCX_OUT (PC15)", "I/O", "..."]
    ],
    "notes": ["1. RST I/O structure when..."]
  }
}
```

### Limites connues (~10% d'erreurs)
- Texte RTL inversé (ex: `kcolc UPC` → `CPU clock`)
- Cellules fusionnées laissées vides (`""`)
- Lignes d'en-tête répétées après un saut de page
- Indices/exposants mal positionnés (ex: `V DD` au lieu de `VDD`)

---

## Phase 2 — Correction LLM (`BatchLLMValidation.py`)

Envoie chaque tableau à Gemini Flash en mode multimodal avec 3 sources simultanées :
1. **Image PNG** de la page (vérité terrain visuelle)
2. **Texte positionné** extrait par pdfplumber (résolution RTL)
3. **JSON brut** de la Phase 1 (objet à corriger)

### Règles du prompt LLM
| Règle | Description |
|-------|-------------|
| RTL inversé | Détecte et corrige le texte vertical lu à l'envers |
| Codes composants | Corrige les codes STM32 mal orthographiés dans les en-têtes |
| Corrections globales | Si une erreur se répète sur plusieurs lignes, corrige **toutes** les lignes |
| Anti-destruction | Interdit de vider une cellule contenant du texte |
| Cases vides | Ne jamais toucher aux cellules `""` |
| Lignes en trop | Les en-têtes répétés (sauts de page) → `lignes_en_trop_supprimees` |

### Boucliers de sécurité (côté Python)
- **BOUCLIER-1** : Interdit d'écraser un vrai texte par un tiret ou vide
- **BOUCLIER-2** : Remplissage d'une case vide seulement si confiance ≥ 90%
- **BOUCLIER-3** : Annule la suppression si le LLM veut supprimer > 20% des lignes

### Usage
```bash
# Un seul datasheet
python BatchLLMValidation.py --family C0 --datasheet stm32c011d6 --workers 2

# Toute une famille
python BatchLLMValidation.py --family C0 --workers 4
```

### Format du JSON de correction produit
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
        {
          "colonne_index": 4,
          "valeur_originale_json": "PC15- OSCX_OUT (PC15)",
          "nouvelle_valeur": "PC15-OSCX_OUT (PC15)",
          "confiance": 98
        }
      ]
    }
  ],
  "lignes_manquantes_ajoutees": [],
  "lignes_en_trop_supprimees": [
    { "row_index": 6, "raison": "Ligne d'en-tête répétée (saut de page)" }
  ]
}
```

### Statuts possibles
| Statut | Description |
|--------|-------------|
| `OK` | Aucune erreur détectée, table parfaite |
| `ERRORS_FOUND` | Erreurs détectées et corrigées dans le JSON |
| `MANUAL_REVIEW_NEEDED` | Structure trop complexe, revue manuelle requise |

---

## Gestionnaire de clés API (`ApiManager.py`)

Système intelligent de rotation des clés API Gemini, conçu pour maximiser l'utilisation de multiples projets Google Cloud sans erreur 429.

### Fonctionnement
- **Pools configurables** via `api_config.json` : chaque pool = un projet Google Cloud distinct
- **Rotation Round-Robin inter-pools** : les workers utilisent des pools différents en alternance
- **Clé la moins récente en priorité** dans chaque pool (`last_used`)
- **Persistance** entre les exécutions via `ApiLog/api_state.json`

### Gestion des erreurs API
| Erreur | Action | Durée du blocage |
|--------|--------|-----------------|
| 429 Rate Limit (RPM/TPM) | `report_rate_limit()` | 60 secondes |
| Quota journalier épuisé (RPD) | `report_exhausted()` | 24 heures |
| Erreur permanente | `report_permanent_error()` | 24 heures |
| Toutes clés bloquées | Attente automatique | Jusqu'au prochain déblocage |

### Configuration des pools (`api_config.json`)
```json
{
  "pools": {
    "pool_A": [1, 10],
    "pool_B": [11, 18],
    "pool_C": [19, 25],
    "pool_D": [26, 34],
    "pool_E": [35, 44],
    "pool_F": [45, 54],
    "pool_G": [55, 64]
  }
}
```
*Les numéros correspondent aux index dans le fichier `.env` (1-based).*

### État persistant (`ApiLog/api_state.json`)
```json
{
  "keys_state": {
    "0": {
      "key_num": 1,
      "status": "AVAILABLE",
      "unblock_time": 0,
      "last_used": 1722772800.5,
      "nb_used": 47,
      "nb_errors": 2,
      "tokens_in": 183420,
      "tokens_out": 12800
    }
  }
}
```

---

## Phase 3 — Application des corrections (`ApplyCorrections.py`)

Fusionne les JSON bruts (`Rag_selective/`) avec les JSON de corrections (`Correction/`) pour produire les JSON finaux dans `correction_Rag/`.

### Logique d'application
1. **Suppressions** : supprime les lignes de `lignes_en_trop_supprimees` (du plus grand index au plus petit pour éviter les décalages)
2. **Insertions** : insère les lignes de `lignes_manquantes_ajoutees` (avec compensation d'offset)
3. **Corrections de cellules** : applique `erreurs_corrigees` sur la ligne ciblée
4. **Propagation globale** : si la même valeur erronée existe dans d'autres lignes de la même colonne, elle est corrigée automatiquement

### Sécurités
- Ne touche **jamais** aux cellules vides (`""`)
- En cas d'erreur sur une table, copie l'original en fallback (le batch continue)
- Encodage UTF-8 forcé sur Windows

### Usage
```bash
# Un seul datasheet
python ApplyCorrections.py --family C0 --datasheet stm32c011d6 --workers 6

# Toute la famille
python ApplyCorrections.py --family C0 --workers 6
```

---

## Phase 4 — Agrégation finale (`UpdateAllTables.py`)

Après des corrections manuelles sur des tables individuelles dans `correction_Rag/`, recrée le fichier `*_all_tables.json` de chaque datasheet pour le maintenir synchronisé.

### Usage
```bash
python UpdateAllTables.py --family C0
```

### Quand l'utiliser ?
Après avoir corrigé manuellement une table marquée `MANUAL_REVIEW_NEEDED`. Le script re-fusionne toutes les tables individuelles dans le bon ordre pour mettre à jour le fichier global.

---

## Dépendances

```bash
pip install google-genai pillow pdfplumber
```

### Fichier `.env`
```
GEMINI_KEY_1=AIza...
GEMINI_KEY_2=AIza...
...
GEMINI_KEY_64=AIza...
```

---

## Modèle LLM

| Paramètre | Valeur |
|-----------|--------|
| Provider | Google AI Studio |
| Modèle | `gemini-flash-latest` |
| Température | `0` (réponses déterministes) |
| Format sortie | JSON strict (`application/json`) |
| Budget de réflexion | 8000 tokens (thinking mode) |

---

## 📋 Référence complète des commandes

---

### `app.py` — Extraction PDF (Phase 1)

| Argument | Type | Défaut | Description |
|----------|------|--------|-------------|
| `--pdf` | str | — | Chemin vers un seul PDF à extraire |
| `--family` | str | — | Nom de la famille (extrait tous les PDFs de la famille) |
| `--all` | flag | — | Extrait tous les PDFs de tous les dossiers |

```powershell
# ── Un seul PDF ──────────────────────────────────────────────────────────────

# Extraire un PDF spécifique famille C0
python app.py --pdf DataSHEET/C0/stm32c011d6.pdf

# Extraire un PDF spécifique famille N6
python app.py --pdf DataSHEET/N6/stm32n645a0.pdf

# ── Toute une famille ─────────────────────────────────────────────────────────

# Extraire tous les PDFs de la famille C0
python app.py --family C0

# Extraire tous les PDFs de la famille N6
python app.py --family N6

# ── Tout extraire ─────────────────────────────────────────────────────────────

# Extraire absolument tous les PDFs de tous les dossiers DataSHEET/
python app.py --all
```

---

### `BatchLLMValidation.py` — Correction LLM (Phase 2)

| Argument | Type | Défaut | Description |
|----------|------|--------|-------------|
| `--family` | str | **requis** | Famille cible (ex: `C0`, `N6`) |
| `--datasheet` | str | tous | Datasheet spécifique (ex: `stm32c011d6`). Si absent, toute la famille |
| `--workers` | int | `6` | Nombre de requêtes API parallèles |

```powershell
# ── Test rapide sur 1 datasheet ───────────────────────────────────────────────

# Tester sur stm32c011d6 avec 2 workers (recommandé si erreurs 429 fréquentes)
python BatchLLMValidation.py --family C0 --datasheet stm32c011d6 --workers 2

# Tester sur stm32c031c4 avec 4 workers
python BatchLLMValidation.py --family C0 --datasheet stm32c031c4 --workers 4

# Tester sur stm32c051c6 avec 6 workers
python BatchLLMValidation.py --family C0 --datasheet stm32c051c6 --workers 6

# Tester sur stm32c071r8 avec 4 workers
python BatchLLMValidation.py --family C0 --datasheet stm32c071r8 --workers 4

# Tester sur stm32c091kb avec 2 workers
python BatchLLMValidation.py --family C0 --datasheet stm32c091kb --workers 2

# ── Toute une famille ─────────────────────────────────────────────────────────

# Traiter toute la famille C0 avec 2 workers (prudent)
python BatchLLMValidation.py --family C0 --workers 2

# Traiter toute la famille C0 avec 4 workers (plus rapide si stable)
python BatchLLMValidation.py --family C0 --workers 4

# Traiter toute la famille N6 avec 6 workers
python BatchLLMValidation.py --family N6 --workers 6

# ── Reprise après interruption ────────────────────────────────────────────────
# Le script ignore automatiquement les tables déjà traitées (fichier déjà présent
# dans Correction/). Il suffit de relancer la même commande pour reprendre.
python BatchLLMValidation.py --family C0 --datasheet stm32c011d6 --workers 2
```

> **Conseils pratiques :**
> - `--workers 2` : recommandé si les erreurs 429 sont fréquentes
> - `--workers 4` : bon compromis vitesse / stabilité avec 7 pools de clés
> - `--workers 6` : uniquement si toutes vos clés sont dans des projets distincts
> - Le script est **reprennable** : relancez-le sans crainte, il saute les tables déjà corrigées

---

### `ApplyCorrections.py` — Application des corrections (Phase 3)

| Argument | Type | Défaut | Description |
|----------|------|--------|-------------|
| `--family` | str | **requis** | Famille cible (ex: `C0`, `N6`) |
| `--datasheet` | str | tous | Datasheet spécifique. Si absent, toute la famille |
| `--workers` | int | `6` | Nombre de workers parallèles (opération locale, pas d'API) |
| `--src-root` | str | `Rag_selective` | Dossier source des tables brutes |
| `--corr-root` | str | `Correction` | Dossier source des corrections LLM |
| `--out-root` | str | `correction_Rag` | Dossier de sortie des tables corrigées |

```powershell
# ── Un seul datasheet ─────────────────────────────────────────────────────────

python ApplyCorrections.py --family C0 --datasheet stm32c011d6 --workers 6
python ApplyCorrections.py --family C0 --datasheet stm32c031c4 --workers 6
python ApplyCorrections.py --family C0 --datasheet stm32c051c6 --workers 6
python ApplyCorrections.py --family C0 --datasheet stm32c071r8 --workers 6
python ApplyCorrections.py --family C0 --datasheet stm32c091kb --workers 6

# ── Plusieurs datasheets en boucle PowerShell ─────────────────────────────────

foreach ($ds in 'stm32c011d6','stm32c031c4','stm32c051c6') {
    python ApplyCorrections.py --family C0 --datasheet $ds --workers 6
}

# Tous les datasheets corrigés de la famille C0 d'un seul coup
foreach ($ds in 'stm32c011d6','stm32c031c4','stm32c051c6','stm32c071r8','stm32c091kb') {
    python ApplyCorrections.py --family C0 --datasheet $ds --workers 6
}

# ── Toute la famille d'un seul coup ───────────────────────────────────────────

python ApplyCorrections.py --family C0 --workers 6
python ApplyCorrections.py --family N6 --workers 6

# ── Dossiers personnalisés ────────────────────────────────────────────────────

# Utiliser des dossiers non standard
python ApplyCorrections.py --family C0 --src-root Rag_selective --corr-root Correction --out-root correction_Rag --workers 6
```

> **Note :** Ce script est 100% local (aucun appel API). Pas besoin de limiter les workers.

---

### `UpdateAllTables.py` — Régénération du fichier agrégé (Phase 4)

| Argument | Type | Défaut | Description |
|----------|------|--------|-------------|
| `--family` | str | **requis** | Famille dont on veut régénérer les `*_all_tables.json` |

```powershell
# Régénérer les all_tables.json de toute la famille C0
python UpdateAllTables.py --family C0

# Régénérer pour la famille N6
python UpdateAllTables.py --family N6

# Régénérer pour toutes les familles (boucle)
foreach ($fam in 'C0','N6') {
    python UpdateAllTables.py --family $fam
}
```

> **Quand l'utiliser ?** Après avoir corrigé manuellement une ou plusieurs tables marquées
> `MANUAL_REVIEW_NEEDED` dans `correction_Rag/<famille>/<datasheet>/`.

---

### Workflows complets

#### Workflow standard — 1 datasheet de test

```powershell
# 1. Extraire le PDF
python app.py --pdf DataSHEET/C0/stm32c011d6.pdf

# 2. Corriger avec le LLM (2 workers pour stabilité)
python BatchLLMValidation.py --family C0 --datasheet stm32c011d6 --workers 2

# 3. Appliquer les corrections
python ApplyCorrections.py --family C0 --datasheet stm32c011d6 --workers 6

# 4. (Optionnel) Corriger manuellement les tables MANUAL_REVIEW_NEEDED
#    → éditer directement les fichiers dans correction_Rag/C0/stm32c011d6/

# 5. Régénérer le all_tables.json final
python UpdateAllTables.py --family C0
```

#### Workflow complet — Toute la famille C0

```powershell
# 1. Extraire tous les PDFs C0
python app.py --family C0

# 2. Valider avec le LLM (toute la famille, 4 workers)
python BatchLLMValidation.py --family C0 --workers 4

# 3. Appliquer toutes les corrections
python ApplyCorrections.py --family C0 --workers 6

# 4. Identifier les tables à révision manuelle
Select-String -Path "Correction\C0\*\*.json" -Pattern "MANUAL_REVIEW_NEEDED" | Select-Object Filename

# 5. Après corrections manuelles → régénérer les agrégats
python UpdateAllTables.py --family C0
```

#### Workflow reprise après interruption

```powershell
# Le BatchLLMValidation.py reprend automatiquement là où il s'est arrêté.
# Il suffit de relancer exactement la même commande :
python BatchLLMValidation.py --family C0 --datasheet stm32c011d6 --workers 2

# Puis ré-appliquer les corrections (écrase les anciens fichiers de sortie)
python ApplyCorrections.py --family C0 --datasheet stm32c011d6 --workers 6
python UpdateAllTables.py --family C0
```

---

### Vérification et diagnostic

```powershell
# ── État des clés API ─────────────────────────────────────────────────────────

# Voir tout l'état brut
Get-Content ApiLog\api_state.json

# Compter les clés AVAILABLE vs BLOCKED
$state = Get-Content ApiLog\api_state.json | ConvertFrom-Json
$state.keys_state.PSObject.Properties.Value | Group-Object status | Format-Table Name, Count

# Voir les 5 clés les plus utilisées
$state.keys_state.PSObject.Properties.Value |
    Sort-Object nb_used -Descending | Select-Object -First 5 key_num, nb_used, nb_errors, tokens_in, tokens_out

# ── Vérifier les tables MANUAL_REVIEW_NEEDED ─────────────────────────────────

# Lister tous les fichiers nécessitant révision manuelle dans la famille C0
Select-String -Path "Correction\C0\*\*.json" -Pattern '"status": "MANUAL_REVIEW_NEEDED"' |
    Select-Object -ExpandProperty Path

# ── Compter les tables corrigées ──────────────────────────────────────────────

# Compter les fichiers de correction produits pour C0
(Get-ChildItem -Recurse "Correction\C0" -Filter "*.json" | Where-Object { $_.Name -notlike "*all_tables*" }).Count

# Compter les tables finales dans correction_Rag
(Get-ChildItem -Recurse "correction_Rag\C0" -Filter "*.json" | Where-Object { $_.Name -notlike "*all_tables*" }).Count
```

