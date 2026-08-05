# Plan: Fix Fix C precision — ne PAS rejeter la continuation si "Table N" n'est pas un caption

## Problème
Fix C (recherche "Table N" sur toute la page) est trop large :
- ✅ Table 50→51 : correctement rejeté (page 88 a "Table 51." = caption)
- ❌ Table 65→90 : incorrectement rejeté alors que page 90 n'a QUE la suite de table_65

## Investigation nécessaire
1. Lire page 90 du PDF stm32c532cb pour voir QUELLE "Table" mention est détectée
2. Comprendre pourquoi `_is_continuation_page()` retourne False pour page 90

## Solution proposée

### Affiner Fix C : ne détecter que les TABLE CAPTIONS, pas les mentions quelconques

**Fichier :** `continuation.py` ligne 294

**Changement :** après le `x0` guard, ajouter une vérification que "Table N" est un **caption** :
- Le mot "Table" DOIT être suivi immédiatement d'un nombre sur la même ligne
- La ligne DOIT commencer par "Table" (vérifié par x0)
- Optionnellement, le mot suivant après le nombre doit être "." ou un titre

```python
# Vérification supplémentaire : "Table N" doit être un caption (début de ligne, suivi de nombre)
# Pas une cross-référence au milieu d'une phrase ou un running footer
if not re.match(r'^table\s+\d+', line_text):
    continue  # Pas un caption → ignorer
```

### Alternative : inverser l'approche

Au lieu de chercher "Table N" dans le texte au-dessus du tableau, utiliser la TOC :
- Si la TOC liste une autre table sur la page courante → rejeter
- Si la TOC ne liste pas d'autre table → accepter
- Utiliser le `y` du caption de l'autre table (de la TOC) pour la position de coupe

### Changement : `_is_continuation_page()` prend `all_refs` en paramètre

```python
def _is_continuation_page(page, expected_col_count, current_table_id,
                          pdf_type, base_header=None, all_refs=None):
    # Au lieu de chercher "Table N" dans le texte:
    if all_refs:
        other_on_page = [r for r in all_refs if r.page == page.page_number 
                         and r.table_id != current_table_id]
        if other_on_page:
            # Vérifier si "(continued)" est présent
            page_text = page.extract_text().lower()
            if f"table {_extract_num(current_table_id)} (continued)" not in page_text:
                return False, None, None, None, False
```

## Validation
1. Table 50 (page 87) → page 88 : CONTINUE à être rejeté (table_51 sur page 88 dans TOC)
2. Table 65 (page 89) → page 90 : CONTINUE à être accepté (aucune table sur page 90 dans TOC)
3. Table 65 (page 89) → page 91 : rejeté (table_66 sur page 91 dans TOC)
4. Full C5 scan : 0 régression
