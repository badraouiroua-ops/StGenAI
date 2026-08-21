# Protocole de test de masse — Dedup inter-table

## Objectif

Valider que `_deduplicate_table_boundaries` ne supprime que les vrais
doublons inter-tables (N vs N+1/N+2) sans toucher aux données légitimes,
sur les 185 PDFs du corpus.

---

## 1. Test unitaire (1 PDF, 1 famille)

```powershell
python table_extractor_raw/main.py --pdf DataSHEET/C5/stm32c532cb.pdf --workers 1
```

### Vérifications

| Check | Commande |
|-------|----------|
| Tables extraites | `python check_quality.py outJason/C5/stm32c532cb` |
| Rapport run OK | `cat outJason/C5/stm32c532cb/_run_report.json \| python -m json.tool` |
| Logs dedup | Chercher `[dedup] removed` dans la sortie console |

### Audit manuel (C5 ciblé)

```powershell
# Compter les rows avant/après dedup par table
python -c "
import json, os
d = 'outJason/C5/stm32c532cb'
for f in sorted(os.listdir(d)):
    if f.startswith('table_') and f.endswith('.json'):
        t = json.load(open(f'{d}/{f}'))
        print(f'{t[\"table_id\"]:15s} rows={len(t[\"rows\"]):3d}  notes={len(t.get(\"heuristics\",{}).get(\"_notes\",[]))}')
"
```

Vérifier que les tables adjacentes 50/51/52 n'ont pas perdu de données
uniques (ex: notes "frequency", "fall", "rise" dans table_51).

---

## 2. Scan famille complète (C5 — 6 PDFs)

```powershell
python table_extractor_raw/main.py --family C5 --workers 1
```

### Vérifications automatiques

```powershell
# 1. Taux de succès
python check_quality.py

# 2. Stats globales
python aggregate_stats.py

# 3. Rapport debug complet
python table_extractor_raw/generate_debug_report.py
```

**Critères de succès :**
- 6/6 PDFs OK, 0 failed
- 100% high confidence
- Tous les `_run_report.json` ont `tables_extracted == tables_found`

### Audit déduplication

```powershell
# Compter les déduplications famille C5
python -c "
import json, os
total_rows = total_notes = 0
for pdf in os.listdir('outJason/C5'):
    d = f'outJason/C5/{pdf}'
    f = f'{d}/_all_tables.json'
    if not os.path.exists(f): continue
    data = json.load(open(f, encoding='utf-8'))
    for item in data:
        if 'table_id' not in item: continue
        meta = item.get('datasheet_metaData', {})
        total_rows += meta.get('rows_count', 0)
    print(f'{pdf:25s} rows={total_rows}')
"
```

---

## 3. Scan complet (185 PDFs)

```powershell
python table_extractor_raw/main.py --all --workers 8
```

### Métriques de qualité

| Métrique | Seuil |
|----------|-------|
| PDFs OK | ≥ 180 / 185 |
| High confidence | 100 % |
| Failed tables | 0 |
| Tables avec `empty_cell_ratio > 0.30` | 0 |
| Tables avec `warnings` | inchangé vs référence |

### Comparaison avant/après dedup

```powershell
# Extraire les stats dedup du log
python -c "
import re, sys
lines = sys.stdin.read()
matches = re.findall(r'\[dedup\] removed ([\d]+) duplicate', lines)
print(f'Total rows dedup: {sum(int(m) for m in matches)}')
"
```

---

## 4. Rapport de debug détaillé

Le rapport `_debug_report_all.json` contient pour chaque table :
- `warnings`
- `rows_count`
- `heuristics._notes` (présence/absence)
- `datasheet_metaData.rows_count`

Générer et inspecter :

```powershell
python table_extractor_raw/generate_debug_report.py
# Lire le rapport
python -m json.tool outJason/_debug_report_all.json
```

### Vérifications de non-régression

```powershell
# 1. Tables qui avaient des notes avant le dedup en ont encore (celles uniques)
python -c "
import json, os
d = 'outJason/C5/stm32c532cb'
tables = {}
for f in sorted(os.listdir(d)):
    if f.startswith('table_') and f.endswith('.json'):
        t = json.load(open(f'{d}/{f}'))
        notes = t.get('heuristics',{}).get('_notes',[])
        if notes:
            tables[t['table_id']] = notes
for tid, notes in sorted(tables.items()):
    print(f'{tid}: {len(notes)} notes')
"
```

**Résultat attendu :** seules les tables 50 et 51 avaient des notes avant
dedup ; table_51 conserve les siennes (frequency, fall, rise), table_50
perd les siennes (identiques à table_51).

---

## 5. Debug rapide (un seul PDF avec logs détaillés)

```powershell
$env:PYTHONDEVMODE = '1'
python table_extractor_raw/main.py --pdf DataSHEET/C5/stm32c532cb.pdf 2>&1 | Select-String -Pattern 'dedup|duplicate' -Context 2,2
```

---

## 6. Gestion des échecs

Si un PDF échoue :

1. Relancer en séquentiel seul : `--pdf DataSHEET/<famille>/<pdf>.pdf --workers 1`
2. Vérifier `_run_report.json` pour les erreurs exactes
3. Vérifier si l'échec existait AVANT le dedup (baseline)
4. Comparer avec un `git stash` et relance

---

## Résumé d'exécution

```powershell
# Tout-en-un
python table_extractor_raw/main.py --all --workers 8 ; `
python check_quality.py ; `
python table_extractor_raw/generate_debug_report.py ; `
python aggregate_stats.py
```
