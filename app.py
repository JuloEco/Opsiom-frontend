"""
Interface front-end pour Opsiom — Flask, proxy vers le serveur d'inférence
qui tourne sur TON PC (CPU) et qui est exposé via un tunnel ngrok.

Ce serveur ne fait JAMAIS d'appel direct depuis le navigateur vers l'API
ngrok : tout passe par ce proxy, pour trois raisons :
  1. Ne jamais exposer l'URL du tunnel / OPSIOM_API_KEY côté client.
  2. Ajouter l'en-tête "ngrok-skip-browser-warning" (sinon ngrok renvoie sa
     page d'avertissement HTML au lieu du JSON sur le plan gratuit).
  3. Centraliser la gestion d'erreurs (PC éteint, tunnel fermé, génération
     lente sur CPU, etc.) à un seul endroit.

Routes exposées au navigateur :
  GET  /          -> templates/index.html
  GET  /status    -> proxy de GET  {API}/health
  GET  /models    -> proxy de GET  {API}/models   (pour construire le sélecteur)
  POST /chat      -> proxy de POST {API}/chat     (accepte "message" + "model")

Variables d'environnement :
  OPSIOM_API_URL     - URL du tunnel ngrok, avec ou sans le suffixe "/api"
                       (défaut: https://pursuable-underpaid-boss.ngrok-free.dev)
  OPSIOM_API_KEY     - ancien secret partagé, gardé en repli seulement si
                       jamais aucune session n'est disponible ; le chemin
                       normal envoie désormais le token de compte de la
                       personne connectée (voir _headers ci-dessous), plus
                       besoin de faire coïncider un secret entre ce
                       service et le serveur d'inférence.
  OPSIOM_TIMEOUT     - défaut: 120 (secondes). L'inférence CPU est lente,
                       surtout avec le modèle 220M.
  DAILY_TOKEN_QUOTA  - défaut: 500. Doit rester identique à la valeur
                       configurée côté Octix (DAILY_TOKEN_QUOTA) : cette
                       variable ne sert ici qu'à afficher un quota par
                       défaut cohérent si Octix est injoignable, le
                       décompte qui fait foi est toujours celui d'Octix.

Quota de tokens :
  Le quota gratuit (500 tokens/jour par défaut) n'est PLUS suivi dans une
  base locale à ce front : il est délégué à Octix (voir /account/quota et
  /account/quota/consume côté Octix_API), le même service qui gère les
  comptes. C'est ce qui permet de PARTAGER un seul et même quota entre
  cette interface web (identifiée par le token de session Octix) et le CLI
  opsiom (identifié par une clé API Octix) : les deux résolvent le même
  compte, donc décomptent le même compteur -- régénérer sa clé API, ou en
  créer une par appareil, ne redonne donc plus de tokens gratuits.
"""
import logging
import os
import time

import requests
from flask import Flask, jsonify, render_template, request, session

from auth import OCTIX_PORTAL_URL, OCTIX_URL, auth_bp, current_username, login_required

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("opsiom_frontend")

app = Flask(__name__)

# --- Config générale ----------------------------------------------------
app.secret_key = os.environ.get("SECRET_KEY", "")
if not app.secret_key:
    logger.warning(
        "SECRET_KEY n'est pas défini : une clé aléatoire temporaire est utilisée, "
        "ce qui déconnecte tout le monde à chaque redémarrage. Définis SECRET_KEY "
        "en production."
    )
    import secrets as _secrets
    app.secret_key = _secrets.token_hex(32)

# --- Authentification ------------------------------------------------
# Déléguée à Octix, via une session Flask simple (même méthode que
# LearnCode/Omnia) : voir auth.py pour login_required, octix_login, etc.
app.register_blueprint(auth_bp)


# --- Quota gratuit — PARTAGÉ par compte, décompté côté Octix -----------
# Valeur affichée par défaut si Octix est injoignable ; le compteur qui
# fait réellement foi (celui qu'on incrémente/vérifie) vit dans Octix,
# voir fetch_quota_status() / consume_quota() ci-dessous.
DAILY_TOKEN_QUOTA = int(os.environ.get("DAILY_TOKEN_QUOTA", "500"))


def _octix_auth_headers() -> dict:
    """Authentifie l'appel à Octix avec le token de session de la personne
    connectée : c'est ce qui fait que le quota décompté ici retombe sur le
    MÊME compte que celui que le CLI décompte via sa clé API (voir
    Octix_API /account/quota, qui accepte indifféremment un Bearer JWT ou
    une clé API — les deux résolvent le même utilisateur)."""
    headers = {"Content-Type": "application/json"}
    token = session.get("octix_token")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_quota_status() -> dict:
    """Statut du quota de TOKENS du compte connecté, tel que tenu par Octix.
    Si Octix est injoignable, on affiche un quota "plein" par défaut plutôt
    que de bloquer le chat pour un problème sans rapport avec le quota."""
    try:
        resp = requests.get(f"{OCTIX_URL}/account/quota", headers=_octix_auth_headers(), timeout=5)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException:
        logger.warning("Octix injoignable sur /account/quota — quota par défaut affiché.")
        return {"used": 0, "limit": DAILY_TOKEN_QUOTA, "remaining": DAILY_TOKEN_QUOTA}


def consume_quota(tokens: int) -> dict:
    """Décompte `tokens` sur le quota Octix du compte connecté et renvoie le
    nouveau statut. Si Octix est injoignable une fois la réponse déjà
    générée, on ne fait pas échouer la requête pour autant : on retombe sur
    le statut par défaut (le prochain /account/quota rattrapera l'état réel
    dès qu'Octix est de nouveau joignable)."""
    try:
        resp = requests.post(
            f"{OCTIX_URL}/account/quota/consume",
            json={"tokens": max(0, int(tokens))},
            headers=_octix_auth_headers(),
            timeout=5,
        )
        data = resp.json()
        if resp.status_code >= 400:
            return data.get("quota") or fetch_quota_status()
        return data.get("quota") or fetch_quota_status()
    except requests.exceptions.RequestException:
        logger.warning("Octix injoignable sur /account/quota/consume — décompte ignoré cette fois.")
        return fetch_quota_status()


def estimate_tokens(text: str) -> int:
    """Estimation grossière (~4 caractères/token), identique à celle du CLI
    opsiom-cli, pour rester cohérent entre les deux façons de consommer le
    même quota. Le compteur qui fait foi reste celui d'Octix : ceci ne sert
    qu'à savoir COMBIEN lui envoyer à décompter."""
    return max(1, round(len(text or "") / 4))


def _normalize_api_url(raw: str) -> str:
    """Accepte 'https://xxx.ngrok-free.dev' ou 'https://xxx.ngrok-free.dev/api'
    et renvoie toujours une URL qui se termine par '/api'."""
    url = raw.strip().rstrip("/")
    if not url.endswith("/api"):
        url += "/api"
    return url


OPSIOM_API_URL = _normalize_api_url(
    os.environ.get("OPSIOM_API_URL", "https://pursuable-underpaid-boss.ngrok-free.dev")
)
OPSIOM_API_KEY = os.environ.get("OPSIOM_API_KEY", "").strip()
REQUEST_TIMEOUT = int(os.environ.get("OPSIOM_TIMEOUT", "120"))

# Modèle utilisé si le front n'en envoie aucun (le serveur a aussi son défaut).
DEFAULT_MODEL_ID = "small"

# Noms affichés dans le sélecteur. Les ids (nano / small / large) sont ceux du
# serveur d'inférence et ne changent pas (le CLI et /api/chat continuent de
# fonctionner) : seul l'affichage est renommé, ici, à un seul endroit.
MODEL_DISPLAY = {
    "nano": {"label": "Opsiom Micro", "params": "25M"},
    "small": {"label": "Opsiom Nano", "params": "45M"},
    "large": {"label": "Opsiom Large", "params": "200M"},
}

# Bornes appliquées côté proxy : l'API ngrok est publique, on évite qu'un
# client puisse demander des générations démesurées sur ton PC.
MAX_MESSAGE_CHARS = 2000
MAX_NEW_TOKENS_LIMIT = 300


def _headers() -> dict:
    headers = {
        "Content-Type": "application/json",
        # Indispensable avec ngrok (plan gratuit) : évite la page
        # d'avertissement HTML qui casserait resp.json().
        "ngrok-skip-browser-warning": "true",
    }
    token = session.get("octix_token")
    if token:
        # Même token de compte que pour Octix (voir _octix_auth_headers) :
        # le serveur d'inférence le vérifie lui-même auprès d'Octix, plus
        # besoin de secret séparé à synchroniser entre les deux services.
        headers["Authorization"] = f"Bearer {token}"
        # On a déjà vérifié/décompté le quota nous-mêmes juste avant/après
        # cet appel (fetch_quota_status / consume_quota, ci-dessus) : ce
        # drapeau évite que le serveur d'inférence le décompte une
        # deuxième fois pour la même réponse.
        headers["X-Opsiom-Quota-Handled"] = "1"
    elif OPSIOM_API_KEY:
        # Compatibilité avec l'ancien secret partagé, si encore configuré
        # et qu'aucune session n'est disponible (ne devrait plus arriver
        # derrière @login_required, mais gardé par prudence).
        headers["Authorization"] = f"Bearer {OPSIOM_API_KEY}"
    return headers


def _clamp(value, default, low, high, cast=float):
    """Convertit value avec cast et le borne entre low et high ; default si invalide."""
    try:
        return max(low, min(high, cast(value)))
    except (TypeError, ValueError):
        return default


def _ngrok_error(resp) -> str | None:
    """Si la réponse vient de ngrok lui-même (et pas de ton serveur Flask),
    renvoie un message explicite. ngrok ajoute l'en-tête 'ngrok-error-code'."""
    code = resp.headers.get("ngrok-error-code")
    if not code:
        return None
    logger.warning(f"Erreur ngrok {code} (HTTP {resp.status_code})")
    if code == "ERR_NGROK_3200":
        return "Le tunnel ngrok est fermé : le serveur Opsiom n'est pas lancé sur le PC."
    if code in ("ERR_NGROK_8012", "ERR_NGROK_8011"):
        return "Le tunnel ngrok est ouvert mais le serveur Opsiom ne répond pas (Flask arrêté ?)."
    return f"Erreur ngrok ({code}). Vérifie que le serveur Opsiom est bien lancé sur le PC."


@app.get("/")
@login_required
def index():
    return render_template(
        "index.html",
        username=current_username(),
        quota=fetch_quota_status(),
        octix_portal_url=OCTIX_PORTAL_URL,
    )


@app.get("/status")
@login_required
def status():
    """Interroge GET {OPSIOM_API_URL}/health pour afficher un vrai statut
    dans la sidebar (pas un badge 'En ligne' codé en dur côté front).
    Renvoie aussi 'models_loaded' tel que fourni par le serveur."""
    try:
        resp = requests.get(f"{OPSIOM_API_URL}/health", headers=_headers(), timeout=15)

        ngrok_msg = _ngrok_error(resp)
        if ngrok_msg:
            return jsonify({"online": False, "error": "tunnel", "message": ngrok_msg}), 200

        resp.raise_for_status()
        data = resp.json()
        return jsonify({"online": True, "quota": fetch_quota_status(), **data})

    except requests.exceptions.Timeout:
        logger.warning("Timeout sur /health — le PC est peut-être occupé par une génération.")
        return jsonify({
            "online": False, "error": "timeout",
            "message": "Le serveur Opsiom met du temps à répondre (génération en cours sur le PC ?).",
        }), 200

    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connexion impossible à Opsiom : {e}")
        return jsonify({
            "online": False, "error": "connection",
            "message": "Impossible de joindre le serveur Opsiom (PC éteint ou pas de connexion ?).",
        }), 200

    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else "?"
        logger.error(f"Erreur HTTP {status_code} sur /health : {getattr(e.response, 'text', '')[:300]}")
        return jsonify({
            "online": False, "error": "http",
            "message": f"Opsiom a répondu une erreur ({status_code}).",
        }), 200

    except ValueError:
        logger.exception("Réponse /health illisible (pas du JSON valide)")
        return jsonify({
            "online": False, "error": "invalid_json",
            "message": "Réponse illisible du serveur (page d'avertissement ngrok ?).",
        }), 200

    except Exception as e:  # garde-fou générique — ne doit jamais faire planter la sidebar
        logger.exception("Erreur inattendue en interrogeant /health")
        return jsonify({"online": False, "error": "unknown", "message": str(e)}), 200


@app.get("/models")
@login_required
def models():
    """Proxy vers GET {OPSIOM_API_URL}/models.
    Réponse du serveur : {"models": [{"id","label","params"}, ...], "default": "small"}
    Le front s'en sert pour construire son sélecteur de modèle."""
    try:
        resp = requests.get(f"{OPSIOM_API_URL}/models", headers=_headers(), timeout=15)

        ngrok_msg = _ngrok_error(resp)
        if ngrok_msg:
            return jsonify({"error": ngrok_msg}), 502

        resp.raise_for_status()
        data = resp.json()
        for m in data.get("models", []):
            m.update(MODEL_DISPLAY.get(m.get("id"), {}))
        return jsonify(data)

    except requests.exceptions.Timeout:
        return jsonify({"error": "Le serveur Opsiom ne répond pas (timeout)."}), 504

    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connexion impossible à Opsiom : {e}")
        return jsonify({"error": "Impossible de joindre le serveur Opsiom (PC éteint ou hors ligne ?)."}), 502

    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else 500
        logger.error(f"Erreur HTTP {status_code} sur /models : {getattr(e.response, 'text', '')[:300]}")
        return jsonify({"error": f"Opsiom a renvoyé une erreur ({status_code})."}), 502

    except ValueError:
        logger.exception("Réponse /models illisible (pas du JSON valide)")
        return jsonify({"error": "Réponse d'Opsiom illisible."}), 502

    except Exception as e:
        logger.exception("Erreur inattendue en appelant /models")
        return jsonify({"error": f"Erreur inattendue côté serveur : {e}"}), 500


@app.post("/chat")
@login_required
def chat():
    """Proxy vers POST {OPSIOM_API_URL}/chat.

    Chaque compte a droit à DAILY_TOKEN_QUOTA tokens par jour, un quota tenu
    par Octix et PARTAGÉ avec le CLI (voir fetch_quota_status/consume_quota
    ci-dessus) — ce n'est plus une table locale à ce front. Le quota est
    vérifié avant d'appeler l'API distante (pour ne rien consommer côté PC
    inutilement) et décompté seulement si la réponse revient avec succès —
    un message qui échoue (timeout, PC éteint...) n'est jamais compté."""
    quota = fetch_quota_status()
    if quota.get("remaining", DAILY_TOKEN_QUOTA) <= 0:
        return jsonify({
            "error": f"Quota gratuit atteint ({quota.get('limit', DAILY_TOKEN_QUOTA)} tokens/jour). Réessaie demain.",
            "quota": quota,
        }), 429

    payload = request.get_json(silent=True) or {}
    message = (payload.get("message") or "").strip()

    if not message:
        return jsonify({"error": "Message vide."}), 400
    if len(message) > MAX_MESSAGE_CHARS:
        return jsonify({"error": f"Message trop long ({MAX_MESSAGE_CHARS} caractères max)."}), 413

    # Identifiant du modèle choisi dans le sélecteur ("nano", "small", "large").
    # Le serveur valide lui-même la valeur et renvoie une 400 si elle est inconnue.
    model_id = payload.get("model") or DEFAULT_MODEL_ID
    if not isinstance(model_id, str) or len(model_id) > 32:
        return jsonify({"error": "Identifiant de modèle invalide."}), 400

    body = {
        "message": message,
        "model": model_id,
        "max_new_tokens": _clamp(payload.get("max_new_tokens", 200), 200, 1, MAX_NEW_TOKENS_LIMIT, int),
        "temperature": _clamp(payload.get("temperature", 0.7), 0.7, 0.0, 2.0),
        "top_k": _clamp(payload.get("top_k", 40), 40, 0, 200, int),
        "top_p": _clamp(payload.get("top_p", 0.9), 0.9, 0.0, 1.0),
        "repetition_penalty": _clamp(payload.get("repetition_penalty", 1.3), 1.3, 1.0, 2.0),
    }

    t0 = time.time()
    try:
        resp = requests.post(f"{OPSIOM_API_URL}/chat", json=body, headers=_headers(), timeout=REQUEST_TIMEOUT)

        ngrok_msg = _ngrok_error(resp)
        if ngrok_msg:
            return jsonify({"error": ngrok_msg}), 502

        resp.raise_for_status()
        data = resp.json()
        logger.info(f"Réponse Opsiom ({model_id}) obtenue en {time.time() - t0:.1f}s")

        response_text = data.get("response", "")
        # Utilise le décompte du serveur d'inférence s'il en fournit un ;
        # sinon retombe sur la même estimation que le CLI, pour rester
        # cohérent entre les deux consommateurs du même quota.
        tokens_used = data.get("tokens_used") or (estimate_tokens(message) + estimate_tokens(response_text))
        quota_after = consume_quota(tokens_used)

        return jsonify({
            "response": response_text,
            "model": data.get("model", model_id),
            "quota": quota_after,
        })

    except requests.exceptions.Timeout:
        logger.warning(f"Timeout après {REQUEST_TIMEOUT}s sur /chat (modèle {model_id}).")
        return jsonify({
            "error": "Opsiom met trop de temps à répondre (génération lente sur CPU). "
                     "Essaie un modèle plus petit ou un message plus court.",
        }), 504

    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connexion impossible à Opsiom : {e}")
        return jsonify({"error": "Impossible de joindre Opsiom. Le PC est peut-être éteint ou hors ligne."}), 502

    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else 500
        body_txt = e.response.text[:300] if e.response is not None else ""
        logger.error(f"Erreur HTTP {status_code} d'Opsiom : {body_txt}")
        if status_code == 400:
            # Le serveur renvoie {"error": "..."} (message manquant, modèle inconnu…)
            try:
                return jsonify({"error": e.response.json().get("error", "Requête invalide.")}), 400
            except ValueError:
                return jsonify({"error": "Requête invalide."}), 400
        if status_code == 401:
            return jsonify({"error": "Session expirée ou invalide auprès du serveur Opsiom. Reconnecte-toi."}), 502
        if status_code == 413:
            return jsonify({"error": "Message rejeté par Opsiom (trop long côté API)."}), 413
        return jsonify({"error": f"Opsiom a renvoyé une erreur ({status_code})."}), 502

    except ValueError:
        # .json() a échoué : la réponse n'était pas du JSON valide
        logger.exception("Réponse d'Opsiom illisible (pas du JSON valide)")
        return jsonify({"error": "Réponse d'Opsiom illisible."}), 502

    except Exception as e:  # garde-fou générique
        logger.exception("Erreur inattendue en appelant /chat")
        return jsonify({"error": f"Erreur inattendue côté serveur : {e}"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
