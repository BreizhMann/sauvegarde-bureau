"""Restaurer un seul fichier ou dossier d'une sauvegarde (« j'ai supprimé un fichier »).

Une fenêtre pour parcourir ou chercher dans le contenu d'une sauvegarde, puis remettre uniquement
l'élément choisi. Toute la logique est dans `backend` (liste, extraction, contrôle SHA-256,
remise en place) : ce module ne fait que l'afficher.
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

from . import backend  # noqa: E402
from .i18n import DATE_FORMAT, ZERO_IS_SINGULAR, _, ngettext  # noqa: E402

MAX_ROWS = 300   # lignes affichées d'un coup : au-delà, la recherche est plus utile qu'une liste interminable


def _n(count: int, one: str, many: str) -> str:
    singular = count < 2 if ZERO_IS_SINGULAR else count == 1
    return f"{count} {one if singular else many}"


def _date(mtime: float) -> str:
    try:
        return datetime.fromtimestamp(mtime).strftime(DATE_FORMAT)
    except (OverflowError, OSError, ValueError):
        return _("date inconnue")


class SingleRestoreDialog(Adw.Dialog):
    """Explorateur d'une sauvegarde : chaque ligne a son bouton « Restaurer »."""

    def __init__(self, window, index: backend.BackupIndex, passphrase: str | None) -> None:
        super().__init__(title=_("Restaurer un fichier ou un dossier"), content_width=680, content_height=680)
        self.window = window
        self.index = index
        self._passphrase = passphrase       # gardé le temps de la fenêtre, jamais écrit ailleurs
        self.cwd = ""                       # dossier affiché ("" : racine du dossier personnel)
        self.running = False
        self.cancel: threading.Event | None = None
        self.gen = 0                        # numéro de la dernière demande : les résultats périmés sont ignorés
        self.connect("closed", self._closed)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=_("Restaurer un fichier ou un dossier"),
                                                subtitle=index.source.name))

        self.search = Gtk.SearchEntry(placeholder_text=_("Chercher un fichier ou un dossier par son nom…"),
                                      hexpand=True)
        self.search.connect("search-changed", lambda _e: self._show())
        self.up = Gtk.Button(icon_name="go-up-symbolic", css_classes=["flat"], tooltip_text=_("Dossier parent"),
                             valign=Gtk.Align.CENTER)
        self.up.connect("clicked", self._go_up)
        self.crumb = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.START,
                               css_classes=["heading"], selectable=False)
        nav = Gtk.Box(spacing=6)
        nav.append(self.up)
        nav.append(self.crumb)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        scroller = Gtk.ScrolledWindow(child=self.listbox, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)

        self.spinner = Gtk.Spinner(visible=False)
        self.status = Gtk.Label(xalign=0, hexpand=True, wrap=True, css_classes=["dim-label", "caption"],
                                label=(_("Seuls les fichiers personnels de la sauvegarde sont listés "
                                       "(configurations, profils de navigateurs et de courrier).")))
        self.stop = Gtk.Button(label=_("Arrêter"), css_classes=["destructive-action"], visible=False,
                               valign=Gtk.Align.CENTER)
        self.stop.connect("clicked", self._stop_clicked)
        bottom = Gtk.Box(spacing=8)
        for w in (self.spinner, self.status, self.stop):
            bottom.append(w)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12,
                      margin_start=12, margin_end=12)
        for w in (self.search, nav, scroller, bottom):
            box.append(w)
        self.toasts = Adw.ToastOverlay(child=box)
        toolbar = Adw.ToolbarView(content=self.toasts)
        toolbar.add_top_bar(header)
        self.set_child(toolbar)
        self.set_focus(self.search)
        self._show()

    # ------------------------------------------------------------------ Affichage

    def _closed(self, _dialog) -> None:
        self._passphrase = None
        self.listbox.remove_all()

    def _go_up(self, _button: Gtk.Button) -> None:
        self.cwd = self.cwd.rpartition("/")[0]
        self._show()

    def _enter(self, target: str) -> None:
        self.search.set_text("")
        self.cwd = target
        self._show()

    def _describe(self, e: backend.BackupEntry) -> str:
        if e.is_dir:
            return _('{v1} · {v2} · modifié le {v3}').format(v1=_n(e.count, _('fichier'), _('fichiers')), v2=backend.fmt_size(e.size), v3=_date(e.mtime))
        return _('{v1} · modifié le {v2}').format(v1=backend.fmt_size(e.size), v2=_date(e.mtime))

    def _row(self, e: backend.BackupEntry, searching: bool) -> Adw.ActionRow:
        subtitle = self._describe(e)
        if searching:
            parent = e.target.rpartition("/")[0]
            subtitle = f"~/{parent}\n{subtitle}" if parent else f"~\n{subtitle}"
        row = Adw.ActionRow(title=e.name, subtitle=subtitle, use_markup=False, subtitle_lines=2,
                            title_lines=1, activatable=True)
        row.add_prefix(Gtk.Image(icon_name="folder-symbolic" if e.is_dir else "text-x-generic-symbolic"))
        button = Gtk.Button(label=_("Restaurer"), css_classes=["flat"], valign=Gtk.Align.CENTER,
                            tooltip_text=_("Remettre cet élément dans le dossier personnel"))
        button.update_property([Gtk.AccessibleProperty.LABEL], [_('Restaurer {v1}').format(v1=e.target)])
        button.connect("clicked", lambda _b, entry=e: self._ask_restore(entry))
        row.add_suffix(button)
        if e.is_dir:
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _r, entry=e: self._enter(entry.target))
        else:
            row.connect("activated", lambda _r, entry=e: self._ask_restore(entry))
        return row

    def _show(self) -> None:
        self.listbox.remove_all()
        text = self.search.get_text().strip()
        searching = bool(text)
        self.up.set_sensitive(bool(self.cwd) and not searching and not self.running)
        more = False
        if searching:
            entries, more = self.index.search(text)
            self.crumb.set_label(_('Résultats pour « {text} »').format(text=text))
        else:
            entries = self.index.children(self.cwd)
            self.crumb.set_label("~/" + self.cwd if self.cwd else _("Dossier personnel (~)"))
        shown = entries[:MAX_ROWS]
        for e in shown:
            self.listbox.append(self._row(e, searching))
        if not entries:
            self.listbox.append(Adw.ActionRow(
                title=_("Aucun résultat") if searching else _("Ce dossier est vide"), use_markup=False,
                subtitle=_("Essayez une partie plus courte du nom.") if searching else ""))
        elif more or len(entries) > len(shown):
            self.listbox.append(Adw.ActionRow(
                title=(_("Il y a d'autres résultats : précisez la recherche.") if searching else
                       _('{v1} : utilisez la recherche.').format(v1=_n(len(entries) - len(shown), _('autre élément'), _('autres éléments')))), use_markup=False, selectable=False))

    # ------------------------------------------------------------------ Restauration

    def _ask_restore(self, entry: backend.BackupEntry) -> None:
        """Calcule ce qui serait remplacé (hors de la boucle graphique), puis demande confirmation."""
        if self.running:
            return
        self.gen += 1
        gen = self.gen
        self.status.set_label(_('Préparation de « {v1} »…').format(v1=entry.name))

        def worker() -> None:
            existing = backend.existing_count(entry.target)
            origins = {f.origin for f in self.index.select(entry.target) if f.origin != "home"}
            open_apps = backend.running_apps(origins) if origins else []
            GLib.idle_add(self._confirm, gen, entry, existing, open_apps)

        threading.Thread(target=worker, daemon=True).start()

    def _confirm(self, gen: int, entry: backend.BackupEntry, existing: int, open_apps: list[str]) -> bool:
        if gen != self.gen or self.running:
            return GLib.SOURCE_REMOVE
        self._idle_status()
        what = _n(entry.count, _("fichier"), _("fichiers")) + f", {backend.fmt_size(entry.size)}"
        lines = [_('« {v1} » ({what}) sera remis dans ~/{v1}.').format(v1=entry.target, what=what)]
        if existing:
            lines.append(
                ngettext(
                    "{n} fichier existe déjà à cet endroit et sera remplacé par la version de la sauvegarde. "
                    "L'ancienne version est conservée à côté, avec l'extension {suffix}.",
                    "{n} fichiers existent déjà à cet endroit et seront remplacés par la version de la "
                    "sauvegarde. L'ancienne version est conservée à côté, avec l'extension {suffix}.",
                    existing).format(n=existing, suffix=backend.BACKUP_SUFFIX))
        else:
            lines.append(_("Rien n'existe à cet emplacement : rien ne sera remplacé."))
        if open_apps:
            lines.append(_("Attention : {v1} est ouvert. Fermez-le avant de restaurer un fichier de son profil, sinon il risque d'écraser le résultat.").format(v1=', '.join(open_apps)))
        if self.index.encrypted:
            lines.append(_("L'archive va être relue en entier, ce qui peut prendre quelques minutes."))
        if entry.size > 1024 ** 3:
            lines.append(_("C'est volumineux : vérifiez que la place disque est suffisante."))
        alert = Adw.AlertDialog(heading=_('Restaurer « {v1} » ?').format(v1=entry.name), body="\n\n".join(lines))
        alert.add_response("cancel", _("Annuler"))
        alert.add_response("elsewhere", _("Dans un autre dossier…"))
        alert.add_response("ok", _("Restaurer ici"))
        alert.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        alert.set_default_response("ok")
        alert.set_close_response("cancel")

        def answered(_d, response: str) -> None:
            if response == "ok":
                self._run(entry, None)
            elif response == "elsewhere":
                self._choose_dest(entry)

        alert.connect("response", answered)
        alert.present(self)
        return GLib.SOURCE_REMOVE

    def _choose_dest(self, entry: backend.BackupEntry) -> None:
        dialog = Gtk.FileDialog(title=_('Où copier « {v1} » ?').format(v1=entry.name))
        dialog.set_initial_folder(Gio.File.new_for_path(str(backend.HOME)))

        def done(dlg: Gtk.FileDialog, result) -> None:
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return
            self._run(entry, Path(folder.get_path()))

        dialog.select_folder(self.window, None, done)

    def _idle_status(self) -> None:
        self.status.set_label(_("Seuls les fichiers personnels de la sauvegarde sont listés "
                              "(configurations, profils de navigateurs et de courrier)."))

    def _set_running(self, running: bool) -> None:
        self.running = running
        self.window.busy = running
        self.window._sync_buttons()
        self.set_can_close(not running)
        self.listbox.set_sensitive(not running)
        self.search.set_sensitive(not running)
        self.up.set_sensitive(not running and bool(self.cwd) and not self.search.get_text().strip())
        self.stop.set_visible(running)
        self.stop.set_sensitive(running)
        self.stop.set_label(_("Arrêter"))
        self.spinner.set_visible(running)
        if running:
            self.spinner.start()
        else:
            self.spinner.stop()

    def _stop_clicked(self, _button: Gtk.Button) -> None:
        if self.cancel is not None:
            self.cancel.set()
            self.stop.set_sensitive(False)
            self.stop.set_label(_("Arrêt…"))

    def _run(self, entry: backend.BackupEntry, dest: Path | None) -> None:
        if self.running or self.window.busy:
            return
        cancel = threading.Event()
        self.cancel = cancel
        self._set_running(True)
        self.status.set_label(_('Restauration de « {v1} »…').format(v1=entry.name))

        def progress(_fraction: float, text: str) -> None:
            if text:
                GLib.idle_add(self.status.set_label, f"« {entry.name} » : {text}…")

        def worker() -> None:
            try:
                outcome = backend.restore_path(self.index, entry.target, self._passphrase, dest,
                                               progress, cancel.is_set)
                GLib.idle_add(self._done, entry, outcome, None)
            except Exception as exc:  # noqa: BLE001 - toute erreur doit être montrée, jamais avalée
                GLib.idle_add(self._done, entry, None, exc)

        threading.Thread(target=worker, daemon=True).start()

    def _done(self, entry: backend.BackupEntry, outcome: backend.RestoreOutcome | None,
              error: Exception | None) -> bool:
        self.cancel = None
        self._set_running(False)
        self._idle_status()
        if error is not None:
            if isinstance(error, backend.OperationCancelled):
                self.toasts.add_toast(Adw.Toast(title=_("Restauration interrompue : rien n'a été modifié."),
                                                use_markup=False))
                return GLib.SOURCE_REMOVE
            if isinstance(error, backend.IntegrityError):
                heading = _("Sauvegarde abîmée")
                body = _('{error}.\n\nUtilisez une autre sauvegarde pour cet élément.').format(error=error)
            else:
                heading = _("Restauration impossible")
                body = _("{error}.\n\nRien n'a été modifié dans votre dossier personnel.").format(error=error)
            alert = Adw.AlertDialog(heading=heading, body=body)
            alert.add_response("ok", _("Fermer"))
            alert.present(self)
            return GLib.SOURCE_REMOVE

        assert outcome is not None
        lines = [_('{v1} ({v2}) dans {v3}.').format(v1=_n(outcome.files, _('fichier remis'), _('fichiers remis')), v2=backend.fmt_size(outcome.size), v3=outcome.where)]
        if outcome.replaced:
            lines.append(_("{v1} : l'ancienne version est conservée à côté, avec l'extension {v2}.").format(v1=_n(outcome.replaced, _('fichier existant a été remplacé'), _('fichiers existants ont été remplacés')), v2=backend.BACKUP_SUFFIX))
        lines.append(_("Chaque fichier a été comparé à sa somme de contrôle avant d'être remis.")
                     if outcome.verified else
                     _("Cette sauvegarde n'a pas de somme de contrôle : le contenu n'a pas pu être comparé."))
        alert = Adw.AlertDialog(heading=_("Restauré"), body="\n\n".join(lines))
        alert.add_response("ok", _("Fermer"))
        alert.add_response("show", _("Afficher dans les fichiers"))
        folder = outcome.where if outcome.where.is_dir() else outcome.where.parent
        alert.connect("response", lambda _d, r: self.window._open_folder(None, GLib.Variant.new_string(str(folder)))
                      if r == "show" else None)
        alert.present(self)
        return GLib.SOURCE_REMOVE

