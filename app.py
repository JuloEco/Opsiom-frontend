"""
Interface front-end pour Opsiom — Flask, indépendant du rendu Gradio.

Ce serveur ne fait JAMAIS d'appel direct depuis le navigateur vers le Space
Hugging Face : tout passe par ce proxy, pour deux raisons :
  1. Ne jamais exposer OPSIOM_API_KEY côté client (JS visible par tout le monde).
  2. Centraliser la gestion d'erreurs / logs (cold start ZeroGPU, Space en
     pause, timeout, etc.) à un seul endroit.

Variables d'environnement (à définir dans Render > Environment) :
  OPSIOM_API_URL   - défaut: https://juloeco-opsiom.hf.space/api
  OPSIOM_API_KEY   - optionnel, si tu as ajouté une auth Bearer côté Space
  OPSIOM_TIMEOUT   - défaut: 90 (secondes) — large car ZeroGPU peut avoir un
                     cold start de plusieurs dizaines de secondes
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

OPSIOM_API_URL = os.environ.get("OPSIOM_API_URL", "https://juloeco-opsiom.hf.space/api").rstrip("/")
OPSIOM_API_KEY = os.environ.get("OPSIOM_API_KEY", "").strip()
REQUEST_TIMEOUT = int(os.environ.get("OPSIOM_TIMEOUT", "90"))


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if OPSIOM_API_KEY:
        headers["Authorization"] = f"Bearer {OPSIOM_API_KEY}"
    return headers


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/status")
def status():
    """Interroge GET {OPSIOM_API_URL}/health pour afficher un vrai statut
    dans la sidebar (pas un badge 'En ligne' codé en dur côté front)."""
    try:
        resp = requests.get(f"{OPSIOM_API_URL}/health", headers=_headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return jsonify({"online": True, **data})

    except requests.exceptions.Timeout:
        logger.warning("Timeout sur /health — Opsiom est probablement en cold start ZeroGPU.")
        return jsonify({
            "online": False, "error": "timeout",
            "message": "Le modèle met du temps à démarrer (cold start ZeroGPU).",
        }), 200

    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connexion impossible à Opsiom : {e}")
        return jsonify({
            "online": False, "error": "connection",
            "message": "Impossible de joindre Opsiom (Space en pause ou hors ligne ?).",
        }), 200

    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else "?"
        logger.error(f"Erreur HTTP {status_code} sur /health : {getattr(e.response, 'text', '')[:300]}")
        return jsonify({
            "online": False, "error": "http",
            "message": f"Opsiom a répondu une erreur ({status_code}).",
        }), 200

    except Exception as e:  # garde-fou générique — ne doit jamais faire planter la sidebar
        logger.exception("Erreur inattendue en interrogeant /health")
        return jsonify({"online": False, "error": "unknown", "message": str(e)}), 200


@app.post("/chat")
def chat():
    """Proxy vers POST {OPSIOM_API_URL}/chat."""
    payload = request.get_json(silent=True) or {}
    message = (payload.get("message") or "").strip()

    if not message:
        return jsonify({"error": "Message vide."}), 400
    if len(message) > 2000:
        return jsonify({"error": "Message trop long (2000 caractères max)."}), 413

    body = {
        "message": message,
        "max_new_tokens": payload.get("max_new_tokens", 200),
        "temperature": payload.get("temperature", 0.7),
        "top_k": payload.get("top_k", 40),
        "top_p": payload.get("top_p", 0.9),
        "repetition_penalty": payload.get("repetition_penalty", 1.3),
    }

    t0 = time.time()
    try:
        resp = requests.post(f"{OPSIOM_API_URL}/chat", json=body, headers=_headers(), timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"Réponse Opsiom obtenue en {time.time() - t0:.1f}s")
        return jsonify({"response": data.get("response", "")})

    except requests.exceptions.Timeout:
        logger.warning(f"Timeout après {REQUEST_TIMEOUT}s sur /chat (cold start ZeroGPU ou génération longue).")
        return jsonify({
            "error": "Opsiom met trop de temps à répondre (cold start ZeroGPU probable). Réessaie dans quelques secondes.",
        }), 504

    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connexion impossible à Opsiom : {e}")
        return jsonify({"error": "Impossible de joindre Opsiom. Le Space est peut-être en pause ou hors ligne."}), 502

    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else 500
        body_txt = e.response.text[:300] if e.response is not None else ""
        logger.error(f"Erreur HTTP {status_code} d'Opsiom : {body_txt}")
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
