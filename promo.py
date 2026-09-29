"""
promo.py — Codes de promo (page utilisateur) et espace administrateur.

Deux parties dans ce module :

  1. Page /promo, ouverte à tout compte connecté : on y saisit un code de
     promo pour obtenir, PENDANT UNE DURÉE LIMITÉE (24 h par défaut), plus de
     tokens par jour et/ou l'accès à des modèles normalement réservés à un
     forfait supérieur. Rien n'est définitif : à l'expiration, le compte
     retrouve les limites de son forfait.

  2. Espace /admin, réservé aux administrateurs (plans.ADMIN_USERNAMES, par
     défaut le compte "Jules") : création, activation/désactivation et
     suppression des codes. L'admin choisit pour chaque code le nombre de
     tokens en plus, les modèles débloqués, la durée, le nombre maximal
     d'utilisations et une éventuelle date limite d'utilisation.

Sécurité :
  - toutes les actions en POST sont protégées par un jeton CSRF (session) ;
  - les tentatives de code invalides sont limitées par compte (anti-devinette) ;
  - /admin répond 404 aux comptes non administrateurs (l'espace ne se révèle
    pas), et chaque route admin revérifie le droit côté serveur.
"""
import json
import re
import secrets
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

import plans
from auth import current_username, login_required
from models import PromoCode, PromoRedemption, db, get_or_create_user

promo_bp = Blueprint("promo", __name__)

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # sans O/0/I/1 : lisible à voix haute
CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9-]{2,38}[A-Z0-9]$")
DEFAULT_DURATION_HOURS = 24
MAX_DURATION_HOURS = 24 * 30
MAX_BONUS_TOKENS = 1_000_000

# Anti-devinette : au plus 5 codes invalides par compte sur 10 minutes.
# Stocké en mémoire du process (suffisant ici : un seul worker gunicorn).
_FAIL_WINDOW_SECONDS = 600
_FAIL_LIMIT = 5
_failed_attempts: dict[str, list[float]] = {}


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------
def _csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        session["csrf"] = token
    return token


def _check_csrf():
    sent = request.form.get("csrf_token", "")
    expected = session.get("csrf", "")
    if not sent or not expected or not secrets.compare_digest(sent, expected):
        abort(400, "Jeton de sécurité invalide : recharge la page et réessaie.")


@promo_bp.app_context_processor
def _inject_template_helpers():
    def paris_time(dt: datetime | None, fmt: str = "%d/%m %H:%M") -> str:
        """Affiche une date UTC (stockée en base) à l'heure de Paris."""
        if dt is None:
            return "—"
        try:
            from zoneinfo import ZoneInfo
            from datetime import timezone
            dt = dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Europe/Paris"))
        except Exception:  # tzdata absent : on affiche l'UTC plutôt que de planter
            fmt += " UTC"
        return dt.strftime(fmt)

    return {"csrf_token": _csrf_token, "paris_time": paris_time}


def normalize_code(raw: str) -> str:
    """'  opsi 7k2m-xx9p ' -> 'OPSI7K2M-XX9P' : insensible à la casse et aux espaces."""
    return re.sub(r"\s+", "", (raw or "")).upper()


def generate_code() -> str:
    while True:
        code = "OPSI-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(4)) \
               + "-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
        if not PromoCode.query.filter_by(code=code).first():
            return code


def _too_many_failures(username: str) -> bool:
    now = time.time()
    recent = [t for t in _failed_attempts.get(username, []) if now - t < _FAIL_WINDOW_SECONDS]
    _failed_attempts[username] = recent
    return len(recent) >= _FAIL_LIMIT


def _register_failure(username: str):
    _failed_attempts.setdefault(username, []).append(time.time())


def admin_required(view_func):
    """Comme login_required, mais 404 pour tout compte non administrateur."""
    @wraps(view_func)
    @login_required
    def wrapped(*args, **kwargs):
        if not get_or_create_user(current_username()).is_admin:
            abort(404)
        return view_func(*args, **kwargs)
    return wrapped


def describe_promo(promo: PromoCode) -> str:
    """Résumé lisible des avantages d'un code, ex: '+3 000 tokens/jour · Opsiom Large'."""
    parts = []
    if promo.bonus_tokens:
        parts.append(f"+{promo.bonus_tokens:,} tokens/jour".replace(",", " "))
    if promo.models:
        parts.append(" + ".join(plans.MODEL_DISPLAY[m]["label"] for m in promo.models))
    return " · ".join(parts) or "Aucun avantage"


# ---------------------------------------------------------------------------
# Page utilisateur : /promo
# ---------------------------------------------------------------------------
@promo_bp.get("/promo")
@login_required
def promo_page():
    user = get_or_create_user(current_username())
    grants = [
        {
            "summary": describe_promo(g.promo),
            "expires_at": g.expires_at,
        }
        for g in user.active_grants()
    ]
    return render_template(
        "promo.html",
        username=user.username,
        is_admin=user.is_admin,
        grants=grants,
    )


@promo_bp.post("/promo/redeem")
@login_required
def promo_redeem():
    _check_csrf()
    user = get_or_create_user(current_username())

    if user.is_admin:
        flash("Ton compte administrateur n'a déjà aucune limite : pas besoin de code.", "info")
        return redirect(url_for("promo.promo_page"))

    if _too_many_failures(user.username):
        flash("Trop de tentatives invalides. Réessaie dans quelques minutes.", "error")
        return redirect(url_for("promo.promo_page"))

    code = normalize_code(request.form.get("code"))
    promo = PromoCode.query.filter_by(code=code).first() if code else None

    # Message volontairement identique pour « inconnu » et « désactivé » côté
    # devinette ; les raisons précises (expiré, épuisé) ne sont données qu'une
    # fois le code réellement trouvé.
    if promo is None:
        _register_failure(user.username)
        flash("Code inconnu. Vérifie la saisie (majuscules et tirets sans importance).", "error")
        return redirect(url_for("promo.promo_page"))

    error = promo.redeemable_error()
    if error:
        flash(error, "error")
        return redirect(url_for("promo.promo_page"))

    already = PromoRedemption.query.filter_by(user_id=user.id, promo_id=promo.id).first()
    if already:
        flash("Tu as déjà utilisé ce code.", "error")
        return redirect(url_for("promo.promo_page"))

    redemption = PromoRedemption(
        user_id=user.id,
        promo_id=promo.id,
        expires_at=datetime.utcnow() + timedelta(hours=promo.duration_hours),
    )
    db.session.add(redemption)
    db.session.commit()
    flash(f"Code activé : {describe_promo(promo)}, jusqu'à la fin de la période de validité.", "success")
    return redirect(url_for("promo.promo_page"))


# ---------------------------------------------------------------------------
# Espace administrateur : /admin
# ---------------------------------------------------------------------------
@promo_bp.get("/admin")
@admin_required
def admin_page():
    codes = PromoCode.query.order_by(PromoCode.created_at.desc()).all()
    now = datetime.utcnow()
    codes_view = []
    for c in codes:
        active_uses = c.redemptions.filter(PromoRedemption.expires_at > now).count()
        codes_view.append({
            "obj": c,
            "summary": describe_promo(c),
            "uses": c.uses_count,
            "active_uses": active_uses,
            "error": c.redeemable_error(),
        })
    return render_template(
        "admin.html",
        username=current_username(),
        codes=codes_view,
        model_choices=plans.MODEL_DISPLAY,
        default_duration=DEFAULT_DURATION_HOURS,
        max_duration=MAX_DURATION_HOURS,
        admin_names=sorted(plans.ADMIN_USERNAMES),
    )


def _parse_int(raw: str | None, low: int, high: int) -> int | None:
    raw = (raw or "").strip().replace(" ", "")
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("nombre invalide")
    if not low <= value <= high:
        raise ValueError(f"valeur hors limites ({low}–{high})")
    return value


@promo_bp.post("/admin/codes")
@admin_required
def admin_create_code():
    _check_csrf()
    form = request.form
    try:
        bonus = _parse_int(form.get("bonus_tokens"), 0, MAX_BONUS_TOKENS) or 0
        duration = _parse_int(form.get("duration_hours"), 1, MAX_DURATION_HOURS) or DEFAULT_DURATION_HOURS
        max_uses = _parse_int(form.get("max_uses"), 1, 1_000_000)
    except ValueError as exc:
        flash(f"Code non créé : {exc}.", "error")
        return redirect(url_for("promo.admin_page"))

    models = [m for m in form.getlist("models") if m in plans.MODEL_DISPLAY]
    if bonus <= 0 and not models:
        flash("Code non créé : choisis au moins des tokens en plus ou un modèle à débloquer.", "error")
        return redirect(url_for("promo.admin_page"))

    valid_until = None
    raw_until = (form.get("valid_until") or "").strip()
    if raw_until:
        try:
            # Fin de la journée choisie (heure serveur = UTC), incluse.
            valid_until = datetime.strptime(raw_until, "%Y-%m-%d") + timedelta(days=1)
        except ValueError:
            flash("Code non créé : date limite invalide.", "error")
            return redirect(url_for("promo.admin_page"))

    custom = normalize_code(form.get("code"))
    if custom:
        if not CODE_PATTERN.match(custom):
            flash("Code non créé : 4 à 40 caractères, lettres/chiffres et tirets uniquement.", "error")
            return redirect(url_for("promo.admin_page"))
        if PromoCode.query.filter_by(code=custom).first():
            flash("Code non créé : ce code existe déjà.", "error")
            return redirect(url_for("promo.admin_page"))
        code = custom
    else:
        code = generate_code()

    promo = PromoCode(
        code=code,
        note=(form.get("note") or "").strip()[:120] or None,
        bonus_tokens=bonus,
        models_json=json.dumps(models),
        duration_hours=duration,
        max_uses=max_uses,
        valid_until=valid_until,
        created_by=current_username(),
    )
    db.session.add(promo)
    db.session.commit()
    flash(f"Code créé : {code}", "success")
    return redirect(url_for("promo.admin_page"))


@promo_bp.post("/admin/codes/<int:code_id>/toggle")
@admin_required
def admin_toggle_code(code_id: int):
    _check_csrf()
    promo = db.session.get(PromoCode, code_id)
    if promo is None:
        abort(404)
    promo.active = not promo.active
    db.session.commit()
    flash(f"Code {promo.code} {'réactivé' if promo.active else 'désactivé'}.", "info")
    return redirect(url_for("promo.admin_page"))


@promo_bp.post("/admin/codes/<int:code_id>/delete")
@admin_required
def admin_delete_code(code_id: int):
    """Supprime le code ET les avantages déjà accordés avec (à utiliser pour
    couper net un code ; pour simplement empêcher de nouvelles utilisations,
    préférer « Désactiver »)."""
    _check_csrf()
    promo = db.session.get(PromoCode, code_id)
    if promo is None:
        abort(404)
    code = promo.code
    db.session.delete(promo)
    db.session.commit()
    flash(f"Code {code} supprimé.", "info")
    return redirect(url_for("promo.admin_page"))
