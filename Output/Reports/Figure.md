# Documentation - Pipeline d'Extraction des Figures (Figure.py)

Ce document décrit le fonctionnement du script `Figure.py` dédié à l'extraction des figures de pinout/ballout depuis les datasheets STM32. Le script exécute un pipeline complet (de bout en bout), allant de la détection intelligente des pages jusqu'à la génération d'un JSON structuré prêt pour le RAG via l'API Gemini.

## 🛠️ Étapes du Pipeline

### 1. Détection du type de document
   - Le script s'appuie sur la logique robuste du projet principal (`table_extractor_raw/main.py` -> `detect_pdf_type`) pour déterminer si le PDF est de Type 1 (Acrobat) ou de Type 2 (Antenna House).

### 2. Repérage des pages de Pinout
   - **Type 1 (Classique)** : Le script lit les marque-pages (Bookmarks / Outline) via `pypdf` pour trouver les titres correspondant aux schémas (ex: "Figure X. LQFP32 pinout").
   - **Type 2 (Antenna House)** : Si l'outline est vide, le script effectue un *fallback* intelligent : il scanne les 20 dernières pages du PDF (où se trouve la Table des Matières) avec une expression régulière spécifique pour déduire les pages exactes des figures.

### 3. Capture et Amélioration (Système de Cache)
   Pour chaque page repérée, le script génère deux fichiers dans `Figure/{Famille}/{Datasheet}/` :
   - **Un fichier PDF unitaire** contenant uniquement la page cible (`page_X.pdf`).
   - **Une image PNG Très Haute Résolution (600 dpi)** (`page_X.png`).
   - **Traitement d'Image (Pillow)** : L'image subit un filtre de contraste (+80%) et de netteté (x2.5) pour éviter toute pixellisation des textes minuscules.
   - **Système de Cache** : Si ces fichiers existent déjà, le script **saute cette étape** et passe directement à l'appel API, permettant un énorme gain de temps lors des relances.

### 4. Extraction Structurée (API Gemini)
   - **Envoi des données** : Le PDF unitaire et le PNG amélioré sont envoyés à Gemini avec un prompt strict.
   - **Multi-figures** : Gemini est capable de repérer et d'extraire plusieurs packages différents (ex: LQFP32 et WLCSP12) figurant sur une même page.
   - **Encapsulation RAG** : Le script lit la 1ère page du datasheet (pour extraire le numéro `DS` et la `Rev`) et encapsule les données de Gemini dans les métadonnées officielles attendues par le RAG.
   - **Structures Avancées (Pinout vs Ballout)** :
     - Pour les boîtiers classiques (LQFP, TSSOP...), le JSON génère une liste `pins` classique (Numéro, Nom, Type).
     - Pour les grilles (WLCSP, BGA...), le JSON ne génère PAS de liste plate, mais produit un objet `grid_layout` contenant les dimensions et une **matrice 2D** (`matrix`) représentant fidèlement la topologie spatiale des billes.
   - **Gestion d'API (ApiManager)** : L'utilisation de `ApiManager.py` garantit la rotation des clés, la gestion des quotas (429/503) et les retrys automatiques.
   - **Sauvegarde** : Les JSON finaux sont séparés par figure et enregistrés dans `correction_Rag_figure/{Famille}/{Datasheet}/`.
   - **Nommage** : Les fichiers ont un nommage strict et significatif, par exemple : `DS13866_Rev_5_figure_4.json`. Le système de cache inspecte le contenu des JSON existants pour éviter les appels API redondants si une page a déjà été traitée.

---

## 💻 Commandes d'Exécution

Le script supporte les arguments en ligne de commande, similaires à ceux du pipeline principal, permettant de traiter un PDF unique ou toute une famille avec le multiprocessing.

### 1. Traiter un PDF spécifique
```bash
python Figure.py --pdf DataSHEET/C0/stm32c011d6.pdf
```

### 2. Traiter toute une famille (Ex: C5)
```bash
python Figure.py --family C5
```

### 3. Traiter toute une famille avec des Workers (Multiprocessing)
Pour maximiser l'usage de l'API et traiter les PDF en parallèle :
```bash
python Figure.py --family C5 --workers 4
```
