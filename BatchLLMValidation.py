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
from google import genai
from google.genai import types
import concurrent.futures
import threading
import argparse
from ApiManager import ApiManager

# Locks for thread safety
stats_lock = threading.Lock()
print_lock = threading.Lock()

def safe_print(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs)

# ==== CONFIG ====
ROOT      = Path(__file__).resolve().parent
RAG_DIR   = ROOT / "Rag_selective"
CAPT_DIR  = ROOT / "capt"
OUT_DIR   = ROOT / "Correction"
MODEL     = "gemini-flash-latest"

# ==== CHARGEMENT DES CLES API ====
def load_api_keys(env_path=".env"):
    keys = []
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        keys.append(parts[1])
    return keys

API_KEYS = load_api_keys()
if not API_KEYS:
    val = os.environ.get("GEMINI_API_KEY", "")
    API_KEYS = [val] if val else []

# L'instance globale de l'ApiManager est initialisée dans main()
API_MANAGER: ApiManager = None

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

def process_table(src: Path, family: str, pdf: str, stats: dict):
    num = table_number(src)
    safe_print(f"\n[Traitement] {src.name} (Table {num})")
    
    # ── Vérification si déjà traité (reprise sur erreur)
    out_dir = OUT_DIR / family / pdf
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / src.name
    if out_file.exists():
        safe_print(f"  -> {src.name} Déjà traité (ignoré)")
        with stats_lock:
            stats["ignored"] += 1
        return

    # ── Images PNG
    img_dir = CAPT_DIR / family / pdf / f"tableau_{num}"
    images = sorted(img_dir.glob("page_*.png"), key=page_number) if img_dir.is_dir() else []
    if not images:
        safe_print(f"  -> {src.name} ERREUR: Pas d'images.")
        with stats_lock:
            stats["no_image"] += 1
        return

    # ── Pages PDF individuelles (extraites par le pipeline, même dossier)
    pdf_pages = sorted(img_dir.glob("page_*.pdf"), key=page_number) if img_dir.is_dir() else []

    # ── Texte PDF
    pdf_path = ROOT / "DataSHEET" / family / f"{pdf}.pdf"
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

    # ── JSON source
    data = json.loads(src.read_text(encoding="utf-8"))
    table_id = data.get("table_id", src.stem)
    payload   = data.get("table_content", data)
    warnings_list = data.get("warnings", [])
    # Injecter table_id ET warnings pour que le LLM les voit
    payload_with_meta = {"table_id": table_id, "warnings": warnings_list, **payload}

    # ── Payload Gemini : Prompt + Images PNG + Pages PDF + Texte PDF + JSON
    contents = [PROMPT]
    for k, img in enumerate(images):
        contents.append("Image page :" if k == 0 else "Suite tableau :")
        contents.append(Image.open(img))
    # Ajouter les pages PDF source (pour que Gemini lise le PDF natif en plus des images)
    for pdf_page_path in pdf_pages:
        try:
            pdf_bytes = pdf_page_path.read_bytes()
            contents.append(
                types.Part.from_bytes(
                    data=pdf_bytes,
                    mime_type="application/pdf"
                )
            )
        except Exception as e:
            safe_print(f"  -> [WARN] Impossible de lire {pdf_page_path.name}: {e}")
    if pdf_text:
        contents.append(f"Texte de référence positionné:\n{pdf_text}")
    contents.append(f"JSON à vérifier:\n{json.dumps(payload_with_meta, ensure_ascii=False, indent=2)}")

    # ── Requête (ApiManager) avec Retry
    max_retries = 1
    for attempt in range(max_retries):
        api_key, k_idx, pool_name = API_MANAGER.get_key()
        
        # Anti-DDoS : Petite pause pour lisser la vitesse des requêtes par IP
        time.sleep(1.5)
        
        client = genai.Client(api_key=api_key)
        real_key_num = k_idx + 1
        n_images = len(images)
        n_pdfs  = len(pdf_pages)
        n_rows = len(payload.get('rows', []))
        safe_print(f"  -> [ENVOI {src.name}] Clé N°{real_key_num} (Pool:{pool_name}) | Modèle: {MODEL} | {n_images} img + {n_pdfs} pdf | {n_rows} lignes JSON (Tentative {attempt+1}/{max_retries})...")
        
        try:
            t0 = time.time()
            response = client.models.generate_content(
                model=MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=0,
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(
                        thinking_budget=8000  # 8000 tokens de reflexion interne
                    )
                )
            )
            t_el = time.time() - t0
            raw = response.text
            size_kb = len(raw.encode('utf-8')) / 1024
            safe_print(f"  -> [RECU {src.name}] Réponse reçue de Clé N°{real_key_num} en {t_el:.1f}s | Taille: {size_kb:.1f} Ko | Parsing JSON...")
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                parsed = {"status": "ERRORS_FOUND", "erreurs_corrigees": parsed}
            safe_print(f"  -> [PARSING {src.name}] Succès.")
            # Récupérer les tokens consommés depuis les métadonnées de la réponse
            try:
                tokens_in  = response.usage_metadata.prompt_token_count or 0
                tokens_out = response.usage_metadata.candidates_token_count or 0
            except Exception:
                tokens_in, tokens_out = 0, 0
            API_MANAGER.report_success(k_idx, tokens_in=tokens_in, tokens_out=tokens_out)
            
            # Plus besoin de bloquer le json si le statut est MANUAL_REVIEW_NEEDED, 
            # on veut juste le sauvegarder avec ce statut pour MergeCorrections.py
            if parsed.get("status") == "ERROR":
                safe_print(f"  -> {src.name} ERREUR CRITIQUE LLM: {parsed.get('erreur_critique', 'Raison non specifiee')}")
                with stats_lock:
                    stats["error"] += 1
                return
            elif parsed.get("status") == "MANUAL_REVIEW_NEEDED":
                safe_print(f"  -> {src.name} REVISION MANUELLE REQUISE (Sauvegarde sans correction)")
                
            # Sécurisation du JSON si le LLM hallucine une liste de listes au lieu de dicts
            erreurs_raw = parsed.get("erreurs_corrigees", [])
            if not isinstance(erreurs_raw, list): erreurs_raw = []
            
            # BOUCLIER ANTI-DESTRUCTION : Filtrage des corrections abusives et Logs
            filtered_erreurs = []
            has_low_confidence = False
            
            for err in erreurs_raw:
                if not isinstance(err, dict): continue # Ignore les erreurs mal formattées (ex: liste)
                
                valid_corrs = []
                for c in err.get("corrections", []):
                    if not isinstance(c, dict): continue
                    
                    vo = str(c.get("valeur_originale_json", "")).strip()
                    nv = str(c.get("nouvelle_valeur", "")).strip()
                    confiance = int(c.get("confiance", 100)) # Par défaut 100% si non spécifié (ex: RTL inversion)
                    
                    # Est-ce que le texte original est un "vrai texte informatif" ? (plus de check len>1)
                    is_informative = vo not in ["", "-", "\u2013", "\u2014"]
                    # Est-ce que la nouvelle valeur est "vide" ou "tiret" ?
                    is_destructive = nv in ["", "-", "\u2013", "\u2014"]
                    
                    # BOUCLIER 1 : Interdit de détruire un vrai texte
                    if is_informative and is_destructive:
                        safe_print(f"      [{src.name}] [BOUCLIER-1] Rejet : tentative d'effacer '{vo}' pour mettre '{nv}'")
                        continue
                    
                    # BOUCLIER 2 : Remplissage de cellule vide / tiret → seuil 90% obligatoire
                    if not is_informative and not is_destructive:
                        if confiance < 90:
                            safe_print(f"      [{src.name}] [BOUCLIER-2] Rejet : confiance {confiance}% < 90% pour remplir cellule vide/tiret par '{nv}'")
                            continue
                        else:
                            safe_print(f"      [{src.name}] [CORRECTION-SPATIALE] Confiance {confiance}% : '{vo}' -> '{nv}'")
                    else:
                        # Correction normale
                        safe_print(f"      [{src.name}] [CORRECTION] Autorisé : '{vo}' -> '{nv}'")
                        
                    # BOUCLIER-4 : Suivi de la confiance globale pour rejet total
                    if confiance < 80:
                        has_low_confidence = True
                        
                    valid_corrs.append(c)
                
                # S'il reste des corrections valides dans cette erreur, on la garde
                if valid_corrs:
                    err["corrections"] = valid_corrs
                    filtered_erreurs.append(err)
            
            # BOUCLIER-4 : Rejet global si une correction est hasardeuse
            if has_low_confidence:
                safe_print(f"  -> [{src.name}] [BOUCLIER-4] ALERTE : Correction à faible confiance (<80%). Annulation totale et mise en MANUAL_REVIEW.")
                parsed["erreurs_corrigees"] = []
                parsed["lignes_en_trop_supprimees"] = []
                parsed["status"] = "MANUAL_REVIEW_NEEDED"
                if "logs" not in parsed: parsed["logs"] = []
                parsed["logs"].append("BOUCLIER-4: Annulation car au moins une correction avait une confiance < 80%.")
                with stats_lock:
                    stats["manual_review"] += 1
                # On s'assure de ne rien corriger
                filtered_erreurs = []
            
            # BOUCLIER-3 : Anti-suppression massive de lignes
            lignes_supprimees = parsed.get("lignes_en_trop_supprimees", [])
            if not isinstance(lignes_supprimees, list): lignes_supprimees = []
            n_total_rows = len(payload.get('rows', []))
            seuil_suppression = max(5, n_total_rows * 0.2)  # Max 20% des lignes ou 5 lignes
            
            if len(lignes_supprimees) > seuil_suppression and parsed.get("status") != "MANUAL_REVIEW_NEEDED":
                safe_print(f"  -> [{src.name}] [BOUCLIER-3] ALERTE : LLM veut supprimer {len(lignes_supprimees)} lignes sur {n_total_rows} (>{seuil_suppression:.0f} max). Suppression annulée, mise en MANUAL_REVIEW.")
                parsed["lignes_en_trop_supprimees"] = []
                parsed["erreurs_corrigees"] = [] # On annule aussi le reste par sécurité
                parsed["status"] = "MANUAL_REVIEW_NEEDED"
                if "logs" not in parsed: parsed["logs"] = []
                parsed["logs"].append("BOUCLIER-3: Tentative de suppression massive annulée.")
                with stats_lock:
                    stats["manual_review"] += 1
                filtered_erreurs = []
            
            parsed["erreurs_corrigees"] = filtered_erreurs
                
            # Sauvegarde
            out_file.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
            # Stats
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
                
            break # Success, on sort de la boucle de retry
                
        except Exception as e:
            err_msg = str(e)
            
            # Rate-limit temporaire → blocage 60s (429, 503, RESOURCE_EXHAUSTED)
            if "429" in err_msg or "503" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "UNAVAILABLE" in err_msg or "exhausted" in err_msg.lower():
                safe_print(f"  -> [{src.name}] RATE-LIMIT. Clé N°{real_key_num} bloquée 60s.")
                API_MANAGER.report_rate_limit(k_idx)
                continue
                
            # Quota journalier épuisé → blocage 24h
            if "quota" in err_msg.lower() or "daily" in err_msg.lower():
                safe_print(f"  -> [{src.name}] QUOTA ÉPUISÉ. Clé N°{real_key_num} bloquée 24h.")
                API_MANAGER.report_exhausted(k_idx)
                continue
            # Erreur permanente → blocage 24h par sécurité
            safe_print(f"  -> [{src.name}] ERREUR PERMANENTE ({err_msg[:60]}). Clé N°{real_key_num} bloquée 24h.")
            API_MANAGER.report_permanent_error(k_idx)
            continue

def main():
    global API_MANAGER
    parser = argparse.ArgumentParser(description="Script de validation LLM par batch.")
    parser.add_argument("--family", type=str, required=True, help="Famille cible (ex: C0, N6)")
    parser.add_argument("--datasheet", type=str, help="Datasheet ciblé (ex: stm32c031c4). Si absent, toute la famille est traitée.")
    parser.add_argument("--workers", type=int, default=6, help="Nombre de requêtes parallèles (défaut: 6)")
    
    args = parser.parse_args()
    
    target_family = args.family
    family_dir = RAG_DIR / target_family
    
    if not family_dir.is_dir():
        print(f"Dossier famille introuvable : {family_dir}")
        return
        
    # Filtrage des datasheets
    if args.datasheet:
        datasheets = [args.datasheet]
        if not (family_dir / args.datasheet).is_dir():
            print(f"Datasheet {args.datasheet} introuvable dans {family_dir}")
            return
    else:
        datasheets = [d.name for d in family_dir.iterdir() if d.is_dir()]

    # ── Initialisation de l'ApiManager (lit api_config.json + api_state.json)
    if not API_KEYS:
        print("[ERREUR] Aucune clé API trouvée dans .env")
        return
    API_MANAGER = ApiManager(API_KEYS)
        
    print(f"Demarrage du Batch LLM sur la Famille {target_family}")
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
        "error": 0,
        "ignored": 0,
        "no_image": 0,
        "crash": 0,
        "total_lines_added": 0,
        "total_lines_removed": 0
    }
    
    for target_ds in datasheets:
        ds_dir = family_dir / target_ds
        all_tables = sorted(
            (f for f in ds_dir.iterdir() if f.is_file() and "table_" in f.name and "all_tables" not in f.name),
            key=table_number
        )
        safe_print(f"\n--- Traitement du Datasheet: {target_ds} ({len(all_tables)} tables) ---")
        stats["total"] += len(all_tables)
        
        # Filtrer les tables déjà traitées avant exécution
        tables_to_process = []
        out_dir = OUT_DIR / target_family / target_ds
        for t in all_tables:
            if (out_dir / t.name).exists():
                safe_print(f"  -> {t.name} Déjà traité (ignoré)")
                stats["ignored"] += 1
            else:
                tables_to_process.append(t)
        
        # Parallel execution: process X tables concurrently
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = []
            for i, t in enumerate(tables_to_process):
                futures.append(executor.submit(process_table, t, target_family, target_ds, stats))
                # Add 60s delay after every 6 tables to avoid API rate limits
                if (i + 1) % 6 == 0 and (i + 1) < len(tables_to_process):
                    safe_print(f"[WAIT] Pause 60s pour l'API après {i + 1} tables...")
                    time.sleep(60)
            
            # Attendre que toutes les tables du datasheet soient traitées
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    safe_print(f"Erreur fatale inattendue sur un thread : {exc}")
                    
        # Génération du review_summary.json pour ce datasheet
        out_dir = OUT_DIR / target_family / target_ds
        summary = []
        if out_dir.is_dir():
            for json_file in sorted(out_dir.glob("*.json"), key=table_number):
                if "review_summary" in json_file.name: continue
                try:
                    data = json.loads(json_file.read_text(encoding="utf-8"))
                    
                    # --- Anti-crash si le LLM a renvoyé une liste au lieu d'un dict ---
                    if isinstance(data, list):
                        data = {"status": "ERRORS_FOUND", "erreurs_corrigees": data}
                    
                    status = data.get("status", "ERRORS_FOUND")
                    if status == "MANUAL_REVIEW_NEEDED":
                        summary.append({
                            "table_file": json_file.name,
                            "table_id": data.get("table_id", ""),
                            "status": status,
                            "logs": data.get("logs", [])
                        })
                except Exception as e:
                    pass
            
            summary_file = out_dir / "review_summary.json"
            summary_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            safe_print(f"  -> [SUMMARY] {len(summary)} tables nécessitent une attention. Sauvegardé dans {target_ds}/review_summary.json")

    # Rapport final + résumé des clés
    report_md = f"""# Rapport d'execution (Famille {target_family})
**Total tableaux détectés** : {stats['total']}

## Statistiques de Correction
- [OK] Parfaits (aucune modification) : {stats['ok']}
- [CORRIGE] Corrigés (modifiés) : {stats['corrected']}
- [A REVISER] Vérification Manuelle (MANUAL_REVIEW_NEEDED) : {stats['manual_review']}

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
    print(f"Tableaux OK : {stats['ok']}")
    print(f"Tableaux CORRIGES : {stats['corrected']}")
    print(f"Tableaux A REVOIR (MANUAL) : {stats['manual_review']}")
    print(f"Fusion terminee ! Fichiers sauvegardes dans : {OUT_DIR}")
    API_MANAGER.print_summary()

if __name__ == "__main__":
    main()
