import os
import sys
# Force UTF-8 pour éviter les crashs sur Windows (ex: caractère μ)
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
import json
import re
import time
from pathlib import Path
from PIL import Image
import pdfplumber
import concurrent.futures
import threading
import argparse

# Provider Myriamx (ST AI Bridge) — provider par défaut
try:
    from llm_provider import STBridgeProvider, GeminiProvider
except ImportError:
    # fallback si exécuté depuis autre cwd
    sys.path.insert(0, str(Path(__file__).parent))
    from llm_provider import STBridgeProvider, GeminiProvider

# ApiManager gardé uniquement pour rollback Gemini
try:
    from ApiManager import ApiManager
except ImportError:
    ApiManager = None

# Locks for thread safety
stats_lock = threading.Lock()
print_lock = threading.Lock()

def safe_print(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs)

# ==== CONFIG ====
ROOT      = Path(__file__).resolve().parent.parent
RAG_DIR   = ROOT / "Output" / "Json" / "Selective_Tables"
CAPT_DIR  = ROOT / "Output" / "Images" / "Tables_Screenshots"
OUT_DIR   = ROOT / "Output" / "Json" / "LLM_Corrections"
MORE_DIR  = ROOT / "Output" / "Json" / "More_Than_7"
MODEL     = "gemini-flash-latest"  # legacy, gardé pour rollback
ST_MODEL  = "st-bridge-myriamx"    # label log ST Bridge

# ==== CHARGEMENT DES CLES API ====
def load_api_keys(env_path=".env", provider="stbridge"):
    keys = []
    if provider in ("stbridge", "myriamx", "st"):
        # ST Bridge mono-clé
        val = os.environ.get("ST_AI_BRIDGE_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        if val:
            return [val]
        # fallback défaut Myriamx
        fallback = "38d5a975-128d-4106-8cc8-394dc1122696"
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        if "ST_AI_BRIDGE_API_KEY" in line:
                            parts = line.split("=", 1)
                            if len(parts) == 2 and parts[1].strip() and "AIza" not in parts[1]:
                                keys.append(parts[1].strip())
        if keys:
            return keys
        return [fallback]
    # Gemini legacy
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        keys.append(parts[1])
    return keys

# L'instance globale du provider est initialisée dans main()
API_MANAGER = None
ST_PROVIDER: STBridgeProvider = None
PROVIDER_NAME = "stbridge"

# ==== PROMPT v5 — INTELLIGENCE VISUELLE ====
PROMPT = """Tu es un ingénieur expert en microcontrôleurs STM32, doté d'une excellente intelligence visuelle.
Tu reçois l'image d'un tableau d'une datasheet STM32 et le JSON généré par un outil d'extraction (pdfplumber).
L'outil d'extraction fait souvent des erreurs de compréhension spatiale et de lecture.
Ton rôle est d'utiliser ton INTELLIGENCE VISUELLE pour corriger ces erreurs afin que le JSON reflète PARFAITEMENT la structure visuelle de l'image.

## ERREURS FRÉQUENTES À CORRIGER INTELLIGEMMENT :
1. **Texte Inversé (RTL) - [TRÈS IMPORTANT]** : Le texte vertical est très souvent lu à l'envers (ex: `)stib 21( CDA` au lieu de `ADC (12 bits)`). Vérifie SOIGNEUSEMENT chaque ligne et chaque en-tête pour t'assurer qu'il n'y a pas de texte inversé. Remets-le impérativement dans le bon sens !
7. **Codes de composants mal orthographiés** : Les en-têtes verticaux ont parfois des lettres inversées ou des erreurs de lecture (ex: `Q3H0X546N` au lieu de `STM32N645X0H3Q`). Corrige-les.
3. **Mots incomplets ou coupés** : Reconstruis les mots qui ont été mal extraits.
4. **CORRECTIONS GLOBALES SUR TOUTES LES LIGNES** : Si tu remarques une erreur (comme une faute, un symbole mal découpé ou mal extrait) qui se répète sur **plusieurs lignes**, tu DOIS impérativement fournir la correction pour **CHACUNE des lignes impactées** (en créant un bloc de correction avec le `row_index` correspondant pour chaque ligne). Ne te contente pas de corriger seulement la 1ère ligne !

## RÈGLE D'OR CONCERNANT LES FUSIONS ET LES CASES VIDES ("") :
- **NE TOUCHE JAMAIS AUX CASES VIDES (`""`)**. Ne tente **JAMAIS** de deviner comment résoudre les fusions complexes (verticales ou horizontales).
- Si une case est vide (`""`) dans le JSON, **LAISSE-LA VIDE (`""`)**. 
- Si tu vois des cases vides, ajoute simplement un message dans le champ `logs` (ex: "Cases vides ignorées pour traitement manuel").
- Tu dois tout de même corriger le reste du texte (inversions, fautes) dans le tableau, en ignorant simplement les cases vides.

## RÈGLE SUR LES EN-TÊTES DUPLIQUÉS (ex: "Conditions" / "Conditions") :
- Si le JSON contient **deux colonnes avec le même nom d'en-tête** (ex: deux colonnes "Conditions"), **ce n'est PAS une erreur**. C'est le comportement normal de l'outil d'extraction (pdfplumber) face à une cellule d'en-tête fusionnée horizontalement dans le PDF qui couvre deux sous-colonnes.
- **NE PAS signaler `ERRORS_FOUND` uniquement à cause d'un en-tête dupliqué.** Ne mets rien dans `logs` pour ce cas.
- Regarde plutôt si les **valeurs** dans ces colonnes sont correctes par rapport à l'image. Si oui, retourne `"status": "OK"`.

## NIVEAU D'EXIGENCE : PERFECTIONNISME ABSOLU
- SOIS EXTRÊMEMENT PRÉCIS ET PERFECTIONNISTE dans ton analyse visuelle de l'image.
- Scanne chaque ligne et chaque colonne. Assure-toi que chaque donnée correspond EXACTEMENT à la bonne colonne (Header).

## CONSIGNES DE SÉCURITÉ ABSOLUES :
- **RÈGLE ANTI-HALLUCINATION TECHNIQUE** : NE JAMAIS utiliser vos connaissances préalables en électronique ou sur les microcontrôleurs pour "corriger" des valeurs techniques (ex: mémoire, nombre de broches, fréquences). Vous êtes un simple correcteur OCR visuel. Ne faites aucune déduction logique sur le regroupement des boîtiers. Si l'image affiche distinctement une valeur dans une colonne, gardez-la, même si cela vous semble techniquement illogique. Ne décalez jamais les valeurs pour forcer une cohérence technique inventée.
- NE JAMAIS vider une case contenant du texte (ne remplace pas un texte par un tiret ou un vide).
- NE JAMAIS modifier les noms des périphériques, des broches ou des signaux (ex: COMP12, PA5), même s'ils semblent être des fautes de frappe.
- NE JAMAIS regrouper plusieurs références de composants (Part numbers) séparées par des virgules sur une seule ligne. Si le JSON a mis chaque référence sur une ligne distincte, c'est intentionnel, ne les fusionne pas !
- NE JAMAIS ajouter ou retirer des colonnes.
- VOUS DEVEZ itérer sur la totalité des lignes du tableau. Ne vous arrêtez pas après 3 corrections si le problème se répète sur d'autres lignes.
- Les lignes d'en-tête (Header) répétées au milieu du tableau (à cause d'un saut de page) doivent être mises dans `lignes_en_trop_supprimees`.

Agis avec le bon sens d'un ingénieur humain. Si le JSON est totalement décalé par rapport aux colonnes de l'image, retourne le statut "MANUAL_REVIEW_NEEDED".

## FORMAT DE SORTIE STRICTEMENT ATTENDU EN JSON :
{
  "table_id": "<Nom du tableau (ex: table_1)>",
  "status": "OK" ou "ERRORS_FOUND" ou "MANUAL_REVIEW_NEEDED",
  "logs": [
    "<Laisse vide [] si OK. Si tu laisses des cases vides incertaines, mets ici le détail précis (ex: 'Ligne 3, Col 2 laissée vide')>"
  ],
  "erreurs_corrigees": [
    {
      "ligne": "<Texte représentatif de la ligne, ex: FMC SDRAM32>",
      "row_index": <int (index de la ligne dans rows[]) ou null pour les en-têtes>,
      "analyse_visuelle": "<Ton raisonnement intelligent décrivant ce que tu vois sur l'image>",
      "corrections": [
        {
          "colonne_index": <int>,
          "valeur_originale_json": "<str>",
          "nouvelle_valeur": "<str>",
          "confiance": <int 0-100>
        }
      ]
    }
  ],
  "lignes_manquantes_ajoutees": [
    {
      "row_index": <int, position où insérer la ligne>,
      "contenu": ["<val_col0>", "<val_col1>", "..."]
    }
  ],
  "lignes_en_trop_supprimees": [<int>, <int>, ...]
}

IMPORTANT FORMAT — lignes_en_trop_supprimees :
- C'est une **liste d'entiers** représentant les index (0-basés) des lignes à supprimer dans rows[].
- Exemple correct : [14, 27, 38, 54]
- Les lignes d'en-tête répétées (à cause d'un saut de page) ont leur contenu identique aux headers du tableau. Donne leur row_index exact.
"""

def table_number(path: Path) -> int:
    m = re.search(r'table_(\d+)', path.name)
    return int(m.group(1)) if m else 0

def page_number(path: Path) -> int:
    m = re.search(r'page_(\d+)', path.name)
    return int(m.group(1)) if m else 0

def _parse_and_shield(src_name: str, raw: str, payload: dict, t_el: float):
    """Parsing commun + boucliers 1-4, retourne parsed ou None si erreur critique"""
    parsed = json.loads(raw)
    if isinstance(parsed, list):
        parsed = {"status": "ERRORS_FOUND", "erreurs_corrigees": parsed}
    # ERREUR CRITIQUE
    if parsed.get("status") == "ERROR":
        safe_print(f"  -> {src_name} ERREUR CRITIQUE LLM: {parsed.get('erreur_critique', 'Raison non specifiee')}")
        return None, "error"
    if parsed.get("status") == "MANUAL_REVIEW_NEEDED":
        safe_print(f"  -> {src_name} REVISION MANUELLE REQUISE (Sauvegarde sans correction)")
    erreurs_raw = parsed.get("erreurs_corrigees", [])
    if not isinstance(erreurs_raw, list):
        erreurs_raw = []
    filtered_erreurs = []
    has_low_confidence = False
    for err in erreurs_raw:
        if not isinstance(err, dict):
            continue
        valid_corrs = []
        for c in err.get("corrections", []):
            if not isinstance(c, dict):
                continue
            vo = str(c.get("valeur_originale_json", "")).strip()
            nv = str(c.get("nouvelle_valeur", "")).strip()
            confiance = int(c.get("confiance", 100))
            is_informative = vo not in ["", "-", "\u2013", "\u2014"]
            is_destructive = nv in ["", "-", "\u2013", "\u2014"]
            if is_informative and is_destructive:
                safe_print(f"      [{src_name}] [BOUCLIER-1] Rejet : tentative d'effacer '{vo}' pour mettre '{nv}'")
                continue
            if not is_informative and not is_destructive:
                if confiance < 90:
                    safe_print(f"      [{src_name}] [BOUCLIER-2] Rejet : confiance {confiance}% < 90% pour remplir cellule vide/tiret par '{nv}'")
                    continue
                else:
                    safe_print(f"      [{src_name}] [CORRECTION-SPATIALE] Confiance {confiance}% : '{vo}' -> '{nv}'")
            else:
                safe_print(f"      [{src_name}] [CORRECTION] Autorisé : '{vo}' -> '{nv}'")
            if confiance < 80:
                has_low_confidence = True
            valid_corrs.append(c)
        if valid_corrs:
            err["corrections"] = valid_corrs
            filtered_erreurs.append(err)
    if has_low_confidence:
        safe_print(f"  -> [{src_name}] [BOUCLIER-4] ALERTE : Correction à faible confiance (<80%). Annulation totale et mise en MANUAL_REVIEW.")
        parsed["erreurs_corrigees"] = []
        parsed["lignes_en_trop_supprimees"] = []
        parsed["status"] = "MANUAL_REVIEW_NEEDED"
        if "logs" not in parsed:
            parsed["logs"] = []
        parsed["logs"].append("BOUCLIER-4: Annulation car au moins une correction avait une confiance < 80%.")
        filtered_erreurs = []
    lignes_supprimees = parsed.get("lignes_en_trop_supprimees", [])
    if not isinstance(lignes_supprimees, list):
        lignes_supprimees = []
    n_total_rows = len(payload.get('rows', []))
    seuil_suppression = max(5, n_total_rows * 0.2)
    if len(lignes_supprimees) > seuil_suppression and parsed.get("status") != "MANUAL_REVIEW_NEEDED":
        safe_print(f"  -> [{src_name}] [BOUCLIER-3] ALERTE : LLM veut supprimer {len(lignes_supprimees)} lignes sur {n_total_rows} (>{seuil_suppression:.0f} max). Suppression annulée, mise en MANUAL_REVIEW.")
        parsed["lignes_en_trop_supprimees"] = []
        parsed["erreurs_corrigees"] = []
        parsed["status"] = "MANUAL_REVIEW_NEEDED"
        if "logs" not in parsed:
            parsed["logs"] = []
        parsed["logs"].append("BOUCLIER-3: Tentative de suppression massive annulée.")
        filtered_erreurs = []
    parsed["erreurs_corrigees"] = filtered_erreurs
    return parsed, None

def process_table(src: Path, family: str, pdf: str, stats: dict, target_pool_state: dict = None):
    num = table_number(src)
    pool_label = target_pool_state.get('pool') if target_pool_state else PROVIDER_NAME
    safe_print(f"\n[Traitement] {src.name} (Table {num} | Provider: {PROVIDER_NAME} | Pool: {pool_label})")
    
    out_dir = OUT_DIR / family / pdf
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / src.name
    if out_file.exists():
        safe_print(f"  -> {src.name} Déjà traité (ignoré)")
        with stats_lock:
            stats["ignored"] += 1
        return

    img_dir = CAPT_DIR / family / pdf / f"tableau_{num}"
    images = sorted(img_dir.glob("page_*.png"), key=page_number) if img_dir.is_dir() else []
    if not images:
        safe_print(f"  -> {src.name} ERREUR: Pas d'images.")
        with stats_lock:
            stats["no_image"] += 1
        return

    # ── GARDE More Than 7 : si >7 images (merged_pages étendu), on skip LLM et on archive à part
    if len(images) > 7:
        more_dir = MORE_DIR / family / pdf
        more_dir.mkdir(parents=True, exist_ok=True)
        more_file = more_dir / src.name
        if not more_file.exists():
            # On garde trace des pages réelles vues (déduites des PNG)
            pages = sorted({page_number(p) for p in images})
            more_payload = {
                "table_id": src.stem,
                "status": "MORE_THAN_7",
                "reason": f"Skipped LLM: {len(images)} images >7 (trop volumineux)",
                "images_count": len(images),
                "merged_pages": pages,
                "pages": pages,
                "is_continued": len(pages) > 1,
                "logs": ["More Than 7 — table dépasse 7 pages/images, nécessite découpage manuel ou traitement spécifique"],
                "erreurs_corrigees": [],
                "lignes_manquantes_ajoutees": [],
                "lignes_en_trop_supprimees": [],
                "images_dir": str(img_dir),
                "source_json": str(src),
            }
            more_file.write_text(json.dumps(more_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        safe_print(f"  -> [{src.name}] MORE_THAN_7 : {len(images)} images >7 — archivé dans More_Than_7/{family}/{pdf}/{src.name} (skip LLM)")
        with stats_lock:
            stats["more_than_7"] = stats.get("more_than_7", 0) + 1
        return

    # Texte PDF
    pdf_path = ROOT / "Input" / "PDFs" / family / f"{pdf}.pdf"
    pdf_text = ""
    if pdf_path.is_file():
        try:
            pages = sorted({page_number(p) for p in images})
            with pdfplumber.open(pdf_path) as pdf_doc:
                parts = []
                for pno in pages:
                    if 1 <= pno <= len(pdf_doc.pages):
                        page = pdf_doc.pages[pno - 1]
                        words = page.extract_words()
                        if not words: continue
                        lines = {}
                        for w in words:
                            lines.setdefault(round(w["top"]/3)*3, []).append(w)
                        buf = [f"--- PAGE {pno} ---"]
                        for top in sorted(lines):
                            ws = sorted(lines[top], key=lambda w: w["x0"])
                            buf.append("  " + " ".join(f"{w['text']}@{round(w['x0'])}" for w in ws))
                        parts.append("\n".join(buf))
            pdf_text = "\n\n".join(parts)
        except Exception as e:
            safe_print(f"  -> {src.name} Erreur lecture PDF: {e}")

    data = json.loads(src.read_text(encoding="utf-8"))
    table_id = data.get("table_id", src.stem)
    payload   = data.get("table_content", data)
    warnings_list = data.get("warnings", [])
    payload_with_meta = {"table_id": table_id, "warnings": warnings_list, **payload}
    json_str = json.dumps(payload_with_meta, ensure_ascii=False, indent=2)

    # ================= ST BRIDGE =================
    if PROVIDER_NAME in ("stbridge", "myriamx", "st", "openai"):
        # Anti-DDoS 5s (demandé utilisateur, vs 15s Myriamx)
        time.sleep(5)
        t0 = time.time()
        n_rows = len(payload.get('rows', []))
        safe_print(f"  -> [ENVOI {src.name}] Provider:STBridge | {len(images)} img | {n_rows} lignes JSON ...")
        try:
            result = ST_PROVIDER.generate(
                prompt=PROMPT,
                images=images,
                json_str=json_str,
                pdf_text=pdf_text,
                responseFormat=None,  # hérite de .env RESPONSE_FORMAT / config.yaml (json_object)
                temperature=None,  # hérite de .env TEMPERATURE (0.2) — bridge exige >0
                maxResponseTokens=None,  # hérite de .env MAX_RESPONSE_TOKENS (32400)
                reasoningEffort=None,
                include_header_zoom=True,
            )
            t_el = time.time() - t0
            raw = result["text"] if isinstance(result, dict) else str(result)
            size_kb = len(raw.encode('utf-8')) / 1024
            safe_print(f"  -> [RECU {src.name}] Réponse STBridge en {t_el:.1f}s | Taille: {size_kb:.1f} Ko | Parsing JSON...")
            # Normaliser markdown ```json si présent
            if raw.strip().startswith("```"):
                import re as _re
                m = _re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
                if m:
                    raw = m.group(1)
            parsed, err = _parse_and_shield(src.name, raw, payload, t_el)
            if err == "error":
                with stats_lock:
                    stats["error"] += 1
                return
            # Sauvegarde
            out_file.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
            status = parsed.get("status", "OK")
            nb_corr = len(parsed.get("erreurs_corrigees", []))
            nb_add = len(parsed.get("lignes_manquantes_ajoutees", []))
            nb_del = len(parsed.get("lignes_en_trop_supprimees", []))
            if status == "OK" and nb_corr == 0 and nb_add == 0 and nb_del == 0:
                safe_print(f"  -> [{src.name}] OK (Aucune erreur) [{t_el:.1f}s]")
                with stats_lock:
                    stats["ok"] += 1
            elif status == "MANUAL_REVIEW_NEEDED":
                with stats_lock:
                    stats["manual_review"] += 1
            else:
                safe_print(f"  -> [{src.name}] CORRIGÉ: {sum(len(e.get('corrections', [])) for e in parsed.get('erreurs_corrigees', []))} cellules fix, {nb_add} lignes +, {nb_del} lignes - [{t_el:.1f}s]")
                with stats_lock:
                    stats["corrected"] += 1
                    stats["total_lines_added"] += nb_add
                    stats["total_lines_removed"] += nb_del
            return
        except Exception as e:
            t_el = time.time() - t0
            safe_print(f"  -> [{src.name}] ERREUR STBridge après {t_el:.1f}s: {e}")
            with stats_lock:
                stats["error"] += 1
            return

    # ================= GEMINI (rollback) =================
    # Garde exacte logique initiale avec ApiManager
    from google import genai
    from google.genai import types
    # Reconstituer contents legacy (avec PDF parts)
    pdf_pages = sorted(img_dir.glob("page_*.pdf"), key=page_number) if img_dir.is_dir() else []
    contents = [PROMPT]
    for k, img in enumerate(images):
        contents.append("Image page :" if k == 0 else "Suite tableau :")
        contents.append(Image.open(img))
    for pdf_page_path in pdf_pages:
        try:
            pdf_bytes = pdf_page_path.read_bytes()
            contents.append(types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"))
        except Exception as e:
            safe_print(f"  -> [WARN] Impossible de lire {pdf_page_path.name}: {e}")
    if pdf_text:
        contents.append(f"Texte de référence positionné:\n{pdf_text}")
    contents.append(f"JSON à vérifier:\n{json_str}")

    pool_size   = len(API_MANAGER._pools.get(target_pool_state.get("pool", ""), [])) if API_MANAGER and target_pool_state else 1
    max_retries = max(pool_size, 5)
    for attempt in range(max_retries):
        if API_MANAGER and target_pool_state:
            api_key, k_idx, pool_name = API_MANAGER.get_key_from_pool(target_pool_state)
        else:
            api_key = load_api_keys(provider="gemini")[0]
            k_idx, pool_name = 0, "default"
        time.sleep(10)
        client = genai.Client(api_key=api_key)
        real_key_num = k_idx + 1
        n_rows = len(payload.get('rows', []))
        safe_print(f"  -> [ENVOI {src.name}] Clé N°{real_key_num} (Pool:{pool_name}) | Modèle: {MODEL} | {len(images)} img | {n_rows} lignes JSON (Tentative {attempt+1}/{max_retries})...")
        try:
            t0 = time.time()
            response = client.models.generate_content(
                model=MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(thinking_budget=8000)
                )
            )
            t_el = time.time() - t0
            raw = response.text
            safe_print(f"  -> [RECU {src.name}] Réponse reçue de Clé N°{real_key_num} en {t_el:.1f}s | Parsing JSON...")
            parsed, err = _parse_and_shield(src.name, raw, payload, t_el)
            if err == "error":
                with stats_lock:
                    stats["error"] += 1
                return
            try:
                tokens_in  = response.usage_metadata.prompt_token_count or 0
                tokens_out = response.usage_metadata.candidates_token_count or 0
            except Exception:
                tokens_in, tokens_out = 0, 0
            if API_MANAGER:
                API_MANAGER.report_success(k_idx, tokens_in=tokens_in, tokens_out=tokens_out)
            out_file.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
            status = parsed.get("status", "OK")
            if parsed.get("status") == "MANUAL_REVIEW_NEEDED":
                with stats_lock:
                    stats["manual_review"] += 1
            elif status == "OK" and len(parsed.get("erreurs_corrigees", []))==0:
                with stats_lock:
                    stats["ok"] += 1
            else:
                with stats_lock:
                    stats["corrected"] += 1
            break
        except Exception as e:
            err_msg = str(e)
            if "429" in err_msg or "503" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "UNAVAILABLE" in err_msg or "exhausted" in err_msg.lower():
                safe_print(f"  -> [{src.name}] RATE-LIMIT. Clé N°{real_key_num} bloquée 60s.")
                if API_MANAGER:
                    API_MANAGER.report_rate_limit(k_idx)
                continue
            if "quota" in err_msg.lower() or "daily" in err_msg.lower():
                safe_print(f"  -> [{src.name}] QUOTA ÉPUISÉ. Clé N°{real_key_num} bloquée 24h.")
                if API_MANAGER:
                    API_MANAGER.report_exhausted(k_idx)
                continue
            safe_print(f"  -> [{src.name}] ERREUR PERMANENTE ({err_msg[:60]}). Clé N°{real_key_num} bloquée 24h.")
            if API_MANAGER:
                API_MANAGER.report_permanent_error(k_idx)
            continue

def main():
    global API_MANAGER, ST_PROVIDER, PROVIDER_NAME
    parser = argparse.ArgumentParser(description="Script de validation LLM par batch.")
    fam_group = parser.add_mutually_exclusive_group(required=True)
    fam_group.add_argument("--family", type=str, help="Famille cible (ex: C0, N6)")
    fam_group.add_argument("--an",     type=str, help="Dossier Application Notes (ex: 41)")
    parser.add_argument("--datasheet", type=str, help="Datasheet ciblé (ex: stm32c031c4 ou AN1709). Si absent, toute la famille est traitée.")
    parser.add_argument("--workers", type=int, default=None, help="Nombre de requêtes parallèles (défaut: 1 pour stbridge, 6 pour gemini)")
    parser.add_argument("--provider", type=str, default="stbridge", choices=["stbridge","myriamx","st","gemini","openai"], help="Provider LLM (défaut: stbridge)")
    
    args = parser.parse_args()
    PROVIDER_NAME = args.provider.lower()
    if args.workers is None:
        args.workers = 1 if PROVIDER_NAME in ("stbridge","myriamx","st","openai") else 6
    # Avertissement workers mono-clé
    if PROVIDER_NAME in ("stbridge","myriamx","st","openai") and args.workers > 2:
        safe_print(f"[WARN] Provider STBridge mono-clé : workers={args.workers} risqué, recommandé 1-2. Forçage conseillé à 1.")
    
    target_family = args.family if args.family else args.an
    family_dir = RAG_DIR / target_family
    
    if not family_dir.is_dir():
        print(f"Dossier famille introuvable : {family_dir}")
        return
        
    if args.datasheet:
        datasheets = [args.datasheet]
        if not (family_dir / args.datasheet).is_dir():
            print(f"Datasheet {args.datasheet} introuvable dans {family_dir}")
            return
    else:
        datasheets = [d.name for d in family_dir.iterdir() if d.is_dir()]

    if PROVIDER_NAME in ("stbridge","myriamx","st","openai"):
        keys = load_api_keys(provider="stbridge")
        if not keys or not keys[0]:
            print("[ERREUR] Aucune clé ST_BRIDGE trouvée. Définir ST_AI_BRIDGE_API_KEY dans .env")
            return
        ST_PROVIDER = STBridgeProvider(api_key=keys[0])
        print(f"Demarrage du Batch LLM sur la Famille {target_family} avec PROVIDER=STBridge (Myriamx)")
        if args.datasheet:
            print(f"Cible spécifique : {args.datasheet}")
        else:
            print(f"Datasheets détectées : {datasheets}")
        print(f"[STBridge] URL={ST_PROVIDER.url} | Key=...{keys[0][-6:]} | Workers={args.workers}")
    else:
        keys = load_api_keys(provider="gemini")
        if not keys or not any(keys):
            print("[ERREUR] Aucune clé API Gemini trouvée dans .env")
            return
        if ApiManager is None:
            print("[ERREUR] ApiManager introuvable pour provider gemini")
            return
        API_MANAGER = ApiManager(keys)
        print(f"Demarrage du Batch LLM sur la Famille {target_family} avec PROVIDER=Gemini (rollback)")
        if args.datasheet:
            print(f"Cible spécifique : {args.datasheet}")
        else:
            print(f"Datasheets détectées : {datasheets}")
        API_MANAGER.print_summary()
    
    stats = {
        "total": 0,
        "ok": 0,
        "corrected": 0,
        "manual_review": 0,
        "more_than_7": 0,
        "error": 0,
        "ignored": 0,
        "no_image": 0,
        "crash": 0,
        "total_lines_added": 0,
        "total_lines_removed": 0
    }
    
    if PROVIDER_NAME in ("stbridge","myriamx","st","openai"):
        # Mode STBridge : pas de pools, traitement séquentiel par datasheet
        for target_ds_idx, target_ds in enumerate(datasheets):
            ds_dir = family_dir / target_ds
            all_tables = sorted(
                (f for f in ds_dir.iterdir() if f.is_file() and "table_" in f.name and "all_tables" not in f.name),
                key=table_number
            )
            safe_print(f"\n--- Traitement du Datasheet: {target_ds} ({len(all_tables)} tables) ---")
            stats["total"] += len(all_tables)
            tables_to_process = []
            out_dir = OUT_DIR / target_family / target_ds
            for t in all_tables:
                if (out_dir / t.name).exists():
                    safe_print(f"  -> {t.name} Déjà traité (ignoré)")
                    stats["ignored"] += 1
                else:
                    tables_to_process.append(t)
            if not tables_to_process:
                safe_print(f"  -> Aucune table à traiter pour {target_ds}")
                continue
            safe_print(f"  -> [STBridge] {len(tables_to_process)} tables à traiter (mono-clé, workers={args.workers})")
            batch_size = args.workers
            batches = [tables_to_process[i:i + batch_size] for i in range(0, len(tables_to_process), batch_size)]
            for batch_idx, batch in enumerate(batches):
                safe_print(f"\n  -> [BATCH {batch_idx+1}/{len(batches)}] Lancement de {len(batch)} table(s) en parallèle...")
                with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
                    futures = []
                    for i, t in enumerate(batch):
                        if i > 0:
                            time.sleep(1)
                        futures.append(executor.submit(process_table, t, target_family, target_ds, stats, {"pool": "stbridge"}))
                    done, _ = concurrent.futures.wait(futures, return_when=concurrent.futures.ALL_COMPLETED)
                    for future in done:
                        try:
                            future.result()
                        except Exception as exc:
                            safe_print(f"Erreur fatale thread : {exc}")
                if batch_idx < len(batches) - 1:
                    safe_print(f"  -> [WAIT] Batch {batch_idx+1} terminé. Pause de 5s...")
                    time.sleep(5)
            # summary
            out_dir = OUT_DIR / target_family / target_ds
            summary = []
            if out_dir.is_dir():
                for json_file in sorted(out_dir.glob("*.json"), key=table_number):
                    if "review_summary" in json_file.name: continue
                    try:
                        data = json.loads(json_file.read_text(encoding="utf-8"))
                        if isinstance(data, list):
                            data = {"status": "ERRORS_FOUND", "erreurs_corrigees": data}
                        if data.get("status") == "MANUAL_REVIEW_NEEDED":
                            summary.append({"table_file": json_file.name, "table_id": data.get("table_id", ""), "status": data.get("status"), "logs": data.get("logs", [])})
                    except Exception:
                        pass
                (out_dir / "review_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
                safe_print(f"  -> [SUMMARY] {len(summary)} tables à revue. Sauvegardé dans {target_ds}/review_summary.json")
            if target_ds_idx < len(datasheets) - 1:
                safe_print(f"\n[WAIT] Datasheet {target_ds} terminé. Pause 5s...")
                time.sleep(5)
    else:
        # Mode Gemini legacy avec pools
        pool_names = API_MANAGER.get_pool_names()
        used_pools_in_cycle = set()
        current_pool_idx = 0
        for target_ds_idx, target_ds in enumerate(datasheets):
            ds_dir = family_dir / target_ds
            all_tables = sorted((f for f in ds_dir.iterdir() if f.is_file() and "table_" in f.name and "all_tables" not in f.name), key=table_number)
            safe_print(f"\n--- Traitement du Datasheet: {target_ds} ({len(all_tables)} tables) ---")
            stats["total"] += len(all_tables)
            tables_to_process = []
            out_dir = OUT_DIR / target_family / target_ds
            for t in all_tables:
                if (out_dir / t.name).exists():
                    safe_print(f"  -> {t.name} Déjà traité (ignoré)")
                    stats["ignored"] += 1
                else:
                    tables_to_process.append(t)
            if not tables_to_process:
                safe_print(f"  -> Aucune table à traiter pour {target_ds}, passage au suivant (pool non consommé).")
                continue
            selected_pool = None
            for offset in range(len(pool_names)):
                candidate = pool_names[(current_pool_idx + offset) % len(pool_names)]
                if candidate not in used_pools_in_cycle:
                    selected_pool = candidate
                    current_pool_idx = (current_pool_idx + offset) % len(pool_names)
                    break
            if selected_pool is None:
                safe_print(f"\n[CYCLE] Tous les pools ont été utilisés. Réinitialisation du cycle.")
                used_pools_in_cycle.clear()
                selected_pool = pool_names[current_pool_idx % len(pool_names)]
            used_pools_in_cycle.add(selected_pool)
            target_pool_state = {"pool": selected_pool, "used_pools": used_pools_in_cycle}
            safe_print(f"  -> [POOL] Utilisation du pool {selected_pool} pour {target_ds} ({len(tables_to_process)} tables à traiter)")
            batch_size = args.workers
            batches = [tables_to_process[i:i + batch_size] for i in range(0, len(tables_to_process), batch_size)]
            for batch_idx, batch in enumerate(batches):
                safe_print(f"\n  -> [BATCH {batch_idx+1}/{len(batches)}] Lancement de {len(batch)} table(s) en parallèle...")
                with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
                    futures = []
                    for i, t in enumerate(batch):
                        if i > 0:
                            time.sleep(1)
                        futures.append(executor.submit(process_table, t, target_family, target_ds, stats, target_pool_state))
                    done, _ = concurrent.futures.wait(futures, return_when=concurrent.futures.ALL_COMPLETED)
                    for future in done:
                        try:
                            future.result()
                        except Exception as exc:
                            if "ALL_POOLS_BLOCKED" in str(exc) or "POOL_BLOCKED" in str(exc):
                                safe_print(f"\n[URGENCE] {exc}")
                                sys.exit(1)
                            safe_print(f"Erreur fatale inattendue sur un thread : {exc}")
                if batch_idx < len(batches) - 1:
                    safe_print(f"  -> [WAIT] Batch {batch_idx+1} terminé. Pause de 5s...")
                    time.sleep(5)
            out_dir = OUT_DIR / target_family / target_ds
            summary = []
            if out_dir.is_dir():
                for json_file in sorted(out_dir.glob("*.json"), key=table_number):
                    if "review_summary" in json_file.name: continue
                    try:
                        data = json.loads(json_file.read_text(encoding="utf-8"))
                        if isinstance(data, list):
                            data = {"status": "ERRORS_FOUND", "erreurs_corrigees": data}
                        if data.get("status") == "MANUAL_REVIEW_NEEDED":
                            summary.append({"table_file": json_file.name, "table_id": data.get("table_id", ""), "status": data.get("status"), "logs": data.get("logs", [])})
                    except Exception:
                        pass
                (out_dir / "review_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
                safe_print(f"  -> [SUMMARY] {len(summary)} tables nécessitent une attention. Sauvegardé dans {target_ds}/review_summary.json")
            if target_ds_idx < len(datasheets) - 1:
                safe_print(f"\n[WAIT] Datasheet {target_ds} termine avec succès. Pause de 5s avant le prochain datasheet...")
                time.sleep(5)
            current_pool_idx = (current_pool_idx + 1) % len(pool_names)

    report_md = f"""# Rapport d'execution (Famille {target_family} | Provider {PROVIDER_NAME})
**Total tableaux détectés** : {stats['total']}

## Statistiques de Correction
- [OK] Parfaits (aucune modification) : {stats['ok']}
- [CORRIGE] Corrigés (modifiés) : {stats['corrected']}
- [A REVISER] Vérification Manuelle (MANUAL_REVIEW_NEEDED) : {stats['manual_review']}
- [MORE_THAN_7] Dépassement >7 images : {stats.get('more_than_7',0)}

## Détails
- Total de lignes ajoutées : {stats['total_lines_added']}
- Total de lignes supprimées : {stats['total_lines_removed']}

## Erreurs et Ignorés
- [IGNORE] Déjà traités (ignorés) : {stats['ignored']}
- [SANS IMAGE] Sans image (capt manquant) : {stats['no_image']}
- [ERREUR] Erreurs API : {stats['error']}
- [CRASH] Crash/Timeout API : {stats['crash']}
"""
    report_file = ROOT / f"Rapport_Validation_{target_family}_Complet.md"
    report_file.write_text(report_md, encoding="utf-8")
    
    print("\n" + "="*40)
    print("=== BILAN FUSION ===")
    print(f"Provider: {PROVIDER_NAME}")
    print(f"Tableaux OK : {stats['ok']}")
    print(f"Tableaux CORRIGES : {stats['corrected']}")
    print(f"Tableaux A REVOIR (MANUAL) : {stats['manual_review']}")
    print(f"Tableaux MORE_THAN_7 : {stats.get('more_than_7',0)} (voir Output/Json/More_Than_7)")
    print(f"Fusion terminee ! Fichiers sauvegardes dans : {OUT_DIR} | More_Than_7: {MORE_DIR}")
    if API_MANAGER:
        API_MANAGER.print_summary()

if __name__ == "__main__":
    main()
