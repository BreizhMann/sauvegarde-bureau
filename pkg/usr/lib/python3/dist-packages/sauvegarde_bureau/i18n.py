"""Traduction de l'interface (gettext).

Les textes du code source sont en français. La langue est choisie ainsi :
  - français, ou langue non définie (C/POSIX) : textes d'origine ;
  - toute autre langue : anglais (catalogue « en »).
"""

from __future__ import annotations

import gettext
import os

DOMAIN = "sauvegarde-bureau"
LOCALEDIR = os.environ.get("SAUVEGARDE_BUREAU_LOCALEDIR", "/usr/share/locale")


def _languages() -> list[str]:
    """Langues demandées par l'environnement, de la plus souhaitée à la moins souhaitée."""
    forced = os.environ.get("SAUVEGARDE_BUREAU_LANG")
    candidates = [forced] if forced else os.environ.get("LANGUAGE", "").split(":")
    if not forced:
        candidates += [os.environ.get(v, "") for v in ("LC_ALL", "LC_MESSAGES", "LANG")]
    result = []
    for value in candidates:
        value = (value or "").strip()
        if not value or value in ("C", "POSIX") or value.startswith(("C.", "POSIX.")):
            continue
        value = value.split(".")[0].split("@")[0].replace("-", "_")
        for item in (value, value.split("_")[0]):
            if item and item.lower() not in result:
                result.append(item.lower() if "_" not in item else item)
    return result


_wanted = _languages()
# Langue d'origine du code : le français. Langue non définie (C/POSIX) : français aussi.
LANG = _wanted[0].split("_")[0].lower() if _wanted else "fr"
_translation = (
    gettext.NullTranslations()
    if LANG == "fr"
    else gettext.translation(DOMAIN, LOCALEDIR, languages=[*_wanted, "en"], fallback=True)
)
IS_FRENCH = LANG == "fr" or not isinstance(_translation, gettext.GNUTranslations)

# Règle des pluriels : en français 0 et 1 sont au singulier, en anglais seul 1 l'est.
ZERO_IS_SINGULAR = IS_FRENCH


def _(message: str) -> str:
    """Retourne le texte traduit (ou le texte d'origine s'il n'y a pas de traduction)."""
    return _translation.gettext(message) if message else message


def ngettext(one: str, many: str, n: int) -> str:
    """Texte au singulier ou au pluriel selon n (règle du français ou de l'anglais)."""
    if IS_FRENCH:
        return one if n < 2 else many
    return _translation.ngettext(one, many, n)


# Date et heure courtes : « 15/10/2026 à 14:30 » en français, « 2026-10-15 14:30 » sinon.
DATE_FORMAT = "%d/%m/%Y" if IS_FRENCH else "%Y-%m-%d"
DATETIME_FORMAT = "%d/%m/%Y à %H:%M" if IS_FRENCH else "%Y-%m-%d %H:%M"

_MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
              "septembre", "octobre", "novembre", "décembre"]
_MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July", "August",
              "September", "October", "November", "December"]


def long_date(year: int, month: int, day: int) -> str:
    """Date en toutes lettres : « 1er octobre 2026 » ou « October 1, 2026 »."""
    if IS_FRENCH:
        return f"{day}{'er' if day == 1 else ''} {_MONTHS_FR[month - 1]} {year}"
    return f"{_MONTHS_EN[month - 1]} {day}, {year}"
