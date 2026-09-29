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
  GET  /             -> templates/index.html
  GET  /progression  -> templates/progression.html (missions & forfaits)
  GET  /api/progression -> version JSON de /progression
  GET  /status    -> proxy de GET  {API}/health
  GET  /models    -> proxy de GET  {API}/models   (pour construire le sélecteur)
  POST /chat      -> proxy de POST {API}/chat     (accepte "message" + "model")
  GET  /promo     -> saisie d'un code de promo (tokens en plus / modèles, 24 h)
  GET  /admin     -> espace administrateur (création des codes), compte "Jules" seulement

Forfaits (voir plans.py) :
  Le quota de tokens/jour, les modèles autorisés et les missions à
  accomplir pour débloquer Plus/Pro sont centralisés dans plans.py. La
  progression est calculée par progression.py, qui lit en lecture seule
  les bases de LearnCode/Classroom/Omnia Mind si les variables
  LEARNCODE_DATABASE_URL / CLASSROOM_DATABASE_URL / OMNIAMIND_DATABASE_URL
  sont configurées (aucune n'est obligatoire : sans elles, les missions
  correspondantes restent simplement à 0/N).

Variables d'environnement :
  OPSIOM_DATABASE_URL - base locale Opsiom (forfait/quota/progression),
                       défaut: sqlite:///opsiom_local.db.
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
  ADMIN_USERNAMES    - pseudos administrateurs, séparés par des virgules
                       (défaut: Jules). Accès illimité + espace /admin.
  DAILY_TOKEN_QUOTA  - défaut: 2000. Doit rester identique à la valeur
                       configurée côté Octix (DAILY_TOKEN_QUOTA) : cette
                       variable ne sert ici qu'à afficher un quota par
                       défaut cohérent si Octix est injoignable, le
                       décompte qui fait foi est toujours celui d'Octix.

Quota de tokens :
  Le quota gratuit (2000 tokens/jour par défaut) n'est PLUS suivi dans une
  base locale à ce front : il est délégué à Octix (voir /account/quota et
  /account/quota/consume côté Octix_API), le même service qui gère les
  comptes. C'est ce qui permet de PARTAGER un seul et même quota entre
  cette interface web (identifiée par le token de session Octix) et le CLI
  opsiom (identifié par une clé API Octix) : les deux résolvent le même
  compte, donc décomptent le même compteur -- régénérer sa clé API, ou en
  créer une par appareil, ne redonne donc plus de tokens gratuits.
"""
import json
import logging
import os
import time

import requests
from flask import Flask, Response, jsonify, render_template, request, session, stream_with_context

from auth import OCTIX_PORTAL_URL, OCTIX_URL, auth_bp, current_username, login_required
import plans
from models import db, get_or_create_user
from progression import refresh_user_progression
from promo import promo_bp

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
app.register_blueprint(promo_bp)   # /promo (codes de promo) et /admin (création des codes)

# Le cookie de session n'est pas envoyé lors d'une requête déclenchée depuis un
# autre site (protection de base contre le CSRF, en plus du jeton des formulaires).
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# --- Base locale : forfaits, quota par forfait, progression -------------
# Octix ne connaît pas la notion de forfait (voir plans.py) : c'est donc ici,
# dans la base locale à Opsiom, que vit le compteur qui FAIT FOI pour le web.
# Octix reste appelé en best-effort (fetch_quota_status/consume_quota) pour
# que le CLI garde un ordre de grandeur cohérent, mais ne bloque plus rien :
# tant qu'Octix n'a pas lui-même la notion de forfait, un compte Plus/Pro
# dont le quota dépasserait la limite fixe d'Octix serait sinon bloqué à
# tort côté CLI. À synchroniser côté Octix quand ce sera possible.
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("OPSIOM_DATABASE_URL", "sqlite:///opsiom_local.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)
with app.app_context():
    db.create_all()


def current_user():
    """Ombre locale (forfait + quota) du compte Octix connecté. Suppose
    @login_required déjà passé (current_username() non None)."""
    return get_or_create_user(current_username())


# Conservée uniquement comme valeur d'affichage par défaut si Octix est
# injoignable pour le décompte "best-effort" décrit ci-dessus.
DAILY_TOKEN_QUOTA = int(os.environ.get("DAILY_TOKEN_QUOTA", "2000"))


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

# Noms affichés dans le sélecteur : définis dans plans.py (MODEL_DISPLAY), car
# les codes de promo doivent aussi pouvoir proposer ces modèles.
MODEL_DISPLAY = plans.MODEL_DISPLAY

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
    user = current_user()
    progress = refresh_user_progression(user)
    grants = user.active_grants()
    return render_template(
        "index.html",
        username=current_username(),
        is_admin=user.is_admin,
        promo_until=grants[0].expires_at if grants else None,
        quota=user.quota_status(),
        plan=plans.plan_config(user.plan),
        plan_id=user.plan,
        lab=user.lab,
        next_plan=plans.plan_config(progress["next_plan"]) if progress["next_plan"] else None,
        octix_portal_url=OCTIX_PORTAL_URL,
    )


@app.get("/progression")
@login_required
def progression_page():
    user = current_user()
    progress = refresh_user_progression(user)
    plans_view = []
    for plan_id in plans.PLAN_ORDER[1:]:
        missions = plans.evaluate_missions(plan_id, progress["raw_stats"])
        plans_view.append({
            "id": plan_id,
            "config": plans.plan_config(plan_id),
            "missions": missions,
            "done_count": sum(1 for m in missions if m["done"]),
            "total_count": len(missions),
            "unlocked": plans.plan_rank(user.plan) >= plans.plan_rank(plan_id),
        })
    return render_template(
        "progression.html",
        username=current_username(),
        plan_id=user.plan,
        current_plan=plans.plan_config(user.plan),
        plans_view=plans_view,
        lab=user.lab,
        lab_label=plans.LAB_LABEL,
        lab_emoji=plans.LAB_EMOJI,
        lab_description=plans.LAB_DESCRIPTION,
        octix_portal_url=OCTIX_PORTAL_URL,
    )


@app.get("/api/progression")
@login_required
def api_progression():
    """Version JSON de /progression, utilisée pour un rafraîchissement sans
    recharger la page (bouton 'Actualiser ma progression')."""
    user = current_user()
    force = request.args.get("force") == "1"
    progress = refresh_user_progression(user, force=force)
    return jsonify({
        "plan": user.plan,
        "upgraded": progress["upgraded"],
        "next_plan": progress["next_plan"],
        "missions": progress["missions"],
        "quota": user.quota_status(),
    })


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
        # Quota LOCAL (forfait + codes de promo) : c'est lui qui autorise ou
        # bloque /chat. Renvoyer ici celui d'Octix (autre compteur, autre
        # limite) faisait afficher un quota faux — d'où un bandeau « quota
        # épuisé » qui apparaissait alors qu'il restait des tokens.
        return jsonify({**data, "online": True, "quota": current_user().quota_status()})

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
        allowed = set(current_user().allowed_models)
        for m in data.get("models", []):
            m.update(MODEL_DISPLAY.get(m.get("id"), {}))
            # Le modèle reste listé (pour donner envie de débloquer le
            # forfait qui y donne accès) mais marqué "locked" : /chat et
            # /chat/stream refusent de toute façon un modèle non autorisé,
            # ce marquage ne sert qu'à l'affichage (cadenas, etc.).
            m["locked"] = m.get("id") not in allowed
        if data.get("default") not in allowed and allowed:
            data["default"] = next(iter(allowed))
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

    Chaque compte a droit à un nombre de tokens/jour qui dépend de son
    FORFAIT (voir plans.py) : c'est le compteur local (models.py, table
    users) qui fait foi ici. Octix est aussi notifié en best-effort (pour
    rester à peu près cohérent avec le CLI) mais ne bloque plus rien. Le
    quota est vérifié avant d'appeler l'API distante (pour ne rien
    consommer côté PC inutilement) et décompté seulement si la réponse
    revient avec succès — un message qui échoue (timeout, PC éteint...)
    n'est jamais compté."""
    user = current_user()
    quota = user.quota_status()
    if not user.can_send_message():
        return jsonify({
            "error": f"Quota {plans.plan_config(user.plan)['label']} atteint ({quota['limit']} tokens/jour). "
                     f"Réessaie demain, débloque un forfait supérieur (voir /progression) ou utilise un code de promo.",
            "quota": quota,
        }), 429

    payload = request.get_json(silent=True) or {}
    message = (payload.get("message") or "").strip()

    if not message:
        return jsonify({"error": "Message vide."}), 400
    if len(message) > MAX_MESSAGE_CHARS:
        return jsonify({"error": f"Message trop long ({MAX_MESSAGE_CHARS} caractères max)."}), 413

    # Identifiant du modèle choisi dans le sélecteur ("nano", "small", "large").
    # Le serveur valide lui-même la valeur et renvoie une 400 si elle est inconnue,
    # mais on vérifie déjà ici que le forfait du compte y donne droit.
    model_id = payload.get("model") or DEFAULT_MODEL_ID
    if not isinstance(model_id, str) or len(model_id) > 32:
        return jsonify({"error": "Identifiant de modèle invalide."}), 400
    if model_id not in user.allowed_models:
        return jsonify({
            "error": f"Le modèle « {model_id} » n'est pas inclus dans {plans.plan_config(user.plan)['label']}.",
        }), 403

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
        user.register_message_sent(tokens_used)
        consume_quota(tokens_used)  # best-effort, voir commentaire en tête de fichier
        quota_after = user.quota_status()

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


@app.post("/chat/stream")
@login_required
def chat_stream():
    """Proxy vers POST {OPSIOM_API_URL}/chat/stream : relaie le flux SSE
    (token par token) tel quel au navigateur, pour l'affichage progressif.

    Mêmes règles de quota que /chat (vérifié avant l'appel), mais le
    décompte réel ne peut se faire qu'une fois le dernier évènement SSE
    reçu (c'est lui qui porte le nombre de tokens réellement générés) :
    on intercepte donc cet évènement 'done' au passage pour y injecter le
    statut de quota à jour, avant de le transmettre au navigateur."""
    user = current_user()
    quota = user.quota_status()
    if not user.can_send_message():
        return jsonify({
            "error": f"Quota {plans.plan_config(user.plan)['label']} atteint ({quota['limit']} tokens/jour). "
                     f"Réessaie demain, débloque un forfait supérieur (voir /progression) ou utilise un code de promo.",
            "quota": quota,
        }), 429

    payload = request.get_json(silent=True) or {}
    message = (payload.get("message") or "").strip()

    if not message:
        return jsonify({"error": "Message vide."}), 400
    if len(message) > MAX_MESSAGE_CHARS:
        return jsonify({"error": f"Message trop long ({MAX_MESSAGE_CHARS} caractères max)."}), 413

    model_id = payload.get("model") or DEFAULT_MODEL_ID
    if not isinstance(model_id, str) or len(model_id) > 32:
        return jsonify({"error": "Identifiant de modèle invalide."}), 400
    if model_id not in user.allowed_models:
        return jsonify({
            "error": f"Le modèle « {model_id} » n'est pas inclus dans {plans.plan_config(user.plan)['label']}.",
        }), 403

    body = {
        "message": message,
        "model": model_id,
        "max_new_tokens": _clamp(payload.get("max_new_tokens", 200), 200, 1, MAX_NEW_TOKENS_LIMIT, int),
        "temperature": _clamp(payload.get("temperature", 0.7), 0.7, 0.0, 2.0),
        "top_k": _clamp(payload.get("top_k", 40), 40, 0, 200, int),
        "top_p": _clamp(payload.get("top_p", 0.9), 0.9, 0.0, 1.0),
        "repetition_penalty": _clamp(payload.get("repetition_penalty", 1.3), 1.3, 1.0, 2.0),
    }

    try:
        upstream = requests.post(
            f"{OPSIOM_API_URL}/chat/stream",
            json=body, headers=_headers(),
            timeout=REQUEST_TIMEOUT, stream=True,
        )
    except requests.exceptions.Timeout:
        logger.warning(f"Timeout après {REQUEST_TIMEOUT}s sur /chat/stream (modèle {model_id}).")
        return jsonify({
            "error": "Opsiom met trop de temps à répondre (génération lente sur CPU). "
                     "Essaie un modèle plus petit ou un message plus court.",
        }), 504
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connexion impossible à Opsiom : {e}")
        return jsonify({"error": "Impossible de joindre Opsiom. Le PC est peut-être éteint ou hors ligne."}), 502

    ngrok_msg = _ngrok_error(upstream)
    if ngrok_msg:
        upstream.close()
        return jsonify({"error": ngrok_msg}), 502

    if upstream.status_code >= 400:
        try:
            data = upstream.json()
            error_msg = data.get("error", "Requête invalide.")
        except ValueError:
            error_msg = f"Opsiom a renvoyé une erreur ({upstream.status_code})."
        upstream.close()
        return jsonify({"error": error_msg}), upstream.status_code if upstream.status_code == 429 else 502

    t0 = time.time()

    def relay():
        try:
            for line in upstream.iter_lines(decode_unicode=True):
                if line is None:
                    continue
                if not line:
                    # Ligne vide = séparateur d'évènement SSE, à préserver telle quelle.
                    yield "\n"
                    continue
                if line.startswith("data: "):
                    try:
                        event = json.loads(line[len("data: "):])
                    except ValueError:
                        event = None
                    if isinstance(event, dict) and event.get("done"):
                        tokens_used = event.get("tokens_used") or 0
                        user.register_message_sent(tokens_used)
                        consume_quota(tokens_used)  # best-effort, voir commentaire en tête de fichier
                        event["quota"] = user.quota_status()
                        logger.info(
                            f"Réponse Opsiom (stream, {model_id}) obtenue en {time.time() - t0:.1f}s"
                        )
                        yield f"data: {json.dumps(event)}\n"
                        continue
                yield line + "\n"
        finally:
            upstream.close()

    return Response(stream_with_context(relay()), mimetype="text/event-stream")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
