"""
app.py — Point d'entrée principal du pipeline STM32 Table Extractor.

Lance le moteur d'extraction (table_extractor_raw/main.py) en lui passant
les arguments CLI reçus. Ce fichier sert de wrapper léger pour simplifier
l'appel depuis la racine du projet.

Usage:
    python app.py --pdf DataSHEET/F1/stm32f103rc.pdf   (un seul PDF)
    python app.py --family G0                           (toute une famille)
    python app.py --all                                 (tous les 200 PDFs)
"""
import subprocess
import sys
from pathlib import Path


def main():
    backend_script = Path(__file__).parent / "table_extractor_raw" / "main.py"

    if not backend_script.exists():
        print(f"Erreur : le script backend {backend_script} est introuvable.")
        sys.exit(1)

    args = sys.argv[1:]
    if not args:
        print("Usage: python app.py --pdf DataSHEET/C0/stm32c011d6.pdf")
        print("       python app.py --family C0")
        print("       python app.py --all")
        sys.exit(1)

    # Exécuter le backend avec le même interpréteur Python
    cmd = [sys.executable, str(backend_script)] + args
    result = subprocess.run(cmd)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
