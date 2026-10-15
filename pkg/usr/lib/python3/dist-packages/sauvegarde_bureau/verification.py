"""Mémoire des contrôles d'intégrité et alerte « sauvegarde trop ancienne ».

Un contrôle qu'on oublie de lancer ne protège de rien : ce module retient quand chaque
sauvegarde a été vérifiée pour la dernière fois, et dit si la dernière sauvegarde est
devenue trop vieille. Il n'utilise pas GTK : l'application graphique et le mode
`sauvegarde-bureau --auto` écrivent dans le même fichier d'état.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, NamedTuple

from . import backend

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sauvegarde-bureau"
STATE = STATE_DIR / "verifications.json"

DEFAULT_ALERT_DAYS = 10     # une sauvegarde hebdomadaire laisse ainsi trois jours de marge
MAX_FOLDERS = 30            # dossiers mémorisés
MAX_ITEMS = 300             # sauvegardes mémorisées par dossier

_lock = threading.Lock()
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[_-](\d{6}))?")


# --------------------------------------------------------------------------
# Fichier d'état
# --------------------------------------------------------------------------

def _load() -> dict:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_name(STATE.name + ".part")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE)


def _when(text: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text))
    except ValueError:
        return None


def record_checks(entries: Iterable[tuple[Path, str, bool | None]],
                  now: datetime | None = None) -> None:
    """Retient le résultat de contrôles : (chemin de la sauvegarde, OK | MISSING | BAD, mot de passe).

    `mot de passe` vaut None s'il n'a pas été testé. Ne lève jamais d'exception :
    perdre la mémoire d'un contrôle ne doit pas faire échouer une sauvegarde."""
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    try:
        with _lock:
            data = _load()
            folders = data.setdefault("folders", {})
            if not isinstance(folders, dict):
                folders = data["folders"] = {}
            for path, status, password in entries:
                items = folders.setdefault(str(path.parent), {})
                entry = {"at": stamp, "status": status, "pw": password}
                previous = items.get(path.name)
                if isinstance(previous, dict) and isinstance(previous.get("rehearsal"), dict):
                    entry["rehearsal"] = previous["rehearsal"]   # un contrôle rapide n'efface pas la répétition
                items[path.name] = entry
            for key, items in list(folders.items()):
                if len(items) > MAX_ITEMS:
                    keep = sorted(items.items(), key=lambda kv: str(kv[1].get("at")), reverse=True)
                    folders[key] = dict(keep[:MAX_ITEMS])
            if len(folders) > MAX_FOLDERS:
                def newest(key: str) -> str:
                    return max((str(v.get("at")) for v in folders[key].values()), default="")
                for key in sorted(folders, key=newest)[:-MAX_FOLDERS]:
                    del folders[key]
            _save(data)
    except (OSError, TypeError, AttributeError):
        pass


# --------------------------------------------------------------------------
# Dates et durées
# --------------------------------------------------------------------------

def backup_time(path: Path) -> datetime:
    """Date d'une sauvegarde : lue dans son nom, à défaut la date de modification.

    Un nom sans heure (« sauvegarde-ubuntu-2026-10-14 ») est compté en fin de journée : dans le
    doute, on ne déclenche pas d'alerte trop tôt."""
    m = _DATE_RE.search(path.name)
    if m:
        try:
            year, month, day = int(m[1]), int(m[2]), int(m[3])
            if m[4]:
                return datetime(year, month, day, int(m[4][:2]), int(m[4][2:4]), int(m[4][4:]))
            return datetime(year, month, day, 23, 59, 59)
        except ValueError:
            pass
    return datetime.fromtimestamp(path.stat().st_mtime)


def date_text(path: Path) -> str:
    """Date d'une sauvegarde pour l'affichage. L'heure n'est donnée que si le nom la porte :
    « sauvegarde-ubuntu-2026-10-14 » ne dit pas à quelle heure elle a été faite."""
    m = _DATE_RE.search(path.name)
    try:
        when = backup_time(path)
    except OSError:
        return "date inconnue"
    return when.strftime("%d/%m/%Y") if m and not m[4] and _valid_date(m) else when.strftime("%d/%m/%Y à %H:%M")


def _valid_date(m: re.Match) -> bool:
    try:
        datetime(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return False
    return True


def _n(count: int, one: str, many: str) -> str:
    return f"{count} {one if count < 2 else many}"


def human_age(delta: timedelta) -> str:
    """« à l'instant », « il y a 3 jours »…"""
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return "à l'instant"
    if seconds < 3600:
        return "il y a " + _n(seconds // 60, "minute", "minutes")
    if seconds < 86400:
        return "il y a " + _n(seconds // 3600, "heure", "heures")
    days = seconds // 86400
    if days < 60:
        return "il y a " + _n(days, "jour", "jours")
    return f"il y a {days // 30} mois"


# --------------------------------------------------------------------------
# État affiché par l'application
# --------------------------------------------------------------------------

class Status(NamedTuple):
    text: str                       # « Vérifiée il y a 3 jours : tout est intact »
    level: str                      # none | ok | warn | bad
    alert: str                      # message d'alerte ; vide si la dernière sauvegarde est assez récente
    last_backup: datetime | None


def _latest_backup(folders: list[Path], state: dict) -> tuple[datetime | None, bool]:
    """(date de la dernière sauvegarde, vue à l'instant ?). Sans sauvegarde visible — disque
    externe débranché, dossier vidé —, on se rappelle la dernière vue : c'est justement le cas
    où personne ne s'aperçoit que les sauvegardes ne se font plus."""
    found = []
    for folder in folders:
        for path in backend.backups_in_folder(folder):
            try:
                found.append(backup_time(path))
            except OSError:
                continue
    if found:
        latest = max(found)
        try:
            with _lock:
                data = _load()
                data["last_backup"] = latest.isoformat(timespec="seconds")
                _save(data)
        except OSError:
            pass
        return latest, True
    return _when(state.get("last_backup")), False


def evaluate(folder: Path | None, search: list[Path] | None = None,
             alert_days: int = DEFAULT_ALERT_DAYS, now: datetime | None = None) -> Status:
    """Dit où en est le contrôle de `folder` et si la dernière sauvegarde (cherchée dans `search`,
    par défaut `folder`) est trop ancienne. Lit des dossiers : à appeler hors de la boucle graphique."""
    now = now or datetime.now()
    state = _load()
    folders = [f for f in (search if search is not None else [folder]) if f and f.is_dir()]
    last, visible = _latest_backup(folders, state)

    alert = ""
    if last is not None and now - last > timedelta(days=alert_days):
        alert = (f"Votre dernière sauvegarde date d'{human_age(now - last)} "
                 f"(alerte au-delà de {_n(alert_days, 'jour', 'jours')})."
                 + ("" if visible else " Le dossier de sauvegarde est-il accessible (disque branché) ?"))

    if folder is None or not folder.is_dir():
        return Status("Aucun dossier de sauvegarde connu", "none", alert, last)
    present = {p.name for p in backend.backups_in_folder(folder)}
    if not present:
        return Status(f"Aucune sauvegarde dans {folder}", "none", alert, last)

    items = state.get("folders", {}).get(str(folder), {})
    checked = {name: i for name, i in items.items()
               if name in present and isinstance(i, dict) and _when(i.get("at"))}
    if not checked:
        return Status("Jamais vérifiée", "warn", alert, last)

    # Une sauvegarde absente de la liste n'a jamais été contrôlée : l'ancienneté affichée est
    # celle du contrôle le plus ancien, pour ne rien promettre de plus que ce qui est vrai.
    oldest = min(_when(i["at"]) for i in checked.values())
    age = human_age(now - oldest)
    bad = sum(1 for i in checked.values() if i.get("status") == backend.BAD)
    refused = sum(1 for i in checked.values() if i.get("pw") is False)
    missing = sum(1 for i in checked.values() if i.get("status") == backend.MISSING)
    unrestorable = sum(1 for i in checked.values() if isinstance(i.get("rehearsal"), dict)
                       and i["rehearsal"].get("verdict") == backend.REH_BAD)
    pending = len(present) - len(checked)

    if unrestorable and not bad:
        text, level = (f"Vérifiée {age} : restauration à blanc échouée pour "
                       f"{_n(unrestorable, 'sauvegarde', 'sauvegardes')}"), "bad"
    elif bad:
        text, level = f"Vérifiée {age} : {_n(bad, 'sauvegarde abîmée', 'sauvegardes abîmées')}", "bad"
    elif refused:
        text, level = (f"Vérifiée {age} : mot de passe refusé par "
                       f"{_n(refused, 'archive', 'archives')}"), "bad"
    elif missing:
        text, level = (f"Vérifiée {age} : {_n(missing, 'sauvegarde', 'sauvegardes')} "
                       "sans somme de contrôle"), "warn"
    else:
        text, level = f"Vérifiée {age} : tout est intact", "ok"
    if pending:
        text += f" · {_n(pending, 'sauvegarde', 'sauvegardes')} pas encore contrôlée{'s' if pending > 1 else ''}"
        level = level if level == "bad" else "warn"
    return Status(text, level, alert, last)


def item_states(folder: Path | None) -> dict[str, dict]:
    """Derniers contrôles mémorisés pour les sauvegardes de `folder` : nom -> {at, status, pw}."""
    if folder is None:
        return {}
    items = _load().get("folders", {}).get(str(folder), {})
    return {name: info for name, info in items.items() if isinstance(info, dict) and _when(info.get("at"))}


def describe_item(info: dict | None, now: datetime | None = None) -> tuple[str, str]:
    """(texte, niveau ok | warn | bad) du dernier contrôle d'UNE sauvegarde ; `info` vient de item_states."""
    if not info:
        return "Jamais vérifiée", "warn"
    now = now or datetime.now()
    when = _when(info.get("at"))
    age = human_age(now - when) if when else "à une date inconnue"
    reh = info.get("rehearsal") if isinstance(info.get("rehearsal"), dict) else None
    reh_when = _when(reh.get("at")) if reh else None
    reh_text = ""
    if reh and reh_when:
        reh_age = human_age(now - reh_when)
        reh_text = {backend.REH_OK: f" · restauration à blanc réussie {reh_age}",
                    backend.REH_PARTIAL: f" · restauration à blanc sans comparaison possible {reh_age}",
                    backend.REH_BAD: f" · restauration à blanc ÉCHOUÉE {reh_age}"}.get(reh.get("verdict"), "")
    if reh_text and reh.get("verdict") == backend.REH_BAD:
        return f"Irrécupérable ? Vérifiée {age}{reh_text}", "bad"
    if info.get("status") == backend.BAD:
        return f"Abîmée · vérifiée {age}", "bad"
    if info.get("pw") is False:
        return f"Mot de passe refusé · vérifiée {age}", "bad"
    if info.get("status") == backend.MISSING:
        return f"Sans somme de contrôle · vérifiée {age}{reh_text}", "warn"
    return f"Intacte · vérifiée {age}" + (" · mot de passe correct" if info.get("pw") else "") + reh_text, "ok"


def record_rehearsal(path: Path, verdict: str, now: datetime | None = None) -> None:
    """Retient le résultat d'une répétition de restauration à blanc (backend.REH_OK | REH_PARTIAL | REH_BAD).

    Les autres issues (mot de passe refusé, place insuffisante, interruption) ne disent rien de l'état de
    l'archive : elles ne sont pas retenues. Ne lève jamais d'exception."""
    if verdict not in (backend.REH_OK, backend.REH_PARTIAL, backend.REH_BAD):
        return
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    try:
        with _lock:
            data = _load()
            folders = data.setdefault("folders", {})
            if not isinstance(folders, dict):
                folders = data["folders"] = {}
            items = folders.setdefault(str(path.parent), {})
            entry = items.get(path.name)
            if not isinstance(entry, dict):
                entry = items[path.name] = {"at": stamp, "status": backend.OK, "pw": None}
            entry["rehearsal"] = {"at": stamp, "verdict": verdict}
            _save(data)
    except (OSError, TypeError, AttributeError):
        pass


def forget(paths: Iterable[Path]) -> None:
    """Oublie les contrôles de sauvegardes supprimées. Ne lève jamais d'exception."""
    try:
        with _lock:
            data = _load()
            folders = data.get("folders")
            if not isinstance(folders, dict):
                return
            for path in paths:
                items = folders.get(str(path.parent))
                if isinstance(items, dict):
                    items.pop(path.name, None)
            _save(data)
    except (OSError, TypeError, AttributeError):
        pass


# --------------------------------------------------------------------------
# Copies cloud
# --------------------------------------------------------------------------

def record_cloud(remote: str, path: str, checks: Iterable[tuple[str, str]],
                 now: datetime | None = None) -> None:
    """Retient le dernier contrôle des copies cloud : (nom de l'archive, état backend.CLOUD_*).

    Le dossier distant est relu en entier à chaque contrôle : la nouvelle liste remplace l'ancienne."""
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    try:
        with _lock:
            data = _load()
            cloud = data.setdefault("cloud", {})
            if not isinstance(cloud, dict):
                cloud = data["cloud"] = {}
            cloud[f"{remote}:{path}"] = {name: {"at": stamp, "status": status} for name, status in checks}
            for key in sorted(cloud)[:-MAX_FOLDERS]:
                del cloud[key]
            _save(data)
    except (OSError, TypeError, AttributeError):
        pass


def cloud_status(remote: str, path: str, now: datetime | None = None) -> tuple[str, str]:
    """(texte, niveau none | ok | warn | bad) du dernier contrôle des copies cloud. Ne lit que la mémoire."""
    now = now or datetime.now()
    items = _load().get("cloud", {}).get(f"{remote}:{path}", {})
    stamps = [t for i in items.values() if isinstance(i, dict) and (t := _when(i.get("at")))]
    if not stamps:
        return "Jamais vérifiées", "warn"
    age = human_age(now - min(stamps))
    states = [i.get("status") for i in items.values() if isinstance(i, dict)]
    bad = states.count(backend.CLOUD_BAD)
    unsure = states.count(backend.CLOUD_WEAK) + states.count(backend.CLOUD_NOREF)
    if bad:
        return f"Vérifiées {age} : {_n(bad, 'copie cloud abîmée', 'copies cloud abîmées')}", "bad"
    if unsure:
        return (f"Vérifiées {age} : {_n(unsure, 'copie', 'copies')} non confirmée{'s' if unsure > 1 else ''} "
                "(taille seulement)"), "warn"
    return f"Vérifiées {age} : toutes identiques à vos copies locales", "ok"
