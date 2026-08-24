"""
llm_provider.py — Abstraction LLM pour StGenAI
Supporte Gemini (google-genai + ApiManager) et ST AI Bridge (Myriamx).

Fournisseur par défaut : stbridge (API Myriamx), gemini gardé pour rollback.
"""
from abc import ABC, abstractmethod
from pathlib import Path
import base64
import hashlib
import io
import json
import os
import random
import time
import urllib3

import requests
import yaml
from PIL import Image, UnidentifiedImageError
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except Exception:
    pass

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
class LLMProvider(ABC):
    @abstractmethod
    def generate(self, prompt: str, images: list[Path] = None, json_str: str = None,
                 pdf_text: str = None, responseFormat: str = None,
                 **overrides) -> dict:
        """
        Retourne dict avec clés au minimum {"text": str, "tokens_in": int, "tokens_out": int}
        "text" = completion brute du LLM
        """
        ...

# ---------------------------------------------------------------------------
# Helpers ST Bridge (reprise exacte Myriamx_datasheet_validatrion/myriamx_prompt.py)
# ---------------------------------------------------------------------------
def _load_st_config(config_path: Path = None):
    cfg = {}
    if config_path is None:
        # chercher PipelineViaLLM/config.yaml puis Myriamx config
        candidates = [
            Path(__file__).parent / "config.yaml",
            Path(__file__).parent.parent / "Myriamx_datasheet_validatrion" / "config.yaml",
            Path(__file__).parent / ".." / "config.yaml",
        ]
        for c in candidates:
            if c.exists():
                config_path = c
                break
    if config_path and Path(config_path).exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            cfg = {}
    return cfg


def generate_token(clientAppName, serviceName, apiKey, timestamp, nonce):
    data_string = f"{clientAppName}_{serviceName}_{apiKey}_{timestamp}_{nonce}"
    return hashlib.sha1(data_string.encode("utf-8")).hexdigest()


def encode_image_base64(path, max_side=1568, quality=92):
    try:
        img = Image.open(path)
    except (UnidentifiedImageError, OSError) as e:
        raise RuntimeError(f"Impossible d'ouvrir l'image {path} : {e}") from e
    w, h = img.size
    if max(w, h) > max_side:
        ratio = max_side / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def crop_header_zoom(path, top_fraction=0.22, max_side=1568, quality=92, tmp_dir=None):
    img = Image.open(path)
    w, h = img.size
    crop_h = int(h * top_fraction)
    header_crop = img.crop((0, 0, w, crop_h))
    ch_w, ch_h = header_crop.size
    if max(ch_w, ch_h) < max_side:
        ratio = max_side / max(ch_w, ch_h)
        header_crop = header_crop.resize((int(ch_w * ratio), int(ch_h * ratio)), Image.LANCZOS)
    if header_crop.mode in ("RGBA", "P"):
        header_crop = header_crop.convert("RGB")
    tmp_dir = Path(tmp_dir) if tmp_dir else Path(path).parent
    out_path = tmp_dir / f"{Path(path).stem}_header_zoom.jpg"
    header_crop.save(out_path, format="JPEG", quality=quality, optimize=True)
    return out_path


def build_multimodal_content(prompt_text, image_paths, include_header_zoom=True, json_text=None, pdf_text=None):
    content = []
    zoom_files_to_cleanup = []
    if image_paths:
        for img_path in image_paths:
            if include_header_zoom:
                try:
                    zoom_path = crop_header_zoom(img_path)
                    zoom_b64 = encode_image_base64(zoom_path)
                    content.append({"type": "image", "content": f"data:image/jpg;base64,{zoom_b64}"})
                    zoom_files_to_cleanup.append(zoom_path)
                except Exception as e:
                    print(f"  [avertissement] zoom en-tête impossible pour {img_path} : {e}")
            b64 = encode_image_base64(img_path)
            content.append({"type": "image", "content": f"data:image/jpg;base64,{b64}"})
    # prompt principal
    content.append({"type": "text", "content": prompt_text})
    # pdf_text fourni comme bloc texte supplémentaire
    if pdf_text:
        content.append({"type": "text", "content": f"Texte de référence positionné:\n{pdf_text}"})
    if json_text:
        content.append({"type": "text", "content": "JSON EXTRAIT À VÉRIFIER (table_content) :\n```json\n" + json_text + "\n```"} )
    for zf in zoom_files_to_cleanup:
        try:
            zf.unlink()
        except OSError:
            pass
    return content


# ---------------------------------------------------------------------------
# ST Bridge Provider
# ---------------------------------------------------------------------------
class STBridgeProvider(LLMProvider):
    def __init__(self, api_key: str = None, config_path: Path = None, url: str = None,
                 clientAppName: str = None, serviceName: str = None):
        cfg = _load_st_config(config_path)
        self.config = cfg
        # Priorité env (.env) > config.yaml > défaut — .env utilise noms CLIENT_APP_NAME, API_KEY, REMOTE_USER, etc.
        env = os.environ
        self.api_key = api_key or env.get("ST_AI_BRIDGE_API_KEY") or env.get("API_KEY") or env.get("OPENAI_API_KEY") or cfg.get("api_key") or cfg.get("API_KEY") or "38d5a975-128d-4106-8cc8-394dc1122696"
        self.clientAppName = clientAppName or env.get("CLIENT_APP_NAME") or cfg.get("clientAppName") or cfg.get("CLIENT_APP_NAME") or "mdrf-gpam-stm32-technical-support-qa"
        self.serviceName = serviceName or env.get("SERVICE_NAME") or cfg.get("serviceName") or cfg.get("SERVICE_NAME") or "chat"
        self.url = url or env.get("ST_AI_BRIDGE_URL") or env.get("ST_BRIDGE_URL") or cfg.get("url") or "https://api-ai-bridge-qa.st.com/chatgpt/api/client-apps"
        # proxies : env PROXY_HTTP/PROXY_HTTPS prioritaires
        env_proxy_http = env.get("PROXY_HTTP") or env.get("HTTP_PROXY") or env.get("http_proxy")
        env_proxy_https = env.get("PROXY_HTTPS") or env.get("HTTPS_PROXY") or env.get("https_proxy")
        if env_proxy_http or env_proxy_https:
            p = {}
            if env_proxy_http: p["http"] = env_proxy_http
            if env_proxy_https: p["https"] = env_proxy_https
            # si variable présente mais vide -> désactiver proxy
            p = {k: v for k, v in p.items() if v and v.strip()}
            self.proxies = p if p else None
        else:
            proxies_cfg = cfg.get("proxies", {})
            if proxies_cfg:
                self.proxies = {k: v for k, v in proxies_cfg.items() if v}
                if not self.proxies:
                    self.proxies = None
            else:
                self.proxies = None
        # remoteUser doit être un email valide pour le bridge
        raw_remote = env.get("REMOTE_USER") or env.get("ST_REMOTE_USER") or cfg.get("remoteUser") or cfg.get("REMOTE_USER") or ""
        raw_remote = (raw_remote or "").strip()
        if raw_remote and "@" not in raw_remote:
            raw_remote = ""
        self.remoteUser = raw_remote or "younes.lahbib@st.com"
        self.persona = env.get("PERSONA") or cfg.get("persona") or "Myriam"
        # temperature doit être >0 pour le bridge (ton .env: 0.2)
        raw_temp = env.get("TEMPERATURE") or cfg.get("temperature")
        try:
            t = float(raw_temp) if raw_temp is not None and str(raw_temp).strip() != "" else 0.2
        except Exception:
            t = 0.2
        if t <= 0:
            t = 0.2
        self.default_temperature = t
        # maxResponseTokens : prioritaire env MAX_RESPONSE_TOKENS (ton .env: 32400)
        raw_max = env.get("MAX_RESPONSE_TOKENS") or env.get("MAX_TOKENS") or cfg.get("maxResponseTokens") or cfg.get("MAX_RESPONSE_TOKENS")
        try:
            self.default_max_tokens = int(raw_max) if raw_max is not None and str(raw_max).strip() != "" else 32400
        except Exception:
            self.default_max_tokens = 32400
        self.default_reasoning = env.get("REASONING_EFFORT") or cfg.get("reasoningEffort") or "high"
        self.default_responseFormat = env.get("RESPONSE_FORMAT") or cfg.get("responseFormat") or "json_object"

    def generate(self, prompt: str, images: list[Path] = None, json_str: str = None,
                 pdf_text: str = None, responseFormat: str = None,
                 temperature: float = None, maxResponseTokens: int = None,
                 reasoningEffort: str = None, include_header_zoom: bool = True,
                 max_retries: int = 3, **overrides) -> dict:
        images = images or []
        # normaliser Path
        image_paths = [Path(p) for p in images]
        content = build_multimodal_content(prompt, image_paths,
                                           include_header_zoom=include_header_zoom,
                                           json_text=json_str,
                                           pdf_text=pdf_text)
        # params effectifs
        temp = temperature if temperature is not None else self.default_temperature
        max_tok = maxResponseTokens if maxResponseTokens is not None else self.default_max_tokens
        reasoning = reasoningEffort or self.default_reasoning
        resp_fmt = responseFormat or self.default_responseFormat
        if overrides.get("responseFormat"):
            resp_fmt = overrides["responseFormat"]
        # allow override persona etc.
        persona = overrides.get("persona", self.persona)

        for attempt in range(1, max_retries + 1):
            timestamp_s = int(time.time())
            nonce = random.randint(0, 999999)
            token = generate_token(self.clientAppName, self.serviceName, self.api_key, timestamp_s, nonce)
            headers = {
                "Content-Type": "application/json",
                "stchatgpt-auth-token": token,
                "stchatgpt-auth-nonce": str(nonce),
            }
            payload = {
                "version": 1,
                "clientAppName": self.clientAppName,
                "service": self.serviceName,
                "timestamp": timestamp_s,
                "remoteUser": self.remoteUser,
                "messages": [{"role": "user", "content": content}],
                "temperature": temp,
                "maxResponseTokens": max_tok,
                "persona": persona,
                "responseFormat": resp_fmt,
                "reasoningEffort": reasoning,
            }
            # inject overrides non déjà gérés (ex: top_p)
            for k, v in overrides.items():
                if k not in payload:
                    payload[k] = v

            try:
                resp = requests.post(
                    self.url,
                    json=payload,
                    headers=headers,
                    proxies=self.proxies,
                    verify=False,
                    timeout=900,
                )
                resp.raise_for_status()
                data = resp.json()
                if "completion" not in data:
                    err_msg = data.get("message", "Unknown API error")
                    raise requests.exceptions.HTTPError(
                        f"API error (HTTP {resp.status_code}): {err_msg}", response=resp)
                completion = data.get("completion", "")
                # ST Bridge ne retourne pas usage tokens ; on mappe 0
                return {"text": completion, "tokens_in": 0, "tokens_out": 0, "raw": data}
            except requests.exceptions.RequestException as e:
                print(f"Attempt {attempt} failed: {e}")
                if attempt == max_retries:
                    raise
                if "Request Timeout" in str(e):
                    wait_time = 30 * attempt
                    print(f"Bridge overloaded (Request Timeout), retrying after {wait_time}s...")
                else:
                    wait_time = 5 * attempt
                    print(f"Retrying after {wait_time}s...")
                time.sleep(wait_time)
        raise RuntimeError("STBridgeProvider: max retries exceeded")


# ---------------------------------------------------------------------------
# Gemini Provider (rollback)
# ---------------------------------------------------------------------------
class GeminiProvider(LLMProvider):
    def __init__(self, api_keys: list[str] = None, model: str = "gemini-flash-latest", api_manager=None):
        self.model = model
        self.api_keys = api_keys or []
        self.api_manager = api_manager
        # lazy import
        try:
            from google import genai
            from google.genai import types
            self._genai = genai
            self._types = types
        except ImportError:
            self._genai = None
            self._types = None

    def generate(self, prompt: str, images: list[Path] = None, json_str: str = None,
                 pdf_text: str = None, pdf_parts: list[Path] = None, **kwargs) -> dict:
        if not self._genai:
            raise ImportError("google-genai not installed")
        from PIL import Image as PILImage
        contents = [prompt]
        if images:
            for idx, img_path in enumerate(images):
                contents.append("Image page :" if idx == 0 else "Suite tableau :")
                contents.append(PILImage.open(img_path))
        if pdf_parts:
            for p in pdf_parts:
                try:
                    pdf_bytes = Path(p).read_bytes()
                    contents.append(self._types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"))
                except Exception:
                    pass
        if pdf_text:
            contents.append(f"Texte de référence positionné:\n{pdf_text}")
        if json_str:
            contents.append(f"JSON à vérifier:\n{json_str}")

        # gestion ApiManager si fourni : rotation clé
        api_key = None
        k_idx = 0
        if self.api_manager and self.api_keys:
            # essayer clé disponible simple
            try:
                from ApiManager import ApiManager  # noqa
                api_key, k_idx, _pool = self.api_manager.get_key()
            except Exception:
                api_key = self.api_keys[0]
        elif self.api_keys:
            api_key = self.api_keys[0]
        else:
            import os as _os
            api_key = _os.environ.get("GEMINI_API_KEY", "")

        client = self._genai.Client(api_key=api_key)
        config_kwargs = {}
        if kwargs.get("temperature") is not None:
            config_kwargs["temperature"] = kwargs["temperature"]
        else:
            config_kwargs["temperature"] = 0
        if kwargs.get("response_mime_type"):
            config_kwargs["response_mime_type"] = kwargs["response_mime_type"]
        elif kwargs.get("responseFormat") == "json_object" or True:
            # defaut JSON pour validation
            config_kwargs["response_mime_type"] = "application/json"
        if kwargs.get("thinking_budget"):
            config_kwargs["thinking_config"] = self._types.ThinkingConfig(thinking_budget=kwargs["thinking_budget"])

        cfg = self._types.GenerateContentConfig(**config_kwargs) if config_kwargs else None
        response = client.models.generate_content(model=self.model, contents=contents, config=cfg)
        text = response.text if hasattr(response, "text") else str(response)
        try:
            tokens_in = response.usage_metadata.prompt_token_count or 0
            tokens_out = response.usage_metadata.candidates_token_count or 0
        except Exception:
            tokens_in, tokens_out = 0, 0
        return {"text": text, "tokens_in": tokens_in, "tokens_out": tokens_out, "raw": response}


def get_provider(name: str, **kwargs) -> LLMProvider:
    name = (name or "stbridge").lower()
    if name in ("stbridge", "myriamx", "st", "openai"):
        return STBridgeProvider(**kwargs)
    elif name in ("gemini", "google"):
        return GeminiProvider(**kwargs)
    else:
        raise ValueError(f"Provider inconnu: {name}")
