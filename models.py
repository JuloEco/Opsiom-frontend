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
"""
import json
from datetime import date, datetime

from flask_sqlalchemy import SQLAlchemy

from plans import DEFAULT_PLAN, plan_config

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

    @property
    def daily_limit(self) -> int:
        return plan_config(self.plan)["daily_tokens"]

    @property
    def allowed_models(self) -> list[str]:
        return plan_config(self.plan)["models"]

    def quota_status(self) -> dict:
        self._reset_quota_if_needed()
        limit = self.daily_limit
        return {
            "used": self.tokens_used_today,
            "limit": limit,
            "remaining": max(0, limit - self.tokens_used_today),
            "plan": self.plan,
        }

    def can_send_message(self) -> bool:
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


def get_or_create_user(username: str) -> User:
    user = User.query.filter_by(username=username).first()
    if user is None:
        user = User(username=username)
        db.session.add(user)
        db.session.commit()
    return user
