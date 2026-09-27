# ============================================================================
# Serveur d'inférence Opsiom
#
# Compatible :
#   - PC local
#   - Render
#   - Hugging Face pour les checkpoints
#   - ngrok pour exposer l'API
#
# PRINCIPALES MODIFICATIONS :
#   - Les modèles ne sont PLUS tous chargés au démarrage.
#   - Un seul modèle est gardé en RAM à la fois.
#   - Les checkpoints sont téléchargés depuis Hugging Face à la demande.
#   - Le cache Hugging Face évite les téléchargements inutiles.
#   - Le port peut être fourni par Render via PORT.
#   - /api/chat et /api/chat/stream s'authentifient désormais avec le token
#     de compte de l'utilisateur (Authorization: Bearer <token>, obtenu via
#     /login) -- le même, que l'appel vienne du proxy Render (avec l'en-tête
#     X-Opsiom-Quota-Handled: 1, quota déjà décompté par le proxy) ou
#     directement du CLI opsiom (quota vérifié puis décompté ici même,
#     voir _authenticate / _quota_status / _consume_quota). Plus de secret
#     séparé à synchroniser entre les deux services. Une ancienne clé API
#     personnelle (X-API-Key) ou un ancien secret partagé (OPSIOM_API_KEY)
#     restent acceptés par compatibilité, mais ne sont plus nécessaires.
#   - /api/chat/stream existe pour le CLI opsiom (SSE "data: {...}\n\n") :
#     la génération reste bloquante côté modèle, seul l'envoi au client est
#     découpé mot par mot pour l'affichage progressif.
#
# Variables d'environnement supplémentaires :
#   OCTIX_URL          - URL de l'API Octix (défaut: http://localhost:5050)
#   OPSIOM_API_KEY      - ancien secret partagé, optionnel, gardé seulement
#                         pour compatibilité (voir _authenticate)
#   DAILY_TOKEN_QUOTA  - défaut: 500, doit matcher la valeur côté Octix
#
# Architecture :
#
#   GitHub
#      ↓
#   app.py
#      ↓
#   Render
#      ↓
#   Hugging Face → checkpoints .pt
#      ↓
#   Opsiom chargé en RAM
#      ↓
#   Flask
#      ↓
#   ngrok
#      ↓
#   Internet
# ============================================================================

import gc
import json
import os
import threading
import time
from dataclasses import dataclass

import requests
import torch
import torch.nn as nn
import torch.nn.functional as F

from flask import Flask, Response, request, jsonify, stream_with_context
from flask_cors import CORS

from pyngrok import ngrok
from huggingface_hub import hf_hub_download

from dotenv import load_dotenv


# ============================================================================
# Environnement
# ============================================================================

load_dotenv()


# ============================================================================
# Configuration
# ============================================================================

HF_REPO_ID = "JuloEco/opsiom-fr-checkpoints"

TOKENIZER_FILENAME = "fr_bpe_tokenizer.json"


# ============================================================================
# Auth & quota — le compte suffit, plus de secret séparé à faire coïncider
# entre deux services.
#
# Façons d'appeler ce serveur, toutes identifiées par le MÊME token de
# compte (Authorization: Bearer <token>, obtenu via /login) :
#   1. Le proxy Render (frontend web) : il a déjà vérifié/décompté le quota
#      du compte connecté auprès d'Octix avant d'appeler /api/chat, et le
#      signale avec l'en-tête "X-Opsiom-Quota-Handled: 1" pour qu'on ne le
#      décompte pas une seconde fois ici (mode "trusted_personal").
#   2. Le CLI opsiom, en direct : même token de compte, mais sans cet
#      en-tête -- le quota est alors vérifié ET décompté ici même, auprès
#      d'Octix (/account/quota, /account/quota/consume). Le MÊME quota est
#      donc partagé entre web et CLI, puisque décompté par compte.
#
# Compatibilité (facultative, plus mise en avant nulle part) :
#   - une ancienne clé API personnelle en "X-API-Key" fonctionne encore
#     (mode "api_key", vérifiée via /verify-api-key).
#   - un ancien secret partagé OPSIOM_API_KEY, s'il est encore défini ici,
#     continue d'être accepté en "Authorization: Bearer <secret>"
#     (mode "trusted").
# ============================================================================

OCTIX_URL = os.environ.get("OCTIX_URL", "http://localhost:5050")
OPSIOM_API_KEY = os.environ.get("OPSIOM_API_KEY", "").strip()
DAILY_TOKEN_QUOTA = int(os.environ.get("DAILY_TOKEN_QUOTA", "500"))


def _estimate_tokens(text: str) -> int:
    """Estimation grossière (~4 caractères/token), identique à celle du CLI
    et du frontend, faute de tokenizer exact partagé entre les trois."""
    return max(1, round(len(text or "") / 4))


def _authenticate(req):
    """Résout l'appelant. Renvoie (mode, credential_ou_none, error_ou_none) :
      - mode == "trusted_personal" : le proxy Render, avec le token de
        compte de l'utilisateur ET l'en-tête X-Opsiom-Quota-Handled: 1 --
        posé uniquement par ce proxy, une fois qu'il a déjà décompté le
        quota lui-même auprès d'Octix. Jamais décompté une seconde fois ici.
      - mode == "personal_token" : un appelant direct (le CLI) avec son
        propre token de compte -- quota vérifié ET décompté ici.
      - mode == "api_key" : compatibilité, ancienne clé API personnelle.
      - mode == "trusted" : compatibilité, ancien secret partagé.
    error_ou_none contient un message si l'authentification a échoué."""
    auth_header = req.headers.get("Authorization", "")
    bearer = auth_header[7:].strip() if auth_header.startswith("Bearer ") else None
    api_key = req.headers.get("X-API-Key") or req.headers.get("X-Api-Key")
    quota_already_handled = req.headers.get("X-Opsiom-Quota-Handled") == "1"

    if api_key:
        try:
            r = requests.post(f"{OCTIX_URL}/verify-api-key", json={"api_key": api_key}, timeout=8)
            data = r.json()
        except (requests.exceptions.RequestException, ValueError):
            return None, None, "service de comptes injoignable, réessaie plus tard."
        if not data.get("valid"):
            return None, None, "clé API invalide ou expirée."
        return "api_key", api_key, None

    if bearer and OPSIOM_API_KEY and bearer == OPSIOM_API_KEY:
        return "trusted", None, None

    if bearer:
        # Token de compte (identique à celui du web ou obtenu via
        # `opsiom login`) : c'est le chemin normal désormais.
        try:
            r = requests.post(f"{OCTIX_URL}/verify", json={"token": bearer}, timeout=8)
            data = r.json()
        except (requests.exceptions.RequestException, ValueError):
            return None, None, "service de comptes injoignable, réessaie plus tard."
        if not data.get("valid"):
            return None, None, "session expirée, reconnecte-toi."
        return ("trusted_personal" if quota_already_handled else "personal_token"), bearer, None

    if OPSIOM_API_KEY:
        return None, None, "authentification requise (connecte-toi avec ton compte)."

    # Aucune clé partagée configurée sur ce serveur (dev local / usage perso) :
    # on n'exige rien, comme avant l'ajout de ce système d'auth.
    return "trusted", None, None


def _quota_headers(mode: str, credential: str) -> dict:
    """En-tête d'authentification à renvoyer à Octix pour les appels de
    quota, selon le mode résolu par _authenticate."""
    if mode == "api_key":
        return {"X-Api-Key": credential}
    return {"Authorization": f"Bearer {credential}"}


def _quota_status(mode: str, credential: str):
    """Statut du quota Octix du compte appelant, sans le décompter. Renvoie
    un quota "plein" si Octix est injoignable."""
    try:
        r = requests.get(f"{OCTIX_URL}/account/quota", headers=_quota_headers(mode, credential), timeout=5)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException:
        return {"used": 0, "limit": DAILY_TOKEN_QUOTA, "remaining": DAILY_TOKEN_QUOTA}


def _consume_quota(mode: str, credential: str, tokens: int):
    """Décompte `tokens` sur le quota Octix du compte appelant et renvoie le
    nouveau statut. Ne bloque pas la réponse déjà générée si Octix est
    injoignable à ce moment précis."""
    try:
        r = requests.post(
            f"{OCTIX_URL}/account/quota/consume",
            json={"tokens": max(0, int(tokens))},
            headers={**_quota_headers(mode, credential), "Content-Type": "application/json"},
            timeout=5,
        )
        data = r.json()
        return data.get("quota") or _quota_status(mode, credential)
    except requests.exceptions.RequestException:
        return _quota_status(mode, credential)


MODEL_CATALOG = [
    {
        "id": "nano",
        "filename": "chat_model_24M.pt",
        "label": "Opsiom Nano",
        "params_label": "24M",
    },
    {
        "id": "small",
        "filename": "chat_model_44M.pt",
        "label": "Opsiom Small",
        "params_label": "44M",
    },
    {
        "id": "large",
        "filename": "chat_model_220M.pt",
        "label": "Opsiom 220M",
        "params_label": "220M",
    },
]

DEFAULT_MODEL_ID = "small"


# ============================================================================
# Balises de chat
# ============================================================================

USER_TAG = "<|Utilisateur|>"
ASSISTANT_TAG = "<|Assistant|>"
EOT_TAG = "<|endoftext|>"

SPECIAL_TAGS = (
    USER_TAG,
    ASSISTANT_TAG,
    EOT_TAG,
)


# ============================================================================
# Environnement d'exécution
# ============================================================================

DEVICE = "cpu"

# Render fournit normalement PORT.
# En local, on utilisera 5000.
PORT = int(os.environ.get("PORT", "5000"))

NGROK_TOKEN = os.environ.get("NGROK_TOKEN", "")


# ============================================================================
# Architecture du modèle
# ============================================================================

@dataclass
class ModelArgs:
    vocab_size: int = 16000
    dim: int = 1024
    n_layers: int = 16
    n_heads: int = 16
    n_kv_heads: int | None = 4
    max_seq_len: int = 512
    dropout: float = 0.1
    rope_theta: float = 10000.0
    norm_eps: float = 1e-6
    device: str = "cpu"


class RMSNorm(nn.Module):

    def __init__(self, dim: int, eps: float = 1e-6):
        super().__init__()

        self.eps = eps
        self.weight = nn.Parameter(torch.ones(dim))

    def _norm(self, x):
        return x * torch.rsqrt(
            x.pow(2).mean(dim=-1, keepdim=True) + self.eps
        )

    def forward(self, x):
        return self._norm(x.float()).type_as(x) * self.weight


class RotaryEmbedding(nn.Module):

    def __init__(
        self,
        dim: int,
        max_seq_len: int = 2048,
        theta: float = 10000.0,
    ):
        super().__init__()

        inv_freq = 1.0 / (
            theta ** (torch.arange(0, dim, 2).float() / dim)
        )

        self.register_buffer(
            "inv_freq",
            inv_freq,
            persistent=False,
        )

        t = torch.arange(max_seq_len).float()

        freqs = torch.outer(t, inv_freq)

        emb = torch.cat(
            (freqs, freqs),
            dim=-1,
        )

        self.register_buffer(
            "cos_cached",
            emb.cos(),
            persistent=False,
        )

        self.register_buffer(
            "sin_cached",
            emb.sin(),
            persistent=False,
        )

    def forward(self, x, seq_len, start_pos=0):

        cos = self.cos_cached[
            start_pos:start_pos + seq_len
        ].to(
            dtype=x.dtype,
            device=x.device,
        )

        sin = self.sin_cached[
            start_pos:start_pos + seq_len
        ].to(
            dtype=x.dtype,
            device=x.device,
        )

        return cos, sin


def rotate_half(x):

    half = x.shape[-1] // 2

    return torch.cat(
        (
            -x[..., half:],
            x[..., :half],
        ),
        dim=-1,
    )


def apply_rotary_emb(
    xq,
    xk,
    cos,
    sin,
):

    cos = cos.unsqueeze(0).unsqueeze(0)
    sin = sin.unsqueeze(0).unsqueeze(0)

    return (
        (xq * cos) + (rotate_half(xq) * sin),
        (xk * cos) + (rotate_half(xk) * sin),
    )


class SwiGLUFeedForward(nn.Module):

    def __init__(self, args: ModelArgs):
        super().__init__()

        hidden_dim = int(8 * args.dim / 3)

        multiple_of = 256

        hidden_dim = multiple_of * (
            (hidden_dim + multiple_of - 1)
            // multiple_of
        )

        self.w1 = nn.Linear(
            args.dim,
            hidden_dim,
            bias=False,
        )

        self.w2 = nn.Linear(
            hidden_dim,
            args.dim,
            bias=False,
        )

        self.w3 = nn.Linear(
            args.dim,
            hidden_dim,
            bias=False,
        )

        self.dropout = nn.Dropout(
            args.dropout
        )

    def forward(self, x):

        return self.dropout(
            self.w2(
                F.silu(self.w1(x))
                * self.w3(x)
            )
        )


class ModernCausalAttention(nn.Module):

    def __init__(self, args: ModelArgs):
        super().__init__()

        self.n_heads = args.n_heads

        self.n_kv_heads = (
            args.n_kv_heads
            if args.n_kv_heads is not None
            else args.n_heads
        )

        self.n_rep = (
            self.n_heads
            // self.n_kv_heads
        )

        self.head_dim = (
            args.dim
            // args.n_heads
        )

        self.dropout_p = args.dropout

        self.wq = nn.Linear(
            args.dim,
            self.n_heads * self.head_dim,
            bias=False,
        )

        self.wk = nn.Linear(
            args.dim,
            self.n_kv_heads * self.head_dim,
            bias=False,
        )

        self.wv = nn.Linear(
            args.dim,
            self.n_kv_heads * self.head_dim,
            bias=False,
        )

        self.wo = nn.Linear(
            self.n_heads * self.head_dim,
            args.dim,
            bias=False,
        )

        self.resid_dropout = nn.Dropout(
            args.dropout
        )

    def forward(
        self,
        x,
        cos,
        sin,
        kv_cache=None,
    ):

        B, T, C = x.shape

        q = self.wq(x).view(
            B,
            T,
            self.n_heads,
            self.head_dim,
        ).transpose(1, 2)

        k = self.wk(x).view(
            B,
            T,
            self.n_kv_heads,
            self.head_dim,
        ).transpose(1, 2)

        v = self.wv(x).view(
            B,
            T,
            self.n_kv_heads,
            self.head_dim,
        ).transpose(1, 2)

        q, k = apply_rotary_emb(
            q,
            k,
            cos,
            sin,
        )

        if kv_cache is not None:

            past_k, past_v = kv_cache

            if past_k is not None:

                k = torch.cat(
                    (
                        past_k,
                        k,
                    ),
                    dim=2,
                )

                v = torch.cat(
                    (
                        past_v,
                        v,
                    ),
                    dim=2,
                )

            new_kv_cache = (
                k,
                v,
            )

        else:

            new_kv_cache = None

        if self.n_rep > 1:

            k = k.repeat_interleave(
                self.n_rep,
                dim=1,
            )

            v = v.repeat_interleave(
                self.n_rep,
                dim=1,
            )

        is_causal = (
            kv_cache is None
            or k.shape[2] == q.shape[2]
        )

        y = F.scaled_dot_product_attention(
            q,
            k,
            v,
            attn_mask=None,
            dropout_p=0.0,
            is_causal=is_causal,
        )

        y = y.transpose(
            1,
            2,
        ).contiguous().view(
            B,
            T,
            self.n_heads * self.head_dim,
        )

        y = self.resid_dropout(
            self.wo(y)
        )

        if kv_cache is not None:
            return y, new_kv_cache

        return y


class TransformerBlock(nn.Module):

    def __init__(self, args: ModelArgs):
        super().__init__()

        self.attn = ModernCausalAttention(args)

        self.ffn = SwiGLUFeedForward(args)

        self.norm1 = RMSNorm(
            args.dim,
            eps=args.norm_eps,
        )

        self.norm2 = RMSNorm(
            args.dim,
            eps=args.norm_eps,
        )

    def forward(
        self,
        x,
        cos,
        sin,
        kv_cache=None,
    ):

        if kv_cache is not None:

            attn_out, new_kv_cache = self.attn(
                self.norm1(x),
                cos,
                sin,
                kv_cache=kv_cache,
            )

            x = x + attn_out

            x = x + self.ffn(
                self.norm2(x)
            )

            return x, new_kv_cache

        x = x + self.attn(
            self.norm1(x),
            cos,
            sin,
        )

        x = x + self.ffn(
            self.norm2(x)
        )

        return x


class ModernLLM(nn.Module):

    def __init__(self, args: ModelArgs):
        super().__init__()

        self.args = args

        self.tok_embeddings = nn.Embedding(
            args.vocab_size,
            args.dim,
        )

        self.dropout = nn.Dropout(
            args.dropout
        )

        head_dim = (
            args.dim
            // args.n_heads
        )

        self.rope = RotaryEmbedding(
            head_dim,
            max_seq_len=args.max_seq_len,
            theta=args.rope_theta,
        )

        self.layers = nn.ModuleList(
            [
                TransformerBlock(args)
                for _ in range(args.n_layers)
            ]
        )

        self.norm_f = RMSNorm(
            args.dim,
            eps=args.norm_eps,
        )

        self.lm_head = nn.Linear(
            args.dim,
            args.vocab_size,
            bias=False,
        )

        self.tok_embeddings.weight = (
            self.lm_head.weight
        )

    def forward(
        self,
        tokens,
        kv_caches=None,
        start_pos=0,
    ):

        B, T = tokens.shape

        x = self.dropout(
            self.tok_embeddings(tokens)
        )

        cos, sin = self.rope(
            x,
            seq_len=T,
            start_pos=start_pos,
        )

        if kv_caches is not None:

            new_kv_caches = []

            for i, layer in enumerate(
                self.layers
            ):

                x, layer_cache = layer(
                    x,
                    cos,
                    sin,
                    kv_cache=kv_caches[i],
                )

                new_kv_caches.append(
                    layer_cache
                )

        else:

            for layer in self.layers:

                x = layer(
                    x,
                    cos,
                    sin,
                )

            new_kv_caches = None

        logits = self.lm_head(
            self.norm_f(x)
        )

        if kv_caches is not None:

            return (
                logits,
                new_kv_caches,
            )

        return logits

    def _sample_next_token(
        self,
        logits,
        temperature,
        top_k,
        top_p,
        generated_ids=None,
        repetition_penalty=1.0,
    ):

        if (
            repetition_penalty != 1.0
            and generated_ids is not None
            and generated_ids.numel() > 0
        ):

            unique_ids = torch.unique(
                generated_ids
            )

            valid_unique_ids = unique_ids[
                unique_ids < logits.size(-1)
            ]

            if valid_unique_ids.numel() > 0:

                prev_logits = logits[
                    0,
                    valid_unique_ids,
                ]

                penalized = torch.where(
                    prev_logits > 0,
                    prev_logits / repetition_penalty,
                    prev_logits * repetition_penalty,
                )

                logits[
                    0,
                    valid_unique_ids,
                ] = penalized

        if temperature <= 0.0:

            return torch.argmax(
                logits,
                dim=-1,
                keepdim=True,
            )

        logits = logits / temperature

        if top_k is not None and top_k > 0:

            top_k_clamped = min(
                top_k,
                logits.size(-1),
            )

            v, _ = torch.topk(
                logits,
                top_k_clamped,
            )

            logits = torch.where(
                logits < v[:, [-1]],
                torch.full_like(
                    logits,
                    float("-inf"),
                ),
                logits,
            )

        probs = F.softmax(
            logits,
            dim=-1,
        )

        if top_p is not None and top_p < 1.0:

            sorted_probs, sorted_indices = torch.sort(
                probs,
                descending=True,
                dim=-1,
            )

            cumulative_probs = torch.cumsum(
                sorted_probs,
                dim=-1,
            )

            sorted_mask = (
                cumulative_probs
                - sorted_probs
                > top_p
            )

            sorted_probs[sorted_mask] = 0.0

            sorted_probs = (
                sorted_probs
                / sorted_probs.sum(
                    dim=-1,
                    keepdim=True,
                )
            )

            probs = torch.zeros_like(
                probs
            ).scatter_(
                -1,
                sorted_indices,
                sorted_probs,
            )

        return torch.multinomial(
            probs,
            num_samples=1,
        )

    @torch.no_grad()
    def generate(
        self,
        prompt,
        tokenizer,
        max_new_tokens=100,
        temperature=0.8,
        top_p=0.9,
        top_k=40,
        repetition_penalty=1.3,
    ):

        self.eval()

        device = next(
            self.parameters()
        ).device

        token_ids = tokenizer.encode(
            prompt,
            allowed_special="all",
        )

        tokens = torch.tensor(
            [token_ids],
            dtype=torch.long,
            device=device,
        )

        tokens = tokens[
            :,
            -(self.args.max_seq_len - 1):,
        ]

        kv_caches = [
            (None, None)
            for _ in range(
                self.args.n_layers
            )
        ]

        logits, kv_caches = self.forward(
            tokens,
            kv_caches=kv_caches,
            start_pos=0,
        )

        logits = logits[:, -1, :]

        cur_pos = tokens.shape[1]

        generated_ids = []

        for _ in range(max_new_tokens):

            penalized_tensor = (
                torch.tensor(
                    [generated_ids],
                    dtype=torch.long,
                    device=device,
                )
                if generated_ids
                else None
            )

            next_token = self._sample_next_token(
                logits,
                temperature,
                top_k,
                top_p,
                generated_ids=penalized_tensor,
                repetition_penalty=repetition_penalty,
            )

            token_val = next_token.item()

            if token_val == tokenizer.eot_token:
                break

            generated_ids.append(
                token_val
            )

            if cur_pos >= self.args.max_seq_len:
                break

            logits, kv_caches = self.forward(
                next_token,
                kv_caches=kv_caches,
                start_pos=cur_pos,
            )

            logits = logits[:, -1, :]

            cur_pos += 1

        response_text = tokenizer.decode(
            generated_ids
        )

        cut_indices = [
            response_text.find(tag)
            for tag in SPECIAL_TAGS
            if tag in response_text
        ]

        if cut_indices:

            response_text = response_text[
                :min(cut_indices)
            ]

        return response_text.strip()


# ============================================================================
# Tokenizer
# ============================================================================

class FrenchTokenizerWrapper:

    def __init__(self, tokenizer):

        self._tok = tokenizer

        eot_id = tokenizer.token_to_id(
            EOT_TAG
        )

        self.eot_token = (
            eot_id
            if eot_id is not None
            else 0
        )

        self.vocab_size = (
            tokenizer.get_vocab_size()
        )

    def encode(
        self,
        text,
        allowed_special="all",
    ):

        return self._tok.encode(
            text
        ).ids

    def decode(self, ids):

        return self._tok.decode(
            ids,
            skip_special_tokens=True,
        )


# ============================================================================
# Utilitaires
# ============================================================================

def build_chat_prompt(message: str) -> str:

    return (
        f"{USER_TAG}\n"
        f"{message}\n"
        f"{ASSISTANT_TAG}\n"
    )


def model_num_params(model):

    unique_params = {
        id(p): p
        for p in model.parameters()
    }

    return sum(
        p.numel()
        for p in unique_params.values()
    )


def get_model_entry(model_id):

    for entry in MODEL_CATALOG:

        if entry["id"] == model_id:
            return entry

    return None


# ============================================================================
# Hugging Face
# ============================================================================

def resolve_local_or_download(filename: str) -> str:

    # 1. Permet de continuer à utiliser le script sur ton PC
    #    avec un fichier placé directement à côté de app.py.
    if os.path.exists(filename):

        print(
            f"✅ {filename} trouvé localement."
        )

        return filename

    # 2. Sinon Hugging Face prend le relais.
    print(
        f"📥 {filename} absent localement."
    )

    print(
        f"   Téléchargement depuis "
        f"{HF_REPO_ID}..."
    )

    path = hf_hub_download(
        repo_id=HF_REPO_ID,
        filename=filename,
    )

    print(
        f"✅ {filename} disponible : {path}"
    )

    return path


def load_tokenizer():

    from tokenizers import Tokenizer

    tokenizer_path = resolve_local_or_download(
        TOKENIZER_FILENAME
    )

    tok = Tokenizer.from_file(
        tokenizer_path
    )

    return FrenchTokenizerWrapper(tok)


def load_single_model(
    checkpoint_filename: str,
) -> ModernLLM:

    checkpoint_path = (
        resolve_local_or_download(
            checkpoint_filename
        )
    )

    print(
        f"📦 Lecture du checkpoint "
        f"{checkpoint_filename}..."
    )

    ckpt = torch.load(
        checkpoint_path,
        map_location="cpu",
        weights_only=False,
    )

    saved_args = ckpt["args"]

    args = ModelArgs(
        vocab_size=saved_args.vocab_size,
        dim=saved_args.dim,
        n_layers=saved_args.n_layers,
        n_heads=saved_args.n_heads,
        n_kv_heads=saved_args.n_kv_heads,
        max_seq_len=saved_args.max_seq_len,
        dropout=0.0,
        rope_theta=saved_args.rope_theta,
        norm_eps=saved_args.norm_eps,
        device="cpu",
    )

    model = ModernLLM(args)

    state_dict = ckpt[
        "model_state_dict"
    ]

    state_dict = {
        k.replace(
            "_orig_mod.",
            "",
        ): v
        for k, v in state_dict.items()
    }

    model.load_state_dict(
        state_dict
    )

    model.to(DEVICE)

    model.eval()

    print(
        f"✅ Modèle chargé : "
        f"{model_num_params(model) / 1e6:.2f}M "
        f"paramètres"
    )

    print(
        f"   val_loss : "
        f"{ckpt.get('val_loss')}"
    )

    return model


# ============================================================================
# Gestion mémoire des modèles
#
# IMPORTANT :
# Un seul modèle reste en RAM.
#
# Exemple :
#
#   requête Small
#       ↓
#   Small chargé
#
#   requête Large
#       ↓
#   Small supprimé
#       ↓
#   Large chargé
# ============================================================================

MODEL_LOCK = threading.Lock()

CURRENT_MODEL = None
CURRENT_MODEL_ID = None


def unload_current_model():

    global CURRENT_MODEL
    global CURRENT_MODEL_ID

    if CURRENT_MODEL is None:
        return

    print(
        f"🧹 Libération du modèle "
        f"{CURRENT_MODEL_ID}..."
    )

    del CURRENT_MODEL

    CURRENT_MODEL = None
    CURRENT_MODEL_ID = None

    gc.collect()

    if torch.cuda.is_available():

        torch.cuda.empty_cache()


def get_model(model_id):

    global CURRENT_MODEL
    global CURRENT_MODEL_ID

    entry = get_model_entry(
        model_id
    )

    if entry is None:

        raise ValueError(
            f"Modèle inconnu : {model_id}"
        )

    with MODEL_LOCK:

        # Le modèle demandé est déjà chargé.
        if (
            CURRENT_MODEL is not None
            and CURRENT_MODEL_ID == model_id
        ):

            return CURRENT_MODEL

        # On libère l'ancien modèle.
        unload_current_model()

        print(
            f"🚀 Chargement de "
            f"{entry['label']}..."
        )

        CURRENT_MODEL = load_single_model(
            entry["filename"]
        )

        CURRENT_MODEL_ID = model_id

        return CURRENT_MODEL


# ============================================================================
# Chargement du tokenizer uniquement
# ============================================================================

print(
    "📥 Chargement du tokenizer..."
)

tokenizer = load_tokenizer()

print(
    "✅ Tokenizer prêt."
)

print(
    "ℹ️ Aucun modèle n'est encore chargé."
)

print(
    "ℹ️ Le premier appel à /api/chat "
    "chargera le modèle demandé."
)


# ============================================================================
# Flask
# ============================================================================

app = Flask(__name__)

CORS(
    app,
    resources={
        r"/api/*": {
            "origins": "*"
        }
    },
    allow_headers=[
        "Content-Type",
        "ngrok-skip-browser-warning",
        "Authorization",
        "X-API-Key",
    ],
)


# ============================================================================
# GET /api/models
# ============================================================================

@app.route(
    "/api/models",
    methods=["GET"],
)
def list_models():

    return jsonify(
        {
            "models": [
                {
                    "id": entry["id"],
                    "label": entry["label"],
                    "params": entry["params_label"],
                }
                for entry in MODEL_CATALOG
            ],
            "default": DEFAULT_MODEL_ID,
            "loaded_model": CURRENT_MODEL_ID,
        }
    )


# ============================================================================
# Génération — factorisée pour être partagée entre /api/chat (non-stream)
# et /api/chat/stream (utilisée par le CLI).
# ============================================================================

def _resolve_model_and_message(data: dict):
    """Valide message/model_id. Renvoie (model_id, message, erreur_jsonify_ou_none)."""
    message = data.get("message", "")
    if not message:
        return None, None, (jsonify({"error": "message manquant"}), 400)

    model_id = data.get("model", DEFAULT_MODEL_ID)
    if get_model_entry(model_id) is None:
        available = ", ".join(entry["id"] for entry in MODEL_CATALOG)
        return None, None, (
            jsonify({"error": f"modèle inconnu '{model_id}'. Disponibles : {available}"}),
            400,
        )
    return model_id, message, None


def _generate(model_id: str, message: str, data: dict) -> str:
    """Lance la génération elle-même (bloquante) et renvoie le texte."""
    model = get_model(model_id)
    prompt = build_chat_prompt(message)
    response_text = model.generate(
        prompt=prompt,
        tokenizer=tokenizer,
        max_new_tokens=int(data.get("max_new_tokens", 100)),
        temperature=float(data.get("temperature", 0.8)),
        top_k=int(data.get("top_k", 40)),
        top_p=float(data.get("top_p", 0.9)),
        repetition_penalty=float(data.get("repetition_penalty", 1.3)),
    )
    return response_text or (
        "(Je n'ai pas réussi à générer de réponse — essayez de reformuler votre message.)"
    )


# ============================================================================
# POST /api/chat
# ============================================================================

@app.route(
    "/api/chat",
    methods=["POST"],
)
def chat():

    mode, credential, auth_error = _authenticate(request)
    if auth_error:
        return jsonify({"error": auth_error}), 401

    personal = mode in ("api_key", "personal_token")
    if personal:
        quota = _quota_status(mode, credential)
        if quota.get("remaining", DAILY_TOKEN_QUOTA) <= 0:
            return jsonify({
                "error": f"quota quotidien de {quota.get('limit', DAILY_TOKEN_QUOTA)} tokens atteint.",
                "quota": quota,
            }), 429

    data = request.get_json(force=True) or {}
    model_id, message, error = _resolve_model_and_message(data)
    if error:
        return error

    try:
        response_text = _generate(model_id, message, data)

        result = {"response": response_text, "model": model_id}
        if personal:
            tokens_used = _estimate_tokens(message) + _estimate_tokens(response_text)
            result["tokens_used"] = tokens_used
            result["remaining_quota"] = _consume_quota(mode, credential, tokens_used).get("remaining", 0)

        return jsonify(result)

    except Exception as exc:
        print("❌ Erreur pendant la génération :", repr(exc))
        return jsonify({"error": "Erreur pendant la génération.", "details": str(exc)}), 500


# ============================================================================
# POST /api/chat/stream — utilisée par le CLI opsiom.
#
# Le modèle local ne produit pas encore ses tokens un par un (model.generate
# renvoie le texte complet d'un coup) : on simule donc un flux en découpant
# la réponse déjà générée en mots, façon SSE ("data: {...}\n\n"), ce qui
# suffit à alimenter l'affichage progressif du CLI sans changer son format
# d'échange. Le dernier événement ({"done": true, ...}) porte le décompte
# de tokens qui fait foi.
# ============================================================================

@app.route(
    "/api/chat/stream",
    methods=["POST"],
)
def chat_stream():

    mode, credential, auth_error = _authenticate(request)
    if auth_error:
        return jsonify({"error": auth_error}), 401

    personal = mode in ("api_key", "personal_token")
    if personal:
        quota = _quota_status(mode, credential)
        if quota.get("remaining", DAILY_TOKEN_QUOTA) <= 0:
            return jsonify({
                "error": f"quota quotidien de {quota.get('limit', DAILY_TOKEN_QUOTA)} tokens atteint.",
                "quota": quota,
            }), 429

    data = request.get_json(force=True) or {}
    model_id, message, error = _resolve_model_and_message(data)
    if error:
        return error

    try:
        response_text = _generate(model_id, message, data)
    except Exception as exc:
        print("❌ Erreur pendant la génération :", repr(exc))
        return jsonify({"error": "Erreur pendant la génération.", "details": str(exc)}), 500

    def _events():
        words = response_text.split(" ")
        for i, word in enumerate(words):
            piece = word if i == 0 else " " + word
            yield f"data: {json.dumps({'token': piece})}\n\n"
            time.sleep(0.015)  # rythme de lecture, purement cosmétique

        final_event = {"done": True, "model": model_id}
        if personal:
            tokens_used = _estimate_tokens(message) + _estimate_tokens(response_text)
            final_event["tokens_used"] = tokens_used
            final_event["remaining_quota"] = _consume_quota(mode, credential, tokens_used).get("remaining", 0)
        yield f"data: {json.dumps(final_event)}\n\n"

    return Response(stream_with_context(_events()), mimetype="text/event-stream")


# ============================================================================
# GET /api/health
# ============================================================================

@app.route(
    "/api/health",
    methods=["GET"],
)
def health():

    return jsonify(
        {
            "status": "ok",
            "device": DEVICE,
            "models_available": [
                entry["id"]
                for entry in MODEL_CATALOG
            ],
            "loaded_model": CURRENT_MODEL_ID,
        }
    )


# ============================================================================
# Route racine
# ============================================================================

@app.route(
    "/",
    methods=["GET"],
)
def index():

    return jsonify(
        {
            "name": "Opsiom Inference API",
            "status": "online",
            "device": DEVICE,
            "loaded_model": CURRENT_MODEL_ID,
        }
    )


# ============================================================================
# Flask
# ============================================================================

def run_flask():

    print(
        f"🌐 Flask démarre sur "
        f"0.0.0.0:{PORT}"
    )

    app.run(
        host="0.0.0.0",
        port=PORT,
        use_reloader=False,
        threaded=True,
    )


# ============================================================================
# NGROK
# ============================================================================

def start_ngrok():

    if not NGROK_TOKEN:

        raise RuntimeError(
            "La variable "
            "d'environnement NGROK_TOKEN "
            "n'est pas définie."
        )

    print(
        "🔐 Configuration du token ngrok..."
    )

    ngrok.set_auth_token(
        NGROK_TOKEN
    )

    print(
        f"🌍 Création du tunnel ngrok "
        f"vers le port {PORT}..."
    )

    public_url = ngrok.connect(
        PORT
    )

    print()
    print(
        "============================================================"
    )
    print(
        "🚀 OPSIOM EST ACCESSIBLE SUR INTERNET"
    )
    print(
        "============================================================"
    )
    print(
        f"🌍 URL publique : {public_url}"
    )
    print(
        f"💬 Chat         : {public_url}/api/chat"
    )
    print(
        f"📋 Modèles      : {public_url}/api/models"
    )
    print(
        f"❤️ Health       : {public_url}/api/health"
    )
    print(
        "============================================================"
    )
    print()


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":

    print()
    print(
        "============================================================"
    )
    print(
        "🧠 OPSIOM INFERENCE SERVER"
    )
    print(
        "============================================================"
    )

    print(
        f"📦 Repository HF : {HF_REPO_ID}"
    )

    print(
        "🖥️ Device        : CPU"
    )

    print(
        f"🌐 Port          : {PORT}"
    )

    print(
        "📌 Modèles       : "
        + ", ".join(
            entry["label"]
            for entry in MODEL_CATALOG
        )
    )

    print(
        "============================================================"
    )
    print()

    # Test très léger :
    # on ne charge PAS de modèle ici.
    #
    # Le premier appel /api/chat chargera
    # réellement le modèle demandé.

    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True,
    )

    flask_thread.start()

    # Laisser Flask démarrer.
    import time

    time.sleep(1)

    start_ngrok()

    print(
        "🟢 Serveur prêt."
    )

    print(
        "ℹ️ Les modèles seront chargés "
        "à la première requête."
    )

    print(
        "ℹ️ Un seul modèle est conservé "
        "en mémoire à la fois."
    )

    # Le processus reste vivant.
    flask_thread.join()
