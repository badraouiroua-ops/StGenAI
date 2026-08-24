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

# Provider Myriamx (ST Bridge) — défaut
try:
    from llm_provider import STBridgeProvider
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from llm_provider import STBridgeProvider

try:
    from ApiManager import ApiManager
except ImportError:
    ApiManager = None

# Force UTF-8
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# ==== CONFIG ====
ROOT = Path(__file__).resolve().parent.parent
CORRECTION_DIR = ROOT / "Output" / "Json" / "LLM_Corrections"
RAG_SELECTIVE_DIR = ROOT / "Output" / "Json" / "Selective_Tables"
CAPT_DIR = ROOT / "Output" / "Images" / "Tables_Screenshots"
OUT_DIR = ROOT / "Output" / "Json" / "Manual_Review"
MORE_DIR = ROOT / "Output" / "Json" / "More_Than_7"
PROMPET_PATH = ROOT / "PipelineViaLLM" / "Prompt_Tables.txt"
MODEL = "gemini-flash-latest"  # legacy

PROVIDER_NAME = "stbridge"
ST_PROVIDER: STBridgeProvider = None
API_MANAGER = None

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
def load_api_keys(env_path=".env", provider="stbridge"):
    keys = []
    if provider in ("stbridge","myriamx","st","openai"):
        val = os.environ.get("ST_AI_BRIDGE_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        if val:
            return [val]
        fallback = "38d5a975-128d-4106-8cc8-394dc1122696"
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "ST_AI_BRIDGE_API_KEY" in line:
                        parts = line.split("=", 1)
                        if len(parts)==2 and parts[1].strip() and "AIza" not in parts[1]:
                            keys.append(parts[1].strip())
        return keys if keys else [fallback]
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        keys.append(parts[1])
    return keys

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
    """Extrait le bloc JSON de la réponse markdown, qu'il y ait les balises ou non. GARDÉ INTACT"""
    text = text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    try:
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
def process_table(family: str, ds: str, table_file: Path, target_pool_state: dict = None):
    table_name = table_file.name
    num_match = re.search(r"_table_(\d+)\.json", table_name)
    if not num_match:
        return
    num = num_match.group(1)

    src_json_path = RAG_SELECTIVE_DIR / family / ds / table_name
    if not src_json_path.is_file():
        safe_print(f"  -> [WARN] Source JSON introuvable: {src_json_path}")
        return

    out_dir = OUT_DIR / family / ds
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / table_name

    if out_path.is_file():
        safe_print(f"  -> [SKIP] Déjà corrigé: {out_path}")
        return

    img_dir = CAPT_DIR / family / ds / f"tableau_{num}"
    images = sorted(img_dir.glob("page_*.png"), key=page_number) if img_dir.is_dir() else []

    if not images:
        safe_print(f"  -> [WARN] Pas d'images pour {family}/{ds}/{table_name}")
        return

    # ── GARDE More Than 7
    if len(images) > 7:
        more_dir = MORE_DIR / family / ds
        more_dir.mkdir(parents=True, exist_ok=True)
        more_file = more_dir / table_name
        if not more_file.exists():
            pages = sorted({page_number(p) for p in images})
            more_payload = {
                "table_id": table_name.replace(".json",""),
                "status": "MORE_THAN_7",
                "reason": f"Skipped MANUAL_REVIEW: {len(images)} images >7",
                "images_count": len(images),
                "merged_pages": pages,
                "logs": ["More Than 7 — MANUAL_REVIEW skip, >7 pages"],
                "images_dir": str(img_dir),
            }
            more_file.write_text(json.dumps(more_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        safe_print(f"  -> [{table_name}] MORE_THAN_7 : {len(images)} images >7 — archivé dans More_Than_7/{family}/{ds} (skip MANUAL_REVIEW)")
        return

    # JSON source string
    try:
        source_data = json.loads(src_json_path.read_text(encoding="utf-8"))
        json_str = json.dumps(source_data, ensure_ascii=False, indent=2)
    except Exception as e:
        safe_print(f"  -> [ERREUR] Lecture source JSON: {e}")
        return

    # ================= STBridge =================
    if PROVIDER_NAME in ("stbridge","myriamx","st","openai"):
        max_attempts = 3
        for attempt in range(max_attempts):
            # STBridge gère son retry interne, mais on garde boucle pour fallback
            safe_print(f"  -> [ENVOI] {ds}/{table_name} (STBridge | Tentative {attempt+1}/{max_attempts})")
            try:
                time.sleep(5 if attempt == 0 else 5)
                result = ST_PROVIDER.generate(
                    prompt=PROMPT,
                    images=images,
                    json_str=json_str,
                    pdf_text=None,
                    responseFormat=None,
                    temperature=None,
                    maxResponseTokens=None,
                    reasoningEffort=None,
                    include_header_zoom=True,
                    max_retries=1,
                )
                raw_text = result["text"] if isinstance(result, dict) else str(result)
                raw_text = raw_text.strip()
                if "✅ OK" in raw_text and not raw_text.strip().startswith("```"):
                    safe_print(f"  -> [SUCCES] {ds}/{table_name}: STBridge valide sans modification (OK)")
                    out_path.write_text(json.dumps(source_data, ensure_ascii=False, indent=2), encoding="utf-8")
                    with stats_lock:
                        stats["success"] += 1
                    return
                corrected_json = extract_json_from_response(raw_text)
                if corrected_json:
                    out_path.write_text(json.dumps(corrected_json, ensure_ascii=False, indent=2), encoding="utf-8")
                    safe_print(f"  -> [SUCCES] {ds}/{table_name}: Correction STBridge sauvegardée.")
                    with stats_lock:
                        stats["success"] += 1
                    return
                else:
                    safe_print(f"  -> [ERREUR] {ds}/{table_name}: Impossible d'extraire JSON valide.")
                    with open(out_dir / f"error_{table_name}.txt", "w", encoding="utf-8") as err_f:
                        err_f.write(raw_text)
                    # retry
                    continue
            except Exception as e:
                err_str = str(e)
                safe_print(f"  -> [ERREUR STBridge] {e}")
                if "Request Timeout" in err_str or "429" in err_str or "503" in err_str:
                    time.sleep(5)
                    continue
                # autre erreur retry
                continue
        with stats_lock:
            stats["failed"] += 1
        safe_print(f"  -> [ECHEC FINAL] {ds}/{table_name}: abandonné après {max_attempts} tentatives.")
        return

    # ================= GEMINI fallback =================
    from google import genai
    from google.genai import types
    pdf_pages = sorted(img_dir.glob("page_*.pdf"), key=page_number) if img_dir.is_dir() else []
    contents = [PROMPT]
    contents.append(f"[JSON]\n{json_str}")
    contents.append("[IMAGE(S)]")
    for img in images:
        contents.append(Image.open(img))
    for pdf_page in pdf_pages:
        try:
            pdf_bytes = pdf_page.read_bytes()
            contents.append(types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"))
        except Exception:
            pass
    pool_size    = len(API_MANAGER._pools.get(target_pool_state.get("pool", ""), [])) if API_MANAGER and target_pool_state else 1
    max_attempts = max(pool_size, 5)
    attempts_used = 0
    while attempts_used < max_attempts:
        if API_MANAGER and target_pool_state:
            api_key, k_idx, pool_name = API_MANAGER.get_key_from_pool(target_pool_state)
        else:
            keys_tmp = load_api_keys(provider="gemini")
            api_key = keys_tmp[0] if keys_tmp else ""
            k_idx, pool_name = 0, "default"
        client = genai.Client(api_key=api_key)
        safe_print(f"  -> [ENVOI] {ds}/{table_name} (Clé N°{k_idx+1}, Pool:{pool_name}) - Tentative {attempts_used+1}/{max_attempts}")
        try:
            time.sleep(10)
            response = client.models.generate_content(
                model=MODEL,
                contents=contents,
                config=types.GenerateContentConfig(temperature=0.1)
            )
            raw_text = response.text.strip()
            if "✅ OK" in raw_text and not raw_text.startswith("```"):
                safe_print(f"  -> [SUCCES] {ds}/{table_name}: Gemini valide sans modification (OK)")
                out_path.write_text(json.dumps(source_data, ensure_ascii=False, indent=2), encoding="utf-8")
                with stats_lock:
                    stats["success"] += 1
                return
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
                attempts_used += 1
        except Exception as e:
            err_str = str(e)
            if "401" in err_str or "UNAUTHENTICATED" in err_str or "ACCOUNT_STATE_INVALID" in err_str:
                safe_print(f"  -> [CLE INVALIDE] Clé N°{k_idx+1} désactivée/invalide — bloquée 24h.")
                if API_MANAGER:
                    API_MANAGER.report_exhausted(k_idx, "401 UNAUTHENTICATED")
            elif "429" in err_str or "503" in err_str or "Quota" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                safe_print(f"  -> [ERREUR API] Quota/Surcharge pour la clé {k_idx+1}.")
                if API_MANAGER:
                    API_MANAGER.report_rate_limit(k_idx)
                attempts_used += 1
            else:
                safe_print(f"  -> [ERREUR API] {e}")
                attempts_used += 1
    with stats_lock:
        stats["failed"] += 1
    safe_print(f"  -> [ECHEC FINAL] {ds}/{table_name}: abandonné après {attempts_used} tentatives quota.")

def main():
    global PROVIDER_NAME, ST_PROVIDER, API_MANAGER
    parser = argparse.ArgumentParser(description="Script de correction manuelle automatisée via STBridge/Gemini")
    parser.add_argument('--workers', type=int, default=None, help='Nombre de workers parallèles (défaut 1 STBridge, 4 Gemini)')
    parser.add_argument('--family', type=str, help='Filtrer par famille (ex: C0, C5)')
    parser.add_argument('--pdf', type=str, help='Filtrer par datasheet (ex: stm32c011d6)')
    parser.add_argument('--provider', type=str, default="stbridge", choices=["stbridge","myriamx","st","gemini"], help='Provider LLM (défaut: stbridge)')
    args = parser.parse_args()

    PROVIDER_NAME = args.provider.lower()
    if args.workers is None:
        args.workers = 1 if PROVIDER_NAME in ("stbridge","myriamx","st") else 4
    if PROVIDER_NAME in ("stbridge","myriamx","st") and args.workers > 2:
        safe_print(f"[WARN] STBridge mono-clé : workers={args.workers} risqué, recommandé 1")

    if PROVIDER_NAME in ("stbridge","myriamx","st"):
        keys = load_api_keys(provider="stbridge")
        ST_PROVIDER = STBridgeProvider(api_key=keys[0] if keys else None)
        safe_print(f"[STBridge] Actif | URL={ST_PROVIDER.url} | Workers={args.workers}")
    else:
        if ApiManager is None:
            safe_print("ApiManager introuvable pour gemini")
            sys.exit(1)
        keys = load_api_keys(provider="gemini")
        API_MANAGER = ApiManager(keys) if keys else None
        if not API_MANAGER:
            safe_print("Pas de clé API Gemini configurée.")
            sys.exit(1)
        safe_print(f"[Gemini] Actif rollback | {len(keys)} clés | Workers={args.workers}")

    safe_print("Analyse du dossier Correction/ pour trouver les tables MANUAL_REVIEW_NEEDED...")
    if not CORRECTION_DIR.is_dir():
        safe_print(f"Dossier introuvable: {CORRECTION_DIR}")
        sys.exit(1)

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

    if PROVIDER_NAME in ("stbridge","myriamx","st"):
        # Pas de pools pour STBridge — traitement direct par datasheet
        for family, ds_dict in datasheet_tasks.items():
            for ds, tables_to_process in ds_dict.items():
                safe_print(f"\n--- Traitement du Datasheet: {ds} ({len(tables_to_process)} tables) ---")
                batch_size = args.workers
                batches = [tables_to_process[i:i + batch_size] for i in range(0, len(tables_to_process), batch_size)]
                for batch_idx, batch in enumerate(batches):
                    safe_print(f"\n  -> [BATCH {batch_idx+1}/{len(batches)}] Lancement de {len(batch)} table(s) en parallèle...")
                    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
                        futures = []
                        for i, t in enumerate(batch):
                            if i > 0:
                                time.sleep(1)
                            futures.append(executor.submit(process_table, family, ds, t, {"pool":"stbridge"}))
                        done, _ = concurrent.futures.wait(futures, return_when=concurrent.futures.ALL_COMPLETED)
                        for future in done:
                            try:
                                future.result()
                            except Exception as exc:
                                safe_print(f"Erreur fatale inattendue : {exc}")
                    if batch_idx < len(batches) - 1:
                        safe_print(f"  -> [WAIT] Batch {batch_idx+1} terminé. Pause 5s...")
                        time.sleep(5)
    else:
        pool_names = API_MANAGER.get_pool_names()
        used_pools_in_cycle = set()
        current_pool_idx = 0
        for family, ds_dict in datasheet_tasks.items():
            for target_ds_idx, (ds, tables_to_process) in enumerate(ds_dict.items()):
                safe_print(f"\n--- Traitement du Datasheet: {ds} ({len(tables_to_process)} tables) ---")
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
                        done, _ = concurrent.futures.wait(futures, return_when=concurrent.futures.ALL_COMPLETED)
                        for future in done:
                            try:
                                future.result()
                            except Exception as exc:
                                if "ALL_POOLS_BLOCKED" in str(exc) or "POOL_BLOCKED" in str(exc):
                                    safe_print(f"\n[URGENCE] {exc}")
                                    sys.exit(1)
                                safe_print(f"Erreur fatale inattendue : {exc}")
                    if batch_idx < len(batches) - 1:
                        safe_print(f"  -> [WAIT] Batch {batch_idx+1} terminé. Pause 5s...")
                        time.sleep(5)
                if target_ds_idx < len(ds_dict) - 1:
                    safe_print(f"\n[WAIT] Datasheet {ds} termine. Pause 5s...")
                    time.sleep(5)
                current_pool_idx = (current_pool_idx + 1) % len(pool_names)

    if API_MANAGER:
        API_MANAGER.print_summary()
    safe_print("\n=== TRAITEMENT TERMINÉ ===")
    safe_print(f"Succès: {stats['success']} | Échecs: {stats['failed']}")

if __name__ == "__main__":
    main()
