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
  OPSIOM_API_URL   - URL du tunnel ngrok, avec ou sans le suffixe "/api"
                     (défaut: https://pursuable-underpaid-boss.ngrok-free.dev)
  OPSIOM_API_KEY   - optionnel, si tu ajoutes une auth Bearer côté serveur
  OPSIOM_TIMEOUT   - défaut: 120 (secondes). L'inférence CPU est lente,
                     surtout avec le modèle 220M.
"""
import logging
import os
import time

import requests
from flask import Flask, jsonify, render_template, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("opsiom_frontend")

app = Flask(__name__)


def _normalize_api_url(raw: str) -> str:
    """Accepte 'https://xxx.ngrok-free.dev' ou 'https://xxx.ngrok-free.dev/api'
    et renvoie toujours une URL qui se termine par '/api'."""
    url = raw.strip().rstrip("/")
    if not url.endswith("/api"):
        url += "/api"
    return url


OPSIOM_API_URL = _normalize_api_url(
    os.environ.get("OPSIOM_API_URL", "")
)
OPSIOM_API_KEY = os.environ.get("OPSIOM_API_KEY", "").strip()
REQUEST_TIMEOUT = int(os.environ.get("OPSIOM_TIMEOUT", "120"))

# Modèle utilisé si le front n'en envoie aucun (le serveur a aussi son défaut).
DEFAULT_MODEL_ID = "small"

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
    if OPSIOM_API_KEY:
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
def index():
    return render_template("index.html")


@app.get("/status")
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
        return jsonify({"online": True, **data})

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
        return jsonify(resp.json())

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
def chat():
    """Proxy vers POST {OPSIOM_API_URL}/chat."""
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
        return jsonify({
            "response": data.get("response", ""),
            "model": data.get("model", model_id),
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
            return jsonify({"error": "Clé API Opsiom invalide ou manquante (OPSIOM_API_KEY)."}), 502
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
