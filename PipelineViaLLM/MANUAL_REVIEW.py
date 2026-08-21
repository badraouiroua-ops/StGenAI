import os
import sys
import json
import time
import re
import argparse
from pathlib import Path
from PIL import Image
import pdfplumber
import threading
import concurrent.futures
from google import genai
from google.genai import types

from ApiManager import ApiManager

# Force UTF-8
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# ==== CONFIG ====
ROOT = Path(__file__).resolve().parent.parent
CORRECTION_DIR = ROOT / "Output" / "Json" / "LLM_Corrections"
RAG_SELECTIVE_DIR = ROOT / "Output" / "Json" / "Selective_Tables"
CAPT_DIR = ROOT / "Output" / "Images" / "Tables_Screenshots"
OUT_DIR = ROOT / "Output" / "Json" / "Manual_Review"
PROMPET_PATH = ROOT / "PipelineViaLLM" / "Prompt_Tables.txt"
MODEL = "gemini-flash-latest"

# Locks
print_lock = threading.Lock()
stats_lock = threading.Lock()

stats = {
    "processed": 0,
    "success": 0,
    "failed": 0
}

def safe_print(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs)

# ==== LOAD API KEYS ====
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

API_MANAGER = ApiManager(API_KEYS) if API_KEYS else None

# ==== LOAD PROMPT ====
def load_prompt():
    if PROMPET_PATH.is_file():
        return PROMPET_PATH.read_text(encoding="utf-8")
    else:
        safe_print(f"[ERREUR] Impossible de trouver le fichier de prompt: {PROMPET_PATH}")
        sys.exit(1)

PROMPT = load_prompt()

# ==== UTILS ====
def extract_json_from_response(text: str):
    """Extrait le bloc JSON de la réponse markdown de Gemini, qu'il y ait les balises ou non."""
    text = text.strip()
    
    # 1. Essayer de trouver un bloc markdown
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
            
    # 2. Chercher le JSON directement (commence par { ou [)
    try:
        # Trouve le premier '[' ou '{' et le dernier ']' ou '}'
        start_idx = -1
        end_idx = -1
        for i, char in enumerate(text):
            if char in ('[', '{'):
                start_idx = i
                break
        for i in range(len(text) - 1, -1, -1):
            if text[i] in (']', '}'):
                end_idx = i
                break
                
        if start_idx != -1 and end_idx != -1 and end_idx >= start_idx:
            json_str = text[start_idx:end_idx+1]
            return json.loads(json_str)
    except json.JSONDecodeError:
        pass
        
    return None

def page_number(path: Path):
    match = re.search(r"page_(\d+)", path.name)
    return int(match.group(1)) if match else 0

# ==== PROCESSING ====
def process_table(family: str, ds: str, table_file: Path, target_pool_state: dict):
    table_name = table_file.name
    num_match = re.search(r"_table_(\d+)\.json", table_name)
    if not num_match:
        return
    num = num_match.group(1)

    # Vérification fichier source
    src_json_path = RAG_SELECTIVE_DIR / family / ds / table_name
    if not src_json_path.is_file():
        safe_print(f"  -> [WARN] Source JSON introuvable: {src_json_path}")
        return

    # Préparer la sortie
    out_dir = OUT_DIR / family / ds
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / table_name

    # Si déjà traité, on skip
    if out_path.is_file():
        safe_print(f"  -> [SKIP] Déjà corrigé: {out_path}")
        return

    # Récupérer images et PDFs
    img_dir = CAPT_DIR / family / ds / f"tableau_{num}"
    images = sorted(img_dir.glob("page_*.png"), key=page_number) if img_dir.is_dir() else []
    pdf_pages = sorted(img_dir.glob("page_*.pdf"), key=page_number) if img_dir.is_dir() else []

    if not images:
        safe_print(f"  -> [WARN] Pas d'images pour {family}/{ds}/{table_name}")
        return

    # Construire le payload
    contents = [PROMPT]
    
    # JSON source
    try:
        source_data = json.loads(src_json_path.read_text(encoding="utf-8"))
        contents.append(f"[JSON]\n{json.dumps(source_data, ensure_ascii=False, indent=2)}")
    except Exception as e:
        safe_print(f"  -> [ERREUR] Lecture source JSON: {e}")
        return

    contents.append("[IMAGE(S)]")
    for img in images:
        contents.append(Image.open(img))

    for pdf_page in pdf_pages:
        try:
            pdf_bytes = pdf_page.read_bytes()
            contents.append(types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"))
        except Exception:
            pass

    # Requête API
    pool_size    = len(API_MANAGER._pools.get(target_pool_state.get("pool", ""), []))
    max_attempts = max(pool_size, 5)
    
    attempts_used = 0
    
    while attempts_used < max_attempts:
        api_key, k_idx, pool_name = API_MANAGER.get_key_from_pool(target_pool_state)
        client = genai.Client(api_key=api_key)
        
        safe_print(f"  -> [ENVOI] {ds}/{table_name} (Clé N°{k_idx+1}, Pool:{pool_name}) - Tentative {attempts_used+1}/{max_attempts}")
        try:
            time.sleep(10)  # Anti-DDoS
            
            response = client.models.generate_content(
                model=MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=0.1
                )
            )
            
            # Vérifier si Gemini a répondu "✅ OK"
            raw_text = response.text.strip()
            if "✅ OK" in raw_text and not raw_text.startswith("```"):
                safe_print(f"  -> [SUCCES] {ds}/{table_name}: Gemini valide sans modification (OK)")
                out_path.write_text(json.dumps(source_data, ensure_ascii=False, indent=2), encoding="utf-8")
                with stats_lock:
                    stats["success"] += 1
                return

            # Extraire le JSON de la réponse
            corrected_json = extract_json_from_response(raw_text)
            
            if corrected_json:
                out_path.write_text(json.dumps(corrected_json, ensure_ascii=False, indent=2), encoding="utf-8")
                safe_print(f"  -> [SUCCES] {ds}/{table_name}: Correction sauvegardée.")
                with stats_lock:
                    stats["success"] += 1
                return
            else:
                safe_print(f"  -> [ERREUR] {ds}/{table_name}: Impossible d'extraire un JSON valide de la réponse.")
                with open(out_dir / f"error_{table_name}.txt", "w", encoding="utf-8") as err_f:
                    err_f.write(raw_text)
                # Réponse invalide → on compte quand même comme tentative
                attempts_used += 1

        except Exception as e:
            err_str = str(e)
            # ── Clé invalide (401) → bloquer 24h, NE PAS compter comme tentative ──
            if "401" in err_str or "UNAUTHENTICATED" in err_str or "ACCOUNT_STATE_INVALID" in err_str:
                safe_print(f"  -> [CLE INVALIDE] Clé N°{k_idx+1} désactivée/invalide — bloquée 24h.")
                API_MANAGER.report_exhausted(k_idx, "401 UNAUTHENTICATED")
                # Pas d'incrément de attempts_used : on va juste récupérer une autre clé
            # ── Quota / surcharge (429/503) → compter comme tentative ──
            elif "429" in err_str or "503" in err_str or "Quota" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                safe_print(f"  -> [ERREUR API] Quota/Surcharge pour la clé {k_idx+1}.")
                API_MANAGER.report_rate_limit(k_idx)
                attempts_used += 1
            else:
                safe_print(f"  -> [ERREUR API] {e}")
                attempts_used += 1

    with stats_lock:
        stats["failed"] += 1
    safe_print(f"  -> [ECHEC FINAL] {ds}/{table_name}: abandonné après {attempts_used} tentatives quota.")

def main():
    parser = argparse.ArgumentParser(description="Script de correction manuelle automatisée via Gemini")
    parser.add_argument('--workers', type=int, default=4, help='Nombre de workers parallèles')
    parser.add_argument('--family', type=str, help='Filtrer par famille (ex: C0, C5)')
    parser.add_argument('--pdf', type=str, help='Filtrer par datasheet (ex: stm32c011d6)')
    args = parser.parse_args()

    if not API_MANAGER:
        safe_print("Pas de clé API configurée.")
        sys.exit(1)

    # Trouver tous les JSONs nécessitant une revue
    tasks = []
    
    safe_print("Analyse du dossier Correction/ pour trouver les tables MANUAL_REVIEW_NEEDED...")
    if not CORRECTION_DIR.is_dir():
        safe_print(f"Dossier introuvable: {CORRECTION_DIR}")
        sys.exit(1)

    # Grouper les tâches par datasheet
    datasheet_tasks = {}
    
    for family_dir in CORRECTION_DIR.iterdir():
        if not family_dir.is_dir(): continue
        family = family_dir.name
        
        if args.family and family.lower() != args.family.lower():
            continue
        
        for ds_dir in family_dir.iterdir():
            if not ds_dir.is_dir(): continue
            ds = ds_dir.name
            
            if args.pdf and ds.lower() != args.pdf.lower().replace(".pdf", ""):
                continue
            
            tables = []
            for jpath in ds_dir.glob("*.json"):
                table_name = jpath.name
                out_path = OUT_DIR / family / ds / table_name
                
                # Si déjà corrigé, on l'affiche et on ignore
                if out_path.is_file():
                    safe_print(f"  -> {table_name} Déjà traité (ignoré)")
                    continue
                    
                try:
                    with open(jpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if data.get("status") == "MANUAL_REVIEW_NEEDED":
                            tables.append(jpath)
                except Exception:
                    pass
            if tables:
                if family not in datasheet_tasks:
                    datasheet_tasks[family] = {}
                datasheet_tasks[family][ds] = tables

    total_tables = sum(len(ts) for fams in datasheet_tasks.values() for ts in fams.values())
    safe_print(f"{total_tables} tables trouvées nécessitant une revue manuelle.")

    if total_tables == 0:
        sys.exit(0)

    pool_names = API_MANAGER.get_pool_names()
    used_pools_in_cycle = set()
    current_pool_idx = 0

    for family, ds_dict in datasheet_tasks.items():
        for target_ds_idx, (ds, tables_to_process) in enumerate(ds_dict.items()):
            safe_print(f"\n--- Traitement du Datasheet: {ds} ({len(tables_to_process)} tables) ---")
            
            # Asign pool
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
            safe_print(f"  -> [POOL] Utilisation du pool {selected_pool} pour {ds}")
            
            # Execution par batches stricts
            batch_size = args.workers
            batches = [tables_to_process[i:i + batch_size] for i in range(0, len(tables_to_process), batch_size)]
            
            for batch_idx, batch in enumerate(batches):
                safe_print(f"\n  -> [BATCH {batch_idx+1}/{len(batches)}] Lancement de {len(batch)} table(s) en parallèle...")
                with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
                    futures = []
                    for i, t in enumerate(batch):
                        if i > 0:
                            time.sleep(1)
                        futures.append(executor.submit(process_table, family, ds, t, target_pool_state))
                    
                    done, not_done = concurrent.futures.wait(futures, return_when=concurrent.futures.ALL_COMPLETED)
                    
                    for future in done:
                        try:
                            future.result()
                        except Exception as exc:
                            if "ALL_POOLS_BLOCKED" in str(exc) or "POOL_BLOCKED" in str(exc):
                                safe_print(f"\n[URGENCE] {exc}")
                                safe_print(f"=> ARRET DU SCRIPT pour le datasheet {ds}.")
                                sys.exit(1)
                            safe_print(f"Erreur fatale inattendue sur un thread : {exc}")
                
                if batch_idx < len(batches) - 1:
                    safe_print(f"  -> [WAIT] Batch {batch_idx+1} terminé. Pause de 30s avant le prochain batch...")
                    time.sleep(30)
            
            # Fin datasheet
            if target_ds_idx < len(ds_dict) - 1:
                safe_print(f"\n[WAIT] Datasheet {ds} termine. Pause de 120s (2min)...")
                time.sleep(120)
            
            current_pool_idx = (current_pool_idx + 1) % len(pool_names)

    API_MANAGER.print_summary()
    safe_print("\n=== TRAITEMENT TERMINÉ ===")
    safe_print(f"Succès: {stats['success']} | Échecs: {stats['failed']}")

if __name__ == "__main__":
    main()
