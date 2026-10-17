"""Sauvegardes automatiques : configuration, timer systemd, trousseau, rotation.

Ce module n'utilise pas GTK : il est lancé sans fenêtre par `sauvegarde-bureau --auto`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from . import backend, verification
from .i18n import _

HOME = Path.home()
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")
STATE_DIR = verification.STATE_DIR
CONFIG = CONFIG_DIR / "sauvegarde-bureau" / "auto.json"
LOG = STATE_DIR / "auto.log"
STATUS = STATE_DIR / "last-run.json"
UNIT_DIR = CONFIG_DIR / "systemd" / "user"
SERVICE = "sauvegarde-bureau.service"
TIMER = "sauvegarde-bureau.timer"
KEYRING_ATTR = ["application", "sauvegarde-bureau"]

FREQUENCIES = ("daily", "weekly", "monthly")
FREQUENCY_LABELS = (_("Tous les jours"), _("Toutes les semaines"), _("Tous les mois"))

# Seules les sauvegardes portant ce nom sont soumises à la rotation :
# les sauvegardes manuelles ne sont jamais supprimées.
AUTO_RE = re.compile(r"^sauvegarde-ubuntu-auto-\d{4}-\d{2}-\d{2}_\d{6}(\.tar\.gz\.gpg)?$")

DEFAULTS = {
    "enabled": False,
    "frequency": "weekly",
    "hour": 12,
    "minute": 0,
    "destination": str(HOME),
    "components": ["apt", "snap", "flatpak", "repos", "extensions", "dconf"],
    "encrypt": False,
    "keep": 5,
    "cloud": False,                              # envoyer aussi une copie via rclone
    "cloud_remote": "",
    "cloud_path": backend.DEFAULT_CLOUD_PATH,
    "verify": True,                              # recontrôler les sauvegardes après chaque sauvegarde
    "alert_days": verification.DEFAULT_ALERT_DAYS,  # alerte si la dernière sauvegarde est plus vieille
}


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG.read_text()))
    except (OSError, ValueError):
        pass
    cfg["frequency"] = cfg["frequency"] if cfg["frequency"] in FREQUENCIES else "weekly"
    cfg["hour"] = min(max(int(cfg["hour"]), 0), 23)
    cfg["minute"] = min(max(int(cfg["minute"]), 0), 59)
    cfg["keep"] = min(max(int(cfg["keep"]), 1), 50)
    cfg["alert_days"] = min(max(int(cfg["alert_days"]), 1), 365)
    cfg["components"] = [c for c in cfg["components"] if c in backend.COMPONENTS]
    cfg["cloud"] = bool(cfg["cloud"])
    cfg["verify"] = bool(cfg["verify"])
    cfg["cloud_remote"] = str(cfg["cloud_remote"])
    cfg["cloud_path"] = str(cfg["cloud_path"])
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


# --------------------------------------------------------------------------
# Mot de passe dans le trousseau GNOME (via libsecret)
# --------------------------------------------------------------------------

def password_store(passphrase: str) -> bool:
    """Enregistre le mot de passe (transmis par l'entrée standard, jamais en argument)."""
    try:
        r = subprocess.run(
            ["secret-tool", "store", _("--label=Sauvegarde du bureau"), *KEYRING_ATTR],
            input=passphrase, text=True, capture_output=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def password_lookup(timeout: int = 60) -> str | None:
    try:
        r = subprocess.run(["secret-tool", "lookup", *KEYRING_ATTR],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 and r.stdout else None


def password_exists() -> bool:
    return password_lookup(timeout=3) is not None


def password_clear() -> None:
    try:
        subprocess.run(["secret-tool", "clear", *KEYRING_ATTR], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


# --------------------------------------------------------------------------
# Timer systemd (utilisateur)
# --------------------------------------------------------------------------

def _on_calendar(cfg: dict) -> str:
    t = f"{cfg['hour']:02d}:{cfg['minute']:02d}:00"
    return {
        "daily": f"*-*-* {t}",
        "weekly": _('Mon *-*-* {t}').format(t=t),
        "monthly": f"*-*-01 {t}",
    }[cfg["frequency"]]


def timer_text(cfg: dict) -> str:
    return (
        _('[Unit]\nDescription=Sauvegardes automatiques du bureau\n\n[Timer]\nOnCalendar={v1}\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n').format(v1=_on_calendar(cfg))
    )


def service_text() -> str:
    return (
        _("[Unit]\n"
        "Description=Sauvegarde automatique du bureau\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/usr/bin/sauvegarde-bureau --auto\n"
        "Nice=10\n"
        "IOSchedulingClass=idle\n")  # ne ralentit pas le bureau
    )


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def install_timer(cfg: dict) -> tuple[bool, str]:
    if not shutil.which("systemctl"):
        return False, _("systemd est introuvable sur ce système")
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    (UNIT_DIR / SERVICE).write_text(service_text())
    (UNIT_DIR / TIMER).write_text(timer_text(cfg))
    for args in (("daemon-reload",), ("enable", TIMER), ("restart", TIMER)):
        r = _systemctl(*args)
        if r.returncode != 0:
            return False, r.stderr.strip() or _('systemctl {v1} a échoué').format(v1=args[0])
    return True, ""


def remove_timer() -> None:
    if shutil.which("systemctl"):
        _systemctl("disable", "--now", TIMER)
    for name in (TIMER, SERVICE):
        (UNIT_DIR / name).unlink(missing_ok=True)
    if shutil.which("systemctl"):
        _systemctl("daemon-reload")


def timer_status() -> dict:
    """{'installed': bool, 'active': bool, 'next': str}"""
    status = {"installed": (UNIT_DIR / TIMER).exists(), "active": False, "next": ""}
    if not shutil.which("systemctl") or not status["installed"]:
        return status
    r = _systemctl("show", TIMER, "-p", "ActiveState", "-p", "NextElapseUSecRealtime")
    for line in r.stdout.splitlines():
        key, _sep, value = line.partition("=")
        if key == "ActiveState":
            status["active"] = value == "active"
        elif key == "NextElapseUSecRealtime":
            status["next"] = value
    return status


def run_now() -> tuple[bool, str]:
    """Lance immédiatement le service, comme le ferait le timer."""
    if not (UNIT_DIR / SERVICE).exists():
        return False, _("Enregistrez d'abord les réglages.")
    r = _systemctl("start", "--no-block", SERVICE)
    return r.returncode == 0, r.stderr.strip()


# --------------------------------------------------------------------------
# Exécution sans fenêtre
# --------------------------------------------------------------------------

def last_run() -> dict | None:
    try:
        return json.loads(STATUS.read_text())
    except (OSError, ValueError):
        return None


def notify(title: str, body: str, failure: bool = False) -> None:
    if shutil.which("notify-send"):
        subprocess.run(
            ["notify-send", "-a", _("Sauvegarde du bureau"), "-i", "sauvegarde-bureau",
             "-u", "critical" if failure else "normal", title, body],
            capture_output=True,
        )


def automatic_backups(parent: Path) -> list[Path]:
    """Sauvegardes automatiques de `parent`, la plus ancienne d'abord. Les manuelles n'y figurent jamais."""
    try:
        return sorted((p for p in parent.iterdir() if AUTO_RE.match(p.name)), key=lambda p: p.name)
    except OSError:
        return []


def rotation_victims(parent: Path, keep: int) -> list[Path]:
    """Sauvegardes automatiques qu'une rotation supprimerait pour n'en garder que `keep`."""
    items = automatic_backups(parent)
    return items[:-keep] if keep > 0 else []


def rotate(parent: Path, keep: int, log) -> None:
    """Supprime les plus anciennes sauvegardes automatiques au-delà de `keep`."""
    for old in rotation_victims(parent, keep):
        try:
            backend.remove_backup(old)
            log(_('Ancienne sauvegarde supprimée : {v1}').format(v1=old.name))
        except OSError as exc:
            log(_('Impossible de supprimer {v1} : {exc}').format(v1=old.name, exc=exc))


def run_auto() -> int:
    """Point d'entrée de `sauvegarde-bureau --auto`. Renvoie le code de sortie."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > 500_000:
        LOG.replace(LOG.with_suffix(".log.1"))
    logfile = open(LOG, "a", encoding="utf-8")

    def log(line: str) -> None:
        logfile.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
        logfile.flush()

    def finish(ok: bool, message: str, path: str = "", title: str = "") -> int:
        log(message)
        STATUS.write_text(json.dumps({
            "date": datetime.now().isoformat(timespec="seconds"),
            "ok": ok, "message": message, "path": path,
        }, ensure_ascii=False))
        notify(_("Sauvegarde du bureau") if ok else title or _("Sauvegarde du bureau : échec"), message, failure=not ok)
        logfile.close()
        return 0 if ok else 1

    cfg = load_config()
    if not cfg["enabled"]:
        log(_("Sauvegardes automatiques désactivées : rien à faire."))
        return 0
    log(_("=== Début de la sauvegarde automatique ==="))

    parent = Path(cfg["destination"])
    if not parent.is_dir():
        # On ne crée jamais le dossier : un disque externe absent ne doit pas
        # entraîner une copie dans un dossier vide du disque interne.
        return finish(False, _('Destination introuvable : {parent} (disque non branché ?)').format(parent=parent))

    passphrase = None
    if cfg["encrypt"]:
        passphrase = password_lookup()
        if not passphrase:
            return finish(False, _("Mot de passe introuvable dans le trousseau : sauvegarde annulée "
                                 "(elle n'est jamais faite en clair si le chiffrement est demandé)."))

    selected = set(cfg["components"])
    if not selected:
        return finish(False, _("Aucun élément à sauvegarder n'est sélectionné."))
    if "home" in selected and not passphrase:
        return finish(False, _("Les configurations personnelles exigent le chiffrement : sauvegarde annulée."))

    cloud = None
    if cfg["cloud"]:
        if not passphrase:
            return finish(False, _("L'envoi vers le cloud exige le chiffrement : sauvegarde annulée."))
        try:
            cloud = backend.cloud_target(cfg["cloud_remote"], cfg["cloud_path"])
        except ValueError as exc:
            return finish(False, _('Envoi cloud mal configuré : {exc}. Sauvegarde annulée.').format(exc=exc))

    total = backend.count_steps(selected, cloud is not None)
    dest = parent / f"sauvegarde-ubuntu-auto-{datetime.now():%Y-%m-%d_%H%M%S}"
    try:
        errors = backend.backup(dest, selected, log, lambda _f, _t: None, passphrase,
                                cloud=cloud)
    except backend.InsufficientSpace as exc:
        # Rien n'a été écrit et aucune ancienne sauvegarde n'est supprimée : on prévient, c'est tout.
        check = exc.check
        return finish(False, _('Sauvegarde annulée : espace disque insuffisant (taille estimée : {v1}). {v2} Libérez de la place, choisissez une autre destination ou allégez la sélection.').format(v1=backend.fmt_size(check.size), v2=check.message().replace(chr(10), ' ')),
                      title=_("Sauvegarde du bureau : espace insuffisant"))
    except Exception as exc:  # noqa: BLE001
        return finish(False, _('Erreur : {exc}').format(exc=exc))

    result = backend.archive_path(dest) if passphrase else dest
    cloud_failed = "cloud" in errors
    local_errors = [e for e in errors if e != "cloud"]
    if local_errors:
        # Pas de rotation : une sauvegarde incomplète ne doit pas remplacer une bonne.
        return finish(False, _('Sauvegarde incomplète : {v1}. Les anciennes sauvegardes sont conservées.').format(v1=backend.report(total, errors)), str(result))
    rotate(parent, cfg["keep"], log)
    if cloud is not None and not cloud_failed:
        try:
            backend.cloud_rotate(*cloud, cfg["keep"], AUTO_RE, log)
        except Exception as exc:  # noqa: BLE001 - la rotation cloud n'est jamais bloquante
            log(_('Rotation cloud ignorée : {exc}').format(exc=exc))

    # Recontrôle des sauvegardes conservées : un disque qui s'abîme, ou un mot de passe qui ne
    # correspond plus, se découvre ici et non le jour où l'on en a besoin. Le mot de passe est celui
    # du trousseau, déjà lu plus haut : le test se fait sans rien demander ni restaurer.
    problem = ""
    if cfg["verify"]:
        kept = sorted((p for p in parent.iterdir() if AUTO_RE.match(p.name) and p != result),
                      key=lambda p: p.name)
        try:
            checks = backend.verify_many(kept, passphrase=passphrase)
        except Exception as exc:  # noqa: BLE001 - la sauvegarde est faite : ne jamais la déclarer perdue
            log(_('Vérification ignorée : {exc}').format(exc=exc))
            checks = []
        for c in checks:
            if c.status == backend.BAD:
                log(_('ALERTE : {v1} est abîmée ({v2}).').format(v1=c.path.name, v2=c.message))
            elif c.password is False:
                log(_("ALERTE : le mot de passe du trousseau n'ouvre pas {v1} ({v2}).").format(v1=c.path.name, v2=c.password_message))
            elif c.status == backend.OK:
                log(_('Intégrité vérifiée : {v1}').format(v1=c.path.name)
                    + (_(" ; mot de passe correct") if c.password else ""))
        # `result` vient d'être contrôlé et déchiffré à sa création (somme de contrôle, puis test)
        verification.record_checks([(c.path, c.status, c.password) for c in checks]
                                   + [(result, backend.OK, None)])
        damaged = [c.path.name for c in checks if c.status == backend.BAD]
        refused = [c.path.name for c in checks if c.password is False and c.status != backend.BAD]
        if damaged:
            problem = _('sauvegarde(s) abîmée(s) : {v1}').format(v1=', '.join(damaged))
        if refused:
            problem += ("" if not problem else " ; ") + (
                _("mot de passe actuel refusé par : {v1} (chiffrées avec un ancien mot de passe ? retrouvez-le avant d'en avoir besoin)").format(v1=', '.join(refused)))

    # Copies cloud : comparées par empreinte, sans rien télécharger (rclone lsjson --hash)
    if cloud is not None and not cloud_failed and cfg["verify"]:
        try:
            cloud_checks = backend.verify_cloud(*cloud, [parent])
        except Exception as exc:  # noqa: BLE001 - la copie vient d'être envoyée et vérifiée en taille
            log(_('Contrôle des copies cloud ignoré : {exc}').format(exc=exc))
            cloud_checks = []
        for c in cloud_checks:
            log(_('Copie cloud {v1} : {v2}').format(v1=c.name, v2=c.message)
                if c.status != backend.CLOUD_BAD else _('ALERTE : copie cloud {v1} : {v2}').format(v1=c.name, v2=c.message))
        if cloud_checks:
            verification.record_cloud(*cloud, [(c.name, c.status) for c in cloud_checks])
        bad_cloud = [c.name for c in cloud_checks if c.status == backend.CLOUD_BAD]
        if bad_cloud:
            problem += ("" if not problem else " ; ") + _('copie(s) cloud abîmée(s) : {v1}').format(v1=', '.join(bad_cloud))
    if problem:
        return finish(False, _('Sauvegarde terminée ({v1}, {v2}), mais ATTENTION : {problem}.').format(v1=result.name, v2=backend.report(total, errors), problem=problem), str(result),
                      title=_("Sauvegarde du bureau : problème détecté"))
    if cloud_failed:
        # La copie locale est bonne (et conservée) ; c'est l'envoi qui a échoué.
        return finish(False, _("Sauvegarde locale terminée ({v1}), mais l'envoi cloud a échoué : {v2}. Voir le journal.").format(v1=result.name, v2=backend.report(total, errors)), str(result))
    return finish(True, _('Sauvegarde terminée : {v1} ({v2})').format(v1=result.name, v2=backend.report(total, errors)),
                  str(result))
