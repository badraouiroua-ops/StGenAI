import os
import time
from google import genai

# Lecture manuelle du .env pour éviter l'erreur python-dotenv
env_path = ".env"
if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            if "=" in line and not line.strip().startswith("#"):
                key, val = line.strip().split("=", 1)
                os.environ[key] = val

pools_to_test = {
    "pool_A": "GEMINI_API_KEY1",
    "pool_B": "GEMINI_API_KEY11",
    "pool_C": "GEMINI_API_KEY19",
    "pool_D": "GEMINI_API_KEY29",
    "pool_F": "GEMINI_API_KEY45",
    "pool_G": "GEMINI_API_KEY55",
    "pool_H": "GEMINI_API_KEY65",
    "pool_I": "GEMINI_API_KEY75",
}

print("========================================")
print("TEST DES POOLS avec gemini-flash-latest")
print("========================================")

for pool_name, key_name in pools_to_test.items():
    api_key = os.environ.get(key_name)
    if not api_key:
        print(f"[ERREUR] {pool_name} : Cle {key_name} introuvable dans .env")
        continue

    print(f"\nTest {pool_name} avec {key_name}...")
    
    try:
        client = genai.Client(api_key=api_key)
        # Test exact avec le modèle utilisé dans le pipeline
        response = client.models.generate_content(
            model="gemini-flash-latest",
            contents="bonjour"
        )
        print(f"[OK] SUCCES - L'API repond: {response.text.strip()}")
            
    except Exception as e:
        error_msg = str(e)
        if "401" in error_msg or "UNAUTHENTICATED" in error_msg:
            print(f"[ERREUR] NON AUTORISE (401) - La cle a expire ou est invalide !")
            print(f"   Detail : {error_msg}")
        elif "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
            print(f"[WARNING] RATE LIMIT (429) - Trop de requetes sur ce compte.")
        else:
            print(f"[ERREUR] Inconnue : {error_msg}")

    print("Pause de 5 secondes...")
    time.sleep(5)

print("\n========================================")
print("TEST TERMINE")
print("========================================")
