"""
progression.py — Calcule la progression d'un compte dans l'écosystème
(LearnCode, Classroom, Omnia Mind, Opsiom) pour déterminer son forfait.

Constat important fait en inspectant les 4 dépôts avant d'écrire ce fichier :
contrairement à ce qu'on pensait, LearnCode, Classroom et Omnia Mind n'ont
PAS une base de données commune — chacun a la sienne (LearnCode : Postgres
en JSONB ; Classroom : SQLite locale ; Omnia Mind : Postgres/SQLite selon
DATABASE_URL). Le SEUL point commun aux quatre apps est l'authentification
Octix (même pseudo partout).

Ce module se connecte donc, en LECTURE SEULE, à chacune des trois bases
d'apprentissage (si leur URL est configurée) pour lire ce qui existe déjà,
sans jamais rien y écrire ni dupliquer leur logique de validation. Si une
base n'est pas joignable/configurée, sa mission reste simplement "non
vérifiable" (progression à 0) plutôt que de faire planter Opsiom.

Variables d'environnement à ajouter (aucune n'est requise pour qu'Opsiom
fonctionne : sans elles, les missions correspondantes restent à 0/N) :
  LEARNCODE_DATABASE_URL   - URL Postgres de LearnCode (lecture seule idéalement)
  CLASSROOM_DATABASE_URL   - URL de la base Classroom (Postgres ou sqlite:///chemin)
  OMNIAMIND_DATABASE_URL   - URL de la base Omnia Mind (Postgres ou sqlite)

Recommandation : créer un rôle Postgres en lecture seule (GRANT SELECT
uniquement) pour ces trois variables plutôt que de réutiliser le compte
applicatif complet de chaque service.
"""
import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger("opsiom_frontend.progression")

LEARNCODE_DATABASE_URL = os.environ.get("LEARNCODE_DATABASE_URL", "")
CLASSROOM_DATABASE_URL = os.environ.get("CLASSROOM_DATABASE_URL", "")
OMNIAMIND_DATABASE_URL = os.environ.get("OMNIAMIND_DATABASE_URL", "")

# Durée de vie du cache de progression avant recalcul (voir models.py /
# User.progression_updated_at). Volontairement pas "temps réel" : ces
# requêtes traversent trois bases externes, pas la peine de le faire à
# chaque message de chat.
PROGRESSION_CACHE_TTL = timedelta(minutes=15)


def _empty_stats() -> dict:
    return {
        "learncode": {"lessons_completed": 0, "lessons_passed": 0, "reachable": False},
        "classroom": {"activities_graded": 0, "activities_passed": 0, "reachable": False},
        "omniamind": {"challenges_passed": 0, "study_sessions": 0, "reachable": False},
    }


# ---------------------------------------------------------------------------
# LearnCode — store JSONB "users" (id=pseudo, data={"score":.., "notes":{...}})
# ---------------------------------------------------------------------------
def _fetch_learncode_stats(username: str) -> dict:
    stats = {"lessons_completed": 0, "lessons_passed": 0, "reachable": False}
    if not LEARNCODE_DATABASE_URL:
        return stats
    try:
        import psycopg2

        url = LEARNCODE_DATABASE_URL
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        conn = psycopg2.connect(url, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM users WHERE id = %s", (username,))
                row = cur.fetchone()
        finally:
            conn.close()
        stats["reachable"] = True
        if row and row[0]:
            notes = (row[0] or {}).get("notes", {}) or {}
            stats["lessons_completed"] = len(notes)
            stats["lessons_passed"] = sum(1 for note in notes.values() if (note or 0) >= 50)
    except Exception:
        logger.warning("LearnCode injoignable pour le calcul de progression.", exc_info=True)
    return stats


# ---------------------------------------------------------------------------
# Classroom — SQLAlchemy "user" / "submission" (grade sur 20)
# ---------------------------------------------------------------------------
def _fetch_classroom_stats(username: str) -> dict:
    stats = {"activities_graded": 0, "activities_passed": 0, "reachable": False}
    if not CLASSROOM_DATABASE_URL:
        return stats
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(CLASSROOM_DATABASE_URL, connect_args={"connect_timeout": 5}
                                if CLASSROOM_DATABASE_URL.startswith("postgresql") else {})
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT s.grade FROM submission s "
                    "JOIN \"user\" u ON u.id = s.student_id "
                    "WHERE u.username = :username AND s.grade IS NOT NULL"
                ),
                {"username": username},
            ).fetchall()
        stats["reachable"] = True
        grades = [r[0] for r in row]
        stats["activities_graded"] = len(grades)
        stats["activities_passed"] = sum(1 for g in grades if (g or 0) >= 10)
    except Exception:
        logger.warning("Classroom injoignable pour le calcul de progression.", exc_info=True)
    return stats


# ---------------------------------------------------------------------------
# Omnia Mind — SQLAlchemy "omniamind_users" / "omniamind_study_logs"
# ---------------------------------------------------------------------------
def _fetch_omniamind_stats(username: str) -> dict:
    stats = {"challenges_passed": 0, "study_sessions": 0, "reachable": False}
    if not OMNIAMIND_DATABASE_URL:
        return stats
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(OMNIAMIND_DATABASE_URL, connect_args={"connect_timeout": 5}
                                if OMNIAMIND_DATABASE_URL.startswith("postgresql") else {})
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT l.mode, l.score FROM omniamind_study_logs l "
                    "JOIN omniamind_users u ON u.id = l.user_id "
                    "WHERE u.octix_username = :username"
                ),
                {"username": username},
            ).fetchall()
        stats["reachable"] = True
        stats["study_sessions"] = len(row)
        # "Défi" = une session quiz/match/swipe réussie à au moins 70%.
        stats["challenges_passed"] = sum(
            1 for mode, score in row if mode in ("quiz", "match", "swipe") and (score or 0) >= 70
        )
    except Exception:
        logger.warning("Omnia Mind injoignable pour le calcul de progression.", exc_info=True)
    return stats


# ---------------------------------------------------------------------------
# Opsiom lui-même — jours actifs / conversations, tenus localement
# (voir models.py: User.active_days_json et User.conversations_total).
# ---------------------------------------------------------------------------
def gather_raw_stats(username: str, opsiom_stats: dict) -> dict:
    """Assemble les stats des 4 sources. `opsiom_stats` vient de l'appelant
    (déjà en base locale Opsiom, pas besoin d'aller le chercher ailleurs)."""
    return {
        "learncode": _fetch_learncode_stats(username),
        "classroom": _fetch_classroom_stats(username),
        "omniamind": _fetch_omniamind_stats(username),
        "opsiom": opsiom_stats,
    }


def refresh_user_progression(user, force: bool = False) -> dict:
    """Point d'entrée utilisé par app.py. Recalcule (ou relit le cache) la
    progression d'un compte, met à jour son forfait si de nouvelles missions
    sont remplies, et renvoie un résumé prêt pour le template :

        {"plan": "plus", "upgraded": False, "raw_stats": {...},
         "missions": {"plus": [...], "pro": [...] ou None}}

    Le forfait ne redescend jamais automatiquement : une mission remplie
    reste acquise même si l'activité cesse ensuite.
    """
    from plans import PLAN_ORDER, compute_unlocked_plan, evaluate_missions, plan_rank

    raw_stats, fresh = (None, False) if force else user.cached_progression()
    if raw_stats is None:
        raw_stats = gather_raw_stats(user.username, user.opsiom_mission_stats())
        user.store_progression(raw_stats)
    else:
        # Le cache ne contient pas les stats Opsiom les plus récentes
        # (celles-ci changent à chaque message) : on les rafraîchit toujours,
        # c'est une lecture locale gratuite, pas un appel réseau.
        raw_stats["opsiom"] = user.opsiom_mission_stats()

    unlocked = compute_unlocked_plan(raw_stats, lab=user.lab)
    upgraded = plan_rank(unlocked) > plan_rank(user.plan)
    if upgraded:
        from datetime import datetime as _dt
        user.plan = unlocked
        user.plan_updated_at = _dt.utcnow()
        from models import db
        db.session.commit()

    next_plan = None
    idx = plan_rank(user.plan)
    if idx + 1 < len(PLAN_ORDER):
        next_plan = PLAN_ORDER[idx + 1]

    return {
        "plan": user.plan,
        "upgraded": upgraded,
        "raw_stats": raw_stats,
        "next_plan": next_plan,
        "missions": evaluate_missions(next_plan, raw_stats) if next_plan else [],
    }
