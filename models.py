"""
Modèles de données — comptes Opsiom, forfaits et progression.

Cette base locale à Opsiom ne stocke JAMAIS de mot de passe (voir auth.py,
authentification déléguée à Octix). Elle sert à trois choses :
  1. Le quota de tokens/jour, désormais dépendant du FORFAIT du compte
     (voir plans.py) et appliqué ici — Octix reste interrogé en best-effort
     pour rester synchronisé avec le CLI, mais l'application de la limite
     par forfait vit ici, car Octix ne connaît pas la notion de forfait.
  2. Le forfait courant (free / plus / pro) et le statut Lab.
  3. Le cache de progression (missions LearnCode/Classroom/Omnia Mind),
     recalculé toutes les PROGRESSION_CACHE_TTL (voir progression.py) pour
     éviter d'interroger trois bases externes à chaque page vue.
  4. Les codes de promo (tokens en plus + accès temporaire à des modèles
     supplémentaires, créés depuis l'espace admin — voir promo.py) et leur
     utilisation par compte.

Administrateur : un pseudo listé dans plans.ADMIN_USERNAMES (par défaut
"Jules") n'a aucune limite de tokens ni de modèles.
"""
import json
from datetime import date, datetime

from flask_sqlalchemy import SQLAlchemy

from plans import ADMIN_USERNAMES, DEFAULT_PLAN, MODEL_DISPLAY, UNLIMITED_REMAINING, plan_config

db = SQLAlchemy()


class User(db.Model):
    """Ombre locale d'un compte Octix : pseudo + forfait + quota + progression.

    Aucun mot de passe n'est stocké ici : l'authentification est déléguée à
    Octix (voir auth.py). Cette table associe un pseudo Octix à son forfait
    Opsiom et à son compteur de tokens/jour.
    """
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(32), unique=True, nullable=False, index=True)
    email = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # --- Forfait -----------------------------------------------------
    plan = db.Column(db.String(16), default=DEFAULT_PLAN, nullable=False)
    plan_updated_at = db.Column(db.DateTime, nullable=True)
    # Statut Lab : séparé des forfaits classiques (voir plans.py). Pour
    # l'instant attribué uniquement à la main (pas de critère automatique).
    lab = db.Column(db.Boolean, default=False, nullable=False)

    # --- Quota de tokens, dépendant du forfait ------------------------
    tokens_used_today = db.Column(db.Integer, default=0, nullable=False)
    quota_date = db.Column(db.Date, default=date.today, nullable=False)

    # --- Suivi "jours actifs" / "conversations" pour les missions Opsiom
    # (voir plans.py: opsiom.active_days / opsiom.conversations).
    # active_days_json = liste JSON des dates ISO (YYYY-MM-DD) où le compte
    # a envoyé au moins un message. Suffisant pour compter les jours
    # distincts sans repasser par une table séparée.
    active_days_json = db.Column(db.Text, default="[]", nullable=False)
    conversations_total = db.Column(db.Integer, default=0, nullable=False)

    # --- Cache de progression (missions LearnCode/Classroom/Omnia Mind) --
    progression_json = db.Column(db.Text, nullable=True)
    progression_updated_at = db.Column(db.DateTime, nullable=True)

    # ------------------------------------------------------------------
    # Quota
    # ------------------------------------------------------------------
    def _reset_quota_if_needed(self):
        """Remet le compteur à zéro si on a changé de jour depuis le dernier appel."""
        if self.quota_date != date.today():
            self.quota_date = date.today()
            self.tokens_used_today = 0

    # ------------------------------------------------------------------
    # Administrateur & codes de promo
    # ------------------------------------------------------------------
    @property
    def is_admin(self) -> bool:
        return self.username in ADMIN_USERNAMES

    def active_grants(self) -> list["PromoRedemption"]:
        """Codes de promo utilisés par ce compte et encore en cours de validité
        (les avantages sont TEMPORAIRES : ils disparaissent à `expires_at`)."""
        if self.id is None:
            return []
        return (
            PromoRedemption.query
            .filter(PromoRedemption.user_id == self.id, PromoRedemption.expires_at > datetime.utcnow())
            .order_by(PromoRedemption.expires_at)
            .all()
        )

    @property
    def bonus_tokens(self) -> int:
        """Tokens/jour supplémentaires apportés par les codes actifs."""
        return sum(g.promo.bonus_tokens for g in self.active_grants())

    @property
    def bonus_models(self) -> list[str]:
        extra: list[str] = []
        for g in self.active_grants():
            for model_id in g.promo.models:
                if model_id not in extra:
                    extra.append(model_id)
        return extra

    @property
    def daily_limit(self) -> int:
        return plan_config(self.plan)["daily_tokens"] + self.bonus_tokens

    @property
    def allowed_models(self) -> list[str]:
        if self.is_admin:
            return list(MODEL_DISPLAY)
        allowed = list(plan_config(self.plan)["models"])
        for model_id in self.bonus_models:
            if model_id not in allowed:
                allowed.append(model_id)
        return allowed

    def quota_status(self) -> dict:
        self._reset_quota_if_needed()
        if self.is_admin:
            return {
                "used": self.tokens_used_today,
                "limit": 0,
                "remaining": UNLIMITED_REMAINING,
                "plan": self.plan,
                "unlimited": True,
                "bonus": 0,
            }
        bonus = self.bonus_tokens
        limit = plan_config(self.plan)["daily_tokens"] + bonus
        return {
            "used": self.tokens_used_today,
            "limit": limit,
            "remaining": max(0, limit - self.tokens_used_today),
            "plan": self.plan,
            "unlimited": False,
            "bonus": bonus,
        }

    def can_send_message(self) -> bool:
        if self.is_admin:
            return True
        self._reset_quota_if_needed()
        return self.tokens_used_today < self.daily_limit

    def register_message_sent(self, tokens: int = 1):
        """À appeler une fois la réponse obtenue avec succès (pas avant),
        pour ne jamais décompter un message qui a échoué côté serveur.
        Met aussi à jour les compteurs utilisés par les missions Opsiom
        (jours actifs, nombre de conversations)."""
        self._reset_quota_if_needed()
        self.tokens_used_today += max(0, int(tokens))

        today_iso = date.today().isoformat()
        try:
            days = set(json.loads(self.active_days_json or "[]"))
        except (ValueError, TypeError):
            days = set()
        days.add(today_iso)
        self.active_days_json = json.dumps(sorted(days)[-90:])  # 90 derniers jours suffisent
        self.conversations_total += 1

        db.session.commit()

    # ------------------------------------------------------------------
    # Stats Opsiom utilisées par progression.py pour les missions
    # ------------------------------------------------------------------
    def opsiom_mission_stats(self) -> dict:
        try:
            days = json.loads(self.active_days_json or "[]")
        except (ValueError, TypeError):
            days = []
        return {
            "active_days": len(days),
            "conversations": self.conversations_total,
        }

    # ------------------------------------------------------------------
    # Progression (cache)
    # ------------------------------------------------------------------
    def cached_progression(self):
        """Renvoie (raw_stats, is_fresh) à partir du cache local, ou
        (None, False) si rien n'est encore en cache."""
        if not self.progression_json or not self.progression_updated_at:
            return None, False
        from progression import PROGRESSION_CACHE_TTL
        fresh = (datetime.utcnow() - self.progression_updated_at) < PROGRESSION_CACHE_TTL
        try:
            return json.loads(self.progression_json), fresh
        except (ValueError, TypeError):
            return None, False

    def store_progression(self, raw_stats: dict):
        self.progression_json = json.dumps(raw_stats)
        self.progression_updated_at = datetime.utcnow()
        db.session.commit()

    def __repr__(self):
        return f"<User {self.username} plan={self.plan}>"


class PromoCode(db.Model):
    """Code de promo créé par un administrateur (voir promo.py).

    Un code donne, à qui l'utilise, des tokens/jour EN PLUS et/ou l'accès à des
    modèles supplémentaires, pendant `duration_hours` (24 h par défaut) à
    partir de l'utilisation. Rien n'est définitif : à l'expiration, le compte
    retrouve exactement les limites de son forfait.
    """
    __tablename__ = "promo_codes"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), unique=True, nullable=False, index=True)
    note = db.Column(db.String(120), nullable=True)          # mémo libre, visible seulement de l'admin
    bonus_tokens = db.Column(db.Integer, default=0, nullable=False)
    models_json = db.Column(db.Text, default="[]", nullable=False)   # ex: ["large"]
    duration_hours = db.Column(db.Integer, default=24, nullable=False)
    max_uses = db.Column(db.Integer, nullable=True)           # None = illimité
    valid_until = db.Column(db.DateTime, nullable=True)       # date limite pour UTILISER le code
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_by = db.Column(db.String(32), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    redemptions = db.relationship(
        "PromoRedemption", backref="promo", cascade="all, delete-orphan", lazy="dynamic",
    )

    @property
    def models(self) -> list[str]:
        try:
            value = json.loads(self.models_json or "[]")
        except (ValueError, TypeError):
            return []
        return [m for m in value if m in MODEL_DISPLAY] if isinstance(value, list) else []

    @property
    def uses_count(self) -> int:
        return self.redemptions.count()

    def redeemable_error(self) -> str | None:
        """None si le code peut encore être utilisé, sinon la raison (en français)."""
        if not self.active:
            return "Ce code n'est plus actif."
        if self.valid_until and datetime.utcnow() > self.valid_until:
            return "Ce code a expiré."
        if self.max_uses is not None and self.uses_count >= self.max_uses:
            return "Ce code a atteint son nombre maximal d'utilisations."
        return None


class PromoRedemption(db.Model):
    """Utilisation d'un code de promo par un compte (une fois par compte et par code)."""
    __tablename__ = "promo_redemptions"
    __table_args__ = (db.UniqueConstraint("user_id", "promo_id", name="uq_promo_user"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    promo_id = db.Column(db.Integer, db.ForeignKey("promo_codes.id"), nullable=False, index=True)
    redeemed_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)

    user = db.relationship("User")


def get_or_create_user(username: str) -> User:
    user = User.query.filter_by(username=username).first()
    if user is None:
        user = User(username=username)
        db.session.add(user)
        db.session.commit()
    return user
