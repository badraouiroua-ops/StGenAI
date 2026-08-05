# Fix: Caption bleed dans la grille pdfplumber (rare)

## Problème
Quand `body_on_next_page` extrait depuis la page de continuation, le titre 
"Table 25. Embedded reset... (continued)" est dans le bbox de la table.
pdfplumber le capture comme 1ère ligne → 14 colonnes fragmentées.

## Fix
1 fichier, 1 bloc à ajouter dans `grid_extractor.py` ligne 1012-1013 (entre `cols_extracted` et `_truncate_at_next_table`)

### Code à ajouter
```python
        # ── [Fix] Suppression des lignes de titre débordant dans la grille ──
        if raw_table and raw_table[0]:
            first_cell = str(raw_table[0][0]).strip()
            m = re.match(r"(?:Table|Tableau)\s+(\d+)[\.:]", first_cell, re.IGNORECASE)
            if m:
                cur_id = int(re.findall(r'\d+', str(ref.table_id))[0])
                if int(m.group(1)) == cur_id:
                    raw_table = raw_table[1:]
                    logger.info(f"{ref.table_id}: removed caption bleed row ({len(raw_table)} rows remaining)")
```

### Test
```bash
python -c "
import sys, json; sys.path.extend([r'table_extractor_raw', r'.'])
from pathlib import Path; from main import process_pdf
pdf = Path(r'DataSHEET/C0/stm32c011d6.pdf')
summary = process_pdf(pdf, 'C0')
# Verifier table_25
t25 = Path('outJason/C0/stm32c011d6/table_25.json')
print(json.dumps(json.loads(t25.read_text()), ensure_ascii=False, indent=2)[:2000])
"
```

### Résultat attendu
- Ligne titre supprimée de la grille
- 14 colonnes conservées (les fragments de titre dans les colonnes internes persistent)
- Autres tables (1-24, 26-77) inchangées
