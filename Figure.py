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
from google import genai
from google.genai import types

# === Import de la détection de Type de PDF (depuis le pipeline existant) ===
current_dir = Path(__file__).resolve().parent
sys.path.append(str(current_dir / "table_extractor_raw"))
from table_extractor_raw.main import detect_pdf_type

# === Import de l'ApiManager (gestion intelligente des clés Gemini) ===
sys.path.append(str(current_dir))
from ApiManager import ApiManager

# =====================================================================
# === CONFIGURATION GEMINI & PROMPT ===================================
# =====================================================================
MODEL = "gemini-flash-latest"

# Prompt strict demandant une sortie JSON pure
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
def load_api_keys(env_path=".env"):
    """Charge les clés API depuis un fichier .env"""
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

# Initialisation des clés et de l'ApiManager (qui va gérer les pools et les erreurs 429)
API_KEYS = load_api_keys()
if not API_KEYS:
    val = os.environ.get("GEMINI_API_KEY", "")
    API_KEYS = [val] if val else []
API_MANAGER = ApiManager(API_KEYS) if API_KEYS else None

# Verrou pour éviter que les prints des différents workers ne se chevauchent
print_lock = threading.Lock()
def safe_print(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs)

# =====================================================================
# === ETAPE 1 & 2 : FONCTIONS D'ANALYSE PDF ===========================
# =====================================================================

def extract_metadata_from_pdf(pdf_path: str):
    """
    Extrait le numéro de document (DSxxxx) et la révision (Rev X) depuis 
    le texte brut de la toute première page du PDF.
    """
    ds_num = "DS_UNKNOWN"
    rev = "Rev_UNKNOWN"
    try:
        with pdfplumber.open(pdf_path) as pdf:
            first_page_text = pdf.pages[0].extract_text()
            # Cherche DS suivi de chiffres
            match_ds = re.search(r'(DS\d+)', first_page_text)
            if match_ds:
                ds_num = match_ds.group(1)
            # Cherche Rev suivi d'un ou plusieurs chiffres
            match_rev = re.search(r'Rev\s+(\d+)', first_page_text, re.IGNORECASE)
            if match_rev:
                rev = f"Rev {match_rev.group(1)}"
    except Exception:
        pass
    return ds_num, rev

def get_all_bookmarks(pdf_path: str) -> list[tuple[int, str]]:
    """
    Parcourt l'Outline (les signets) du PDF pour extraire tous les titres 
    et leurs numéros de page (0-indexés convertis en 1-indexés).
    """
    reader = pypdf.PdfReader(pdf_path)
    entries = []
    def _walk(items):
        for item in items:
            if isinstance(item, list):
                _walk(item)
            else:
                title = (item.get("/Title", "") or "").strip()
                if not title: continue
                if item.get("/Page") is None: continue
                try:
                    page_num = reader.get_destination_page_number(item) + 1
                    entries.append((page_num, title))
                except Exception:
                    continue
    try:
        _walk(reader.outline or [])
    except Exception:
        pass
    return sorted(entries, key=lambda x: x[0])

def detect_pinout_figures(pdf_path: str) -> dict[int, int]:
    """
    Étape 2 : Identifie toutes les pages contenant des figures de pinout/ballout
    et compte précisément combien de figures sont attendues par page.
    - Type 1 : Recherche dans l'Outline (bookmarks).
    - Type 2 : Si l'Outline échoue, scanne le texte des 20 dernières pages (Table des matières).
    Retourne un dict {page_num: nb_figures_attendues} pour un cache précis à 100%.
    """
    pdf_type = detect_pdf_type(pdf_path)
    bookmarks = get_all_bookmarks(pdf_path)
    # Regex : "Figure" + (chiffre entre 2 et 20) + ... + "pinout" ou "ballout"
    figure_regex = re.compile(r'(?i)Figure\s+([2-9]|1[0-9]|20)\b.*?(pinout|ballout)')
    
    pinout_figures = []
    # 1. Recherche dans les signets (Type 1)
    for page_num, title in bookmarks:
        if figure_regex.search(title):
            pinout_figures.append((page_num, title))
            
    # 2. Fallback pour Type 2 (Antenna House) : Scan de la Table des Matières à la fin du doc
    if not pinout_figures:
        if pdf_type == 2:
            with pdfplumber.open(pdf_path) as pdf:
                pages = pdf.pages[-20:] # On ne scanne que la fin du PDF pour gagner du temps
                # Regex stricte pour extraire le titre de la figure ET son numéro de page dans la TOC
                toc_regex = re.compile(r'(?i)(Figure\s+([2-9]|1[0-9]|20)\b.*?(?:pinout|ballout).*?)\s*\.\s*(?:\.\s*)*(\d+)')
                for p in pages:
                    text = p.extract_text() or ""
                    for match in toc_regex.finditer(text):
                        title = match.group(1).strip()
                        page_str = match.group(3)
                        pinout_figures.append((int(page_str), title))

    if not pinout_figures:
        return {}

    # Construction du dictionnaire {page: nb_figures_attendues sur cette page}
    page_figure_count: dict[int, int] = {}
    for page_num, _ in pinout_figures:
        page_figure_count[page_num] = page_figure_count.get(page_num, 0) + 1

    return page_figure_count


# =====================================================================
# === ETAPE 4 : APPEL API GEMINI ======================================
# =====================================================================
def call_gemini_extraction(img_path: Path, pdf_page_path: Path, ds_num: str, rev: str, datasheet_name: str, family: str, page_num: int, expected_figures: int = 1):
    """
    Envoie l'image et la page PDF unitaire à Gemini pour extraire le JSON de chaque figure.
    - Utilise ApiManager pour les retrys et les quotas.
    - Encapsule le JSON dans les métadonnées officielles attendues par le RAG.
    - expected_figures : nombre de figures attendues sur cette page (déterminé depuis la TOC/bookmarks).
      Le SKIP n'est déclenché que si le nombre de JSONs existants pour cette page >= expected_figures.
    """
    if not API_MANAGER:
        safe_print("Pas de clé API configurée.")
        return

    output_dir = Path("correction_Rag_figure") / family / datasheet_name
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- LOGIQUE DE CACHING INTELLIGENT ---
    # On compte combien de JSON existent déjà pour cette page
    existing_count = 0
    for jpath in output_dir.glob("*.json"):
        try:
            with open(jpath, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("page") == page_num:
                    existing_count += 1
        except Exception:
            pass

    # SKIP seulement si TOUTES les figures attendues sont déjà présentes
    if existing_count >= expected_figures:
        safe_print(f"  -> [SKIP] Page {page_num} complète ({existing_count}/{expected_figures} figures en cache)")
        return

    # Préparation du payload (Prompt + Image + PDF)
    contents = [FIGURE_PROMPT]
    contents.append("Image de la page :")
    contents.append(Image.open(img_path))
    
    try:
        pdf_bytes = pdf_page_path.read_bytes()
        contents.append(types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"))
    except Exception as e:
        safe_print(f"  -> [WARN] Impossible de lire le PDF {pdf_page_path.name}: {e}")

    max_retries = 3
    for attempt in range(max_retries):
        # Récupération d'une clé API valide via l'ApiManager
        api_key, k_idx, pool_name = API_MANAGER.get_key()
        time.sleep(1.5) # Pause anti rate-limit IP
        client = genai.Client(api_key=api_key)
        
        safe_print(f"  -> [GEMINI] Envoi Page {page_num} | Clé N°{k_idx+1} (Pool:{pool_name}) | Tentative {attempt+1}/{max_retries}...")
        
        try:
            # Appel de l'API avec ThinkingConfig (intelligence accrue)
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
            # Nettoyage des balises markdown si Gemini en a mis
            if raw.startswith("```json"):
                raw = raw[7:]
            if raw.endswith("```"):
                raw = raw[:-3]
            
            figures_data = json.loads(raw.strip())
            
            # Au cas où Gemini ne retourne qu'un objet au lieu d'une liste
            if not isinstance(figures_data, list):
                figures_data = [figures_data]

            # Traitement de chaque figure trouvée sur la page
            for i, fig in enumerate(figures_data):
                fig_num = fig.get('figure_number', str(i+1))
                
                # Création du JSON final encapsulé avec les métadonnées officielles RAG
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
                
                # Nommage cohérent (demande utilisateur) sans la page: ex: DS15137_Rev_2_figure_3.json
                out_filename = f"{ds_num}_{rev.replace(' ', '_')}_figure_{fig_num}.json"
                out_path = output_dir / out_filename
                
                # Sauvegarde du JSON
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(final_json, f, indent=2, ensure_ascii=False)
                    
                safe_print(f"  -> [SUCCESS] Enregistré {out_filename}")
            
            # Succès complet pour cette page, on sort de la boucle de retry
            break 
            
        except Exception as e:
            safe_print(f"  -> [ERREUR GEMINI] {e}")
            API_MANAGER.report_rate_limit(k_idx) # Signale l'erreur 429/503 pour bloquer la clé temporairement
            if attempt == max_retries - 1:
                safe_print(f"  -> [ABANDON] Page {page_num} ignorée après {max_retries} échecs.")


# =====================================================================
# === PROCESSUS PRINCIPAL (ORCHESTRATEUR) =============================
# =====================================================================
def process_pdf(pdf_path: Path):
    """
    Fonction principale exécutée pour un datasheet spécifique.
    Coordonne l'extraction des métadonnées, la détection des pages (Étapes 1 & 2),
    la capture d'image (Étape 3) et l'appel à Gemini (Étape 4).
    """
    safe_print(f"\n=== Traitement de {pdf_path.name} ===")
    try:
        # Extraire DS et Rev depuis la 1ère page
        ds_num, rev = extract_metadata_from_pdf(str(pdf_path))

        # Déterminer précisément quelles pages traiter et combien de figures sont attendues par page
        page_figure_map = detect_pinout_figures(str(pdf_path))

        if not page_figure_map:
            safe_print(f"Aucune figure trouvée pour {pdf_path.name}.")
            return

        safe_print(f"  -> Pages détectées : { {p: f'{c} figure(s)' for p, c in page_figure_map.items()} }")

        datasheet_name = pdf_path.stem
        family = pdf_path.parent.name
        output_dir_fig = Path("Figure") / family / datasheet_name
        output_dir_fig.mkdir(parents=True, exist_ok=True)

        reader = pypdf.PdfReader(str(pdf_path))

        with pdfplumber.open(str(pdf_path)) as pdf:
            # Itère directement sur le dictionnaire : on ne traite QUE les pages utiles
            for page_num, expected_count in page_figure_map.items():
                img_path = output_dir_fig / f"page_{page_num}.png"
                pdf_out_path = output_dir_fig / f"page_{page_num}.pdf"
                
                # --- ETAPE 3 : CAPTURE (Skip si le fichier existe déjà) ---
                if not img_path.exists() or not pdf_out_path.exists():
                    safe_print(f"  -> Capture de la page {page_num}...")
                    if 0 <= page_num - 1 < len(pdf.pages):
                        page_img = pdf.pages[page_num - 1]
                        
                        # Création de l'image en 600 dpi pour une netteté parfaite
                        img_obj = page_img.to_image(resolution=600)
                        
                        # Amélioration du contraste et de la netteté avec Pillow
                        pil_img = img_obj.original
                        enhancer_contrast = ImageEnhance.Contrast(pil_img)
                        pil_img = enhancer_contrast.enhance(1.8) # Les noirs sont plus profonds
                        enhancer_sharpness = ImageEnhance.Sharpness(pil_img)
                        pil_img = enhancer_sharpness.enhance(2.5) # Le contour des lettres est plus "crisp"
                        pil_img.save(str(img_path))
                        
                        # Création du fichier PDF unitaire (1 seule page)
                        writer = pypdf.PdfWriter()
                        writer.add_page(reader.pages[page_num - 1])
                        with open(pdf_out_path, "wb") as f_out:
                            writer.write(f_out)
                else:
                    safe_print(f"  -> [CACHE] Capture page {page_num} existe déjà. Saut de l'étape 3.")
                
                # --- ETAPE 4 : EXTRACTION GEMINI (avec le nombre de figures attendues pour un cache précis) ---
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
    parser.add_argument("--workers", type=int, default=1, help="Nombre de processus parallèles (défaut: 1)")
    args = parser.parse_args()
    
    pdfs_to_process = []
    
    # Mode Fichier Unique
    if args.pdf:
        if args.pdf.exists():
            pdfs_to_process.append(args.pdf)
        else:
            safe_print(f"Fichier introuvable : {args.pdf}")
            sys.exit(1)
            
    # Mode Famille entière
    elif args.family:
        family_dir = Path("DataSHEET") / args.family
        if family_dir.exists() and family_dir.is_dir():
            pdfs_to_process = list(family_dir.glob("*.pdf"))
        else:
            safe_print(f"Dossier de famille introuvable : {family_dir}")
            sys.exit(1)
            
    else:
        safe_print("Veuillez spécifier soit --pdf soit --family. Exemple: python Figure.py --pdf DataSHEET/C0/stm32c011d6.pdf")
        sys.exit(1)
        
    safe_print(f"Nombre de PDFs à traiter : {len(pdfs_to_process)}")
    
    # Exécution parallèle ou séquentielle
    if args.workers > 1:
        safe_print(f"Lancement de {args.workers} workers...")
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as executor:
            executor.map(process_pdf, pdfs_to_process)
    else:
        for pdf in pdfs_to_process:
            process_pdf(pdf)
