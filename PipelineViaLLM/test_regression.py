import os
import json
import logging
from pathlib import Path
import subprocess
import shutil

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

ROOT = Path(r"C:\Users\user\Desktop\rag1")
PDF_DIR = ROOT / "Input" / "41"
TEST_INPUT_DIR = ROOT / "Input" / "test_41"
Final_RAG_DIR = ROOT / "Output" / "Json" / "Final_RAG" / "41"
OLD_RAW_DIR = ROOT / "Output" / "Json" / "Raw_Extracted_OLD" / "41"
NEW_RAW_DIR = ROOT / "Output" / "Json" / "Raw_Extracted" / "test_41"

def load_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def count_differences(table1: dict, table2: dict) -> int:
    """Simple row-by-row difference count"""
    rows1 = table1.get("rows", [])
    rows2 = table2.get("rows", [])
    diff = 0
    for r1, r2 in zip(rows1, rows2):
        if r1 != r2:
            diff += 1
    diff += abs(len(rows1) - len(rows2))
    return diff

def main():
    # Pick 4 datasheets to test (which gives ~50-100 tables)
    datasheets_to_test = ["AN4277", "AN5036", "AN5225", "AN5284", "AN5449"]
    
    # 1. Prepare test input directory
    TEST_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    for ds in datasheets_to_test:
        src = PDF_DIR / f"{ds}.pdf"
        dst = TEST_INPUT_DIR / f"{ds}.pdf"
        if src.exists():
            shutil.copy2(src, dst)
            
    # 2. Run the new extraction
    logger.info("Running table_extractor_raw on test datasheets...")
    subprocess.run(
        ["python", "main.py", "--an", "test_41"],
        cwd=str(ROOT / "table_extractor_raw")
    )

    # 3. Compare the tables
    improvements = 0
    regressions = 0
    neutral = 0
    
    improved_details = []
    regressed_details = []
    
    total_tested = 0
    
    for ds in datasheets_to_test:
        old_ds_dir = OLD_RAW_DIR / ds
        new_ds_dir = NEW_RAW_DIR / ds
        final_ds_dir = Final_RAG_DIR / ds
        
        if not new_ds_dir.exists() or not final_ds_dir.exists():
            continue
            
        for new_file in new_ds_dir.glob("*.json"):
            table_id = new_file.stem
            old_file = old_ds_dir / f"{table_id}.json"
            final_file = final_ds_dir / f"{table_id}.json"
            
            if not old_file.exists() or not final_file.exists():
                continue
                
            new_data = load_json(new_file)
            old_data = load_json(old_file)
            final_data = load_json(final_file)
            
            if not new_data or not old_data or not final_data:
                continue
                
            total_tested += 1
            
            # Compare differences against Ground Truth (Final_RAG)
            diff_old = count_differences(old_data, final_data)
            diff_new = count_differences(new_data, final_data)
            
            if diff_new < diff_old:
                improvements += 1
                improved_details.append(f"{ds}/{table_id}: Diff went from {diff_old} to {diff_new}")
            elif diff_new > diff_old:
                regressions += 1
                regressed_details.append(f"{ds}/{table_id}: Diff went from {diff_old} to {diff_new}")
            else:
                neutral += 1

    logger.info("\n=== RAPPORT DE TEST DE NON-REGRESSION ===")
    logger.info(f"Total des tables testees : {total_tested}")
    logger.info(f"Neutres (aucun changement) : {neutral}")
    logger.info(f"Ameliorations (plus proche du Final_RAG) : {improvements}")
    logger.info(f"Regressions (plus eloigne du Final_RAG) : {regressions}")
    
    if improvements > 0:
        logger.info("\n[+] AMELIORATIONS :")
        for detail in improved_details:
            logger.info(f"    - {detail}")
            
    if regressions > 0:
        logger.info("\n[-] REGRESSIONS :")
        for detail in regressed_details:
            logger.info(f"    - {detail}")

if __name__ == "__main__":
    main()
