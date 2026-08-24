import sys
import os
import re
import json
import time
import argparse
import threading
import concurrent.futures
from pathlib import Path

# Force UTF-8 pour la console Windows afin d'éviter les crashs sur des caractères spéciaux (ex: μ)
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

import pypdf
import pdfplumber
from PIL import Image, ImageEnhance

# Provider Myriamx (ST Bridge) — défaut, Gemini en rollback
try:
    from llm_provider import STBridgeProvider
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from llm_provider import STBridgeProvider

try:
    from ApiManager import ApiManager
except ImportError:
    ApiManager = None

# === Import de la détection de Type de PDF (depuis le pipeline existant) ===
current_dir = Path(__file__).resolve().parent
repo_root = current_dir.parent
sys.path.append(str(repo_root / "table_extractor_raw"))
try:
    from table_extractor_raw.main import detect_pdf_type
except ImportError:
    def detect_pdf_type(pdf_path): return 1

PROVIDER_NAME = "stbridge"
ST_PROVIDER: STBridgeProvider = None
API_MANAGER = None

# =====================================================================
# === CONFIGURATION & PROMPT ==========================================
# =====================================================================
MODEL = "gemini-flash-latest"  # legacy

# Prompt strict demandant une sortie JSON pure — GARDÉ INTACT
FIGURE_PROMPT = """Tu es un expert en microcontrôleurs STM32 et en lecture de datasheets.
Tu reçois une image très haute résolution et le fichier PDF correspondant d'une page de datasheet STM32 contenant des schémas de Pinout / Ballout.
Ton rôle est d'extraire de manière exhaustive et parfaite chaque figure présente sur la page, et de formater le résultat en JSON.

ATTENTION : Une seule page peut contenir PLUSIEURS figures (par exemple un package LQFP32 et un UFQFPN32). 
Tu dois retourner une LISTE d'objets JSON, un objet pour chaque figure trouvée sur la page.

## INSTRUCTIONS :
1. Repère toutes les figures de Pinout ou Ballout.
2. SPÉCIFIQUE AUX PINOUTS (LQFP, TSSOP...) :
   - `pins` : La liste exhaustive des pins visibles. Pour chaque pin, donne le `pin_number`, le `pin_name` (exactement tel qu'écrit), et déduis le `type` (Power, I/O, Ground, etc.) si possible, sinon "".
3. SPÉCIFIQUE AUX BALLOUTS (WLCSP, BGA...) : Si l'image montre une grille 2D (ballout), tu DOIS obligatoirement créer un objet `grid_layout` (ET NE PAS CRÉER DE LISTE `pins`, PAS DE CHAMP `matrix`).
   - `grid_layout.balls` : Liste EXHAUSTIVE de toutes les billes présentes. Chaque entrée doit contenir `"ball"` (coordonnée ex: "A2"), `"pin_name"` (le nom exact), et `"type"` (Power, I/O, Ground, etc.).
4. Extrait également pour toutes les figures : `package_type`, `pin_count`, `view`, `notes`.
5. Crée un champ `text_helper` qui résume cette figure. Si c'est un ballout, mentionne les billes Power et Ground et les coordonnées importantes.
6. Ne rate AUCUNE figure de la page.

## FORMAT DE SORTIE STRICTEMENT ATTENDU :
Retourne UNIQUEMENT une liste JSON valide, sans aucun texte autour, ni Markdown (pas de ```json ... ```).

[
  {
    "figure_number": "4",
    "title": "Figure 4. STM32C011DxY WLCSP12 ballout",
    "section": "Pinouts and pin description",
    "section_title": "Pinouts and pin description",
    "text_helper": "Ballout for WLCSP12 package (12 balls). Power: VDD/VDDA at C4. Ground: VSS/VSSA at E4.",
    "figure_content": {
      "package_type": "WLCSP12",
      "pin_count": 12,
      "view": "Top view",
      "grid_layout": {
        "balls": [
          {"ball": "A2", "pin_name": "PB6",             "type": "I/O"},
          {"ball": "A4", "pin_name": "PC15-OSCX_OUT",   "type": "I/O"},
          {"ball": "B1", "pin_name": "PA13",            "type": "I/O"},
          {"ball": "B3", "pin_name": "PC14-OSCX_IN",    "type": "I/O"},
          {"ball": "C2", "pin_name": "PA14-BOOT0",      "type": "I/O"},
          {"ball": "C4", "pin_name": "VDD/VDDA",        "type": "Power"},
          {"ball": "D1", "pin_name": "PA11[PA9]/PA8",   "type": "I/O"},
          {"ball": "D3", "pin_name": "PB7",             "type": "I/O"},
          {"ball": "E2", "pin_name": "PA12[PA10]/PA7",  "type": "I/O"},
          {"ball": "E4", "pin_name": "VSS/VSSA",        "type": "Ground"},
          {"ball": "F1", "pin_name": "PA3/PA4/PA5/PA6", "type": "I/O"},
          {"ball": "F3", "pin_name": "PF2-NRST/PA0/PA1/PA2", "type": "I/O"}
        ]
      },
      "notes": []
    }
  }
]
"""

# =====================================================================
# === CHARGEMENT DES CLES API & LOGGING ===============================
# =====================================================================
def load_api_keys(env_path=".env", provider="stbridge"):
    keys = []
    if provider in ("stbridge", "myriamx", "st", "openai"):
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
                        if len(parts) == 2 and parts[1].strip() and "AIza" not in parts[1]:
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

API_KEYS = load_api_keys(provider="stbridge")

# Verrou pour éviter que les prints des différents workers ne se chevauchent
print_lock = threading.Lock()
def safe_print(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs)

# =====================================================================
# === ETAPE 1 & 2 : FONCTIONS D'ANALYSE PDF ===========================
# =====================================================================

def extract_metadata_from_pdf(pdf_path: str):
    ds_num = "DS_UNKNOWN"
    rev = "Rev_UNKNOWN"
    try:
        with pdfplumber.open(pdf_path) as pdf:
            first_page_text = pdf.pages[0].extract_text()
            match_ds = re.search(r'(DS\d+)', first_page_text)
            if match_ds:
                ds_num = match_ds.group(1)
            match_rev = re.search(r'Rev\s+(\d+)', first_page_text, re.IGNORECASE)
            if match_rev:
                rev = f"Rev {match_rev.group(1)}"
    except Exception:
        pass
    return ds_num, rev

def get_all_bookmarks(pdf_path: str) -> list[tuple[int, str]]:
    reader = pypdf.PdfReader(pdf_path)
    entries = []
    def _walk(items):
        for item in items:
            if isinstance(item, list):
                _walk(item)
            else:
                title = (item.get("/Title", "") or "").strip()
                if not title: return
                if item.get("/Page") is None: return
                try:
                    page_num = reader.get_destination_page_number(item) + 1
                    entries.append((page_num, title))
                except Exception:
                    return
    try:
        _walk(reader.outline or [])
    except Exception:
        pass
    return sorted(entries, key=lambda x: x[0])

def detect_pinout_figures(pdf_path: str) -> dict[int, int]:
    pdf_type = detect_pdf_type(pdf_path)
    bookmarks = get_all_bookmarks(pdf_path)
    figure_regex = re.compile(r'(?i)Figure\s+([2-9]|1[0-9]|20)\b.*?(pinout|ballout)')
    pinout_figures = []
    for page_num, title in bookmarks:
        if figure_regex.search(title):
            pinout_figures.append((page_num, title))
    if not pinout_figures:
        if pdf_type == 2:
            with pdfplumber.open(pdf_path) as pdf:
                pages = pdf.pages[-20:]
                toc_regex = re.compile(r'(?i)(Figure\s+([2-9]|1[0-9]|20)\b.*?(?:pinout|ballout).*?)\s*\.\s*(?:\.\s*)*(\d+)')
                for p in pages:
                    text = p.extract_text() or ""
                    for match in toc_regex.finditer(text):
                        title = match.group(1).strip()
                        page_str = match.group(3)
                        pinout_figures.append((int(page_str), title))
    if not pinout_figures:
        return {}
    page_figure_count: dict[int, int] = {}
    for page_num, _ in pinout_figures:
        page_figure_count[page_num] = page_figure_count.get(page_num, 0) + 1
    return page_figure_count


# =====================================================================
# === ETAPE 4 : APPEL LLM (STBridge par défaut, Gemini fallback) =======
# =====================================================================
def call_gemini_extraction(img_path: Path, pdf_page_path: Path, ds_num: str, rev: str, datasheet_name: str, family: str, page_num: int, expected_figures: int = 1):
    """
    Wrapper historique conservé pour compatibilité. Dispatche vers STBridge ou Gemini.
    Supprime l'envoi PDF binaire (types.Part) pour STBridge — images PNG 600 DPI suffisent.
    """
    if PROVIDER_NAME in ("stbridge", "myriamx", "st", "openai"):
        return call_stbridge_extraction(img_path, ds_num, rev, datasheet_name, family, page_num, expected_figures)
    # fallback Gemini
    return call_gemini_legacy(img_path, pdf_page_path, ds_num, rev, datasheet_name, family, page_num, expected_figures)


def call_stbridge_extraction(img_path: Path, ds_num: str, rev: str, datasheet_name: str, family: str, page_num: int, expected_figures: int = 1):
    output_dir = Path("Output") / "Json" / "Final_Figures" / family / datasheet_name
    output_dir.mkdir(parents=True, exist_ok=True)
    existing_count = 0
    for jpath in output_dir.glob("*.json"):
        try:
            with open(jpath, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("page") == page_num:
                    existing_count += 1
        except Exception:
            pass
    if existing_count >= expected_figures:
        safe_print(f"  -> [SKIP] Page {page_num} complète ({existing_count}/{expected_figures} figures en cache)")
        return

    max_retries = 3
    for attempt in range(max_retries):
        time.sleep(1.5 if attempt == 0 else 5)
        safe_print(f"  -> [STBridge] Envoi Page {page_num} | Tentative {attempt+1}/{max_retries}...")
        try:
            t0 = time.time()
            result = ST_PROVIDER.generate(
                prompt=FIGURE_PROMPT,
                images=[img_path],
                responseFormat=None,
                temperature=None,
                maxResponseTokens=None,
                reasoningEffort=None,
                include_header_zoom=False,
                max_retries=1,
            )
            raw = result["text"] if isinstance(result, dict) else str(result)
            t_el = time.time() - t0
            if raw.strip().startswith("```"):
                if raw.strip().startswith("```json"):
                    raw = raw.strip()[7:]
                if raw.strip().endswith("```"):
                    raw = raw.strip()[:-3]
            figures_data = json.loads(raw.strip())
            if not isinstance(figures_data, list):
                figures_data = [figures_data]
            for i, fig in enumerate(figures_data):
                fig_num = fig.get('figure_number', str(i+1))
                final_json = {
                    "figure_id": f"figure_{fig_num}",
                    "document": f"{ds_num} {rev} - {fig.get('title', '')}",
                    "rev": rev,
                    "figure_number": fig_num,
                    "title": fig.get('title', ''),
                    "page": page_num,
                    "section": fig.get('section', ''),
                    "section_title": fig.get('section_title', ''),
                    "semantic_type": "pinout",
                    "tags": fig.get('tags', []),
                    "url": f"https://www.st.com/resource/en/datasheet/{datasheet_name}.pdf#page={page_num}",
                    "url_pdf": f"https://www.st.com/resource/en/datasheet/{datasheet_name}.pdf",
                    "text_helper": fig.get('text_helper', ''),
                    "figure_content": fig.get('figure_content', {})
                }
                out_filename = f"{ds_num}_{rev.replace(' ', '_')}_figure_{fig_num}.json"
                out_path = output_dir / out_filename
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(final_json, f, indent=2, ensure_ascii=False)
                safe_print(f"  -> [SUCCESS] Enregistré {out_filename} [{t_el:.1f}s]")
            break
        except Exception as e:
            safe_print(f"  -> [ERREUR STBridge] {e}")
            if attempt == max_retries - 1:
                safe_print(f"  -> [ABANDON] Page {page_num} ignorée après {max_retries} échecs.")


def call_gemini_legacy(img_path: Path, pdf_page_path: Path, ds_num: str, rev: str, datasheet_name: str, family: str, page_num: int, expected_figures: int = 1):
    from google import genai
    from google.genai import types
    output_dir = Path("Output") / "Json" / "Final_Figures" / family / datasheet_name
    output_dir.mkdir(parents=True, exist_ok=True)
    existing_count = 0
    for jpath in output_dir.glob("*.json"):
        try:
            with open(jpath, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("page") == page_num:
                    existing_count += 1
        except Exception:
            pass
    if existing_count >= expected_figures:
        safe_print(f"  -> [SKIP] Page {page_num} complète ({existing_count}/{expected_figures} figures en cache)")
        return
    max_retries = 3
    for attempt in range(max_retries):
        if not API_MANAGER:
            safe_print("Pas de clé API Gemini configurée.")
            return
        api_key, k_idx, pool_name = API_MANAGER.get_key()
        time.sleep(1.5)
        client = genai.Client(api_key=api_key)
        safe_print(f"  -> [GEMINI] Envoi Page {page_num} | Clé N°{k_idx+1} (Pool:{pool_name}) | Tentative {attempt+1}/{max_retries}...")
        try:
            # Reconstituer contents legacy avec PDF part
            contents = [FIGURE_PROMPT, "Image de la page :", Image.open(img_path)]
            try:
                pdf_bytes = pdf_page_path.read_bytes()
                contents.append(types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"))
            except Exception as e:
                safe_print(f"  -> [WARN] Impossible de lire le PDF {pdf_page_path.name}: {e}")
            response = client.models.generate_content(
                model=MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=0,
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(thinking_budget=4000)
                )
            )
            raw = response.text
            if raw.startswith("```json"):
                raw = raw[7:]
            if raw.endswith("```"):
                raw = raw[:-3]
            figures_data = json.loads(raw.strip())
            if not isinstance(figures_data, list):
                figures_data = [figures_data]
            for i, fig in enumerate(figures_data):
                fig_num = fig.get('figure_number', str(i+1))
                final_json = {
                    "figure_id": f"figure_{fig_num}",
                    "document": f"{ds_num} {rev} - {fig.get('title', '')}",
                    "rev": rev,
                    "figure_number": fig_num,
                    "title": fig.get('title', ''),
                    "page": page_num,
                    "section": fig.get('section', ''),
                    "section_title": fig.get('section_title', ''),
                    "semantic_type": "pinout",
                    "tags": fig.get('tags', []),
                    "url": f"https://www.st.com/resource/en/datasheet/{datasheet_name}.pdf#page={page_num}",
                    "url_pdf": f"https://www.st.com/resource/en/datasheet/{datasheet_name}.pdf",
                    "text_helper": fig.get('text_helper', ''),
                    "figure_content": fig.get('figure_content', {})
                }
                out_filename = f"{ds_num}_{rev.replace(' ', '_')}_figure_{fig_num}.json"
                out_path = output_dir / out_filename
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(final_json, f, indent=2, ensure_ascii=False)
                safe_print(f"  -> [SUCCESS] Enregistré {out_filename}")
            break
        except Exception as e:
            safe_print(f"  -> [ERREUR GEMINI] {e}")
            if API_MANAGER:
                API_MANAGER.report_rate_limit(k_idx)
            if attempt == max_retries - 1:
                safe_print(f"  -> [ABANDON] Page {page_num} ignorée après {max_retries} échecs.")


# =====================================================================
# === PROCESSUS PRINCIPAL (ORCHESTRATEUR) =============================
# =====================================================================
def process_pdf(pdf_path: Path):
    safe_print(f"\n=== Traitement de {pdf_path.name} ===")
    try:
        ds_num, rev = extract_metadata_from_pdf(str(pdf_path))
        page_figure_map = detect_pinout_figures(str(pdf_path))
        if not page_figure_map:
            safe_print(f"Aucune figure trouvée pour {pdf_path.name}.")
            return
        safe_print(f"  -> Pages détectées : { {p: f'{c} figure(s)' for p, c in page_figure_map.items()} }")
        datasheet_name = pdf_path.stem
        family = pdf_path.parent.name
        output_dir_fig = Path("Output") / "Images" / "Figures_Screenshots" / family / datasheet_name
        output_dir_fig.mkdir(parents=True, exist_ok=True)
        reader = pypdf.PdfReader(str(pdf_path))
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page_num, expected_count in page_figure_map.items():
                img_path = output_dir_fig / f"page_{page_num}.png"
                pdf_out_path = output_dir_fig / f"page_{page_num}.pdf"
                if not img_path.exists() or not pdf_out_path.exists():
                    safe_print(f"  -> Capture de la page {page_num}...")
                    if 0 <= page_num - 1 < len(pdf.pages):
                        page_img = pdf.pages[page_num - 1]
                        img_obj = page_img.to_image(resolution=600)
                        pil_img = img_obj.original
                        enhancer_contrast = ImageEnhance.Contrast(pil_img)
                        pil_img = enhancer_contrast.enhance(1.8)
                        enhancer_sharpness = ImageEnhance.Sharpness(pil_img)
                        pil_img = enhancer_sharpness.enhance(2.5)
                        pil_img.save(str(img_path))
                        writer = pypdf.PdfWriter()
                        writer.add_page(reader.pages[page_num - 1])
                        with open(pdf_out_path, "wb") as f_out:
                            writer.write(f_out)
                else:
                    safe_print(f"  -> [CACHE] Capture page {page_num} existe déjà. Saut de l'étape 3.")
                call_gemini_extraction(img_path, pdf_out_path, ds_num, rev, datasheet_name, family, page_num, expected_figures=expected_count)
    except Exception as e:
        safe_print(f"Erreur globale lors du traitement de {pdf_path.name}: {e}")

# =====================================================================
# === POINT D'ENTREE (CLI) ============================================
# =====================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extraction des figures Pinout (Étapes 1 à 4)")
    parser.add_argument("--pdf", type=Path, help="Chemin vers un fichier PDF spécifique")
    parser.add_argument("--family", type=str, help="Famille de datasheets à traiter (ex: C0, C5)")
    parser.add_argument("--an", type=str, help="Dossier Application Notes à traiter (ex: 41) → Input/41/")
    parser.add_argument("--workers", type=int, default=1, help="Nombre de processus parallèles (défaut: 1, mono-clé STBridge)")
    parser.add_argument("--provider", type=str, default="stbridge", choices=["stbridge","myriamx","st","gemini"], help="Provider LLM (défaut: stbridge)")
    args = parser.parse_args()

    PROVIDER_NAME = args.provider.lower()
    if PROVIDER_NAME in ("stbridge","myriamx","st"):
        keys = load_api_keys(provider="stbridge")
        ST_PROVIDER = STBridgeProvider(api_key=keys[0] if keys else None)
        safe_print(f"[STBridge] Provider actif | URL={ST_PROVIDER.url} | Key=...{(keys[0][-6:] if keys and keys[0] else 'none')}")
    else:
        if ApiManager is None:
            safe_print("ApiManager introuvable pour gemini")
            sys.exit(1)
        keys = load_api_keys(provider="gemini")
        API_MANAGER = ApiManager(keys) if keys else None
        safe_print(f"[Gemini] Provider actif (rollback) | {len(keys)} clés")

    pdfs_to_process = []
    if args.pdf:
        if args.pdf.exists():
            pdfs_to_process.append(args.pdf)
        else:
            safe_print(f"Fichier introuvable : {args.pdf}")
            sys.exit(1)
    elif args.family:
        family_dir = Path("Input") / "PDFs" / args.family
        if family_dir.exists() and family_dir.is_dir():
            pdfs_to_process = list(family_dir.glob("*.pdf"))
        else:
            safe_print(f"Dossier de famille introuvable : {family_dir}")
            sys.exit(1)
    elif args.an:
        an_dir = Path("Input") / args.an
        if an_dir.exists() and an_dir.is_dir():
            pdfs_to_process = list(an_dir.glob("*.pdf"))
        else:
            safe_print(f"Dossier AN introuvable : {an_dir}")
            sys.exit(1)
    else:
        safe_print("Veuillez spécifier soit --pdf, --family, soit --an. Exemple: python Figure.py --an 41")
        sys.exit(1)
        
    safe_print(f"Nombre de PDFs à traiter : {len(pdfs_to_process)}")
    if args.workers > 1:
        safe_print(f"Lancement de {args.workers} workers...")
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as executor:
            executor.map(process_pdf, pdfs_to_process)
    else:
        for pdf in pdfs_to_process:
            process_pdf(pdf)
