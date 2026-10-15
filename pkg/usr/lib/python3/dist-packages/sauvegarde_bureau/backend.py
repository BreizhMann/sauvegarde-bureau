"""Logique de sauvegarde et de restauration (indépendante de l'interface)."""

from __future__ import annotations

import ast
import filecmp
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import tarfile
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, NamedTuple

Log = Callable[[str], None]
Progress = Callable[[float, str], None]

HOME = Path.home()
EXT_DIR = HOME / ".local/share/gnome-shell/extensions"
EGO_URL = "https://extensions.gnome.org"
EGO_TIMEOUT = 20            # secondes, par requête
EGO_MAX_ZIP = 30 * 1024 * 1024  # taille maximale d'une extension téléchargée
EXT_INDEX = "extensions.json"
APT_DIR = Path("/etc/apt")

# Ordre d'exécution (dépôts avant paquets, extensions avant réglages)
COMPONENTS = ("repos", "apt", "snap", "flatpak", "extensions", "dconf", "profiles", "home")

LABELS = {
    "repos": "Dépôts tiers",
    "apt": "Applications (apt)",
    "snap": "Snaps",
    "flatpak": "Flatpaks",
    "extensions": "Extensions GNOME",
    "dconf": "Réglages du bureau",
    "profiles": "Navigateurs et courrier",
    "home": "Configurations personnelles",
    "cloud": "Envoi cloud",  # étape finale d'une sauvegarde, pas un composant
}

# Éléments copiés avec « configurations personnelles »
HOME_ITEMS = [
    ".config", ".local/share", ".var/app", "snap",
    ".ssh", ".bashrc", ".zshrc", ".gitconfig", ".profile",
]
HOME_EXCLUDES = [
    ".local/share/Trash", ".local/share/flatpak", ".local/share/containers",
    "Cache", "cache", ".cache", "GPUCache", "Code Cache", "CachedData",
]

# Les dépôts système d'Ubuntu ne doivent jamais être restaurés : ils sont liés
# à la version installée et casseraient une version plus récente.
IGNORED_SOURCES = {"ubuntu.sources", "ubuntu.list"}


# --------------------------------------------------------------------------
# Utilitaires
# --------------------------------------------------------------------------

def has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def capture(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def run(cmd, log: Log, on_line: Callable[[str], None] | None = None, stdin=None) -> int:
    """Lance une commande, envoie sa sortie au journal, renvoie le code retour."""
    cmd = [str(c) for c in cmd]
    log("$ " + " ".join(shlex.quote(c) for c in cmd))
    try:
        proc = subprocess.Popen(
            cmd, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace",
        )
    except FileNotFoundError:
        log(f"Commande introuvable : {cmd[0]}")
        return 127
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if on_line:
            on_line(line)
        log(line)
    return proc.wait()


def os_name() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "Linux"


def _read_lines(path: Path) -> list[str]:
    return [l.strip() for l in path.read_text().splitlines() if l.strip()]


def _repo_files(base: Path, sub: str) -> list[Path]:
    """Fichiers de dépôts/clés à sauvegarder ou restaurer (hors système Ubuntu)."""
    d = base / sub
    if not d.is_dir():
        return []
    files = [p for p in sorted(d.iterdir()) if p.is_file()]
    if sub == "sources.list.d":
        files = [p for p in files
                 if p.suffix in (".list", ".sources") and p.name not in IGNORED_SOURCES]
    elif sub == "trusted.gpg.d":
        files = [p for p in files if not p.name.startswith("ubuntu-keyring")]
    return files


REPO_DIRS = ("sources.list.d", "keyrings", "trusted.gpg.d")


# --------------------------------------------------------------------------
# Dossier temporaire privé et chiffrement
# --------------------------------------------------------------------------

def _workdir_path() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or HOME / ".cache") / "sauvegarde-bureau"


def _workdir() -> Path:
    base = _workdir_path()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    return base


def new_tempdir() -> Path:
    """Dossier temporaire accessible uniquement par l'utilisateur."""
    return Path(tempfile.mkdtemp(dir=_workdir()))


def cleanup_stale() -> None:
    """Supprime les restes d'une opération interrompue (données en clair)."""
    shutil.rmtree(_workdir(), ignore_errors=True)


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _passphrase_fd(passphrase: str) -> int:
    """Tube contenant le mot de passe : il n'apparaît jamais dans la liste des processus."""
    r, w = os.pipe()
    os.write(w, (passphrase + "\n").encode())
    os.close(w)
    return r


GPG_BASE = ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback"]


def encrypt_directory(folder: Path, out: Path, passphrase: str) -> None:
    """tar.gz du dossier, chiffré en AES-256 avec le mot de passe."""
    fd = _passphrase_fd(passphrase)
    try:
        tar = subprocess.Popen(["tar", "czf", "-", "-C", str(folder.parent), folder.name],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        gpg = subprocess.Popen(
            [*GPG_BASE, "--passphrase-fd", str(fd), "--symmetric", "--cipher-algo", "AES256",
             "--s2k-digest-algo", "SHA512", "--s2k-count", "65011712",
             "--compress-algo", "none", "-o", str(out)],
            stdin=tar.stdout, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", pass_fds=(fd,),
        )
        assert tar.stdout is not None
        tar.stdout.close()
        output = gpg.communicate()[0]
        tar_rc = tar.wait()
    finally:
        os.close(fd)
    if gpg.returncode != 0 or tar_rc != 0:
        out.unlink(missing_ok=True)
        raise RuntimeError("Le chiffrement a échoué : " + output.strip()[-300:])
    os.chmod(out, 0o600)


def _gpg_tar(archive: Path, passphrase: str, tar_args: list[str]) -> tuple[bool, str]:
    """Déchiffre l'archive et envoie le résultat à tar. Renvoie (succès, message d'erreur)."""
    fd = _passphrase_fd(passphrase)
    errors = tempfile.TemporaryFile()
    try:
        gpg = subprocess.Popen([*GPG_BASE, "--passphrase-fd", str(fd), "-d", str(archive)],
                               stdout=subprocess.PIPE, stderr=errors, pass_fds=(fd,))
        tar = subprocess.Popen(["tar", *tar_args], stdin=gpg.stdout,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        assert gpg.stdout is not None
        gpg.stdout.close()
        tar_err = tar.communicate()[1].decode(errors="replace")
        gpg.wait()
    finally:
        os.close(fd)
    errors.seek(0)
    gpg_err = errors.read().decode(errors="replace")
    errors.close()
    if gpg.returncode != 0:
        return False, "mot de passe incorrect ou archive illisible"
    if tar.returncode != 0:
        return False, tar_err.strip()[-300:] or "archive corrompue"
    return True, ""


def decrypt_archive(archive: Path, passphrase: str) -> tuple[Path, Path]:
    """Déchiffre une archive dans un dossier temporaire privé.

    Renvoie (dossier temporaire à supprimer ensuite, dossier de la sauvegarde).
    """
    tmp = new_tempdir()
    ok, err = _gpg_tar(archive, passphrase, ["xzf", "-", "-C", str(tmp)])
    if not ok:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(err)
    folders = [p for p in tmp.iterdir() if p.is_dir()]
    return tmp, (folders[0] if len(folders) == 1 else tmp)


GZIP_MAGIC = b"\x1f\x8b"
PASSPHRASE_TEST_TIMEOUT = 60  # secondes ; la dérivation de la clé en prend environ une


def check_passphrase(archive: Path, passphrase: str) -> tuple[bool, str]:
    """Teste le mot de passe d'une archive sans la déchiffrer en entier ni rien écrire sur le disque.

    gpg est lancé puis arrêté dès que les deux premiers octets du contenu (l'en-tête gzip du tar.gz)
    sont lus. Un mauvais mot de passe fait échouer gpg avant le moindre octet ; dans le cas rarissime
    où il passerait le contrôle interne de gpg, l'en-tête lu serait illisible. Renvoie (succès, message)."""
    fd = _passphrase_fd(passphrase)
    try:
        gpg = subprocess.Popen([*GPG_BASE, "--passphrase-fd", str(fd), "-d", str(archive)],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, pass_fds=(fd,))
    except OSError as exc:
        os.close(fd)
        return False, f"gpg est introuvable ou inutilisable : {exc.strerror or exc}"
    assert gpg.stdout is not None
    timer = threading.Timer(PASSPHRASE_TEST_TIMEOUT, gpg.kill)
    timer.start()
    try:
        head = gpg.stdout.read(len(GZIP_MAGIC))
    finally:
        timer.cancel()
        gpg.kill()
        gpg.wait()
        gpg.stdout.close()
        os.close(fd)
    if head == GZIP_MAGIC:
        return True, ""
    return False, "mot de passe incorrect ou archive illisible"


# --------------------------------------------------------------------------
# Répétition de restauration à blanc
# --------------------------------------------------------------------------
#
# Le test du mot de passe ne lit que les deux premiers octets de l'archive : il ne dit rien du
# reste du fichier. La répétition va jusqu'au bout : elle déchiffre toute l'archive dans un dossier
# temporaire privé, l'extrait, contrôle les sommes de contrôle internes (SHA256SUMS), puis supprime
# tout. Rien n'est installé ni restauré sur la machine. C'est facultatif : il faut de la place
# disque temporaire (une fois et demie à deux fois la taille de l'archive, en général) et du temps.

REH_OK = "ok"                # déchiffrée, extraite, sommes de contrôle internes toutes conformes
REH_PARTIAL = "partial"      # déchiffrée et extraite, mais rien à comparer (pas de SHA256SUMS)
REH_BAD = "bad"              # déchiffrement, extraction ou sommes de contrôle en échec
REH_PASSWORD = "password"    # mot de passe refusé : l'archive n'a pas été lue
REH_SPACE = "space"          # pas assez de place pour la répétition : rien d'écrit
REH_CANCELLED = "cancelled"  # interrompue par l'utilisateur

REHEARSAL_SPACE_FACTOR = 2   # estimation prudente : la place dépend du taux de compression du tar.gz


class RehearsalResult(NamedTuple):
    verdict: str
    message: str
    files: int = 0           # fichiers contrôlés
    size: int = 0            # octets extraits puis supprimés
    cleaned: bool = True     # le dossier temporaire a bien disparu

    @property
    def proven(self) -> bool:
        """Vrai seulement si la sauvegarde a été relue en entier ET comparée à ses sommes de contrôle."""
        return self.verdict == REH_OK


def rehearsal_space(archive: Path) -> tuple[int, int | None]:
    """(place conseillée, place libre) dans le dossier de travail. Libre vaut None si non mesurable."""
    try:
        size = archive.stat().st_size
    except OSError:
        size = 0
    needed = size * REHEARSAL_SPACE_FACTOR + SPACE_MARGIN_MIN
    try:
        free = shutil.disk_usage(_existing_parent(_workdir_path())).free
    except OSError:
        free = None
    return needed, free


_TAR_TRUNCATED = re.compile(r"unexpected end|unexpected eof|not in gzip|truncated|crc error|invalid compressed",
                            re.IGNORECASE)
_NO_SPACE = re.compile(r"no space left|disk quota|write error", re.IGNORECASE)


def _rehearsal_extract(archive: Path, passphrase: str, dest: Path,
                       cancelled: Callable[[], bool],
                       members: list[str] | None = None) -> tuple[str, str]:
    """Déchiffre toute l'archive et l'extrait dans `dest`. Renvoie (REH_OK | REH_BAD | REH_SPACE |
    REH_CANCELLED, message). gpg vérifie lui-même l'intégrité du chiffrement jusqu'au dernier octet :
    une archive tronquée ou altérée fait échouer le déchiffrement."""
    fd = _passphrase_fd(passphrase)
    errors = tempfile.TemporaryFile()
    stop, killed = threading.Event(), threading.Event()
    procs: list[subprocess.Popen] = []

    def watch() -> None:
        while not stop.wait(0.3):
            if cancelled():
                killed.set()
                for proc in procs:
                    if proc.poll() is None:
                        proc.kill()
                return

    try:
        gpg = subprocess.Popen([*GPG_BASE, "--passphrase-fd", str(fd), "-d", str(archive)],
                               stdout=subprocess.PIPE, stderr=errors, pass_fds=(fd,))
        procs.append(gpg)
        tar_cmd = ["tar", "xzf", "-", "-C", str(dest)]
        if members:   # un seul fichier ou dossier : noms exacts (pas de jokers), le reste est ignoré
            tar_cmd += ["--no-wildcards", "--", *members]
        tar = subprocess.Popen(tar_cmd, stdin=gpg.stdout,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        procs.append(tar)
        assert gpg.stdout is not None
        gpg.stdout.close()
        threading.Thread(target=watch, daemon=True).start()
        tar_err = tar.communicate()[1].decode(errors="replace").strip()
        gpg.wait()
    except OSError as exc:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
        return REH_BAD, f"gpg ou tar est inutilisable : {exc.strerror or exc}"
    finally:
        stop.set()
        os.close(fd)
    errors.seek(0)
    gpg_err = errors.read().decode(errors="replace").strip()
    errors.close()

    if killed.is_set():
        return REH_CANCELLED, "Répétition interrompue."
    if _NO_SPACE.search(tar_err):
        return REH_SPACE, "la place disque a manqué pendant l'extraction"
    if gpg.returncode == 0 and tar.returncode == 0:
        return REH_OK, ""
    tail = lambda text: (text.splitlines() or [""])[-1][-200:]  # noqa: E731
    if gpg.returncode != 0 and (not tar_err or _TAR_TRUNCATED.search(tar_err)):
        detail = tail(gpg_err)
        return REH_BAD, ("le déchiffrement s'est arrêté avant la fin : l'archive est tronquée ou abîmée"
                         + (f" ({detail})" if detail else ""))
    return REH_BAD, "l'extraction a échoué : " + (tail(tar_err) or "archive corrompue")


def rehearse_restore(archive: Path, passphrase: str, progress: Progress | None = None,
                     cancelled: Callable[[], bool] | None = None) -> RehearsalResult:
    """Répétition de restauration à blanc d'une archive chiffrée : déchiffre tout dans un dossier
    temporaire privé, extrait, contrôle les sommes de contrôle internes, puis supprime tout.

    Ne restaure rien et ne touche à aucun fichier de l'utilisateur. Le dossier temporaire est
    supprimé dans tous les cas (succès, échec, interruption). Ne lève pas d'exception."""
    say = progress or (lambda _fraction, _text: None)
    stop = cancelled or (lambda: False)
    if not archive.is_file():
        return RehearsalResult(REH_BAD, "archive introuvable")

    say(0.0, "Test du mot de passe")
    accepted, message = check_passphrase(archive, passphrase)
    if not accepted:
        return RehearsalResult(REH_PASSWORD, message)
    if stop():
        return RehearsalResult(REH_CANCELLED, "Répétition interrompue.")

    needed, free = rehearsal_space(archive)
    minimum = archive.stat().st_size + SPACE_MARGIN_MIN
    if free is not None and free < minimum:
        return RehearsalResult(REH_SPACE, f"il faut environ {fmt_size(needed)} de place temporaire "
                                          f"dans {_workdir_path()}, il reste {fmt_size(free)}")

    tmp = None
    verdict, text, files, size = REH_BAD, "", 0, 0
    try:
        tmp = new_tempdir()
        say(0.1, "Déchiffrement et extraction de toute l'archive")
        verdict, text = _rehearsal_extract(archive, passphrase, tmp, stop)
        if verdict == REH_OK:
            say(0.8, "Contrôle des sommes de contrôle internes")
            folders = [p for p in tmp.iterdir() if p.is_dir()]
            root = folders[0] if len(folders) == 1 else tmp
            for _rel, path in _folder_files(root):
                files += 1
                try:
                    size += path.stat().st_size
                except OSError:
                    pass
            status, detail = verify_folder(root)
            if status == BAD:
                verdict, text = REH_BAD, "contenu extrait différent des sommes de contrôle : " + detail
            elif status == MISSING:
                verdict, text = REH_PARTIAL, ("l'archive se déchiffre et s'extrait entièrement, mais elle ne "
                                              "contient pas de SHA256SUMS : le contenu n'est pas comparé")
    except OSError as exc:
        verdict, text = REH_BAD, f"lecture ou écriture impossible : {exc.strerror or exc}"
    finally:
        if tmp is not None:
            say(0.95, "Suppression du dossier temporaire")
            remove_tree(tmp)
    cleaned = tmp is None or not tmp.exists()
    say(1.0, "")
    return RehearsalResult(verdict, text, files, size, cleaned)


# --------------------------------------------------------------------------
# Restaurer un seul fichier ou dossier
# --------------------------------------------------------------------------
#
# « J'ai supprimé un fichier » : inutile de tout restaurer. On lit l'archive une fois pour en
# dresser la liste (rien n'est écrit sur le disque), l'utilisateur parcourt ou cherche, puis on
# relit l'archive pour n'en extraire que l'élément choisi, dans un dossier temporaire. Chaque
# fichier extrait est comparé à sa somme SHA-256 avant d'être remis en place.
#
# Ne sont proposés que les fichiers personnels de la sauvegarde : « home/ » (configurations) et
# « profils/<application>/ » (navigateurs, courrier), qui reviennent dans le dossier personnel au
# même chemin que l'original. Les listes de paquets, dépôts et réglages dconf n'en font pas partie.
# Les documents personnels (Documents, Images…) ne sont pas sauvegardés par l'application.

SINGLE_MAX_RESULTS = 200
BACKUP_SUFFIX = ".avant-restauration"


class OperationCancelled(RuntimeError):
    """L'utilisateur a interrompu l'opération."""


class BackupFile(NamedTuple):
    target: str      # chemin relatif au dossier personnel : ".config/gtk-3.0/bookmarks"
    member: str      # chemin dans la sauvegarde (sans le dossier racine) : "home/.config/gtk-3.0/bookmarks"
    kind: str        # "file" | "link"
    size: int
    mtime: float
    origin: str      # "home" ou clé de profil ("firefox"…)


class BackupEntry(NamedTuple):
    """Une ligne de l'explorateur : un fichier, un lien ou un dossier."""
    name: str
    target: str
    is_dir: bool
    count: int       # fichiers contenus (1 pour un fichier)
    size: int
    mtime: float


def _member_prefix(origin: str) -> str:
    return "home" if origin == "home" else f"profils/{origin}"


def _origin_rank(origin: str) -> tuple[int, str]:
    return (0, "") if origin == "home" else (1, origin)   # une copie de profil passe après « home »


def _target_of(parts: list[str]) -> tuple[str, str] | None:
    """(chemin dans le dossier personnel, origine) d'un élément de la sauvegarde, ou None."""
    if any(part in ("", ".", "..") for part in parts):
        return None
    if parts[0] == "home" and len(parts) >= 2:
        return "/".join(parts[1:]), "home"
    if parts[0] == "profils" and len(parts) >= 3:
        return "/".join(parts[2:]), parts[1]
    return None


class BackupIndex:
    """Contenu restaurable d'une sauvegarde (archive chiffrée ou dossier), prêt à être parcouru."""

    def __init__(self, source: Path, files: list[BackupFile], root: str = "", has_sums: bool = False,
                 encrypted: bool = False) -> None:
        self.source, self.root, self.has_sums, self.encrypted = source, root, has_sums, encrypted
        self.files = files
        unique: dict[str, BackupFile] = {}
        for f in sorted(files, key=lambda f: _origin_rank(f.origin)):
            unique[f.target] = f          # même chemin dans « home » et dans un profil : le profil l'emporte
        self._children: dict[str, dict[str, BackupEntry]] = {}
        stats: dict[str, list] = {}        # dossier -> [fichiers, octets, date la plus récente]
        for f in unique.values():
            parts = f.target.split("/")
            parent = ""
            for depth, name in enumerate(parts[:-1]):
                path = "/".join(parts[:depth + 1])
                st = stats.setdefault(path, [0, 0, 0.0])
                st[0] += 1
                st[1] += f.size
                st[2] = max(st[2], f.mtime)
                self._children.setdefault(parent, {})[name] = BackupEntry(name, path, True, 0, 0, 0.0)
                parent = path
            self._children.setdefault(parent, {})[parts[-1]] = BackupEntry(
                parts[-1], f.target, False, 1, f.size, f.mtime)
        for kids in self._children.values():
            for name, entry in list(kids.items()):
                if entry.is_dir:
                    count, size, mtime = stats[entry.target]
                    kids[name] = entry._replace(count=count, size=size, mtime=mtime)
        self._unique = unique
        self._dirs = stats

    def __len__(self) -> int:
        return len(self._unique)

    def children(self, prefix: str = "") -> list[BackupEntry]:
        """Contenu d'un dossier (« » pour la racine) : dossiers d'abord, puis fichiers, par nom."""
        kids = self._children.get(prefix.strip("/"), {})
        return sorted(kids.values(), key=lambda e: (not e.is_dir, e.name.casefold()))

    def entry(self, target: str) -> BackupEntry | None:
        target = target.strip("/")
        parent, _, name = target.rpartition("/")
        return self._children.get(parent, {}).get(name)

    def search(self, text: str, limit: int = SINGLE_MAX_RESULTS) -> tuple[list[BackupEntry], bool]:
        """Éléments (fichiers et dossiers) dont le chemin contient tous les mots de `text`, sans tenir
        compte des majuscules, et dont le NOM contient au moins l'un d'eux : chercher « firefox »
        trouve le dossier firefox, pas les milliers de fichiers qu'il contient.
        Renvoie (résultats, y en a-t-il davantage ?), les noms qui correspondent entièrement d'abord."""
        words = text.casefold().split()
        if not words:
            return [], False
        found: list[BackupEntry] = []
        for target in [*self._dirs, *self._unique]:
            low = target.casefold()
            if all(w in low for w in words) and any(w in low.rpartition("/")[2] for w in words):
                entry = self.entry(target)
                if entry:
                    found.append(entry)

        def rank(e: BackupEntry) -> tuple[int, int, str]:
            name = e.name.casefold()
            return (0 if all(w in name for w in words) else 1, 1 if e.is_dir else 0, e.target.casefold())

        found.sort(key=rank)
        return (found[:limit], True) if len(found) > limit else (found, False)

    def select(self, target: str) -> list[BackupFile]:
        """Tous les éléments à remettre pour `target` (un fichier, ou un dossier et son contenu),
        « home » d'abord puis les profils. Liste vide si `target` n'existe pas."""
        target = target.strip("/")
        prefix = target + "/"
        picked = [f for f in self.files if f.target == target or f.target.startswith(prefix)]
        return sorted(picked, key=lambda f: (_origin_rank(f.origin), f.target))

    def summary(self, target: str) -> tuple[int, int]:
        """(nombre de fichiers, octets) à restaurer pour `target`, sans compter deux fois un chemin."""
        entry = self.entry(target)
        return (entry.count, entry.size) if entry else (0, 0)


def list_folder_files(folder: Path, cancelled: Callable[[], bool] | None = None) -> BackupIndex:
    """Contenu restaurable d'un dossier de sauvegarde non chiffré."""
    stop = cancelled or (lambda: False)
    files: list[BackupFile] = []
    roots = [("home", folder / "home")]
    profils = folder / "profils"
    try:
        roots += [(d.name, d) for d in sorted(profils.iterdir()) if d.is_dir()]
    except OSError:
        pass
    for origin, base in roots:
        if not base.is_dir():
            continue
        for root, dirs, names in os.walk(base):
            if stop():
                raise OperationCancelled("Lecture interrompue.")
            here = Path(root)
            for name in list(dirs):
                if (here / name).is_symlink():    # lien vers un dossier : traité comme un lien, jamais suivi
                    dirs.remove(name)
                    names.append(name)
            for name in names:
                path = here / name
                rel = path.relative_to(folder).as_posix()
                parts = _target_of(rel.split("/"))
                if parts is None:
                    continue
                try:
                    st = path.lstat()
                except OSError:
                    continue
                kind = "link" if stat.S_ISLNK(st.st_mode) else "file"
                if kind == "file" and not stat.S_ISREG(st.st_mode):
                    continue
                files.append(BackupFile(parts[0], rel, kind, st.st_size if kind == "file" else 0,
                                        st.st_mtime, origin))
    return BackupIndex(folder, files, "", (folder / FOLDER_SUMS).is_file(), False)


def list_archive_files(archive: Path, passphrase: str, cancelled: Callable[[], bool] | None = None,
                       progress: Progress | None = None) -> BackupIndex:
    """Lit TOUTE l'archive chiffrée pour en dresser la liste, sans rien écrire sur le disque.

    Lève RuntimeError (mot de passe refusé, archive abîmée ou tronquée) ou OperationCancelled.
    L'archive est relue jusqu'au dernier octet : gpg en contrôle ainsi l'intégrité."""
    stop = cancelled or (lambda: False)
    say = progress or (lambda _fraction, _text: None)
    accepted, message = check_passphrase(archive, passphrase)
    if not accepted:
        raise RuntimeError(message)
    fd = _passphrase_fd(passphrase)
    killed, done = threading.Event(), threading.Event()
    try:
        gpg = subprocess.Popen([*GPG_BASE, "--passphrase-fd", str(fd), "-d", str(archive)],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, pass_fds=(fd,))
    except OSError as exc:
        os.close(fd)
        raise RuntimeError(f"gpg est introuvable ou inutilisable : {exc.strerror or exc}") from exc
    assert gpg.stdout is not None

    def watch() -> None:
        while not done.wait(0.3):
            if stop():
                killed.set()
                gpg.kill()
                return

    threading.Thread(target=watch, daemon=True).start()
    files: list[BackupFile] = []
    root, has_sums, seen = "", False, 0
    try:
        with tarfile.open(fileobj=gpg.stdout, mode="r|gz") as tf:
            for info in tf:
                seen += 1
                if seen % 500 == 0:
                    say(0.0, f"{seen} éléments lus")
                parts = [x for x in info.name.split("/") if x not in ("", ".")]
                if not parts:
                    continue
                if not root:
                    root = parts[0]
                if parts[0] != root or len(parts) < 2:
                    continue
                rel_parts = parts[1:]
                if rel_parts == [FOLDER_SUMS] and info.isfile():
                    has_sums = True
                    continue
                if not (info.isfile() or info.issym()):
                    continue
                mapped = _target_of(rel_parts)
                if mapped is None:
                    continue
                files.append(BackupFile(mapped[0], "/".join(rel_parts), "file" if info.isfile() else "link",
                                        info.size if info.isfile() else 0, float(info.mtime), mapped[1]))
        gpg.stdout.read()      # va jusqu'au bout : gpg signale ici une archive tronquée ou modifiée
        gpg.wait()
    except (tarfile.TarError, EOFError, OSError, zlib.error) as exc:
        gpg.kill()
        if killed.is_set():
            raise OperationCancelled("Lecture interrompue.") from exc
        raise RuntimeError("l'archive est abîmée ou tronquée : impossible de la lire en entier") from exc
    finally:
        done.set()
        if gpg.poll() is None:
            gpg.kill()
        gpg.wait()
        gpg.stdout.close()
        os.close(fd)
    if killed.is_set():
        raise OperationCancelled("Lecture interrompue.")
    if gpg.returncode != 0:
        raise RuntimeError("l'archive est tronquée ou a été modifiée : le déchiffrement s'est arrêté avant la fin")
    say(1.0, "")
    return BackupIndex(archive, files, root, has_sums, True)


class RestoreOutcome(NamedTuple):
    files: int           # fichiers remis
    size: int            # octets remis
    replaced: int        # fichiers existants remplacés (conservés en .avant-restauration)
    where: Path          # emplacement final de l'élément restauré
    verified: bool       # comparé aux sommes de contrôle de la sauvegarde ?


def existing_count(target: str) -> int:
    """Fichiers de `target` déjà présents dans le dossier personnel (ils seraient remplacés)."""
    base = HOME / target
    if not os.path.lexists(base):
        return 0
    if base.is_symlink() or base.is_file():
        return 1
    total = 0
    for _root, _dirs, names in os.walk(base):
        total += len(names)
    return total


def restore_path(index: BackupIndex, target: str, passphrase: str | None = None, dest: Path | None = None,
                 progress: Progress | None = None, cancelled: Callable[[], bool] | None = None) -> RestoreOutcome:
    """Remet UN fichier ou dossier de la sauvegarde.

    `dest` None : à son emplacement d'origine dans le dossier personnel ; les fichiers qui existent
    déjà sont conservés avec l'extension .avant-restauration. Sinon : copié dans le dossier `dest`
    (sous son nom), sans toucher à l'original.

    Chaque fichier extrait est comparé à sa somme SHA-256 AVANT d'être copié : en cas de différence,
    rien n'est copié (IntegrityError). Lève RuntimeError, IntegrityError ou OperationCancelled."""
    stop = cancelled or (lambda: False)
    say = progress or (lambda _fraction, _text: None)
    target = target.strip("/")
    picked = index.select(target)
    if not picked:
        raise RuntimeError(f"« {target} » n'existe pas dans cette sauvegarde")
    if not has("rsync"):
        raise RuntimeError("le paquet rsync est requis")
    files_count, size = index.summary(target)
    origins = sorted({f.origin for f in picked}, key=_origin_rank)
    members = [f"{_member_prefix(o)}/{target}" for o in origins]

    tmp: Path | None = None
    try:
        if index.encrypted:
            if passphrase is None:
                raise RuntimeError("mot de passe requis")
            try:
                free = shutil.disk_usage(_existing_parent(_workdir_path())).free
            except OSError:
                free = None
            need = sum(f.size for f in picked) + SPACE_MARGIN_MIN
            if free is not None and free < need:
                raise RuntimeError(f"place temporaire insuffisante dans {_workdir_path()} : il faut "
                                   f"{fmt_size(need)}, il reste {fmt_size(free)}")
            tmp = new_tempdir()
            wanted = [f"{index.root}/{m}" for m in members]
            if index.has_sums:
                wanted.append(f"{index.root}/{FOLDER_SUMS}")
            say(0.1, "Lecture de l'archive et extraction de l'élément choisi")
            verdict, text = _rehearsal_extract(index.source, passphrase, tmp, stop, wanted)
            if verdict == REH_CANCELLED:
                raise OperationCancelled("Restauration interrompue.")
            if verdict != REH_OK:
                raise RuntimeError(text)
            src_root = tmp / index.root
        else:
            src_root = index.source
        if stop():
            raise OperationCancelled("Restauration interrompue.")

        # Contrôle de chaque fichier extrait avant de toucher au dossier personnel
        verified = False
        sums_file = src_root / FOLDER_SUMS
        if sums_file.is_file():
            say(0.7, "Contrôle des sommes de contrôle")
            try:
                expected = {name: digest for digest, name in _parse_sums(sums_file.read_text(encoding="utf-8"))}
            except OSError as exc:
                raise RuntimeError(f"{FOLDER_SUMS} illisible : {exc.strerror or exc}") from exc
            problems = []
            for f in picked:
                if f.kind != "file":
                    continue
                path = src_root / f.member
                try:
                    if f.member not in expected:
                        problems.append(f"{f.target} (sans somme de contrôle)")
                    elif _sha256_file(path) != expected[f.member]:
                        problems.append(f"{f.target} (modifié)")
                except OSError:
                    problems.append(f"{f.target} (illisible)")
            if problems:
                more = f" et {len(problems) - 3} autre(s)" if len(problems) > 3 else ""
                raise IntegrityError("fichiers abîmés dans la sauvegarde : " + ", ".join(problems[:3]) + more
                                     + ". Rien n'a été restauré")
            verified = True

        final = (HOME / target) if dest is None else (dest / Path(target).name)
        replaced = sum(1 for f in {x.target: x for x in picked}.values()
                       if os.path.lexists(final if f.target == target else final / f.target[len(target) + 1:]))
        say(0.85, "Remise en place")
        for member in members:
            source = src_root / member
            if source.is_dir() and not source.is_symlink():
                final.mkdir(parents=True, exist_ok=True)
                cmd = ["rsync", "-a", "--backup", f"--suffix={BACKUP_SUFFIX}", f"{source}/", f"{final}/"]
            else:
                final.parent.mkdir(parents=True, exist_ok=True)
                cmd = ["rsync", "-a", "--backup", f"--suffix={BACKUP_SUFFIX}", str(source), str(final)]
            rc = run(cmd, lambda _line: None)
            if rc not in (0, 23, 24):
                raise RuntimeError(f"rsync a échoué (code {rc})")
        say(1.0, "")
        return RestoreOutcome(files_count, size, replaced, final, verified)
    finally:
        if tmp is not None:
            remove_tree(tmp)


# --------------------------------------------------------------------------
# Vérification d'intégrité (sommes de contrôle SHA-256)
# --------------------------------------------------------------------------
#
# - archive chiffrée « x.tar.gz.gpg » : fichier voisin « x.tar.gz.gpg.sha256 » ;
# - dossier non chiffré : fichier « SHA256SUMS » à l'intérieur du dossier.
# Les deux sont au format de `sha256sum`, donc contrôlables à la main avec
# `sha256sum -c`. Une sauvegarde ancienne, sans somme de contrôle, reste utilisable.

CHECKSUM_SUFFIX = ".sha256"
FOLDER_SUMS = "SHA256SUMS"
OK, MISSING, BAD = "ok", "missing", "bad"


class IntegrityError(RuntimeError):
    """La sauvegarde est abîmée ou a été modifiée depuis sa création."""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def checksum_path(archive: Path) -> Path:
    return archive.with_name(archive.name + CHECKSUM_SUFFIX)


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def write_checksum(archive: Path) -> str:
    digest = _sha256_file(archive)
    _write_atomic(checksum_path(archive), f"{digest}  {archive.name}\n")
    return digest


def _parse_sums(text: str) -> list[tuple[str, str]]:
    entries = []
    for line in text.splitlines():
        digest, sep, name = line.partition("  ")
        if sep and re.fullmatch(r"[0-9a-f]{64}", digest) and name:
            entries.append((digest, name))
    return entries


def verify_checksum(archive: Path) -> tuple[str, str]:
    """Contrôle une archive. Renvoie (OK | MISSING | BAD, message)."""
    sums = checksum_path(archive)
    if not sums.is_file():
        return MISSING, "fichier .sha256 introuvable à côté de l'archive"
    try:
        entries = _parse_sums(sums.read_text(encoding="utf-8"))
        if not entries:
            return BAD, f"{sums.name} est illisible"
        if _sha256_file(archive) != entries[0][0]:
            return BAD, ("la somme de contrôle ne correspond plus : l'archive est corrompue "
                         "ou a été modifiée depuis sa création")
    except OSError as exc:
        return BAD, f"lecture impossible : {exc.strerror or exc}"
    return OK, ""


def _folder_files(folder: Path):
    for root, _dirs, files in os.walk(folder):
        for name in sorted(files):
            p = Path(root) / name
            rel = p.relative_to(folder).as_posix()
            if rel != FOLDER_SUMS and p.is_file() and not p.is_symlink():
                yield rel, p


def write_folder_sums(folder: Path) -> int:
    lines = [f"{_sha256_file(p)}  {rel}\n" for rel, p in sorted(_folder_files(folder))]
    _write_atomic(folder / FOLDER_SUMS, "".join(lines))
    return len(lines)


def verify_folder(folder: Path) -> tuple[str, str]:
    """Contrôle un dossier de sauvegarde non chiffré."""
    sums = folder / FOLDER_SUMS
    if not sums.is_file():
        return MISSING, "fichier SHA256SUMS absent du dossier"
    try:
        entries = _parse_sums(sums.read_text(encoding="utf-8"))
    except OSError as exc:
        return BAD, f"{FOLDER_SUMS} illisible : {exc.strerror or exc}"
    if not entries:
        return BAD, f"{FOLDER_SUMS} est vide ou illisible"
    problems = []
    for digest, rel in entries:
        p = folder / rel
        try:
            if not p.is_file():
                problems.append(f"{rel} (manquant)")
            elif _sha256_file(p) != digest:
                problems.append(f"{rel} (modifié)")
        except OSError:
            problems.append(f"{rel} (illisible)")
    if problems:
        more = f" et {len(problems) - 3} autre(s)" if len(problems) > 3 else ""
        return BAD, "fichiers abîmés : " + ", ".join(problems[:3]) + more
    return OK, ""


def archives_in_folder(folder: Path) -> list[Path]:
    """Archives chiffrées (.tar.gz.gpg) posées directement dans `folder`, la plus récente d'abord.

    Renvoie une liste vide si `folder` est lui-même une sauvegarde non chiffrée : le dossier
    est alors utilisable tel quel et il n'y a rien à proposer à la place."""
    try:
        if (folder / FOLDER_SUMS).is_file() or any(detect(folder)["components"].values()):
            return []
        found = [p for p in folder.iterdir() if p.is_file() and p.name.endswith(".tar.gz.gpg")]
    except OSError:
        return []
    return sorted(found, key=lambda p: p.name, reverse=True)


# Nature d'un élément glissé sur la fenêtre (voir classify_drop)
DROP_ARCHIVE = "archive"        # fichier .gpg : une archive chiffrée
DROP_BACKUP = "backup"          # dossier de sauvegarde déjà déchiffré
DROP_ARCHIVES = "archives"      # dossier qui contient des archives chiffrées
DROP_FOLDER = "folder"          # tout autre dossier : un emplacement
DROP_OTHER = "other"            # le reste : rien à en faire


def classify_drop(path: Path) -> str:
    """Dit ce qu'est un élément déposé sur la fenêtre, pour que l'application choisisse l'action.

    Ne lit que le dossier lui-même : rien n'est ouvert ni déchiffré."""
    try:
        if path.is_dir():
            if (path / FOLDER_SUMS).is_file() or any(detect(path)["components"].values()):
                return DROP_BACKUP
            if archives_in_folder(path):
                return DROP_ARCHIVES
            return DROP_FOLDER
        if path.is_file() and path.name.lower().endswith(".gpg"):
            return DROP_ARCHIVE
    except OSError:
        pass
    return DROP_OTHER


def verify_backup(path: Path) -> tuple[str, str]:
    """Contrôle une sauvegarde, dossier ou archive chiffrée."""
    return verify_folder(path) if path.is_dir() else verify_checksum(path)


def check_archive(archive: Path) -> str:
    """Lève IntegrityError si l'archive est abîmée. Renvoie l'état (OK ou MISSING)."""
    status, msg = verify_checksum(archive)
    if status == BAD:
        raise IntegrityError(msg)
    return status


def remove_backup(path: Path) -> None:
    """Supprime une sauvegarde (dossier ou archive) et sa somme de contrôle."""
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
        checksum_path(path).unlink(missing_ok=True)


def verify_existing(names: list[Path], log: Log,
                    statuses: dict[Path, str] | None = None) -> list[Path]:
    """Recontrôle des sauvegardes déjà faites. Renvoie celles qui sont abîmées.

    Si `statuses` est fourni, il reçoit l'état (OK | MISSING | BAD) de chaque sauvegarde contrôlée."""
    damaged = []
    for path in names:
        status, msg = verify_backup(path)
        if statuses is not None:
            statuses[path] = status
        if status == BAD:
            log(f"ALERTE : {path.name} est abîmée ({msg}).")
            damaged.append(path)
        elif status == OK:
            log(f"Intégrité vérifiée : {path.name}")
    return damaged


class BackupCheck(NamedTuple):
    path: Path
    status: str      # OK | MISSING | BAD (somme de contrôle)
    message: str
    password: bool | None = None   # None : non testé ; True : accepté ; False : refusé
    password_message: str = ""


def is_backup_folder(path: Path) -> bool:
    """Vrai si `path` est lui-même une sauvegarde non chiffrée (et non un dossier qui en contient)."""
    try:
        return path.is_dir() and ((path / FOLDER_SUMS).is_file()
                                  or path.name.startswith("sauvegarde-ubuntu")
                                  or any(detect(path)["components"].values()))
    except OSError:
        return False


def backups_in_folder(folder: Path) -> list[Path]:
    """Toutes les sauvegardes posées directement dans `folder`, la plus récente d'abord.

    Ce sont les archives chiffrées (.tar.gz.gpg) et les dossiers non chiffrés
    (« sauvegarde-ubuntu-… », ou tout dossier qui porte un fichier SHA256SUMS)."""
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []
    found = []
    for entry in entries:
        try:
            if entry.is_file():
                if entry.name.endswith(".tar.gz.gpg"):
                    found.append(entry)
            elif entry.is_dir() and (entry.name.startswith("sauvegarde-ubuntu")
                                     or (entry / FOLDER_SUMS).is_file()):
                found.append(entry)
        except OSError:
            continue
    return sorted(found, key=lambda p: p.name, reverse=True)


def verify_many(paths: list[Path], progress: Progress | None = None,
                cancelled: Callable[[], bool] | None = None,
                passphrase: str | None = None) -> list[BackupCheck]:
    """Contrôle plusieurs sauvegardes d'affilée. S'arrête entre deux sauvegardes si `cancelled()`.

    Une sauvegarde illisible ou disparue en cours de route est signalée abîmée : le contrôle continue.
    Avec `passphrase`, le mot de passe est aussi testé sur chaque archive chiffrée qui n'est pas abîmée
    (inutile d'essayer de déchiffrer un fichier déjà reconnu corrompu)."""
    results = []
    total = len(paths)
    for index, path in enumerate(paths):
        if cancelled and cancelled():
            break
        if progress:
            progress(index / total, f"{index + 1}/{total} : {path.name}")
        try:
            status, message = verify_backup(path)
        except OSError as exc:
            status, message = BAD, f"lecture impossible : {exc.strerror or exc}"
        accepted, pw_message = None, ""
        if passphrase is not None and status != BAD and path.is_file():
            accepted, pw_message = check_passphrase(path, passphrase)
        results.append(BackupCheck(path, status, message, accepted, pw_message))
    if progress:
        progress(1.0, "")
    return results


# --------------------------------------------------------------------------
# Sauvegarde
# --------------------------------------------------------------------------

def _backup_apt(dest: Path, log: Log) -> None:
    out = capture(["apt-mark", "showmanual"])
    (dest / "apt-manual.txt").write_text(out)
    log(f"{len(out.split())} paquets apt enregistrés")


def _backup_snap(dest: Path, log: Log) -> None:
    if not has("snap"):
        log("Snap n'est pas installé : étape ignorée")
        return
    names = [l.split()[0] for l in capture(["snap", "list"]).splitlines()[1:] if l.strip()]
    (dest / "snap.txt").write_text("\n".join(names) + "\n")
    log(f"{len(names)} snaps enregistrés")


def _backup_flatpak(dest: Path, log: Log) -> None:
    if not has("flatpak"):
        log("Flatpak n'est pas installé : étape ignorée")
        return
    out = capture(["flatpak", "list", "--app", "--columns=application"])
    (dest / "flatpak.txt").write_text(out)
    log(f"{len(out.split())} flatpaks enregistrés")


def _backup_repos(dest: Path, log: Log) -> None:
    count = 0
    for sub in REPO_DIRS:
        for f in _repo_files(APT_DIR, sub):
            target = dest / sub / f.name
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, target)
                count += 1
            except OSError as exc:
                log(f"Impossible de copier {f} : {exc}")
    log(f"{count} fichiers de dépôts/clés copiés (dépôts système Ubuntu exclus)")


class _Offline(Exception):
    """Pas de connexion à extensions.gnome.org."""


def _ego_get(url: str, limit: int = 1024 * 1024) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "sauvegarde-bureau"})
    with urllib.request.urlopen(req, timeout=EGO_TIMEOUT) as resp:
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise ValueError("réponse trop volumineuse")
    return data


def _ego_info(uuid: str, shell: str | None = None, pk: int | str | None = None) -> dict | None:
    """Fiche de l'extension sur extensions.gnome.org, ou None si le site ne la connaît pas
    (ou pas pour cette version de GNOME). Lève _Offline si le site est injoignable.

    Cherche par uuid ; à défaut, par identifiant numérique (le « 9419 » de l'adresse
    /extension/9419/nom/) si la sauvegarde l'a conservé."""
    info = _ego_query({"uuid": uuid}, shell)
    if info is None and pk is not None:
        info = _ego_query({"pk": str(pk)}, shell)
    return info


def _ego_query(query: dict, shell: str | None) -> dict | None:
    if shell:
        query["shell_version"] = shell
    url = f"{EGO_URL}/extension-info/?{urllib.parse.urlencode(query)}"
    try:
        info = json.loads(_ego_get(url))
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 404):
            return None
        raise _Offline(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _Offline(str(exc)) from exc
    except ValueError:
        return None
    return info if isinstance(info, dict) else None


def _enabled_extensions() -> set[str]:
    if not has("gnome-extensions"):
        return set()
    return set(_capture_or_empty(["gnome-extensions", "list", "--enabled"]).split())


def _backup_extensions(dest: Path, log: Log) -> None:
    """Enregistre la liste des extensions (identifiants), pas leurs fichiers.

    À la restauration, chaque extension est retéléchargée depuis extensions.gnome.org dans
    la version compatible avec le GNOME de la machine. Les fichiers ne sont copiés que pour
    les extensions que le site ne connaît pas (ou si le site est injoignable maintenant).
    """
    enabled = _enabled_extensions()
    if has("gnome-extensions"):
        (dest / "extensions-actives.txt").write_text("\n".join(sorted(enabled)) + "\n")
    folders = sorted(d for d in EXT_DIR.iterdir() if d.is_dir()) if EXT_DIR.is_dir() else []
    if not folders:
        log("Aucune extension installée par l'utilisateur")
        return

    entries, online, offline = [], 0, False
    local: list[str] = []
    for folder in folders:
        uuid = folder.name
        meta = _read_metadata(folder)
        entry = {
            "uuid": uuid,
            "name": str(meta.get("name") or uuid),
            "version": _ext_version(meta),
            "enabled": uuid in enabled,
            "ego": None,  # True : sur extensions.gnome.org · False : absente · None : inconnu
        }
        if not offline:
            try:
                info = _ego_info(uuid)
            except _Offline as exc:
                offline = True
                log(f"extensions.gnome.org injoignable ({exc}) : les fichiers des extensions "
                    "seront copiés par précaution.")
            else:
                entry["ego"] = info is not None
                if info and info.get("pk") is not None:
                    entry["pk"] = info["pk"]
                    # ex. /extension/9419/live-lock-screen/
                    link = str(info.get("link") or f"/extension/{info['pk']}/")
                    entry["url"] = urllib.parse.urljoin(EGO_URL + "/", link)
        if entry["ego"]:
            online += 1
        else:
            shutil.copytree(folder, dest / "extensions" / uuid, symlinks=True, dirs_exist_ok=True)
            local.append(entry["name"])
        entries.append(entry)

    (dest / EXT_INDEX).write_text(json.dumps(
        {"version": 2, "shell": _gnome_major(), "extensions": entries},
        indent=2, ensure_ascii=False))
    log(f"{len(entries)} extensions enregistrées : {online} seront retéléchargées depuis "
        f"extensions.gnome.org, {len(local)} copiées en fichiers"
        + (f" ({', '.join(local)})" if local else ""))


def _backup_dconf(dest: Path, log: Log) -> None:
    out = capture(["dconf", "dump", "/"])
    (dest / "dconf-reglages.ini").write_text(out)
    log(f"{len(out.splitlines())} lignes de réglages enregistrées")


def _backup_home(dest: Path, log: Log) -> None:
    target = dest / "home"
    target.mkdir(exist_ok=True)
    excludes = [f"--exclude={e}" for e in HOME_EXCLUDES]
    for item in HOME_ITEMS:
        if not (HOME / item).exists():
            continue
        log(f"Copie de ~/{item}")
        rc = run(["rsync", "-aR", *excludes, f"{HOME}/./{item}", str(target) + "/"], log)
        if rc not in (0, 23, 24):  # 23/24 : fichiers illisibles ou disparus en cours de copie
            raise RuntimeError(f"rsync a échoué pour ~/{item} (code {rc})")


# --------------------------------------------------------------------------
# Navigateurs et clients mail (profils)
# --------------------------------------------------------------------------

PROFILE_INDEX = "profils.json"


class ProfileItem(NamedTuple):
    key: str
    label: str
    app: str
    paths: tuple[str, ...]             # dossiers à copier, relatifs au dossier personnel
    excludes: tuple[str, ...] = ()     # noms (jokers autorisés) jamais copiés : caches, verrous…
    only_dirs: tuple[str, ...] = ()    # si renseigné : seuls ces dossiers (et leur contenu) sont copiés
    processes: tuple[str, ...] = ()    # processus qui indiquent que l'application est ouverte
    heavy: bool = False                # volumineux : non coché par défaut


_MOZ_EXCLUDES = ("lock", ".parentlock", "parent.lock", "cache2", "startupCache", "crashes",
                 "minidumps", "saved-telemetry-pings", "datareporting", "shader-cache",
                 "OfflineCache", "thumbnails", "Crash Reports", "Pending Pings")
_CHROME_EXCLUDES = ("Singleton*", "ShaderCache", "GrShaderCache", "DawnCache", "Crashpad",
                    "BrowserMetrics*", "Service Worker")
_TB_PATHS = (".thunderbird", "snap/thunderbird/common/.thunderbird",
             ".var/app/org.mozilla.Thunderbird/.thunderbird")
_TB_PROCS = ("thunderbird", "thunderbird-bin")

# Chaque application peut être installée avec apt, snap ou flatpak : chacune range son profil
# à un endroit différent, on copie ceux qui existent.
PROFILE_ITEMS = (
    ProfileItem("firefox", "Firefox", "Firefox",
                (".mozilla/firefox", "snap/firefox/common/.mozilla/firefox",
                 ".var/app/org.mozilla.firefox/.mozilla/firefox"),
                _MOZ_EXCLUDES, processes=("firefox", "firefox-bin")),
    ProfileItem("thunderbird", "Thunderbird (comptes et réglages)", "Thunderbird", _TB_PATHS,
                _MOZ_EXCLUDES + ("Mail", "ImapMail", "global-messages-db.sqlite*"),
                processes=_TB_PROCS),
    ProfileItem("thunderbird-mail", "Thunderbird (courriers téléchargés)", "Thunderbird", _TB_PATHS,
                only_dirs=("Mail", "ImapMail"), processes=_TB_PROCS, heavy=True),
    ProfileItem("chromium", "Chromium", "Chromium",
                (".config/chromium", "snap/chromium/common/chromium",
                 ".var/app/org.chromium.Chromium/config/chromium"),
                _CHROME_EXCLUDES, processes=("chromium", "chrome")),
    ProfileItem("chrome", "Google Chrome", "Google Chrome", (".config/google-chrome",),
                _CHROME_EXCLUDES, processes=("chrome",)),
)


def detect_profile_items() -> list[ProfileItem]:
    """Les profils présents sur cette machine."""
    return [i for i in PROFILE_ITEMS if any((HOME / p).is_dir() for p in i.paths)]


def default_profile_keys() -> set[str]:
    return {i.key for i in PROFILE_ITEMS if not i.heavy}


def running_apps(keys) -> list[str]:
    """Applications ouvertes parmi celles des profils `keys` (copier un profil ouvert est risqué)."""
    found: set[str] = set()
    for item in PROFILE_ITEMS:
        if item.key not in keys or item.app in found:
            continue
        for proc in item.processes:
            try:
                if subprocess.run(["pgrep", "-x", proc], capture_output=True).returncode == 0:
                    found.add(item.app)
                    break
            except OSError:  # pgrep absent
                return sorted(found)
    return sorted(found)


def _profile_files(root: Path, item: ProfileItem):
    """Fichiers (et liens) du profil à copier, en chemins relatifs à `root`."""
    skip = item.excludes + tuple(HOME_EXCLUDES)
    only = set(item.only_dirs)

    def skipped(name: str) -> bool:
        return any(fnmatch.fnmatchcase(name, pat) for pat in skip)

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        links = [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]
        dirnames[:] = [d for d in dirnames if d not in links and not skipped(d)]
        if only and not only & set(rel_dir.parts):
            continue  # hors des dossiers voulus : on descend sans rien copier
        for name in filenames + links:
            if not skipped(name):
                yield rel_dir / name


def _ensure_dir(src_root: Path, dst_root: Path, rel: Path) -> None:
    """Crée les dossiers parents en reprenant les droits d'origine (profils en 700)."""
    dst_root.mkdir(parents=True, exist_ok=True)
    cur = Path()
    for part in rel.parts:
        cur /= part
        target = dst_root / cur
        if not target.is_dir():
            target.mkdir()
            try:
                shutil.copymode(src_root / cur, target)
            except OSError:
                pass


def _copy_file(src: Path, dst: Path) -> None:
    if os.path.islink(src):
        if os.path.lexists(dst):
            os.unlink(dst)
        os.symlink(os.readlink(src), dst)
        return
    try:
        shutil.copy2(src, dst)
    except OSError:
        if not src.exists():
            raise
        shutil.copyfile(src, dst)  # disque sans gestion des droits (FAT, exFAT)


def _copy_profile(src_root: Path, dst_root: Path, item: ProfileItem, log: Log) -> tuple[int, int]:
    copied = failed = 0
    for rel in _profile_files(src_root, item):
        try:
            _ensure_dir(src_root, dst_root, rel.parent)
            _copy_file(src_root / rel, dst_root / rel)
            copied += 1
        except OSError as exc:
            failed += 1
            if failed <= 5:
                log(f"Impossible de copier {src_root / rel} : {exc}")
    return copied, failed


def _merge_profile(src_root: Path, dst_root: Path, log: Log) -> tuple[int, int]:
    """Copie la sauvegarde sur le profil actuel. Un fichier différent déjà présent est
    conservé sous le nom « ….avant-restauration ». Renvoie (copiés, déjà identiques)."""
    copied = same = failed = 0
    for dirpath, dirnames, filenames in os.walk(src_root):
        rel_dir = Path(dirpath).relative_to(src_root)
        links = [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]
        dirnames[:] = [d for d in dirnames if d not in links]
        for name in filenames + links:
            rel = rel_dir / name
            src, dst = src_root / rel, dst_root / rel
            try:
                _ensure_dir(src_root, dst_root, rel_dir)
                if os.path.lexists(dst):
                    if (not os.path.islink(src) and not os.path.islink(dst) and dst.is_file()
                            and filecmp.cmp(src, dst, shallow=False)):
                        same += 1
                        continue
                    os.replace(dst, str(dst) + ".avant-restauration")
                _copy_file(src, dst)
                copied += 1
            except OSError as exc:
                failed += 1
                if failed <= 5:
                    log(f"Impossible de restaurer {rel} : {exc}")
    if failed:
        raise OSError(f"{failed} fichier(s) non restauré(s)")
    return copied, same


def _dir_size(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def _fmt_size(n: float) -> str:
    for unit in ("o", "Ko", "Mo", "Go"):
        if n < 1024 or unit == "Go":
            return (f"{n:.0f} {unit}" if unit == "o" else f"{n:.1f} {unit}".replace(".", ","))
        n /= 1024
    return ""


fmt_size = _fmt_size


def backup_size(path: Path) -> int:
    """Place prise par une sauvegarde : l'archive et sa somme de contrôle, ou tout le dossier."""
    if path.is_dir():
        return _dir_size(path)
    size = path.stat().st_size
    try:
        size += checksum_path(path).stat().st_size
    except OSError:
        pass
    return size


def _backup_profiles(dest: Path, log: Log, items: set[str] | None = None) -> None:
    keys = default_profile_keys() if items is None else set(items)
    running = running_apps(keys)
    if running:
        log(f"Attention : {', '.join(running)} ouvert : la copie peut être incohérente. "
            "Fermez l'application puis recommencez pour une sauvegarde fiable.")
    index = []
    for item in PROFILE_ITEMS:
        if item.key not in keys:
            continue
        present = [r for r in item.paths if (HOME / r).is_dir()]
        if not present:
            log(f"{item.label} : introuvable sur cette machine, ignoré")
            continue
        copied = failed = 0
        for rel in present:
            log(f"Copie de ~/{rel}")
            c, f = _copy_profile(HOME / rel, dest / "profils" / item.key / rel, item, log)
            copied, failed = copied + c, failed + f
        index.append({"key": item.key, "label": item.label, "paths": present})
        log(f"{item.label} : {copied} fichiers copiés"
            + (f", {failed} en échec" if failed else ""))
        if failed:
            raise RuntimeError(f"{item.label} : {failed} fichier(s) non copiés")
    if not index:
        log("Aucun profil de navigateur ou de client mail trouvé")
        return
    (dest / PROFILE_INDEX).write_text(json.dumps({"version": 1, "items": index},
                                                 indent=2, ensure_ascii=False))


def _load_profile_index(src: Path) -> list[dict]:
    try:
        entries = json.loads((src / PROFILE_INDEX).read_text())["items"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    return [e for e in entries if isinstance(e, dict) and e.get("key") and e.get("paths")]


def profile_keys_in(src: Path, labels: set[str] | None = None) -> set[str]:
    """Clés des profils d'une sauvegarde (limités aux libellés choisis, s'il y en a)."""
    return {e["key"] for e in _load_profile_index(src)
            if labels is None or e.get("label") in labels}


def _install_kind(rel: str) -> str:
    if rel.startswith("snap/"):
        return "installation snap"
    if rel.startswith(".var/app/"):
        return "installation flatpak"
    return "installation apt ou locale"


def _preview_profiles(src: Path) -> Preview:
    index = _load_profile_index(src)
    if not index:
        raise RuntimeError("liste des profils absente ou illisible")
    p = Preview()
    for e in index:
        key, label = str(e["key"]), str(e.get("label") or e["key"])
        paths = [str(x) for x in e["paths"]]
        folder = src / "profils" / key
        parts = [_fmt_size(_dir_size(folder))] if folder.is_dir() else ["dossier manquant"]
        kinds = sorted({_install_kind(x) for x in paths})
        parts.append(", ".join(kinds))
        warn, status = False, NEW
        if not folder.is_dir():
            p.items.append(Item(label, SKIP, " · ".join(parts)))
            continue
        if any((HOME / x).exists() for x in paths):
            status = CHANGE
            parts.append("fusionné avec le profil actuel (fichiers remplacés gardés en .avant-restauration)")
        missing = [k for k in kinds if (k == "installation snap" and not has("snap"))
                   or (k == "installation flatpak" and not has("flatpak"))]
        if missing:
            warn = True
            parts.append(f"{missing[0].split()[-1]} n'est pas installé sur cette machine")
        running = running_apps({key})
        if running:
            warn = True
            parts.append(f"{running[0]} est ouvert : fermez-le avant de restaurer")
        p.items.append(Item(label, status, " · ".join(parts), warn))
    if p.items:
        p.note = ("Installez et lancez une première fois l'application avant de restaurer son "
                  "profil si possible. Un profil créé avec une version récente peut être refusé "
                  "par une version plus ancienne.")
    return p


def _restore_profiles(src: Path, log: Log, only: dict | None = None) -> None:
    labels = (only or {}).get("profiles")
    entries = [e for e in _load_profile_index(src) if labels is None or e.get("label") in labels]
    if not entries:
        raise RuntimeError("aucun profil à restaurer")
    failed: list[str] = []
    for e in entries:
        key, label = str(e["key"]), str(e.get("label") or e["key"])
        if running_apps({key}):
            log(f"{label} : application ouverte, profil non restauré (fermez-la puis recommencez).")
            failed.append(label)
            continue
        folder = src / "profils" / key
        if not folder.is_dir():
            log(f"{label} : dossier absent de la sauvegarde.")
            failed.append(label)
            continue
        try:
            copied = same = 0
            for rel in e["paths"]:
                if not (folder / rel).is_dir():
                    continue
                log(f"Restauration de ~/{rel}")
                c, s = _merge_profile(folder / rel, HOME / rel, log)
                copied, same = copied + c, same + s
            log(f"{label} : {copied} fichiers restaurés, {same} déjà identiques")
        except OSError as exc:
            log(f"{label} : {exc}")
            failed.append(label)
    if failed:
        raise RuntimeError(f"profils non restaurés : {', '.join(failed)}")


# --------------------------------------------------------------------------
# Place nécessaire (vérifiée avant de sauvegarder)
# --------------------------------------------------------------------------

SPACE_MARGIN_MIN = 64 * 1024 * 1024  # marge minimale : métadonnées, sommes de contrôle, manifeste
SPACE_SMALL_STEP = 2 * 1024 * 1024   # listes de paquets, dépôts, réglages : bien moins en pratique


class Shortage(NamedTuple):
    kind: str      # « destination », « temporaire » (copie de travail) ou « partagé » (même disque)
    path: Path     # dossier existant mesuré
    needed: int
    free: int


class SpaceCheck(NamedTuple):
    size: int                            # taille estimée des données à copier
    needed: int                          # place à prévoir pour une copie (données + marge)
    free: int | None                     # place libre à la destination ; None : mesure impossible
    shortages: tuple[Shortage, ...] = ()
    encrypted: bool = False

    @property
    def ok(self) -> bool:
        return not self.shortages

    def summary(self) -> str:
        text = f"Taille estimée : {fmt_size(self.size)}"
        if self.free is not None:
            text += f" · place libre à la destination : {fmt_size(self.free)}"
        return text

    def message(self) -> str:
        """Ce qui manque, en une phrase par disque concerné."""
        lines = []
        for s in self.shortages:
            figures = f"il faut environ {fmt_size(s.needed)}, il reste {fmt_size(s.free)}"
            if s.kind == "partagé":
                lines.append(f"Le disque de {s.path} doit contenir la copie de travail et l'archive "
                             f"chiffrée : {figures}.")
            elif s.kind == "temporaire":
                lines.append(f"Dossier de travail ({s.path}) : le chiffrement y prépare d'abord une "
                             f"copie en clair, {figures}.")
            else:
                lines.append(f"Destination ({s.path}) : {figures}.")
        return "\n".join(lines)


class InsufficientSpace(RuntimeError):
    """La place libre ne suffit pas à la sauvegarde : rien n'a été écrit."""

    def __init__(self, check: SpaceCheck):
        super().__init__(f"Espace disque insuffisant (taille estimée : {fmt_size(check.size)}). "
                         + check.message().replace("\n", " "))
        self.check = check


def _excluded(rel: str, name: str, patterns) -> bool:
    """Même règle que `rsync --exclude` : un nom, ou un chemin si le motif contient « / »."""
    for pat in patterns:
        if "/" in pat:
            if rel == pat or rel.endswith("/" + pat):
                return True
        elif fnmatch.fnmatchcase(name, pat):
            return True
    return False


def _use(st: os.stat_result) -> int:
    """Place prise par un fichier : la plus grande de sa taille et de ses blocs alloués."""
    return max(st.st_size, getattr(st, "st_blocks", 0) * 512)


def _tree_use(root: Path, excludes=(), anchor: Path | None = None) -> int:
    """Place que prendra la copie de `root` (fichier ou dossier), noms exclus ignorés.

    Les liens ne sont pas suivis (comme `rsync -a`). `anchor` sert de base aux motifs à chemin.
    """
    try:
        st = os.lstat(root)
    except OSError:
        return 0
    if not stat.S_ISDIR(st.st_mode):
        return _use(st)
    base = str(anchor or root)
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        kept = []
        for name in dirnames:
            full = os.path.join(dirpath, name)
            if _excluded(os.path.relpath(full, base), name, excludes):
                continue
            kept.append(name)
            if os.path.islink(full):  # os.walk ne descend pas dans les liens : on compte le lien
                try:
                    total += _use(os.lstat(full))
                except OSError:
                    pass
        dirnames[:] = kept
        for name in filenames:
            full = os.path.join(dirpath, name)
            if _excluded(os.path.relpath(full, base), name, excludes):
                continue
            try:
                total += _use(os.lstat(full))
            except OSError:
                pass
    return total


def _profiles_use(keys: set[str]) -> int:
    total = 0
    for item in PROFILE_ITEMS:
        if item.key not in keys:
            continue
        for rel in item.paths:
            root = HOME / rel
            if not root.is_dir():
                continue
            for f in _profile_files(root, item):
                try:
                    total += _use(os.lstat(root / f))
                except OSError:
                    pass
    return total


def estimate_size(selected: set[str], profile_items: set[str] | None = None) -> int:
    """Taille approximative (octets) des données que copiera la sauvegarde, avant compression."""
    total = 0
    for name in selected:
        if name == "home":
            total += sum(_tree_use(HOME / item, HOME_EXCLUDES, HOME) for item in HOME_ITEMS)
        elif name == "profiles":
            keys = default_profile_keys() if profile_items is None else set(profile_items)
            total += _profiles_use(keys)
        elif name == "extensions":
            # Au pire, toutes les extensions sont copiées en fichiers (site injoignable ou inconnues)
            total += SPACE_SMALL_STEP + (_tree_use(EXT_DIR) if EXT_DIR.is_dir() else 0)
        else:
            total += SPACE_SMALL_STEP
    return total


def _existing_parent(path: Path) -> Path:
    """Le plus proche dossier existant : c'est lui qui indique le disque où l'on écrira."""
    path = Path(os.path.abspath(path))
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def check_space(dest: Path, selected: set[str], encrypted: bool,
                profile_items: set[str] | None = None, log: Log | None = None) -> SpaceCheck:
    """Estime la taille de la sauvegarde et la compare à la place libre.

    L'estimation est prudente : aucune compression n'est comptée pour l'archive. Sans chiffrement,
    les données sont copiées telles quelles à la destination. Avec chiffrement, une copie en clair
    est d'abord préparée dans le dossier de travail (~/.cache), puis l'archive est écrite à la
    destination : si les deux sont sur le même disque, il doit contenir les deux à la fois.

    Si la place libre ne peut pas être mesurée, la vérification est ignorée : mieux vaut tenter
    la sauvegarde que la bloquer à tort.
    """
    say = log or (lambda _line: None)
    say("Estimation de la taille de la sauvegarde…")
    size = estimate_size(selected, profile_items)
    needed = size + max(size // 20, SPACE_MARGIN_MIN)
    shortages: list[Shortage] = []
    try:
        dest_at = _existing_parent(dest.parent)
        free = shutil.disk_usage(dest_at).free
        if not encrypted:
            if free < needed:
                shortages.append(Shortage("destination", dest_at, needed, free))
        else:
            work_at = _existing_parent(_workdir_path())
            work_free = shutil.disk_usage(work_at).free
            if os.stat(work_at).st_dev == os.stat(dest_at).st_dev:
                if free < 2 * needed:
                    shortages.append(Shortage("partagé", dest_at, 2 * needed, free))
            else:
                if free < needed:
                    shortages.append(Shortage("destination", dest_at, needed, free))
                if work_free < needed:
                    shortages.append(Shortage("temporaire", work_at, needed, work_free))
    except OSError as exc:
        say(f"Taille estimée : {fmt_size(size)} ; place libre non mesurable ({exc}) : "
            "vérification ignorée.")
        return SpaceCheck(size, needed, None, (), encrypted)
    check = SpaceCheck(size, needed, free, tuple(shortages), encrypted)
    say(check.summary())
    return check


BACKUP_STEPS = {
    "repos": _backup_repos, "apt": _backup_apt, "snap": _backup_snap,
    "flatpak": _backup_flatpak, "extensions": _backup_extensions,
    "dconf": _backup_dconf, "profiles": _backup_profiles, "home": _backup_home,
}


def _do_backup(dest: Path, selected: set[str], log: Log, progress: Progress,
               profile_items: set[str] | None = None) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    log(f"Destination : {dest}")
    steps = [c for c in COMPONENTS if c in selected]
    errors: list[str] = []
    for i, name in enumerate(steps):
        progress(i / len(steps), LABELS[name])
        log(f"== {LABELS[name]}")
        try:
            if name == "profiles":
                _backup_profiles(dest, log, profile_items)
            else:
                BACKUP_STEPS[name](dest, log)
        except Exception as exc:  # noqa: BLE001 - on continue avec les autres étapes
            log(f"Erreur : {exc}")
            errors.append(name)
    manifest = {
        "version": 1,
        "date": datetime.now().isoformat(timespec="seconds"),
        "machine": socket.gethostname(),
        "systeme": os_name(),
        "composants": [s for s in steps if s not in errors],
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    return errors


def archive_path(dest: Path) -> Path:
    """Chemin du fichier chiffré correspondant à un dossier de sauvegarde."""
    return dest.with_name(dest.name + ".tar.gz.gpg")


def backup(dest: Path, selected: set[str], log: Log, progress: Progress,
           passphrase: str | None = None, profile_items: set[str] | None = None,
           cloud: tuple[str, str] | None = None, skip_space_check: bool = False) -> list[str]:
    """Sauvegarde les composants choisis. Renvoie la liste de ceux qui ont échoué.

    Avec un mot de passe, la sauvegarde est préparée dans un dossier temporaire
    privé puis écrite uniquement sous forme d'archive chiffrée : aucune donnée
    en clair n'est déposée à la destination.

    `cloud` = (remote rclone, dossier distant) : une fois l'archive créée et vérifiée,
    une copie est envoyée via rclone. Exige le chiffrement ; un échec de l'envoi est
    signalé par « cloud » dans la liste renvoyée, sans remettre en cause la copie locale.

    Avant de copier quoi que ce soit, la taille est estimée et comparée à la place libre : si elle
    manque, `InsufficientSpace` est levée et rien n'est écrit. `skip_space_check` : l'appelant a
    déjà fait cette vérification (et l'utilisateur a choisi de continuer).
    """
    if cloud is not None and not passphrase:
        raise ValueError("L'envoi vers le cloud exige une sauvegarde chiffrée.")
    if not skip_space_check:
        space = check_space(dest, selected, bool(passphrase), profile_items, log)
        if not space.ok:
            raise InsufficientSpace(space)
    if cloud is None:
        return _backup_local(dest, selected, log, progress, passphrase, profile_items, True)
    errors = _backup_local(dest, selected, log,
                           lambda f, t: progress(f * 0.85, t), passphrase, profile_items, False)
    progress(0.85, LABELS["cloud"])
    log(f"== {LABELS['cloud']}")
    try:
        upload_to_cloud(archive_path(dest), cloud[0], cloud[1], log)
    except Exception as exc:  # noqa: BLE001 - la copie locale reste valable
        log(f"Erreur : {exc}")
        errors.append("cloud")
    progress(1.0, "Terminé")
    log(f"Bilan : {report(count_steps(selected, True), errors)}.")
    log("Sauvegarde terminée.")
    return errors


def _backup_local(dest: Path, selected: set[str], log: Log, progress: Progress,
                  passphrase: str | None, profile_items: set[str] | None,
                  announce: bool) -> list[str]:
    """Sauvegarde sur disque. `announce` : écrire le bilan et le message de fin."""
    if not passphrase:
        errors = _do_backup(dest, selected, log, lambda f, t: progress(f * 0.9, t), profile_items)
        progress(0.9, "Somme de contrôle")
        log("== Somme de contrôle (SHA-256)")
        n = write_folder_sums(dest)
        status, msg = verify_folder(dest)
        if status != OK:
            raise IntegrityError("La vérification de la sauvegarde a échoué : " + msg)
        log(f"{n} fichiers protégés par {FOLDER_SUMS}.")
        progress(1.0, "Terminé")
        if announce:
            log(f"Bilan : {report(count_steps(selected), errors)}.")
            log("Sauvegarde terminée.")
        return errors

    out = archive_path(dest)
    work = new_tempdir()
    try:
        folder = work / dest.name
        errors = _do_backup(folder, selected, log, lambda f, t: progress(f * 0.8, t), profile_items)
        out.parent.mkdir(parents=True, exist_ok=True)
        progress(0.8, "Chiffrement de l'archive")
        log("== Chiffrement de l'archive (AES-256)")
        encrypt_directory(folder, out, passphrase)
        progress(0.88, "Somme de contrôle")
        log("== Somme de contrôle (SHA-256)")
        write_checksum(out)
        progress(0.93, "Vérification de l'archive")
        log("== Vérification de l'archive")
        # Relecture complète depuis le disque : somme de contrôle, puis déchiffrement test
        status, err = verify_checksum(out)
        if status == OK:
            ok, err = _gpg_tar(out, passphrase, ["tzf", "-"])
        else:
            ok = False
        if not ok:
            remove_backup(out)
            raise IntegrityError("La vérification de l'archive a échoué : " + err)
        log(f"Archive chiffrée créée : {out}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    progress(1.0, "Terminé")
    if announce:
        log(f"Bilan : {report(count_steps(selected), errors)}.")
        log("Sauvegarde terminée.")
    return errors


# --------------------------------------------------------------------------
# Envoi vers le cloud (rclone)
# --------------------------------------------------------------------------

DEFAULT_CLOUD_PATH = "sauvegardes-bureau"
# Services dont l'accès par rclone est jugé fragile : (type rclone) -> avertissement
CLOUD_WARNINGS = {
    "protondrive": (
        "Proton Drive : l'accès par rclone est en bêta et repose sur une API que Proton ne "
        "documente pas. Il peut cesser de fonctionner sans préavis, la double authentification "
        "pose des problèmes à certains utilisateurs, et les clés de chiffrement doivent d'abord "
        "avoir été créées par une connexion via le navigateur. Ne vous y fiez pas comme seule "
        "copie : la copie locale reste toujours faite en premier."
    ),
}
_REMOTE_RE = re.compile(r"[\w.@+][\w.@+ -]*")


def rclone_remotes() -> dict[str, str]:
    """Services rclone déjà configurés : {nom: type}. Vide si rclone est absent ou muet.

    Seuls les noms et types sont lus (`listremotes --long`) : jamais les jetons d'accès."""
    if not has("rclone"):
        return {}
    try:
        r = subprocess.run(["rclone", "listremotes", "--long", "--ask-password=false"],
                           capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    remotes: dict[str, str] = {}
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            name, sep, kind = line.partition(":")
            if sep and name.strip():
                remotes[name.strip()] = kind.strip()
    return remotes


def clean_cloud_path(path: str) -> str:
    """Dossier distant nettoyé. Lève ValueError s'il est vide ou suspect."""
    path = path.strip().strip("/")
    parts = path.split("/")
    if not path or any(p in ("", ".", "..") for p in parts) or re.search(r"[\x00-\x1f]", path):
        raise ValueError("dossier distant invalide (exemple : sauvegardes-bureau)")
    return path


def cloud_target(remote: str, path: str) -> tuple[str, str]:
    """Valide (remote, dossier) avant tout envoi. Lève ValueError si quelque chose cloche."""
    remote = remote.strip().removesuffix(":")
    if not _REMOTE_RE.fullmatch(remote):
        raise ValueError(f"nom de service rclone invalide : {remote!r}")
    return remote, clean_cloud_path(path)


def _rclone(args: list[str], log: Log) -> int:
    return run(["rclone", *args, "--ask-password=false", "--retries", "3"], log)


def _remote_sizes(spec: str) -> dict[str, int] | None:
    """Tailles des fichiers d'un dossier distant (nom -> octets), None si illisible."""
    try:
        r = subprocess.run(["rclone", "lsjson", spec, "--files-only", "--ask-password=false"],
                           capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            return None
        return {str(e["Name"]): int(e["Size"]) for e in json.loads(r.stdout)}
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return None


def upload_to_cloud(archive: Path, remote: str, path: str, log: Log) -> None:
    """Envoie l'archive chiffrée et sa somme de contrôle, puis contrôle leur taille à distance.

    rclone vérifie lui-même l'envoi quand le service fournit des empreintes ; la taille est
    recontrôlée ici pour tous les services (certains, comme Proton Drive, n'en fournissent pas).
    Lève RuntimeError au moindre doute : une copie cloud douteuse ne doit pas passer pour bonne."""
    remote, path = cloud_target(remote, path)
    if not has("rclone"):
        raise RuntimeError("rclone n'est pas installé (paquet « rclone »).")
    if not archive.is_file():
        raise RuntimeError(f"archive introuvable : {archive}")
    kind = rclone_remotes().get(remote)
    if kind is None:
        raise RuntimeError(f"le service « {remote} » n'est pas configuré dans rclone "
                           "(lancez « rclone config » dans un terminal).")
    if kind in CLOUD_WARNINGS:
        log(CLOUD_WARNINGS[kind])
    files = [archive] + ([checksum_path(archive)] if checksum_path(archive).is_file() else [])
    log(f"Envoi vers {remote}:{path} ({', '.join(_fmt_size(f.stat().st_size) for f in files)})")
    for f in files:
        rc = _rclone(["copyto", f, f"{remote}:{path}/{f.name}", "-v",
                      "--stats", "15s", "--stats-one-line"], log)
        if rc != 0:
            raise RuntimeError(f"rclone a échoué pour {f.name} (code {rc})")
    sizes = _remote_sizes(f"{remote}:{path}")
    if sizes is None:
        raise RuntimeError("envoi terminé, mais impossible de relire le dossier distant pour le contrôler")
    for f in files:
        if sizes.get(f.name) != f.stat().st_size:
            raise RuntimeError(f"{f.name} : taille distante différente de la taille locale")
    log(f"Copie cloud vérifiée (taille) : {remote}:{path}/{archive.name}")


def cloud_rotate(remote: str, path: str, keep: int, pattern: re.Pattern, log: Log) -> None:
    """Supprime sur le cloud les plus anciennes archives automatiques au-delà de `keep`.

    Seuls les fichiers dont le nom correspond à `pattern` (archives automatiques) sont touchés."""
    remote, path = cloud_target(remote, path)
    sizes = _remote_sizes(f"{remote}:{path}")
    if sizes is None:
        log("Rotation cloud ignorée : dossier distant illisible.")
        return
    archives = sorted(n for n in sizes if n.endswith(".tar.gz.gpg") and pattern.match(n))
    for old in archives[:-keep] if keep > 0 else []:
        for name in (old, old + CHECKSUM_SUFFIX):
            if name in sizes:
                if _rclone(["deletefile", f"{remote}:{path}/{name}"], log) != 0:
                    log(f"Impossible de supprimer {name} sur le cloud.")
                else:
                    log(f"Ancienne sauvegarde cloud supprimée : {name}")


# --------------------------------------------------------------------------
# Restauration depuis le cloud (rclone)
# --------------------------------------------------------------------------

class CloudBackup(NamedTuple):
    """Archive chiffrée trouvée dans le dossier distant."""
    name: str
    size: int
    modified: str         # date ISO fournie par rclone, vide si inconnue
    has_checksum: bool    # la somme de contrôle « .sha256 » est aussi présente


# Nom d'une archive chiffrée, sans aucun séparateur de chemin
_CLOUD_ARCHIVE_RE = re.compile(r"[\w.@+-]+\.tar\.gz\.gpg")


def _check_remote(remote: str, path: str) -> tuple[str, str, str]:
    """Valide le service et le dossier. Renvoie (service, dossier, type rclone)."""
    remote, path = cloud_target(remote, path)
    if not has("rclone"):
        raise RuntimeError("rclone n'est pas installé (paquet « rclone »).")
    kind = rclone_remotes().get(remote)
    if kind is None:
        raise RuntimeError(f"le service « {remote} » n'est pas configuré dans rclone "
                           "(lancez « rclone config » dans un terminal).")
    return remote, path, kind


def list_cloud_backups(remote: str, path: str) -> list[CloudBackup]:
    """Archives chiffrées du dossier distant, les plus récentes d'abord.

    Lève RuntimeError (message lisible) si le service ou le dossier est inaccessible."""
    remote, path, _kind = _check_remote(remote, path)
    try:
        r = subprocess.run(["rclone", "lsjson", f"{remote}:{path}", "--files-only",
                            "--ask-password=false"],
                           capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise RuntimeError("le service cloud ne répond pas (délai dépassé)") from None
    except OSError as exc:
        raise RuntimeError(f"rclone n'a pas pu démarrer : {exc.strerror or exc}") from None
    if r.returncode == 3:  # code rclone « dossier introuvable »
        raise RuntimeError(f"le dossier « {path} » n'existe pas sur {remote}")
    if r.returncode != 0:
        lines = [l.strip() for l in r.stderr.splitlines() if l.strip()]
        raise RuntimeError(lines[-1][-300:] if lines else f"rclone a échoué (code {r.returncode})")
    try:
        entries = json.loads(r.stdout)
        names = {str(e["Name"]) for e in entries}
        found = [CloudBackup(str(e["Name"]), int(e["Size"]), str(e.get("ModTime") or ""),
                             str(e["Name"]) + CHECKSUM_SUFFIX in names)
                 for e in entries if _CLOUD_ARCHIVE_RE.fullmatch(str(e["Name"]))]
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("réponse de rclone illisible") from None
    return sorted(found, key=lambda b: (b.modified, b.name), reverse=True)


def download_from_cloud(remote: str, path: str, name: str, log: Log) -> tuple[Path, Path, str]:
    """Télécharge une archive chiffrée (et sa somme de contrôle) dans un dossier privé.

    Contrôle la taille reçue puis la somme de contrôle SHA-256 avant de rendre la main.
    Renvoie (dossier temporaire à supprimer ensuite, archive, OK | MISSING).
    Lève IntegrityError si l'archive est abîmée, RuntimeError pour les autres échecs."""
    if not _CLOUD_ARCHIVE_RE.fullmatch(name):
        raise ValueError(f"nom d'archive invalide : {name!r}")
    remote, path, kind = _check_remote(remote, path)
    if kind in CLOUD_WARNINGS:
        log(CLOUD_WARNINGS[kind])
    sizes = _remote_sizes(f"{remote}:{path}")
    if sizes is None:
        raise RuntimeError("impossible de lire le dossier distant")
    if name not in sizes:
        raise RuntimeError(f"« {name} » n'existe plus sur {remote}:{path}")
    names = [name] + ([name + CHECKSUM_SUFFIX] if name + CHECKSUM_SUFFIX in sizes else [])
    tmp = new_tempdir()
    try:
        log(f"Téléchargement depuis {remote}:{path} ({', '.join(_fmt_size(sizes[n]) for n in names)})")
        for n in names:
            rc = _rclone(["copyto", f"{remote}:{path}/{n}", tmp / n, "-v",
                          "--stats", "5s", "--stats-one-line"], log)
            if rc != 0:
                raise RuntimeError(f"rclone a échoué pour {n} (code {rc})")
            if (tmp / n).stat().st_size != sizes[n]:
                raise RuntimeError(f"{n} : taille reçue différente de la taille distante")
        archive = tmp / name
        os.chmod(archive, 0o600)
        status = check_archive(archive)  # IntegrityError si la somme ne correspond pas
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if status == OK:
        log(f"Intégrité vérifiée (SHA-256) : {name}")
    else:
        log(f"Pas de somme de contrôle pour {name} : intégrité non vérifiable.")
    return tmp, archive, status


# --------------------------------------------------------------------------
# Contrôle des copies cloud sans les télécharger
# --------------------------------------------------------------------------
#
# Le service cloud calcule (ou connaît déjà) l'empreinte de chaque fichier : `rclone lsjson --hash`
# la renvoie sans rien télécharger. On la compare à celle de la copie locale, calculée sur place.
# Les services n'offrent pas tous les mêmes empreintes (MD5, SHA-1, SHA-256…) ni toujours aucune :
# on ne compare que les types que les deux côtés ont en commun, et on le dit.

CLOUD_OK = "ok"          # taille et au moins une empreinte identiques à la référence
CLOUD_WEAK = "weak"      # taille identique, mais aucune empreinte comparable chez ce service
CLOUD_BAD = "bad"        # taille ou empreinte différente : la copie cloud est abîmée
CLOUD_NOREF = "noref"    # rien de fiable à comparer (pas de copie locale)

LOCAL_HASHERS = {"md5": hashlib.md5, "sha1": hashlib.sha1,
                 "sha256": hashlib.sha256, "sha512": hashlib.sha512}


class CloudCheck(NamedTuple):
    name: str
    status: str      # CLOUD_OK | CLOUD_WEAK | CLOUD_BAD | CLOUD_NOREF
    message: str
    size: int = 0


class _CloudFile(NamedTuple):
    name: str
    size: int
    hashes: dict[str, str]    # type rclone (md5, sha1…) -> empreinte en minuscules


def _cloud_files(remote: str, path: str) -> list[_CloudFile]:
    """Fichiers du dossier distant avec leurs empreintes, sans rien télécharger."""
    try:
        r = subprocess.run(["rclone", "lsjson", f"{remote}:{path}", "--files-only", "--hash",
                            "--ask-password=false"],
                           capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise RuntimeError("le service cloud ne répond pas (délai dépassé)") from None
    except OSError as exc:
        raise RuntimeError(f"rclone n'a pas pu démarrer : {exc.strerror or exc}") from None
    if r.returncode == 3:
        raise RuntimeError(f"le dossier « {path} » n'existe pas sur {remote}")
    if r.returncode != 0:
        lines = [l.strip() for l in r.stderr.splitlines() if l.strip()]
        raise RuntimeError(lines[-1][-300:] if lines else f"rclone a échoué (code {r.returncode})")
    try:
        return [_CloudFile(str(e["Name"]), int(e["Size"]),
                           {str(k).lower(): str(v).lower() for k, v in (e.get("Hashes") or {}).items() if v})
                for e in json.loads(r.stdout)]
    except (ValueError, KeyError, TypeError, AttributeError):
        raise RuntimeError("réponse de rclone illisible") from None


def _rclone_local_hash(path: Path, kind: str) -> str | None:
    """Empreinte d'un fichier local d'un type que hashlib ne connaît pas (QuickXor, Dropbox…)."""
    try:
        r = subprocess.run(["rclone", "hashsum", kind, str(path.parent), "--include", path.name,
                            "--ask-password=false"],
                           capture_output=True, text=True, timeout=3600, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in r.stdout.splitlines():
        digest, _, name = line.partition("  ")
        if r.returncode == 0 and name.strip() == path.name and digest.strip():
            return digest.strip().lower()
    return None


def _local_hashes(path: Path, kinds: set[str]) -> dict[str, str]:
    """Empreintes locales des types demandés (le fichier n'est lu qu'une fois pour hashlib)."""
    hashers = {k: LOCAL_HASHERS[k]() for k in kinds if k in LOCAL_HASHERS}
    if hashers:
        with open(path, "rb") as f:
            while chunk := f.read(1024 * 1024):
                for h in hashers.values():
                    h.update(chunk)
    found = {k: h.hexdigest() for k, h in hashers.items()}
    for kind in kinds - set(hashers):
        digest = _rclone_local_hash(path, kind)
        if digest:
            found[kind] = digest
    return found


def _recorded_sha256(archive: Path) -> str | None:
    """SHA-256 noté à la création, dans le .sha256 voisin de l'archive locale."""
    try:
        entries = _parse_sums(checksum_path(archive).read_text(encoding="utf-8"))
    except OSError:
        return None
    return entries[0][0] if entries else None


def _remote_recorded_sha256(remote: str, path: str, name: str) -> str | None:
    """SHA-256 noté à la création, lu dans le petit fichier .sha256 du cloud (jamais l'archive)."""
    try:
        r = subprocess.run(["rclone", "cat", f"{remote}:{path}/{name}{CHECKSUM_SUFFIX}",
                            "--count", "4096", "--ask-password=false"],
                           capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    entries = _parse_sums(r.stdout) if r.returncode == 0 else []
    return entries[0][0] if entries else None


def _check_cloud_file(remote: str, path: str, f: _CloudFile, local_folders: list[Path],
                      has_sums: bool) -> CloudCheck:
    note = "" if has_sums else " · fichier .sha256 absent du cloud"
    local = next((c for d in local_folders if (c := d / f.name).is_file()), None)
    skipped = ""
    if local is not None:
        try:
            local_size = local.stat().st_size
            # SHA-256 en plus : sert à s'assurer que la copie locale, elle, n'est pas abîmée
            # (sinon on accuserait à tort le cloud)
            wanted = {"sha256"} if local_size != f.size else set(f.hashes) | {"sha256"}
            mine = _local_hashes(local, wanted)
        except OSError:
            local = None
    if local is not None:
        recorded = _recorded_sha256(local)
        if recorded is None or mine.get("sha256") == recorded:
            if local_size != f.size:
                return CloudCheck(f.name, CLOUD_BAD,
                                  f"taille cloud ({_fmt_size(f.size)}) différente de la copie locale "
                                  f"({_fmt_size(local_size)})", f.size)
            common = [k for k in f.hashes if k in mine]
            differ = [k for k in common if mine[k] != f.hashes[k]]
            if differ:
                return CloudCheck(f.name, CLOUD_BAD,
                                  f"empreinte {', '.join(differ)} différente de la copie locale : "
                                  "la copie cloud est abîmée ou incomplète", f.size)
            if common:
                return CloudCheck(f.name, CLOUD_OK,
                                  f"identique à la copie locale (taille, {', '.join(common)})" + note, f.size)
            return CloudCheck(f.name, CLOUD_WEAK,
                              "même taille que la copie locale, mais ce service ne fournit pas "
                              "d'empreinte comparable : copie non confirmée" + note, f.size)
        skipped = "copie locale abîmée, ignorée ; "
    digest = _remote_recorded_sha256(remote, path, f.name) if has_sums else None
    cloud_sha = f.hashes.get("sha256")
    if digest and cloud_sha:
        if digest == cloud_sha:
            return CloudCheck(f.name, CLOUD_OK,
                              skipped + "empreinte SHA-256 identique à celle notée à la création", f.size)
        return CloudCheck(f.name, CLOUD_BAD,
                          skipped + "empreinte SHA-256 différente de celle notée à la création : "
                          "la copie cloud est abîmée", f.size)
    why = "copie locale abîmée, ignorée" if skipped else "pas de copie locale à comparer"
    return CloudCheck(f.name, CLOUD_NOREF,
                      why + ("" if cloud_sha else " et ce service ne fournit pas SHA-256")
                      + " : seule la taille est connue" + note, f.size)


def verify_cloud(remote: str, path: str, local_folders: list[Path] | None = None,
                 progress: Progress | None = None,
                 cancelled: Callable[[], bool] | None = None) -> list[CloudCheck]:
    """Contrôle les archives du dossier distant sans les télécharger, la plus récente d'abord.

    Chaque archive est comparée à sa copie locale (cherchée dans `local_folders`) par taille et
    par les empreintes que le service fournit. À défaut de copie locale, l'empreinte SHA-256 notée
    à la création (petit fichier .sha256 du cloud) sert de référence si le service fournit SHA-256.
    Lève RuntimeError (message lisible) si le service ou le dossier est inaccessible."""
    remote, path, _kind = _check_remote(remote, path)
    files = _cloud_files(remote, path)
    names = {f.name for f in files}
    archives = sorted((f for f in files if _CLOUD_ARCHIVE_RE.fullmatch(f.name)),
                      key=lambda f: f.name, reverse=True)
    results = []
    for index, f in enumerate(archives):
        if cancelled and cancelled():
            break
        if progress:
            progress(index / len(archives), f"{index + 1}/{len(archives)} : {f.name}")
        try:
            results.append(_check_cloud_file(remote, path, f, list(local_folders or []),
                                             f.name + CHECKSUM_SUFFIX in names))
        except OSError as exc:
            results.append(CloudCheck(f.name, CLOUD_NOREF, f"lecture impossible : {exc.strerror or exc}", f.size))
    if progress:
        progress(1.0, "")
    return results


# --------------------------------------------------------------------------
# Restauration
# --------------------------------------------------------------------------

def detect(src: Path) -> dict:
    """Indique quels composants contient un dossier de sauvegarde."""
    components = {
        "repos": any(_repo_files(src, s) for s in REPO_DIRS),
        "apt": (src / "apt-manual.txt").is_file(),
        "snap": (src / "snap.txt").is_file(),
        "flatpak": (src / "flatpak.txt").is_file(),
        "extensions": (src / EXT_INDEX).is_file() or (src / "extensions").is_dir(),
        "dconf": (src / "dconf-reglages.ini").is_file(),
        "profiles": (src / PROFILE_INDEX).is_file(),
        "home": (src / "home").is_dir(),
    }
    manifest: dict = {}
    try:
        manifest = json.loads((src / "manifest.json").read_text())
    except (OSError, ValueError):
        pass
    return {"components": components, "manifest": manifest}


# --------------------------------------------------------------------------
# Aperçu avant restauration (ne modifie rien sur la machine)
# --------------------------------------------------------------------------

NEW, SAME, CHANGE, SKIP = "new", "same", "change", "skip"
STATUS_LABELS = {NEW: "À ajouter", SAME: "Déjà présent", CHANGE: "Sera modifié", SKIP: "Ignoré"}

# Formulations du résumé de chaque composant : pour (à ajouter, à modifier, identique, ignoré),
# la forme au singulier puis au pluriel
PHRASES = {
    "repos": (("à ajouter",) * 2, ("à remplacer",) * 2, ("identique", "identiques"), ("ignoré", "ignorés")),
    "apt": (("à installer",) * 2, ("", ""), ("déjà installé", "déjà installés"),
            ("introuvable", "introuvables")),
    "snap": (("à installer",) * 2, ("", ""), ("déjà installé", "déjà installés"), ("ignoré", "ignorés")),
    "flatpak": (("à installer",) * 2, ("", ""), ("déjà installé", "déjà installés"),
                ("ignoré", "ignorés")),
    "extensions": (("à installer",) * 2, ("à remplacer",) * 2, ("déjà installée", "déjà installées"),
                   ("ignorée", "ignorées")),
    "dconf": (("à ajouter",) * 2, ("à modifier",) * 2, ("identique", "identiques"), ("", "")),
    "profiles": ((("à ajouter",) * 2), ("existant à fusionner", "existants à fusionner"),
                 ("", ""), ("ignoré", "ignorés")),
    "home": (("nouveau fichier", "nouveaux fichiers"), ("fichier à remplacer", "fichiers à remplacer"),
             ("", ""), ("", "")),
}


def phrase(name: str, index: int, n: int) -> str:
    """Formulation du résumé (index : 0 ajouter, 1 modifier, 2 identique, 3 ignoré) accordée à n."""
    return PHRASES[name][index][0 if n < 2 else 1]


DCONF_LABELS = {
    "org/gnome/desktop/interface": "Apparence et polices",
    "org/gnome/desktop/background": "Fond d'écran",
    "org/gnome/desktop/wm/keybindings": "Raccourcis des fenêtres",
    "org/gnome/desktop/wm/preferences": "Fenêtres",
    "org/gnome/settings-daemon/plugins/media-keys": "Raccourcis multimédia",
    "org/gnome/shell": "Shell GNOME (favoris, extensions activées)",
    "org/gnome/mutter": "Gestionnaire de fenêtres",
    "org/gnome/terminal/legacy": "Terminal",
}


class Item(NamedTuple):
    name: str
    status: str
    detail: str = ""
    warn: bool = False
    group: str = ""  # section dconf, pour regrouper les réglages


@dataclass
class Preview:
    """Ce que la restauration d'un composant ferait sur cette machine."""
    items: list[Item] = field(default_factory=list)  # à ajouter, à modifier ou ignorés
    same: list[str] = field(default_factory=list)    # déjà identiques (noms listés)
    same_count: int = 0                              # déjà identiques (non listés)
    note: str = ""
    error: str = ""

    @property
    def n_same(self) -> int:
        return len(self.same) + self.same_count

    def count(self, status: str) -> int:
        return sum(1 for i in self.items if i.status == status)


def plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n < 2 else many}"


def count_steps(selected: set[str], cloud: bool = False) -> int:
    """Nombre d'étapes (composants) qu'une sauvegarde ou une restauration va exécuter,
    plus l'envoi cloud s'il est demandé."""
    return sum(1 for c in COMPONENTS if c in selected) + (1 if cloud else 0)


def report(total: int, errors: list[str]) -> str:
    """Bilan en une ligne, par exemple « 3 étapes réussies, 1 en échec (Snaps) »."""
    text = plural(max(total - len(errors), 0), "étape réussie", "étapes réussies")
    if errors:
        names = ", ".join(LABELS.get(e, e) for e in errors)
        text += f", {len(errors)} en échec ({names})"
    return text


def _short(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _capture_or_empty(cmd: list[str]) -> str:
    try:
        return capture(cmd)
    except (subprocess.CalledProcessError, OSError):
        return ""


def dconf_label(path: str) -> str:
    """Nom lisible d'une section dconf (le chemin brut à défaut)."""
    if path in DCONF_LABELS:
        return DCONF_LABELS[path]
    prefix = "org/gnome/shell/extensions/"
    if path.startswith(prefix):
        return "Extension " + path[len(prefix):]
    return path


def _preview_apt(src: Path) -> Preview:
    wanted = sorted(set(_read_lines(src / "apt-manual.txt")))
    installed = set()
    for line in capture(["dpkg-query", "-W", "-f", r"${Package}\t${db:Status-Abbrev}\n"]).splitlines():
        name, _, status = line.partition("\t")
        if status.startswith("ii"):
            installed.add(name)
    available = set(capture(["apt-cache", "pkgnames"]).split())
    # Des dépôts de la sauvegarde absents de cette machine peuvent fournir les paquets inconnus
    adds_repos = bool(_preview_repos(src).items)
    p = Preview()
    for pkg in wanted:
        base = pkg.split(":")[0]
        if base in installed:
            p.same.append(pkg)
        elif base in available:
            p.items.append(Item(pkg, NEW))
        elif adds_repos:
            p.items.append(Item(pkg, NEW, "inconnu pour l'instant : installé si un dépôt tiers "
                                          "de la sauvegarde le fournit", warn=True))
        else:
            p.items.append(Item(pkg, SKIP, "inconnu des dépôts de cette machine"))
    if any(i.warn for i in p.items):
        p.note = ("Les paquets marqués ⚠ ne sont pas connus de apt pour le moment. Ils seront "
                  "installés si les dépôts tiers de la sauvegarde (cochez « Dépôts tiers ») "
                  "les fournissent, sinon ignorés.")
    elif p.count(SKIP):
        p.note = "Les paquets introuvables seront ignorés : aucun dépôt de la sauvegarde ne les fournit."
    return p


def _preview_names(src: Path, listfile: str, tool: str, installed_cmd: list[str],
                   first_line_is_header: bool) -> Preview:
    wanted = sorted(set(_read_lines(src / listfile)))
    if not has(tool):
        return Preview(items=[Item(n, SKIP, f"{tool} n'est pas installé sur cette machine") for n in wanted])
    lines = _capture_or_empty(installed_cmd).splitlines()
    if first_line_is_header:
        lines = lines[1:]
    installed = {l.split()[0] for l in lines if l.strip()}
    p = Preview()
    for name in wanted:
        if name in installed:
            p.same.append(name)
        else:
            p.items.append(Item(name, NEW))
    return p


def _preview_snap(src: Path) -> Preview:
    return _preview_names(src, "snap.txt", "snap", ["snap", "list"], True)


def _preview_flatpak(src: Path) -> Preview:
    return _preview_names(src, "flatpak.txt", "flatpak",
                          ["flatpak", "list", "--app", "--columns=application"], False)


def _repo_summary(path: Path) -> str:
    """Adresse du dépôt décrit par un fichier .list / .sources (ou « clé de signature »)."""
    if path.suffix not in (".list", ".sources"):
        return "clé de signature"
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("deb ", "deb-src ", "URIs:")):
            m = re.search(r"(?:https?|ftp)://[^\s\]]+", line)
            if m:
                return m.group(0).split("://", 1)[1]
    return ""


def _preview_repos(src: Path) -> Preview:
    p = Preview()
    for sub in REPO_DIRS:
        for f in _repo_files(src, sub):
            label = f"{sub}/{f.name}"
            detail = _repo_summary(f)
            dest = APT_DIR / sub / f.name
            if not dest.exists():
                p.items.append(Item(label, NEW, detail))
                continue
            try:
                same = filecmp.cmp(f, dest, shallow=False)
            except OSError:
                same = False
            if same:
                p.same.append(label)
            else:
                p.items.append(Item(label, CHANGE, "diffère du fichier actuel"
                                    + (f" · {detail}" if detail else "")))
    return p


def _read_metadata(folder: Path) -> dict:
    try:
        data = json.loads((folder / "metadata.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _ext_version(meta: dict) -> str:
    return str(meta.get("version-name") or meta.get("version") or "?")


def _gnome_major() -> str | None:
    if not has("gnome-shell"):
        return None
    m = re.search(r"(\d+)", _capture_or_empty(["gnome-shell", "--version"]))
    return m.group(1) if m else None


def _load_ext_index(src: Path) -> list[dict] | None:
    """Liste des extensions d'une sauvegarde récente ; None pour une ancienne (fichiers copiés)."""
    try:
        data = json.loads((src / EXT_INDEX).read_text())
        entries = data["extensions"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return [e for e in entries if isinstance(e, dict) and e.get("uuid")]


def _preview_extensions(src: Path) -> Preview:
    entries = _load_ext_index(src)
    if entries is None:
        return _preview_extensions_files(src)
    major = _gnome_major()
    p = Preview()
    for e in entries:
        uuid, name = str(e["uuid"]), str(e.get("name") or e["uuid"])
        if (EXT_DIR / uuid).is_dir():
            p.same.append(name)
            continue
        has_files = (src / "extensions" / uuid).is_dir()
        parts = [f"v{e.get('version', '?')}"]
        status, warn = NEW, False
        if e.get("ego") is False and not has_files:
            status = SKIP
            parts.append("absente de extensions.gnome.org et sans copie")
        elif e.get("ego") is False:
            parts.append("hors extensions.gnome.org : fichiers copiés depuis la sauvegarde")
        else:
            parts.append("téléchargée depuis extensions.gnome.org"
                         + (f" (version compatible avec GNOME {major})" if major else ""))
            if has_files:
                parts.append("copie de secours disponible")
        if e.get("enabled"):
            parts.append("activée dans la sauvegarde")
        if name != uuid:
            parts.append(uuid)
        p.items.append(Item(name, status, " · ".join(parts), warn))
    if p.count(NEW):
        p.note = ("Les extensions sont retéléchargées depuis extensions.gnome.org (connexion "
                  "internet requise) : si aucune version ne convient à ce GNOME, elle est ignorée.")
    return p


def _preview_extensions_files(src: Path) -> Preview:
    major = _gnome_major()
    try:
        enabled_in_backup = set(_read_lines(src / "extensions-actives.txt"))
    except OSError:
        enabled_in_backup = set()
    p = Preview()
    for folder in sorted(d for d in (src / "extensions").iterdir() if d.is_dir()):
        uuid = folder.name
        meta = _read_metadata(folder)
        name = str(meta.get("name") or uuid)
        installed = EXT_DIR / uuid
        parts: list[str] = []
        if not installed.is_dir():
            status = NEW
            parts.append(f"v{_ext_version(meta)}")
        else:
            old, new = _ext_version(_read_metadata(installed)), _ext_version(meta)
            if old == new:
                p.same.append(name)
                continue
            status = CHANGE
            parts.append(f"v{old} → v{new}")
        if uuid in enabled_in_backup:
            parts.append("activée dans la sauvegarde")
        warn = False
        versions = {str(v).split(".")[0] for v in meta.get("shell-version", []) or []}
        if major and versions and major not in versions:
            warn = True
            parts.append(f"non déclarée compatible avec GNOME {major}")
        if name != uuid:
            parts.append(uuid)
        p.items.append(Item(name, status, " · ".join(parts), warn))
    return p


def _parse_dconf(text: str) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in text.splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
        elif current is not None and "=" in line:
            key, _, value = line.partition("=")
            current[key.strip()] = value.strip()
    return sections


def _preview_dconf(src: Path) -> Preview:
    if not has("dconf"):
        raise RuntimeError("le paquet dconf-cli est requis")
    wanted = _parse_dconf((src / "dconf-reglages.ini").read_text(errors="replace"))
    current = _parse_dconf(capture(["dconf", "dump", "/"]))
    p = Preview()
    for section in sorted(wanted):
        here = current.get(section, {})
        for key, value in sorted(wanted[section].items()):
            if key not in here:
                p.items.append(Item(key, NEW, _short(value), group=section))
            elif here[key] != value:
                p.items.append(Item(key, CHANGE, f"{_short(here[key], 40)} → {_short(value, 40)}",
                                    group=section))
            else:
                p.same_count += 1
    if p.items:
        p.note = "Les réglages absents de la sauvegarde restent tels quels."
    return p


def _preview_home(src: Path) -> Preview:
    """Simulation (rsync -n) : compte les fichiers qui seraient créés ou remplacés."""
    if not has("rsync"):
        raise RuntimeError("le paquet rsync est requis")
    proc = subprocess.Popen(
        ["rsync", "-a", "-n", "--out-format=%i|%n", f"{src / 'home'}/", f"{HOME}/"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace",
    )
    assert proc.stdout is not None
    counts: dict[str, list[int]] = {}  # chemin -> [nouveaux, remplacés]
    for line in proc.stdout:
        flags, _, name = line.rstrip("\n").partition("|")
        if flags[:1] not in (">", "c") or flags[1:2] not in ("f", "L"):
            continue  # dossiers et autres entrées : sans intérêt ici
        top = max((i for i in HOME_ITEMS if name == i or name.startswith(i + "/")),
                  key=len, default="")
        depth = len(top.split("/")) + 1 if top else 2
        key = "/".join(name.split("/")[:depth])
        counts.setdefault(key, [0, 0])[0 if set(flags[2:]) == {"+"} else 1] += 1
    proc.wait()
    p = Preview()
    for key in sorted(counts):
        new, changed = counts[key]
        detail = ", ".join(t for t in (
            plural(new, "nouveau fichier", "nouveaux fichiers") if new else "",
            plural(changed, "fichier remplacé", "fichiers remplacés") if changed else "") if t)
        p.items.append(Item(f"~/{key}", CHANGE if changed else NEW, detail))
    if p.items:
        p.note = "Chaque fichier remplacé est conservé avec l'extension .avant-restauration."
    return p


PREVIEW_STEPS = {
    "repos": _preview_repos, "apt": _preview_apt, "snap": _preview_snap,
    "flatpak": _preview_flatpak, "extensions": _preview_extensions,
    "dconf": _preview_dconf, "profiles": _preview_profiles, "home": _preview_home,
}


def preview_all(src: Path, names: list[str]) -> dict[str, Preview]:
    """Compare la sauvegarde à cette machine, composant par composant."""
    result: dict[str, Preview] = {}
    for name in COMPONENTS:
        if name not in names:
            continue
        try:
            result[name] = PREVIEW_STEPS[name](src)
        except Exception as exc:  # noqa: BLE001 - un composant en échec n'empêche pas les autres
            result[name] = Preview(error=str(exc) or exc.__class__.__name__)
    return result


def summarize(name: str, preview: Preview) -> str:
    """Résumé d'une ligne : « 12 à installer · 148 déjà installés »."""
    if preview.error:
        return f"Aperçu indisponible : {preview.error}"
    new, change, skip = preview.count(NEW), preview.count(CHANGE), preview.count(SKIP)
    parts = [f"{n} {phrase(name, i, n)}" for i, n in enumerate((new, change, preview.n_same, skip))
             if n and phrase(name, i, n)]
    if not parts:
        return "Rien à changer"
    return ("Rien à changer · " if not (new or change) else "") + " · ".join(parts)


# --------------------------------------------------------------------------
# Comparaison de deux sauvegardes (dépôts, applications, extensions)
# --------------------------------------------------------------------------

COMPARABLE = ("repos", "apt", "snap", "flatpak", "extensions")
LIST_FILES = {"apt": "apt-manual.txt", "snap": "snap.txt", "flatpak": "flatpak.txt"}


@dataclass
class Diff:
    """Différences d'un composant entre une sauvegarde ancienne et une récente."""
    added: list[str] = field(default_factory=list)            # présents seulement dans la récente
    removed: list[str] = field(default_factory=list)          # présents seulement dans l'ancienne
    changed: list[tuple[str, str]] = field(default_factory=list)  # (nom, détail)
    same_count: int = 0
    details: dict[str, str] = field(default_factory=dict)     # précision par nom (adresse d'un dépôt…)
    error: str = ""   # comparaison impossible (composant absent d'une des deux sauvegardes…)

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


def _ext_entries(src: Path) -> dict[str, dict] | None:
    """Extensions d'une sauvegarde, par uuid (récente : extensions.json ; ancienne : dossier)."""
    entries = _load_ext_index(src)
    if entries is not None:
        return {str(e["uuid"]): e for e in entries}
    folder = src / "extensions"
    if not folder.is_dir():
        return None
    try:
        enabled = set(_read_lines(src / "extensions-actives.txt"))
    except OSError:
        enabled = set()
    result = {}
    for d in sorted(x for x in folder.iterdir() if x.is_dir()):
        meta = _read_metadata(d)
        result[d.name] = {"uuid": d.name, "name": str(meta.get("name") or d.name),
                          "version": _ext_version(meta), "enabled": d.name in enabled}
    return result


def _repo_map(src: Path) -> dict[str, Path] | None:
    """Fichiers de dépôts et clés d'une sauvegarde, par nom (« sources.list.d/x.list »)."""
    files = {f"{sub}/{f.name}": f for sub in REPO_DIRS for f in _repo_files(src, sub)}
    return files or None


def _diff_repos(old: dict[str, Path], new: dict[str, Path]) -> Diff:
    d = Diff(added=sorted(new.keys() - old.keys(), key=str.lower),
             removed=sorted(old.keys() - new.keys(), key=str.lower))
    for name in d.added:
        d.details[name] = _repo_summary(new[name])
    for name in d.removed:
        d.details[name] = _repo_summary(old[name])
    for name in sorted(old.keys() & new.keys(), key=str.lower):
        try:
            same = filecmp.cmp(old[name], new[name], shallow=False)
        except OSError:
            same = False
        if same:
            d.same_count += 1
        else:
            summary = _repo_summary(new[name])
            d.changed.append((name, "contenu différent" + (f" · {summary}" if summary else "")))
    return d


def _diff_lists(old: set[str], new: set[str]) -> Diff:
    return Diff(added=sorted(new - old, key=str.lower), removed=sorted(old - new, key=str.lower),
                same_count=len(old & new))


def _diff_extensions(old: dict[str, dict], new: dict[str, dict]) -> Diff:
    def label(e: dict) -> str:
        return str(e.get("name") or e["uuid"])

    d = Diff(added=sorted((label(new[u]) for u in new.keys() - old.keys()), key=str.lower),
             removed=sorted((label(old[u]) for u in old.keys() - new.keys()), key=str.lower))
    for uuid in sorted(old.keys() & new.keys()):
        a, b = old[uuid], new[uuid]
        notes = []
        if str(a.get("version")) != str(b.get("version")):
            notes.append(f"v{a.get('version', '?')} → v{b.get('version', '?')}")
        if bool(a.get("enabled")) != bool(b.get("enabled")):
            notes.append("activée" if b.get("enabled") else "désactivée")
        if notes:
            d.changed.append((label(b), " · ".join(notes)))
        else:
            d.same_count += 1
    d.changed.sort(key=lambda c: c[0].lower())
    return d


def compare_backups(old: Path, new: Path) -> dict[str, Diff]:
    """Ce qui a été ajouté, retiré ou modifié entre `old` et `new` (ne modifie rien)."""
    result: dict[str, Diff] = {}
    for name in COMPARABLE:
        try:
            if name == "extensions":
                a, b = _ext_entries(old), _ext_entries(new)
            elif name == "repos":
                a, b = _repo_map(old), _repo_map(new)
            else:
                f = LIST_FILES[name]
                a = set(_read_lines(old / f)) if (old / f).is_file() else None
                b = set(_read_lines(new / f)) if (new / f).is_file() else None
            if a is None and b is None:
                continue  # composant absent des deux : rien à dire
            if a is None or b is None:
                side = "l'ancienne" if a is None else "la récente"
                result[name] = Diff(error=f"Absent de {side} sauvegarde : comparaison impossible")
            elif name == "extensions":
                result[name] = _diff_extensions(a, b)
            elif name == "repos":
                result[name] = _diff_repos(a, b)
            else:
                result[name] = _diff_lists(a, b)
        except Exception as exc:  # noqa: BLE001 - un composant en échec n'empêche pas les autres
            result[name] = Diff(error=str(exc) or exc.__class__.__name__)
    return result


def diff_summary(d: Diff) -> str:
    """Résumé d'une ligne : « 3 ajoutés · 1 retiré · 140 inchangés »."""
    if d.error:
        return d.error
    parts = []
    if d.added:
        parts.append(plural(len(d.added), "ajouté", "ajoutés"))
    if d.removed:
        parts.append(plural(len(d.removed), "retiré", "retirés"))
    if d.changed:
        parts.append(plural(len(d.changed), "modifié", "modifiés"))
    if not parts:
        return "Aucun changement" + (f" ({d.same_count} identiques)" if d.same_count else "")
    if d.same_count:
        parts.append(plural(d.same_count, "inchangé", "inchangés"))
    return " · ".join(parts)



PICKABLE = ("apt", "snap", "flatpak", "profiles")  # composants dont on peut choisir les éléments un par un


def installable(preview: Preview, name: str = "") -> list[str]:
    """Noms que la restauration installerait (ceux qu'on peut cocher un par un).

    Pour les profils, ceux qui existent déjà sont aussi proposés : ils seront fusionnés."""
    wanted = (NEW, CHANGE) if name == "profiles" else (NEW,)
    return sorted(i.name for i in preview.items if i.status in wanted)


def restore_overview(previews: dict[str, Preview], selected: set[str],
                     only: dict[str, set[str]] | None = None) -> str:
    """Une ligne par composant choisi, pour la confirmation avant restauration."""
    lines = []
    for n in COMPONENTS:
        if n not in selected or n not in previews:
            continue
        if only and n in only:
            total = len(installable(previews[n], n))
            lines.append(f"• {LABELS[n]} : {len(only[n])} choisis sur {total} à installer")
        else:
            lines.append(f"• {LABELS[n]} : {summarize(n, previews[n])}")
    return "\n".join(lines)


def _restore_root(src: Path, parts: list[str], log: Log, on_marker: Callable[[str], None],
                  only: dict[str, set[str]] | None = None) -> bool:
    """Dépôts, paquets apt et snaps : un seul appel pkexec (une seule authentification).

    `only` limite apt et snap aux éléments cochés (tout sinon).
    """
    only = only or {}
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    tmp = Path(tempfile.mkdtemp(prefix="sauvegarde-bureau-", dir=runtime if runtime else None))
    try:
        lines = ["#!/bin/bash", "export DEBIAN_FRONTEND=noninteractive"]

        if "repos" in parts:
            lines.append("echo '== repos'")
            for sub in REPO_DIRS:
                for f in _repo_files(src, sub):
                    staged = tmp / sub / f.name  # copie locale : root ne lit pas toujours un disque externe
                    staged.parent.mkdir(exist_ok=True)
                    shutil.copy2(f, staged)
                    lines.append(
                        f"install -D -m 644 {shlex.quote(str(staged))} "
                        f"{shlex.quote(str(APT_DIR / sub / f.name))}"
                    )
            lines.append("apt-get update || echo 'apt update a signalé des erreurs "
                         "(un dépôt n'\\''est peut-être pas compatible avec cette version)'")

        if "apt" in parts:
            wanted = _read_lines(src / "apt-manual.txt")
            if "apt" in only:
                wanted = [p for p in wanted if p in only["apt"]]
                log(f"Restauration sélective : {len(wanted)} paquets choisis")
            wanted_file, avail_file, ok_file = tmp / "apt-voulus.txt", tmp / "apt-dispo.txt", tmp / "apt.txt"
            wanted_file.write_text("\n".join(wanted) + "\n")
            w, a, q = (shlex.quote(str(f)) for f in (wanted_file, avail_file, ok_file))
            # La liste des paquets disponibles est lue ici, une fois les dépôts tiers ajoutés
            # et « apt-get update » exécuté : un paquet fourni par un PPA de la sauvegarde
            # est donc bien installé, et seuls les paquets réellement introuvables sont ignorés.
            lines += [
                "echo '== apt'",
                f"apt-cache pkgnames | sort -u > {a}",
                "awk 'NR==FNR { known[$1] = 1; next } "
                "{ name = $0; sub(/:.*/, \"\", name); "
                "if (name in known) print $0; "
                "else print \"Paquet introuvable sur ce système (ignoré) : \" $0 }' "
                f"{a} {w} > {q}.tmp",
                f"grep -v '^Paquet introuvable' {q}.tmp > {q}",
                f"grep '^Paquet introuvable' {q}.tmp || true",
                f"if ! xargs -r -a {q} apt-get install -y; then",
                "  echo 'Installation groupée échouée, essai paquet par paquet…'",
                f"  while read -r p; do apt-get install -y \"$p\" || echo \"Échec apt : $p\"; done < {q}",
                "fi",
            ]

        if "snap" in parts:
            snaps = _read_lines(src / "snap.txt")
            if "snap" in only:
                snaps = [n for n in snaps if n in only["snap"]]
                log(f"Restauration sélective : {len(snaps)} snaps choisis")
            snap_list = tmp / "snap.txt"
            snap_list.write_text("\n".join(snaps) + "\n")
            q = shlex.quote(str(snap_list))
            lines += [
                "echo '== snap'",
                f"while read -r s; do [ -n \"$s\" ] || continue; "
                f"snap install \"$s\" || snap install --classic \"$s\" || echo \"Échec snap : $s\"; done < {q}",
            ]

        script = tmp / "restauration.sh"
        script.write_text("\n".join(lines) + "\n")

        def marker(line: str) -> None:
            if line.startswith("== "):
                on_marker(line[3:].strip())

        log("Demande des droits administrateur…")
        rc = run(["pkexec", "/bin/bash", script], log, on_line=marker)
        if rc in (126, 127) and not has("pkexec"):
            log("pkexec est introuvable.")
        elif rc in (126, 127):
            log("Authentification refusée ou annulée : dépôts, paquets et snaps non restaurés.")
        return rc == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _restore_flatpak(src: Path, log: Log, only: dict[str, set[str]] | None = None) -> None:
    if not has("flatpak"):
        log("Flatpak n'est pas installé : étape ignorée (installez le paquet « flatpak » puis relancez).")
        return
    run(["flatpak", "remote-add", "--if-not-exists", "flathub",
         "https://dl.flathub.org/repo/flathub.flatpakrepo"], log)
    apps = _read_lines(src / "flatpak.txt")
    if only and "flatpak" in only:
        apps = [a for a in apps if a in only["flatpak"]]
        log(f"Restauration sélective : {len(apps)} flatpaks choisis")
    for app in apps:
        if run(["flatpak", "install", "-y", "--noninteractive", "flathub", app], log) != 0:
            log(f"Échec flatpak : {app}")


def _shell_version() -> str | None:
    """Version de GNOME Shell telle que l'attend extensions.gnome.org (« 46 », ou « 3.38 »)."""
    if not has("gnome-shell"):
        return None
    m = re.search(r"(\d+)\.(\d+)", _capture_or_empty(["gnome-shell", "--version"]))
    if not m:
        return None
    return m.group(1) if int(m.group(1)) >= 40 else f"{m.group(1)}.{m.group(2)}"


def _unpack_extension(zip_path: Path, uuid: str, log: Log) -> None:
    """Installe un zip d'extension dans ~/.local/share/gnome-shell/extensions/<uuid>."""
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        if any(n.startswith("/") or ".." in Path(n).parts for n in names):
            raise ValueError("archive invalide (chemins suspects)")
        try:
            meta = json.loads(zf.read("metadata.json"))
        except (KeyError, ValueError) as exc:
            raise ValueError("archive sans metadata.json valide") from exc
        if meta.get("uuid") != uuid:
            raise ValueError(f"l'archive contient « {meta.get('uuid')} », pas « {uuid} »")
        if has("gnome-extensions"):
            if run(["gnome-extensions", "install", "--force", zip_path], log) == 0:
                return
            log("gnome-extensions install a échoué : extraction directe.")
        target = EXT_DIR / uuid
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True)
        zf.extractall(target)


def _install_from_ego(uuid: str, shell: str | None, log: Log, pk=None) -> str:
    """Télécharge et installe la version compatible. Renvoie « ok », « incompatible » ou « absent ».

    Lève _Offline si extensions.gnome.org est injoignable.
    """
    info = _ego_info(uuid, shell, pk)
    if not info or not info.get("download_url"):
        # Le site connaît-il l'extension, mais pas pour cette version de GNOME ?
        return "absent" if _ego_info(uuid, None, pk) is None else "incompatible"
    url = urllib.parse.urljoin(EGO_URL + "/", str(info["download_url"]))
    if urllib.parse.urlparse(url).netloc != urllib.parse.urlparse(EGO_URL).netloc:
        raise ValueError("adresse de téléchargement inattendue")
    log(f"Téléchargement de {uuid} (v{info.get('version', '?')})…")
    try:
        data = _ego_get(url, EGO_MAX_ZIP)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _Offline(str(exc)) from exc
    tmp = new_tempdir()
    try:
        zip_path = tmp / f"{uuid}.zip"
        zip_path.write_bytes(data)
        _unpack_extension(zip_path, uuid, log)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return "ok"


def _enable_extension(uuid: str, log: Log) -> None:
    """Active l'extension. Si le shell en cours ne la connaît pas encore (reconnexion
    nécessaire), on l'ajoute directement à la liste des extensions activées."""
    if has("gnome-extensions") and run(["gnome-extensions", "enable", uuid], log) == 0:
        return
    if not has("gsettings"):
        log(f"{uuid} : activation impossible (gsettings absent), elle sera appliquée par les réglages.")
        return
    try:
        raw = capture(["gsettings", "get", "org.gnome.shell", "enabled-extensions"]).strip()
        current = list(ast.literal_eval(raw.removeprefix("@as ")))
        if uuid not in current:
            current.append(uuid)
            capture(["gsettings", "set", "org.gnome.shell", "enabled-extensions", repr(current)])
        log(f"{uuid} : activée au prochain démarrage de la session.")
    except (subprocess.CalledProcessError, OSError, ValueError, SyntaxError) as exc:
        log(f"{uuid} : activation impossible ({exc}).")


def _restore_extensions(src: Path, log: Log, only: dict | None = None) -> None:
    entries = _load_ext_index(src)
    if entries is None:  # ancienne sauvegarde : les fichiers ont été copiés
        EXT_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src / "extensions", EXT_DIR, symlinks=True, dirs_exist_ok=True)
        log("Extensions copiées (sauvegarde ancien format). Une extension conçue pour une "
            "autre version de GNOME peut ne pas fonctionner.")
        return

    shell = _shell_version()
    offline = False
    failed: list[str] = []
    for e in entries:
        uuid, name = str(e["uuid"]), str(e.get("name") or e["uuid"])
        if not re.fullmatch(r"[\w.@+-]+", uuid):
            log(f"Identifiant d'extension ignoré (caractères inattendus) : {uuid!r}")
            failed.append(name)
            continue
        if (EXT_DIR / uuid).is_dir():
            log(f"{name} : déjà installée")
        else:
            result = "absent" if e.get("ego") is False else "offline"
            if e.get("ego") is not False and not offline:
                try:
                    result = _install_from_ego(uuid, shell, log, e.get("pk"))
                except _Offline as exc:
                    offline, result = True, "offline"
                    log(f"extensions.gnome.org injoignable ({exc}).")
                except (ValueError, OSError, zipfile.BadZipFile) as exc:
                    result = "error"
                    log(f"{name} : installation échouée ({exc}).")
            if result == "ok":
                log(f"{name} : installée depuis extensions.gnome.org")
            else:
                saved = src / "extensions" / uuid
                if saved.is_dir():
                    shutil.copytree(saved, EXT_DIR / uuid, symlinks=True, dirs_exist_ok=True)
                    why = {"incompatible": "aucune version compatible avec GNOME " + str(shell),
                           "offline": "pas de connexion", "error": "erreur de téléchargement",
                           "absent": "hors extensions.gnome.org"}[result]
                    log(f"{name} : fichiers de la sauvegarde copiés ({why}) ; "
                        "elle peut ne pas fonctionner avec cette version de GNOME.")
                else:
                    reason = {"incompatible": f"aucune version compatible avec GNOME {shell}",
                              "absent": "inconnue de extensions.gnome.org et sans copie",
                              "offline": "pas de connexion à extensions.gnome.org",
                              "error": "erreur d'installation"}[result]
                    log(f"{name} : non restaurée ({reason})."
                        + (f" Page de l'extension : {e['url']}" if e.get("url") else ""))
                    failed.append(name)
                    continue
        if e.get("enabled"):
            _enable_extension(uuid, log)
    if failed:
        raise RuntimeError(f"{len(failed)} extension(s) non restaurée(s) : {', '.join(failed)}")


def _restore_dconf(src: Path, log: Log, only: dict | None = None) -> None:
    with open(src / "dconf-reglages.ini") as f:
        if run(["dconf", "load", "/"], log, stdin=f) != 0:
            raise RuntimeError("dconf load a échoué")


def _restore_home(src: Path, log: Log, only: dict | None = None) -> None:
    rc = run(["rsync", "-a", "--backup", "--suffix=.avant-restauration",
              f"{src / 'home'}/", f"{HOME}/"], log)
    if rc not in (0, 23, 24):
        raise RuntimeError(f"rsync a échoué (code {rc})")


RESTORE_STEPS = {
    "flatpak": _restore_flatpak, "extensions": _restore_extensions,
    "dconf": _restore_dconf, "profiles": _restore_profiles, "home": _restore_home,
}


def restore(src: Path, selected: set[str], log: Log, progress: Progress,
            only: dict[str, set[str]] | None = None, notice: bool = True) -> list[str]:
    """Restaure les composants choisis. Renvoie la liste de ceux qui ont échoué.

    `only` : pour apt, snap et flatpak, les seuls éléments à installer (tout sinon).
    `notice` : afficher le rappel de reconnexion final (l'assistant le gère lui-même).
    """
    steps = [c for c in COMPONENTS if c in selected]
    index = {name: i for i, name in enumerate(steps)}
    total = max(len(steps), 1)
    errors: list[str] = []

    def mark(name: str) -> None:
        progress(index[name] / total, LABELS[name])

    root_parts = [s for s in ("repos", "apt", "snap") if s in steps]
    if root_parts:
        mark(root_parts[0])

        def on_marker(name: str) -> None:
            if name in index:
                mark(name)
                log(f"== {LABELS[name]}")

        if not _restore_root(src, root_parts, log, on_marker, only):
            errors += root_parts

    for name in steps:
        if name in root_parts:
            continue
        mark(name)
        log(f"== {LABELS[name]}")
        try:
            RESTORE_STEPS[name](src, log, only)
        except Exception as exc:  # noqa: BLE001
            log(f"Erreur : {exc}")
            errors.append(name)

    progress(1.0, "Terminé")
    log(f"Bilan : {report(len(steps), errors)}.")
    if notice:
        log("Restauration terminée. Déconnectez-vous puis reconnectez-vous pour appliquer extensions et réglages.")
    return errors


# --------------------------------------------------------------------------
# Mode « nouvelle machine » : restauration guidée, étape par étape
# --------------------------------------------------------------------------

class WizardStep(NamedTuple):
    key: str
    title: str
    intro: str
    parts: tuple[str, ...]  # composants de la sauvegarde restaurés par cette étape


# Même ordre que COMPONENTS : les dépôts avant les paquets qui en dépendent, les paquets
# avant les extensions, les extensions avant les réglages qui les concernent.
WIZARD_STEPS = (
    WizardStep(
        "repos", "Dépôts tiers",
        "Ajoute d'abord vos dépôts (PPA) et leurs clés de signature, pour que les applications "
        "qui en dépendent soient trouvées à l'étape suivante. Votre mot de passe administrateur "
        "sera demandé (ici, puis à l'étape des applications).",
        ("repos",),
    ),
    WizardStep(
        "apps", "Applications",
        "Installe vos applications apt, snap et flatpak. Cette étape peut durer plusieurs "
        "minutes. Votre mot de passe administrateur sera demandé pour apt et snap.",
        ("apt", "snap", "flatpak"),
    ),
    WizardStep(
        "extensions", "Extensions GNOME",
        "Télécharge vos extensions depuis extensions.gnome.org et les active. Elles ne "
        "apparaîtront qu'à la prochaine ouverture de session.",
        ("extensions",),
    ),
    WizardStep(
        "settings", "Réglages du bureau",
        "Applique vos réglages : apparence, dock, raccourcis, fond d'écran… Cette étape vient "
        "après les extensions, pour que les réglages qui les concernent trouvent leur place.",
        ("dconf",),
    ),
    WizardStep(
        "files", "Navigateurs et fichiers personnels",
        "Restaure vos profils (Firefox, Thunderbird…) et vos configurations personnelles. "
        "Fermez d'abord ces applications. Les fichiers remplacés sont conservés avec "
        "l'extension .avant-restauration. Ces éléments sont décochés par défaut.",
        ("profiles", "home"),
    ),
)

# Composants décochés par défaut (comme sur la page « Restaurer »)
OPT_IN = ("profiles", "home")


def wizard_steps(found: dict[str, bool]) -> list[WizardStep]:
    """Étapes de l'assistant que cette sauvegarde permet d'exécuter, dans l'ordre."""
    return [step for step in WIZARD_STEPS if any(found.get(p) for p in step.parts)]


def restart_session(log: Log) -> bool:
    """Demande la fermeture de la session GNOME (GNOME affiche lui-même sa confirmation).

    Les extensions et réglages restaurés ne sont pris en compte qu'à la reconnexion."""
    if has("gnome-session-quit"):
        return run(["gnome-session-quit", "--logout"], log) == 0
    if has("gdbus"):
        return run(["gdbus", "call", "--session", "--dest", "org.gnome.SessionManager",
                    "--object-path", "/org/gnome/SessionManager",
                    "--method", "org.gnome.SessionManager.Logout", "0"], log) == 0
    log("Ni gnome-session-quit ni gdbus ne sont disponibles.")
    return False
