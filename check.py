#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
from pathlib import Path

# Liste complète des Application Notes du document
complete_list = [
    "AN1709", "AN2606", "AN2639", "AN2834", "AN3126", "AN3155", "AN3156",
    "AN4013", "AN4230", "AN4277", "AN4286", "AN4566", "AN4655", "AN4750",
    "AN4776", "AN4803", "AN4838", "AN4879", "AN4899", "AN4908", "AN4989",
    "AN5027", "AN5036", "AN5050", "AN5156", "AN5212", "AN5225", "AN5325",
    "AN5342", "AN5348", "AN5405", "AN5537", "AN5543", "AN5612", "AN5647",
    "AN5879", "AN6205", "AN6274", "AN6308", "AN6363", "AN6457"
]

# Dossier à vérifier
folder_path = "Output\\Json\\Raw_Extracted\\41"

# Vérifier que le dossier existe
if not os.path.exists(folder_path):
    print(f"❌ Le dossier '{folder_path}' n'existe pas!")
    exit(1)

# Extraire les numéros AN présents dans le dossier
present_an = set()
for filename in os.listdir(folder_path):
    # Chercher les numéros AN dans les noms de fichiers
    matches = re.findall(r'AN\d{4}', filename)
    for match in matches:
        present_an.add(match)

# Trouver les manquants
missing_an = set(complete_list) - present_an

# Afficher les résultats
print(f"📊 Analyse du dossier '{folder_path}'\n")
print(f"✅ Application Notes trouvées: {len(present_an)}/{len(complete_list)}")
print(f"❌ Manquantes: {len(missing_an)}\n")

if missing_an:
    print("📝 Liste des AN manquantes:")
    print("-" * 40)
    missing_sorted = sorted(missing_an, key=lambda x: int(x[2:]))
    for an in missing_sorted:
        print(f"  • {an}")
    
    print("\n" + "-" * 40)
    print(f"Total manquant: {len(missing_an)} fichiers")
else:
    print("✨ Aucune Application Note manquante! Vous avez tout!")

# Bonus: afficher les présentes aussi
if present_an:
    print(f"\n📌 Vous avez ces AN: {', '.join(sorted(present_an, key=lambda x: int(x[2:])))}")