# GUIDE AGENT IA — Migration Gemini Free → GPT 4o-mini Local Société

> **Audience :** Agent IA autonome (Muse, GPT, etc.) chargé d'exécuter la migration.
> **Objectif :** Passer de `Gemini Free` (84 clés, 10 pools, `ApiManager`) à `GPT 4o-mini` **local société, mono-clé**, avec les bonnes pratiques, sans régression.
> **Principe cardinal :** **LIRE LE CODE D'ABORD, LIER L'EXEMPLE ENSUITE, CODER EN DERNIER.**

---

## 0. Contexte Prod

| Avant (actuel) | Après (cible) |
|---|---|
| `MODEL="gemini-flash-latest"` | `MODEL="gpt-4o-mini"` (ou modèle local équivalent) |
| 84 clés `GEMINI_KEY_1..103` dans `.env` + `PipelineViaLLM/api_config.json` (10 pools A-J) | **1 seule clé** `OPENAI_API_KEY` (+ `OPENAI_BASE_URL` si local) |
| `ApiManager.py:1-409` : Round-Robin inter-pools, blocage 60s/24h, `ApiLog/api_state.json` | Retry simple exponentiel, plus de pools |
| `google-genai` `from google import genai` + `types.Part.from_bytes(pdf)` + `thinking_config` | `openai` `from openai import OpenAI` + `base64 image_url` + `response_format json_object` |

**Fichiers couplés Gemini à migrer impérativement :**
- `PipelineViaLLM/BatchLLMValidation.py:12-13,32,198-247,236-246,384-397`
- `PipelineViaLLM/Figure.py:18-19,34,251-278`
- `PipelineViaLLM/MANUAL_REVIEW.py:12-13,28,182-188`
- `PipelineViaLLM/ApiManager.py:1-409` + `PipelineViaLLM/api_config.json` + `ApiLog/api_state.json` + `.env`

---

## 1. RÈGLE D'OR — LIRE LE CODE AVANT TOUTE MODIF

**L'agent NE DOIT PAS modifier une ligne avant d'avoir :**

1. Lu en entier `BatchLLMValidation.py` (599 lignes) — comprendre `PROMPT:56-128`, construction `contents:198-216`, appel `client.models.generate_content:236-246`, boucliers `278-352`, gestion pools `453-497`.
2. Lu en entier `Figure.py` (456 lignes) — comprendre `FIGURE_PROMPT:37-88`, `call_gemini_extraction:218-333`, `thinking_budget=4000:278`.
3. Lu en entier `MANUAL_REVIEW.py` (366 lignes) — comprendre `load_prompt:65-72`, `extract_json_from_response:75-107`, `process_table:114-233`.
4. Lu `ApiManager.py` (409 lignes) — comprendre `BLOCK_1MIN/24H:27-30`, `_load_pools:68-88`, `get_key_from_pool:168-260`, `report_rate_limit/exhausted:351-387`.
5. Lu `requirements.txt:1-19` et `table_extractor_raw/requirements.txt:1-4`.

**Vérification :** L'agent doit pouvoir citer `file:line` des 4 boucliers et du `types.Part.from_bytes` avant de coder.

---

## 2. LIER L'EXEMPLE GPT FOURNI PAR L'UTILISATEUR

> **L'exemple est la vérité terrain pour le GPT local.** Le modèle local est spécifique à la société, son API peut différer légèrement d'OpenAI public. **Tout le mapping doit partir de l'exemple, ne jamais inventer.**

### 2.1 Emplacement attendu de l'exemple

L'utilisateur fournira un fichier **exemple de référence** à l'un de ces endroits (l'agent doit chercher dans cet ordre) :

1. `examples/gpt_local_example.py` (recommandé)
2. `PipelineViaLLM/gpt_example.py`
3. Bloc de code collé dans ce guide (section `## EXEMPLE GPT LOCAL` à remplir)

**Si aucun exemple n'est trouvé : STOPPER et demander à l'utilisateur. Ne pas deviner l'API.**

### 2.2 Mapping obligatoire Exemple → Code existant

| Concept Gemini (actuel) | Concept GPT local (exemple) | Action |
|---|---|---|
| `from google import genai` | `from openai import OpenAI` (ou import de l'exemple) | Remplacer import |
| `genai.Client(api_key=key)` | `OpenAI(api_key=..., base_url=...)` | Adapter constructeur avec `base_url` si local |
| `contents = [PROMPT, Image.open(png), Part(pdf_bytes)]` | `messages=[{"role":"system","content":...}, {"role":"user","content":[{"type":"text","text":PROMPT}, {"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}]}]` | **Convertir chaque `PIL.Image` en base64** `base64.b64encode(img_bytes).decode()`. **Supprimer `Part(pdf_bytes)`** — GPT local ne prend pas `application/pdf` natif, les PNG suffisent (déjà 600 DPI `Figure.py:379`). |
| `config=GenerateContentConfig(temperature=0, response_mime_type="application/json", thinking_config=ThinkingConfig(8000))` | `temperature=0, response_format={"type":"json_object"}` | Retirer `thinking_config`, ajouter `response_format`. Sur `MANUAL_REVIEW.py:186` c'était `temperature=0.1` sans json mode — uniformiser à `0` + `json_object`. |
| `response.text` + `json.loads(raw)` | `response.choices[0].message.content` + `json.loads` | Adapter parsing. Garder `extract_json_from_response()` `MANUAL_REVIEW.py:75` tel quel (déjà robuste aux ```json). |
| `response.usage_metadata.prompt_token_count` | `response.usage.prompt_tokens` | Adapter comptage tokens `BatchLLMValidation.py:257-260`. |
| `"429" in err_msg` string matching `BatchLLMValidation.py:384` | `except openai.RateLimitError` / `APIStatusError` | Remplacer détection d'erreur par exceptions typées OpenAI. |
| `API_MANAGER.get_key_from_pool()` + `report_rate_limit()` | `try/except` + `time.sleep(backoff)` simple | Simplifier — plus de pools. |

---

## 3. GOOD PRACTICES IMPOSÉES

1. **Abstraction** : Créer `PipelineViaLLM/llm_provider.py` avec interface `LLMProvider` → `GeminiProvider` (wrappe code actuel) + `OpenAIProvider` (nouveau). Permet `--provider {gemini,openai}` et rollback instantané. **Ne pas faire de remplacement brutal sans abstraction.**

2. **Config** : `.env` → une seule ligne `OPENAI_API_KEY=sk-...` + `OPENAI_BASE_URL=https://gpt-local.societe.internal/v1` si local. `.env.example` doit être MAJ. **Ne jamais commiter `.env` (déjà dans `.gitignore:31`).**

3. **Dépendances** : `requirements.txt:15` ajouter `openai>=1.50.0` (déjà `2.50.0` dans `venv`). Garder `google-genai` en option pour fallback.

4. **Retry** : Exponentiel `1s → 2s → 4s → 30s` max, jitter, sur `RateLimitError` uniquement. Pas de `ApiLog/api_state.json` pour OpenAI.

5. **Workers** : Avec mono-clé, forcer `workers=1` (ou 2 max) par défaut. `BatchLLMValidation.py:406` `default=6` → adapter aide CLI.

6. **Logging** : Garder `safe_print` + `threading.Lock` existants, ajouter `model` et `tokens` dans les logs.

7. **Prompts** : **NE JAMAIS modifier** `PROMPT` `BatchLLMValidation.py:56-128` et `FIGURE_PROMPT` `Figure.py:37-88` sans test A/B. Ce sont les garde-fous anti-hallucination.

---

## 4. CHECKLIST — AJOUTER

- [ ] `PipelineViaLLM/llm_provider.py` (nouveau) — voir squelette ci-dessous
- [ ] `requirements.txt` : `openai>=1.50.0` + commentaire `# GPT local` (garder `google-genai` en option)
- [ ] `.env.example` : remplacer le bloc `GEMINI_KEY_1..84` par `OPENAI_API_KEY` + `OPENAI_BASE_URL`
- [ ] `BatchLLMValidation.py` : flag `--provider` + `load_api_keys()` adapté `BatchLLMValidation.py:35-50`
- [ ] `Figure.py` + `MANUAL_REVIEW.py` : idem
- [ ] Helper `image_to_base64(pil_image)` : `buffered = io.BytesIO(); image.save(buffered, format="PNG"); return base64.b64encode(buffered.getvalue()).decode()`
- [ ] `README.md` + `AGENT_GUIDE.md` : documenter le switch provider

**Squelette `llm_provider.py` attendu :**
```python
from abc import ABC, abstractmethod
class LLMProvider(ABC):
    @abstractmethod
    def generate(self, prompt: str, images: list[Image], json_payload: dict) -> dict: ...
class GeminiProvider(LLMProvider): ... # wrappe le code actuel
class OpenAIProvider(LLMProvider):
    def __init__(self, api_key, base_url=None):
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key, base_url=base_url)
    def generate(self, prompt, images, json_payload):
        # base64 + messages + response_format json_object
```

---

## 5. CHECKLIST — SUPPRIMER / SIMPLIFIER

- [ ] `types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")` `BatchLLMValidation.py:207` + `Figure.py:257` + `MANUAL_REVIEW.py:164` → **supprimer** (ou convertir PDF en PNG si besoin)
- [ ] `thinking_config=ThinkingConfig(thinking_budget=8000)` `BatchLLMValidation.py:242-243` + `4000` `Figure.py:278` → **supprimer**
- [ ] `response_mime_type="application/json"` → remplacé par `response_format`
- [ ] `ApiManager.py` pools `A-J` `api_config.json` pour OpenAI → **ne plus utiliser** (garder le fichier pour Gemini fallback, mais `OpenAIProvider` ne l'appelle pas). Ne pas supprimer physiquement `ApiManager.py`, le rendre optionnel.
- [ ] `ApiLog/api_state.json` — plus de persistance 24h pour mono-clé (optionnel à garder pour Gemini)
- [ ] Détection d'erreur par `if "429" in err_msg` `BatchLLMValidation.py:384` → remplacer par `except RateLimitError`

**NE JAMAIS SUPPRIMER :**
- `PROMPT` complet, `FIGURE_PROMPT`, les 4 boucliers `BatchLLMValidation.py:278-352`, le cache `Output/Images/`, `Output/Json/Selective_Tables`, `load_api_keys()` (à adapter, pas supprimer)

---

## 6. CHECKLIST — GARDER INTACT

- [ ] `PROMPT` `BatchLLMValidation.py:56-128` (RTL, anti-destruction, anti-hallucination)
- [ ] `FIGURE_PROMPT` `Figure.py:37-88`
- [ ] Boucliers 1-4 `BatchLLMValidation.py:278-352` (BOUCLIER-1 destructif, BOUCLIER-2 confiance 90%, BOUCLIER-3 suppression >20%, BOUCLIER-4 confiance <80%)
- [ ] `extract_json_from_response()` `MANUAL_REVIEW.py:75-107`
- [ ] Cache `Output/Images/Tables_Screenshots` + `Figures_Screenshots` (600 DPI `Figure.py:379`)
- [ ] `Output/Json/Selective_Tables` structure + `table_content`
- [ ] `safe_print` + `stats_lock` + `time.sleep(10)` anti-DDoS `BatchLLMValidation.py:225`

---

## 7. GUIDE FICHIER PAR FICHIER

| Fichier | Lignes à toucher | Action exacte |
|---|---|---|
| `BatchLLMValidation.py` | `12-13,32,35-50,198-247,257-260,384-397,406` | Ajouter `llm_provider`, base64, `response_format`, adapter `load_api_keys` → `OPENAI_API_KEY`, simplifier retry |
| `Figure.py` | `18-19,34,218-333,272-278` | Idem, retirer `Part(pdf)`, 600 DPI déjà OK |
| `MANUAL_REVIEW.py` | `12-13,28,75-107,146-188` | Idem, garder `extract_json...` |
| `ApiManager.py` | `1-409` | Rendre optionnel, ne pas appeler pour `openai` |
| `requirements.txt` | `15` | Ajouter `openai` |
| `.env.example` | tout le bloc `GEMINI_KEY` | Remplacer par `OPENAI_API_KEY` + `OPENAI_BASE_URL` |

---

## 8. VALIDATION (OBLIGATOIRE)

1. **Dry-run 1 table** : `python PipelineViaLLM/BatchLLMValidation.py --an 41 --workers 1` avec `--provider openai` → vérifier JSON identique à Gemini sur 1 table connue (ex: `AN5036/table_10`)
2. **5 tables** : comparer `status`/`erreurs_corrigees` avec run Gemini précédent (`Output/Json/LLM_Corrections`)
3. **Full AN 41** : `python PipelineViaLLM/BatchLLMValidation.py --an 41 --workers 1` (pas 6 avec mono-clé)
4. **Figure** : `python PipelineViaLLM/Figure.py --an 41 --workers 1`
5. **Coût** : relever `tokens_in/out` `BatchLLMValidation.py:257` → estimer `$` pour 18k tables

Si vision GPT < Gemini sur RTL inversé → ajuster prompt ou repasser à `gpt-4o` (pas mini) ou garder Gemini fallback.

---

## 9. ROLLBACK

Grâce à `llm_provider.py`, rollback = `python ... --provider gemini` . Ne jamais supprimer `google-genai` de `requirements.txt` tant que la validation n'est pas 100% OK.

---

## 10. EXEMPLE GPT LOCAL — À REMPLIR PAR L'UTILISATEUR

```python
# examples/gpt_local_example.py  (à fournir par l'utilisateur)
# Coller ici l'exemple réel de la société (client, base_url, appel, parsing)
# L'agent doit s'en inspirer à l'identique pour OpenAIProvider.generate()
```

**Sans cet exemple, l'agent DOIT s'arrêter et le demander.**

---

*Généré pour guider tout agent IA — lire le code, lier l'exemple, respecter les boucliers.*
