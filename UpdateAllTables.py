import argparse
import json
import re
from pathlib import Path

def load_json(p: Path) -> dict:
    with p.open('r', encoding='utf-8') as f:
        return json.load(f)

def dump_json(data, p: Path):
    with p.open('w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def update_all_tables(family: str):
    base_dir = Path("correction_Rag") / family
    if not base_dir.is_dir():
        print(f"[ERREUR] Le dossier {base_dir} n'existe pas.")
        return

    # Parcourir chaque datasheet (ex: stm32c011d6)
    for ds_dir in base_dir.iterdir():
        if not ds_dir.is_dir():
            continue

        # Trouver toutes les tables individuelles (ignorer all_tables.json)
        table_files = [
            p for p in ds_dir.iterdir() 
            if p.is_file() and '_table_' in p.name and not p.name.endswith('_all_tables.json')
        ]

        if not table_files:
            continue

        # Trier par numéro de table (ex: extraction de "table_5" -> 5)
        table_files = sorted(
            table_files, 
            key=lambda x: int(re.search(r'_table_(\d+)', x.name).group(1))
        )

        aggregated = []
        for p in table_files:
            try:
                aggregated.append(load_json(p))
            except Exception as e:
                print(f"[ERREUR] Impossible de lire {p.name} : {e}")

        # Trouver le préfixe du datasheet (ex: DS13867_Rev_4 à partir de DS13867_Rev_4_table_1.json)
        sample_name = table_files[0].name
        prefix = sample_name.split('_table_')[0]
        agg_path = ds_dir / f"{prefix}_all_tables.json"

        # Sauvegarder le fichier fusionné
        dump_json(aggregated, agg_path)
        print(f"[SUCCES] {ds_dir.name} : {len(aggregated)} tables fusionnées -> {agg_path.name}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Met à jour le fichier all_tables.json après des corrections manuelles.")
    parser.add_argument('--family', required=True, help="La famille de microcontrôleurs (ex: C0)")
    args = parser.parse_args()

    update_all_tables(args.family)
