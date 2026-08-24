# RAG STM32 — Pipeline d'extraction et correction des tableaux de datasheets

Pipeline complet pour extraire, corriger et indexer les tableaux techniques des datasheets STM32 (familles C0, H7, N6, AN…) pour un système RAG.

---

## Architecture du pipeline

```
PDF Datasheet STM32
      │
      ▼
┌─────────────────────────────────────┐
│  PHASE 1 — Extraction               │  app.py → table_extractor_raw/
│  Outil : pdfplumber + PyMuPDF       │  Précision : ~90%
│  Sortie : Output/Json/Selective_Tables/ (+Raw_Extracted) │
└──────────────┬──────────────────────┘
                │
         ┌──────┴──────┐
         │ len(images) │
         └──────┬──────┘
                │
     ┌──────────┼──────────┐
     │ >7       │ ≤7       │
     ▼          ▼
┌──────────┐ ┌─────────────────────────────────────┐
│ ÉTAPE    │ │  PHASE 2 — Correction LLM           │  PipelineViaLLM/BatchLLMValidation.py
│ More     │ │  Provider par défaut : STBridge     │  Myriamx (ST AI Bridge)
│ Than 7   │ │  Fallback : Gemini Flash (ApiManager)│
│ Output/  │ │  Sortie : Output/Json/LLM_Corrections/ │
│ Json/    │ │  More_Than_7 → Output/Json/More_Than_7/  │
│ More_Than_7/│ └──────────────┬──────────────────────┘
└──────────┘                │
                ▼
┌─────────────────────────────────────┐
│  PHASE 2b — Manual Review (option.) │  PipelineViaLLM/MANUAL_REVIEW.py
│  Re-corrige MANUAL_REVIEW_NEEDED    │  Skip >7 (More_Than_7)
│  Sortie : Output/Json/Manual_Review/│
└──────────────┬──────────────────────┘
                │
                ▼
┌─────────────────────────────────────┐
│  PHASE 3 — Application              │  PipelineViaLLM/ApplyCorrections.py
│  Gère MORE_THAN_7 → copie originale │  Fusion source + corrections
│  Sortie : Output/Json/Final_RAG/    │
└──────────────┬──────────────────────┘
                │
                ▼
┌─────────────────────────────────────┐
│  PHASE 4 — Agrégation finale        │  UpdateAllTables.py
│  Sortie : *_all_tables.json         │  Après retouches manuelles
└─────────────────────────────────────┘
      │
      ▼
┌─────────────────────────────────────┐
│  EXTRA — Figures Pinout/Ballout     │  PipelineViaLLM/Figure.py (STBridge)
│  Sortie : Output/Json/Final_Figures/│
└─────────────────────────────────────┘
```

**Providers LLM :**
- **STBridge (Myriamx)** par défaut — `https://api-ai-bridge-qa.st.com/chatgpt/api/client-apps` via `PipelineViaLLM/llm_provider.py:134`, auth SHA1, `data:image/jpg;base64`, 5s entre tables.
- **Gemini** en rollback `--provider gemini` avec `ApiManager` pools.

---

## Structure des dossiers

```
StGenAI-malek/
├── app.py                    # Wrapper Phase 1
├── .env                      # ST_AI_BRIDGE_API_KEY, REMOTE_USER, TEMPERATURE=0.2, MAX_RESPONSE_TOKENS=32400 (non versionné)
├── .env.example              # Template STBridge
├── requirements.txt
│
├── PipelineViaLLM/
│   ├── llm_provider.py       # Abstraction STBridge/Gemini (STBridgeProvider, encode_image_base64, build_multimodal_content)
│   ├── config.yaml           # STBridge : url, remoteUser, temperature 0.2, maxResponseTokens 32400, persona Myriam
│   ├── BatchLLMValidation.py # Phase 2 : Correction batch (STBridge, MORE_DIR, 5s)
│   ├── MANUAL_REVIEW.py      # Phase 2b : Re-correction (skip >7)
│   ├── Figure.py             # Figures Pinout/Ballout (STBridge, 600 DPI)
│   ├── ApplyCorrections.py   # Phase 3 : Fusion (gère MORE_THAN_7)
│   ├── ApiManager.py         # Legacy Gemini pools (rollback)
│   ├── Prompt_Tables.txt
│   └── api_config.json
│
├── table_extractor_raw/      # Moteur pdfplumber + PyMuPDF (Phase 1)
│   └── main.py               # Capture Output/Images/Tables_Screenshots/<family>/<ds>/tableau_N/page_*.png (150 dpi) + PDF page
│
├── Input/
│   └── PDFs/                 # PDFs par famille (C0, H7, N6) + AN (Input/41/)
│
├── Output/
│   ├── Images/
│   │   ├── Tables_Screenshots/   # C0/stm32c011d6/tableau_12/page_29.png (+page_*.pdf)
│   │   └── Figures_Screenshots/
│   ├── Json/
│   │   ├── Raw_Extracted/
│   │   ├── Selective_Tables/     # Phase 1 output
│   │   ├── LLM_Corrections/      # Phase 2 output (OK/ERRORS_FOUND/MANUAL_REVIEW_NEEDED)
│   │   ├── More_Than_7/          # NOUVEAU : >7 images (status MORE_THAN_7, voir ci-dessous)
│   │   ├── Manual_Review/        # Phase 2b output
│   │   ├── Final_RAG/            # Phase 3 output (corrigé)
│   │   └── Final_Figures/
│   └── Reports/
└── ApiLog/
    └── api_state.json        # Gemini seulement
```

---

## Phase 1 — Extraction PDF (`app.py` → `table_extractor_raw/main.py`)

Wrapper `table_extractor_raw/`. Détecte les tables (TOC/scan), extrait via `grid_extractor`, valide `RawTable`, capture 1 PNG (150 dpi) + 1 PDF par `merged_pages`.

### Usage
```bash
python app.py --pdf Input/PDFs/C0/stm32c011d6.pdf
python app.py --family C0
python app.py --family H7
python app.py --an 41
python app.py --all
```

### Sortie
`Output/Json/Selective_Tables/<family>/<ds>/DSxxxx_table_N.json` + `Output/Images/Tables_Screenshots/.../tableau_N/page_*.png`

**Log More_Than_7 dès Phase 1** (`table_extractor_raw/main.py:365`): si `len(merged_pages)>7` → `warnings: ["more_than_7_pages:9"]`.

---

## Phase 2 — Correction LLM (`PipelineViaLLM/BatchLLMValidation.py`)

Provider **STBridge Myriamx** par défaut. Envoie `images JPG base64` (avec `crop_header_zoom` 22% `llm_provider.py:84`), prompt expert `PROMPT`, `pdf_text` positionné et JSON brut.

### Règle More Than 7 (nouvelle étape)
Si `len(images) > 7` (table éclatée sur >7 pages) :
- **Skip LLM** — pas d'appel STBridge (coût/limite tokens)
- Archive dans `Output/Json/More_Than_7/<family>/<ds>/DSxxxx_table_N.json` :
```json
{
  "table_id": "table_42",
  "status": "MORE_THAN_7",
  "reason": "Skipped LLM: 9 images >7",
  "images_count": 9,
  "merged_pages": [45,46,47,48,49,50,51,52,53],
  "logs": ["More Than 7 — découpage manuel requis"]
}
```
- `ApplyCorrections.py` copie l'original sans correction (statut tracé dans `Rapport_Validation`).

### Règles prompt & boucliers
| Règle | Description |
|-------|-------------|
| RTL inversé | Texte vertical `)stib 21(` → `ADC (12 bits)` |
| Codes composants | `Q3H0X546N` corrigé |
| Anti-destruction BOUCLIER-1 | Interdit vider texte → tiret/vide |
| Cases vides BOUCLIER-2 | Remplissage seulement si confiance ≥90% |
| Suppression massive BOUCLIER-3 | >20% lignes → `MANUAL_REVIEW_NEEDED` |
| Faible confiance BOUCLIER-4 | <80% → `MANUAL_REVIEW_NEEDED` |

### Usage STBridge (par défaut, 32400 tokens)
```bash
# 1 datasheet H7
python -u PipelineViaLLM/BatchLLMValidation.py --family H7 --datasheet stm32h7a3ag --workers 1

# Famille C0, AN
python -u PipelineViaLLM/BatchLLMValidation.py --family C0 --workers 1
python -u PipelineViaLLM/BatchLLMValidation.py --an 41 --workers 1

# Rollback Gemini
python PipelineViaLLM/BatchLLMValidation.py --provider gemini --family C0 --workers 4
```
- `--provider {stbridge,gemini}` défaut `stbridge`
- `--workers 1` recommandé STBridge mono-clé (max 2)
- Pause 5s entre tables (configuré), reprise automatique (skip `out_file.exists()`)

### Statuts
| Statut | Description |
|--------|-------------|
| `OK` | Parfait |
| `ERRORS_FOUND` | Corrigé |
| `MANUAL_REVIEW_NEEDED` | À re-corriger via `MANUAL_REVIEW.py` |
| `MORE_THAN_7` | >7 images, archivé dans `More_Than_7` |

---

## Phase 2b — Manual Review (`PipelineViaLLM/MANUAL_REVIEW.py`)

Re-corrige les tables `MANUAL_REVIEW_NEEDED` de `LLM_Corrections`. Skip aussi `>7 images` → `More_Than_7`.

```bash
python PipelineViaLLM/MANUAL_REVIEW.py --family C0 --workers 1
python PipelineViaLLM/MANUAL_REVIEW.py --provider gemini --family C0
```

---

## Phase 3 — Application (`PipelineViaLLM/ApplyCorrections.py`)

Fusionne `Selective_Tables` + `LLM_Corrections` (priorité `Manual_Review`) vers `Final_RAG`. Gère `MORE_THAN_7` → copie originale.

```bash
python PipelineViaLLM/ApplyCorrections.py --family C0 --datasheet stm32c011d6 --workers 6
python PipelineViaLLM/ApplyCorrections.py --family C0 --workers 6
python PipelineViaLLM/ApplyCorrections.py --family H7 --workers 6
python PipelineViaLLM/ApplyCorrections.py --an 41 --workers 6
```

---

## Phase 4 — Agrégation (`UpdateAllTables.py`)
```bash
python UpdateAllTables.py --family C0
```

---

## Figures Pinout/Ballout (`PipelineViaLLM/Figure.py`)
```bash
python -u PipelineViaLLM/Figure.py --family C0 --workers 1
python -u PipelineViaLLM/Figure.py --pdf Input/PDFs/C0/stm32c011d6.pdf
python PipelineViaLLM/Figure.py --an 41 --workers 1
```
Capture 600 dpi `Output/Images/Figures_Screenshots/`, sortie `Output/Json/Final_Figures/`.

---

## Dépendances

```bash
pip install -r requirements.txt
# - pdfplumber, PyMuPDF>=1.24 (import fitz/pymupdf), pypdf, pydantic
# - Pillow, requests, pyyaml, urllib3, tqdm
# - google-genai (rollback uniquement)
```

### `.env` STBridge (actuel)
```
ST_AI_BRIDGE_API_KEY=38d5a975-128d-4106-8cc8-394dc1122696
ST_AI_BRIDGE_URL=https://api-ai-bridge-qa.st.com/chatgpt/api/client-apps
CLIENT_APP_NAME=mdrf-gpam-stm32-technical-support-qa
REMOTE_USER=younes.lahbib@st.com
SERVICE_NAME=chat
PERSONA=Myriam
TEMPERATURE=0.2
MAX_RESPONSE_TOKENS=32400
RESPONSE_FORMAT=json_object
REASONING_EFFORT=high
```

`PipelineViaLLM/config.yaml` reflète les mêmes valeurs (surchargées par `.env`).

---

## Modèle LLM

| Paramètre | STBridge (défaut) | Gemini (rollback) |
|-----------|-------------------|-------------------|
| Provider | ST AI Bridge QA | Google AI Studio |
| Auth | SHA1 `stchatgpt-auth-token` | `GEMINI_KEY_*` pools |
| Température | `0.2` (>0 requis) | `0` |
| Max tokens | `32400` (timeout 900s) | 4096 |
| Images | `data:image/jpg;base64` + header zoom 22% | `Part(pdf)` |
| Pause | 5s entre tables | pools Round-Robin |

---

## 📋 Référence complète des commandes

### `app.py`
```powershell
python app.py --pdf Input/PDFs/H7/stm32h7a3ag.pdf
python app.py --family H7
python app.py --an 41
python app.py --all
python app.py --pdf Input/PDFs/C0/stm32c011d6.pdf --tables 2,5,10
```

### `BatchLLMValidation.py`
```powershell
# STBridge (défaut)
python -u PipelineViaLLM/BatchLLMValidation.py --family H7 --datasheet stm32h7a3ag --workers 1
python -u PipelineViaLLM/BatchLLMValidation.py --family C0 --workers 1
python -u PipelineViaLLM/BatchLLMValidation.py --an 41 --workers 1
# Gemini rollback
python PipelineViaLLM/BatchLLMValidation.py --provider gemini --family C0 --workers 4
```

### `MANUAL_REVIEW.py`
```powershell
python PipelineViaLLM/MANUAL_REVIEW.py --family C0 --workers 1
```

### `Figure.py`
```powershell
python -u PipelineViaLLM/Figure.py --family C0 --workers 1
python -u PipelineViaLLM/Figure.py --an 41 --workers 1
```

### `ApplyCorrections.py` + `UpdateAllTables.py`
```powershell
python PipelineViaLLM/ApplyCorrections.py --family H7 --workers 6
python UpdateAllTables.py --family H7
```

### Vérification More_Than_7
```powershell
Get-ChildItem -Recurse Output/Json/More_Than_7 | Format-Table Name
Get-ChildItem -Recurse Output/Json/LLM_Corrections -Filter *.json | Measure-Object
Select-String -Path "Output/Json/More_Than_7/*/*/*.json" -Pattern "MORE_THAN_7"
```

### Workflows complets
```powershell
# 1 datasheet H7 complet
python app.py --pdf Input/PDFs/H7/stm32h7a3ag.pdf
python -u PipelineViaLLM/BatchLLMValidation.py --family H7 --datasheet stm32h7a3ag --workers 1
python PipelineViaLLM/ApplyCorrections.py --family H7 --datasheet stm32h7a3ag --workers 6

# Famille C0
python app.py --family C0
python -u PipelineViaLLM/BatchLLMValidation.py --family C0 --workers 1
python PipelineViaLLM/ApplyCorrections.py --family C0 --workers 6

# Reprise après interruption (auto-skip)
python -u PipelineViaLLM/BatchLLMValidation.py --family H7 --datasheet stm32h7a3ag --workers 1
```
