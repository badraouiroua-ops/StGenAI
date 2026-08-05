"""
ApiManager.py
─────────────
Gestionnaire intelligent de clés API avec :
  - Pools de clés configurables via api_config.json
  - Rotation Round-Robin inter-pools pour éviter les 429
  - Blocage 60s sur rate-limit temporaire (429/503)
  - Blocage 24h sur quota journalier épuisé (RPD)
  - Persistance de l'état entre exécutions via ApiLog/api_state.json
  - Stats complètes par clé : nb_used, nb_errors, tokens_in, tokens_out
"""

import json
import time
import threading
import os
from pathlib import Path

# ─── Chemins ─────────────────────────────────────────────────────────────────
ROOT           = Path(__file__).resolve().parent
CONFIG_PATH    = ROOT / "api_config.json"
API_LOG_DIR    = ROOT / "ApiLog"
STATE_PATH     = API_LOG_DIR / "api_state.json"

# ─── Durées de blocage ────────────────────────────────────────────────────────
BLOCK_1MIN  = 60          # Rate-limit temporaire (429 RPM/TPM)
BLOCK_24H   = 86400       # Quota journalier épuisé (429 RPD / Exhausted)

# ─── Status ───────────────────────────────────────────────────────────────────
STATUS_AVAILABLE  = "AVAILABLE"
STATUS_BLOCKED_1M = "BLOCKED_1M"
STATUS_BLOCKED_24H = "BLOCKED_24H"


class ApiManager:
    """
    Gère la rotation intelligente des clés API par pools.
    Thread-safe grâce à un verrou global.
    """

    def __init__(self, all_keys: list[str]):
        """
        all_keys : liste ordonnée de toutes les clés API (index 1-based dans api_config.json).
        """
        self._lock       = threading.Lock()
        self._all_keys   = all_keys        # index 0 = clé numéro 1

        # ── Charger la config des pools
        self._pools      = self._load_pools()  # dict: pool_name -> list[int] (index 0-based)

        # ── Charger (ou initialiser) l'état persistant
        API_LOG_DIR.mkdir(exist_ok=True)
        self._state      = self._load_state()

        # ── Curseur Round-Robin par pool (index dans la liste des clés du pool)
        self._pool_cursors = {name: 0 for name in self._pools}
        # ── Curseur Round-Robin pour les pools eux-mêmes
        self._pool_names   = list(self._pools.keys())
        self._pool_cursor  = 0

        self._save_state()

    # ─────────────────────────────────────────────────────────────────────────
    # Chargement
    # ─────────────────────────────────────────────────────────────────────────

    def _load_pools(self) -> dict:
        """Lit api_config.json et retourne un dict pool_name -> [idx0, idx1, ...]"""
        if not CONFIG_PATH.exists():
            # Pool unique par défaut = toutes les clés
            return {"pool_default": list(range(len(self._all_keys)))}

        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        pools = {}
        for name, range_def in cfg.get("pools", {}).items():
            start, end = range_def[0] - 1, range_def[1] - 1   # 1-based -> 0-based
            pools[name] = [i for i in range(start, end + 1) if i < len(self._all_keys)]
        return pools

    def _load_state(self) -> dict:
        """Charge api_state.json ou crée une structure vide."""
        if STATE_PATH.exists():
            try:
                return json.loads(STATE_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass

        # Initialiser avec toutes les clés disponibles
        keys_state = {}
        for i in range(len(self._all_keys)):
            keys_state[str(i)] = {
                "key_num"      : i + 1,
                "status"       : STATUS_AVAILABLE,
                "unblock_time" : 0,
                "last_used"    : 0,
                # ── Stats ──
                "nb_used"      : 0,
                "nb_errors"    : 0,
                "tokens_in"    : 0,
                "tokens_out"   : 0
            }
        return {"keys_state": keys_state}

    def _save_state(self):
        """Persist l'état sur disque (appelé sous lock)."""
        STATE_PATH.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Logique de disponibilité
    # ─────────────────────────────────────────────────────────────────────────

    def _unblock_if_ready(self, idx: int):
        """Débloquer automatiquement une clé si le délai est écoulé."""
        entry = self._state["keys_state"][str(idx)]
        if entry["status"] != STATUS_AVAILABLE:
            if time.time() >= entry["unblock_time"]:
                entry["status"]       = STATUS_AVAILABLE
                entry["unblock_time"] = 0

    def _is_available(self, idx: int) -> bool:
        self._unblock_if_ready(idx)
        return self._state["keys_state"][str(idx)]["status"] == STATUS_AVAILABLE

    def _get_next_in_pool(self, pool_name: str) -> int | None:
        """
        Retourne le prochain index de clé disponible dans le pool donné.
        Stratégie : clé avec le last_used le plus ancien parmi les disponibles.
        Retourne None si toutes les clés du pool sont bloquées.
        """
        pool_indices = self._pools[pool_name]
        available    = [i for i in pool_indices if self._is_available(i)]
        if not available:
            return None
        # La clé la moins récemment utilisée en premier
        return min(available, key=lambda i: self._state["keys_state"][str(i)]["last_used"])

    # ─────────────────────────────────────────────────────────────────────────
    # Interface publique
    # ─────────────────────────────────────────────────────────────────────────

    def get_key(self) -> tuple[str, int, str]:
        """
        Retourne (api_key, key_idx_0based, pool_name).
        Fait une rotation inter-pools pour distribuer équitablement.
        Si toutes les clés sont bloquées, attend jusqu'au prochain déblocage.
        """
        with self._lock:
            nb_pools = len(self._pool_names)
            attempts = 0

            while True:
                # Essayer tous les pools en Round-Robin
                for _ in range(nb_pools):
                    pool_name = self._pool_names[self._pool_cursor % nb_pools]
                    self._pool_cursor += 1
                    idx = self._get_next_in_pool(pool_name)
                    if idx is not None:
                        # Mettre à jour last_used
                        self._state["keys_state"][str(idx)]["last_used"] = time.time()
                        self._save_state()
                        return self._all_keys[idx], idx, pool_name

                # Toutes les clés bloquées → calculer le prochain déblocage
                attempts += 1
                next_unblock = min(
                    self._state["keys_state"][str(i)]["unblock_time"]
                    for pool in self._pools.values() for i in pool
                    if self._state["keys_state"][str(i)]["status"] != STATUS_AVAILABLE
                )
                wait = max(1, next_unblock - time.time())
                print(f"  [ApiManager] Toutes les clés bloquées. Attente de {wait:.0f}s avant déblocage...")
                self._save_state()
                # Libérer le lock pendant l'attente pour ne pas bloquer les autres threads
                self._lock.release()
                time.sleep(wait)
                self._lock.acquire()

    def report_success(self, idx: int, tokens_in: int = 0, tokens_out: int = 0):
        """Enregistrer un appel réussi avec les tokens consommés."""
        with self._lock:
            entry = self._state["keys_state"][str(idx)]
            entry["nb_used"]    += 1
            entry["tokens_in"]  += tokens_in
            entry["tokens_out"] += tokens_out
            self._save_state()

    def report_rate_limit(self, idx: int):
        """429 RPM/TPM : bloquer 60 secondes."""
        with self._lock:
            entry = self._state["keys_state"][str(idx)]
            entry["status"]       = STATUS_BLOCKED_1M
            entry["unblock_time"] = time.time() + BLOCK_1MIN
            entry["nb_errors"]   += 1
            self._save_state()
            print(f"  [ApiManager] Clé N°{idx+1} bloquée 60s (rate-limit).")

    def report_exhausted(self, idx: int):
        """Quota journalier épuisé : bloquer 24 heures."""
        with self._lock:
            entry = self._state["keys_state"][str(idx)]
            entry["status"]       = STATUS_BLOCKED_24H
            entry["unblock_time"] = time.time() + BLOCK_24H
            entry["nb_errors"]   += 1
            self._save_state()
            print(f"  [ApiManager] Clé N°{idx+1} bloquée 24h (quota épuisé).")

    def report_permanent_error(self, idx: int):
        """Erreur permanente : bloquer 24h par sécurité."""
        self.report_exhausted(idx)

    def print_summary(self):
        """Affiche un résumé de l'état de toutes les clés."""
        print("\n" + "="*60)
        print("=== RAPPORT DES CLÉS API ===")
        for pool_name, indices in self._pools.items():
            print(f"\n  Pool: {pool_name}")
            for idx in indices:
                e = self._state["keys_state"][str(idx)]
                status = e["status"]
                if status != STATUS_AVAILABLE:
                    remaining = max(0, e["unblock_time"] - time.time())
                    status_str = f"{status} (déblocage dans {remaining:.0f}s)"
                else:
                    status_str = STATUS_AVAILABLE
                print(
                    f"    Clé N°{idx+1:>3} | {status_str:<45} | "
                    f"utilisée {e['nb_used']:>4}x | "
                    f"erreurs {e['nb_errors']:>3} | "
                    f"tokens in={e['tokens_in']:>8} out={e['tokens_out']:>8}"
                )
        print("="*60 + "\n")
