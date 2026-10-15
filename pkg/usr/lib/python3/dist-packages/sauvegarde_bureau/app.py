"""Interface GTK4 / libadwaita."""

from __future__ import annotations

import os
import sys
import threading
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from . import __author__, __date__, __version__, auto, backend, single_restore, verification  # noqa: E402

APP_ID = "org.sauvegardebureau.SauvegardeBureau"
ICON = "sauvegarde-bureau"


class TaskView(Gtk.Box):
    """Barre de progression et journal détaillé d'une opération."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12, visible=False)
        self.status = Gtk.Label(xalign=0, css_classes=["heading"])
        self.bar = Gtk.ProgressBar()
        self.log_view = Gtk.TextView(
            editable=False, cursor_visible=False, monospace=True,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            top_margin=8, bottom_margin=8, left_margin=10, right_margin=10,
        )
        scroller = Gtk.ScrolledWindow(
            min_content_height=220, child=self.log_view, css_classes=["card"]
        )
        self.expander = Gtk.Expander(label="Détails", child=scroller)
        for widget in (self.status, self.bar, self.expander):
            self.append(widget)
        self.buffer = self.log_view.get_buffer()
        self.end_mark = self.buffer.create_mark("end", self.buffer.get_end_iter(), False)

    def reset(self) -> None:
        self.buffer.set_text("")
        self.bar.set_fraction(0)
        self.status.set_label("Préparation…")
        self.set_visible(True)

    def set_progress(self, fraction: float, text: str) -> bool:
        self.bar.set_fraction(fraction)
        self.status.set_label(text)
        return GLib.SOURCE_REMOVE

    def add_line(self, line: str) -> bool:
        self.buffer.insert(self.buffer.get_end_iter(), line + "\n")
        self.log_view.scroll_mark_onscreen(self.end_mark)
        return GLib.SOURCE_REMOVE


def switch_row(title: str, subtitle: str, active: bool = True, available: bool = True,
               unavailable_text: str = "Non installé sur ce système") -> Adw.SwitchRow:
    return Adw.SwitchRow(
        title=title,
        subtitle=subtitle if available else unavailable_text,
        active=active and available,
        sensitive=available,
        use_markup=False,
    )


class CloudGroup:
    """Copie dans le cloud via rclone : interrupteur, service, dossier distant, réserves."""

    def __init__(self, can_encrypt: bool, unavailable_encrypt: str, enabled: bool = False,
                 remote: str = "", path: str = backend.DEFAULT_CLOUD_PATH, on_enable=None) -> None:
        self.group = Adw.PreferencesGroup(
            title="Copie dans le cloud (rclone)",
            description="Envoie aussi l'archive chiffrée vers un service déjà configuré dans rclone. "
                        "La copie locale est toujours faite en premier ; un échec de l'envoi ne "
                        "l'annule pas.",
        )
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER,
                             css_classes=["flat"], tooltip_text="Relire les services rclone")
        refresh.connect("clicked", lambda _b: self.refresh())
        self.group.set_header_suffix(refresh)

        have_rclone = backend.has("rclone")
        ready = have_rclone and can_encrypt
        reason = "Le paquet rclone est requis" if not have_rclone else unavailable_encrypt
        self.switch = switch_row("Envoyer une copie dans le cloud",
                                 "Exige le chiffrement : rien n'est jamais envoyé en clair",
                                 active=enabled, available=ready, unavailable_text=reason)
        self.combo = Adw.ComboRow(title="Service", use_markup=False)
        self.path_row = Adw.EntryRow(title="Dossier distant", text=path or backend.DEFAULT_CLOUD_PATH)
        self.warning = Adw.ActionRow(title="À savoir", use_markup=False, subtitle_lines=0)
        self.warning.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
        for row in (self.combo, self.path_row):
            self.switch.bind_property("active", row, "visible", GObject.BindingFlags.SYNC_CREATE)
        for row in (self.switch, self.combo, self.path_row, self.warning):
            self.group.add(row)
        self.remotes: dict[str, str] = {}
        self.refresh(remote)
        self.combo.connect("notify::selected", lambda *_: self._update_warning())
        self.switch.connect("notify::active", lambda *_: self._update_warning())
        if on_enable:
            self.switch.connect("notify::active", lambda r, _p: r.get_active() and on_enable())

    def refresh(self, keep: str = "") -> None:
        """Relit les services rclone (après un « rclone config » par exemple)."""
        keep = keep or self.selected()
        self.remotes = backend.rclone_remotes()
        names = list(self.remotes)
        self.combo.set_model(Gtk.StringList.new(names))
        if keep in names:
            self.combo.set_selected(names.index(keep))
        self.combo.set_sensitive(bool(names))
        self.combo.set_subtitle(
            "Pour en ajouter un, lancez « rclone config » dans un terminal, puis actualisez."
            if names else
            "Aucun service configuré : lancez « rclone config » dans un terminal, puis actualisez.")
        self._update_warning()

    def selected(self) -> str:
        item = self.combo.get_selected_item()
        return item.get_string() if item else ""

    def _update_warning(self) -> None:
        text = backend.CLOUD_WARNINGS.get(self.remotes.get(self.selected(), ""), "")
        self.warning.set_subtitle(text)
        self.warning.set_visible(bool(text) and self.switch.get_active())

    @property
    def active(self) -> bool:
        return self.switch.get_active() and self.switch.get_sensitive()

    def target(self) -> tuple[str, str]:
        """(service, dossier) validés. Lève ValueError avec un message lisible."""
        if not self.selected():
            raise ValueError("choisissez un service rclone (ou configurez-en un avec « rclone config »)")
        return backend.cloud_target(self.selected(), self.path_row.get_text())


def choose_button(callback, label: str = "Choisir…", tooltip: str | None = None) -> Gtk.Button:
    button = Gtk.Button(label=label, valign=Gtk.Align.CENTER, tooltip_text=tooltip)
    button.connect("clicked", callback)
    return button


def open_menu(entries: list[tuple[str, str, object, bool, str | None]]) -> Gtk.MenuButton:
    """Bouton unique « Ouvrir… » : un menu d'actions (titre, explication, rappel, actif, infobulle)."""
    button = Gtk.MenuButton(label="Ouvrir…", valign=Gtk.Align.CENTER, always_show_arrow=True,
                            tooltip_text="Choisir la sauvegarde à ouvrir")
    popover = Gtk.Popover()
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                  margin_start=6, margin_end=6)
    for title, detail, callback, enabled, tooltip in entries:
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=4,
                        margin_bottom=4, margin_start=6, margin_end=6)
        inner.append(Gtk.Label(label=title, xalign=0))
        inner.append(Gtk.Label(label=detail, xalign=0, wrap=True, max_width_chars=44,
                               css_classes=["dim-label", "caption"]))
        item = Gtk.Button(child=inner, css_classes=["flat"], sensitive=enabled, tooltip_text=tooltip)

        def clicked(_b, cb=callback) -> None:
            popover.popdown()
            cb(button)

        item.connect("clicked", clicked)
        box.append(item)
    popover.set_child(box)
    button.set_popover(popover)
    return button


MAX_ROWS = 80      # lignes de détail affichées par composant
MAX_GROUPS = 60    # sections de réglages affichées
MAX_NAMES = 300    # noms listés dans « déjà présents »
COMPARE_ROWS = 250  # changements listés par composant dans la comparaison
STATUS_CSS = {backend.NEW: "success", backend.CHANGE: "warning",
              backend.SKIP: "dim-label", backend.SAME: "dim-label"}
STATUS_ORDER = {backend.CHANGE: 0, backend.NEW: 1, backend.SKIP: 2, backend.SAME: 3}


def info_row(text: str, subtitle: str = "") -> Adw.ActionRow:
    return Adw.ActionRow(title=text, subtitle=subtitle, use_markup=False,
                         css_classes=["dim-label"])


def item_row(item: backend.Item) -> Adw.ActionRow:
    row = Adw.ActionRow(title=item.name, subtitle=item.detail, use_markup=False)
    if item.warn:
        row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
    row.add_suffix(Gtk.Label(label=backend.STATUS_LABELS[item.status], valign=Gtk.Align.CENTER,
                             css_classes=[STATUS_CSS[item.status]]))
    return row


def add_item_rows(expander: Adw.ExpanderRow, items: list[backend.Item]) -> None:
    items = sorted(items, key=lambda i: (STATUS_ORDER[i.status], i.name.lower()))
    for item in items[:MAX_ROWS]:
        expander.add_row(item_row(item))
    if len(items) > MAX_ROWS:
        expander.add_row(info_row(f"… et {len(items) - MAX_ROWS} autres éléments non affichés"))


def fill_expander(expander: Adw.ExpanderRow, name: str, pv: backend.Preview) -> None:
    """Remplit le détail d'un composant : ce qui change, puis ce qui est déjà présent."""
    if pv.error:
        expander.add_row(info_row("Comparaison impossible", pv.error))
        return
    if pv.note:
        expander.add_row(info_row(pv.note))
    if any(i.group for i in pv.items):  # réglages : une sous-section par chemin dconf
        groups: dict[str, list[backend.Item]] = {}
        for item in pv.items:
            groups.setdefault(item.group, []).append(item)
        paths = sorted(groups, key=lambda g: backend.dconf_label(g).lower())
        for path in paths[:MAX_GROUPS]:
            label = backend.dconf_label(path)
            count = backend.plural(len(groups[path]), "réglage", "réglages")
            sub = Adw.ExpanderRow(title=label, use_markup=False,
                                  subtitle=count if label == path else f"{count} · {path}")
            add_item_rows(sub, groups[path])
            expander.add_row(sub)
        if len(paths) > MAX_GROUPS:
            expander.add_row(info_row(f"… et {len(paths) - MAX_GROUPS} autres sections non affichées"))
    else:
        add_item_rows(expander, pv.items)
    if pv.n_same:
        same_label = backend.phrase(name, 2, pv.n_same).capitalize()
        names = ", ".join(pv.same[:MAX_NAMES]) + (" …" if len(pv.same) > MAX_NAMES else "")
        expander.add_row(info_row(f"{same_label} ({pv.n_same})", names))
    if not pv.items and not pv.n_same:
        expander.add_row(info_row("Rien à comparer"))


def pick_dialog(parent: Gtk.Widget, title: str, names: list[str], chosen: set[str] | None,
                on_done) -> None:
    """Liste à cocher avec recherche. `chosen` None = tout coché. Valider appelle on_done(set)."""
    checks: dict[str, Gtk.CheckButton] = {}
    listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
    counter = Gtk.Label(xalign=0, hexpand=True, css_classes=["dim-label"])
    search = Gtk.SearchEntry(placeholder_text="Filtrer par nom…", hexpand=True)

    def update(*_args) -> None:
        n = sum(1 for c in checks.values() if c.get_active())
        counter.set_label(f"{n} sur {len(checks)} choisis")

    def matches(name: str) -> bool:
        return search.get_text().strip().lower() in name.lower()

    for name in names:
        check = Gtk.CheckButton(label=name, active=chosen is None or name in chosen, hexpand=True,
                                margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        check.connect("toggled", update)
        checks[name] = check
        listbox.append(Gtk.ListBoxRow(child=check, activatable=False))
    listbox.set_filter_func(lambda row: matches(row.get_child().get_label()))
    search.connect("search-changed", lambda _e: listbox.invalidate_filter())

    def set_visible_checks(state: bool) -> None:  # agit sur les éléments affichés par le filtre
        for name, check in checks.items():
            if matches(name):
                check.set_active(state)

    all_btn = Gtk.Button(label="Tout cocher")
    none_btn = Gtk.Button(label="Tout décocher")
    ok_btn = Gtk.Button(label="Valider", css_classes=["suggested-action"])
    all_btn.connect("clicked", lambda _b: set_visible_checks(True))
    none_btn.connect("clicked", lambda _b: set_visible_checks(False))

    scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                  child=Adw.Clamp(child=listbox, margin_top=6, margin_bottom=12,
                                                  margin_start=12, margin_end=12))
    search_bar = Gtk.Box(margin_start=12, margin_end=12, margin_bottom=6)
    search_bar.append(search)
    bottom = Gtk.Box(spacing=6, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
    for w in (counter, all_btn, none_btn, ok_btn):
        bottom.append(w)

    toolbar = Adw.ToolbarView(content=scroller)
    toolbar.add_top_bar(Adw.HeaderBar())
    toolbar.add_top_bar(search_bar)
    toolbar.add_bottom_bar(bottom)
    dialog = Adw.Dialog(title=title, child=toolbar, content_width=480, content_height=680)

    def validate(_b) -> None:
        on_done({n for n, c in checks.items() if c.get_active()})
        dialog.close()

    ok_btn.connect("clicked", validate)
    update()
    dialog.present(parent)


def page(*children: Gtk.Widget) -> Gtk.ScrolledWindow:
    box = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL, spacing=24,
        margin_top=24, margin_bottom=24, margin_start=12, margin_end=12,
    )
    for child in children:
        box.append(child)
    clamp = Adw.Clamp(maximum_size=640, child=box)
    return Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title="Sauvegarde du bureau",
                         default_width=900, default_height=800,
                         width_request=360, height_request=400)
        self.busy = False
        self.backup_parent = Path.home()
        self.restore_src: Path | None = None
        self.restore_ready = False
        self.restore_tmp: Path | None = None  # copie déchiffrée temporaire
        self.restore_found: dict[str, bool] = {}
        self.previews: dict[str, backend.Preview] = {}
        self.preview_gen = 0  # numéro de l'analyse en cours : les résultats périmés sont ignorés
        self.p_rows: list[Gtk.Widget] = []
        self.p_expanders: dict[str, Adw.ExpanderRow] = {}
        self.r_picks: dict[str, set[str] | None] = {}  # éléments cochés ; absent ou None = tout
        self.r_pick_buttons: dict[str, Gtk.Button] = {}
        self.c_src: dict[str, Path | None] = {"old": None, "new": None}
        self.c_tmp: dict[str, Path | None] = {"old": None, "new": None}  # copies déchiffrées
        self.c_label: dict[str, str | None] = {"old": None, "new": None}
        self.c_rows: dict[str, Adw.ActionRow] = {}
        self.c_result_rows: list[Gtk.Widget] = []
        self.compare_gen = 0
        # Assistant « nouvelle machine »
        self.w_steps: list[backend.WizardStep] = []
        self.w_index = 0
        self.w_status: list[str] = []   # par étape : todo, ok, warn ou skip
        self.w_phase = "ready"          # ready (à lancer), ok (réussie) ou warn (erreurs)
        self.w_part_rows: dict[str, Adw.SwitchRow] = {}
        self.w_progress: list[tuple[Adw.ActionRow, Gtk.Image]] = []
        # Page « Mes sauvegardes »
        self.m_folder: Path | None = None   # dossier choisi sur cette page ; None : le dossier habituel
        self.m_gen = 0                      # numéro de la lecture en cours : les résultats périmés sont ignorés
        self.m_rows: list[Gtk.Widget] = []
        self.m_actions: list[Gtk.Widget] = []   # boutons à griser pendant une opération
        backend.cleanup_stale()

        self.stack = Adw.ViewStack()
        self.stack.add_titled_with_icon(self._build_backup_page(), "backup",
                                        "Sauvegarder", "document-save-symbolic")
        self.stack.add_titled_with_icon(self._build_restore_page(), "restore",
                                        "Restaurer", "document-open-symbolic")
        self.stack.add_titled_with_icon(self._build_mine_page(), "mine",
                                        "Mes sauvegardes", "folder-symbolic")

        self.stack.add_titled_with_icon(self._build_wizard_page(), "wizard",
                                        "Nouveau PC", "computer-symbolic")
        self.stack.add_titled_with_icon(self._build_compare_page(), "compare",
                                        "Comparer", "view-dual-symbolic")
        self.stack.add_titled_with_icon(self._build_auto_page(), "auto",
                                        "Automatique", "alarm-symbolic")
        self.stack.connect("notify::visible-child-name", lambda *_: self._refresh_auto_status())

        # Navigation latérale (style GNOME récent) ; se replie en tiroir sur fenêtre étroite
        self._install_style()
        self.split = Adw.OverlaySplitView(min_sidebar_width=210, max_sidebar_width=270,
                                          sidebar_width_fraction=0.27)
        self.nav_title = Adw.WindowTitle(title="Sauvegarder", subtitle="")
        self.nav_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE,
                                    css_classes=["navigation-sidebar"])
        self.nav_rows: dict[str, Gtk.ListBoxRow] = {}
        pages = self.stack.get_pages()
        for i in range(pages.get_n_items()):
            page = pages.get_item(i)
            line = Gtk.Box(spacing=12, margin_top=5, margin_bottom=5)
            line.append(Gtk.Image(icon_name=page.get_icon_name()))
            line.append(Gtk.Label(label=page.get_title(), xalign=0, hexpand=True))
            row = Gtk.ListBoxRow(child=line)
            row.nav_name = page.get_name()
            self.nav_rows[row.nav_name] = row
            self.nav_list.append(row)
        self.nav_list.connect("row-selected", self._nav_selected)
        self.stack.connect("notify::visible-child-name", self._nav_sync)
        self._nav_sync()

        brand = Gtk.Box(spacing=8)
        brand.append(Gtk.Image(icon_name=ICON, pixel_size=22))
        brand.append(Gtk.Label(label="Sauvegarde du bureau", css_classes=["heading"]))
        side_header = Adw.HeaderBar(show_end_title_buttons=False, title_widget=brand)
        footer = Gtk.Label(label=f"{__author__} · version {__version__}", xalign=0,
                           css_classes=["dim-label", "caption", "nav-footer"])
        side = Adw.ToolbarView(content=Gtk.ScrolledWindow(
            child=self.nav_list, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        side.add_top_bar(side_header)
        side.add_bottom_bar(footer)
        self.split.set_sidebar(side)

        header = Adw.HeaderBar(title_widget=self.nav_title)
        toggle = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", tooltip_text="Afficher le menu")
        flags = GObject.BindingFlags.SYNC_CREATE
        self.split.bind_property("show-sidebar", toggle, "active",
                                 flags | GObject.BindingFlags.BIDIRECTIONAL)
        self.split.bind_property("collapsed", toggle, "visible", flags)
        self.split.bind_property("collapsed", header, "show-start-title-buttons", flags)
        header.pack_start(toggle)
        menu = Gio.Menu()
        menu.append("À propos", "app.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True))

        # Alerte « sauvegarde trop ancienne », visible quelle que soit la page
        self.banner = Adw.Banner(title="", button_label="Sauvegarder maintenant",
                                 use_markup=False, revealed=False)
        self.banner.connect("button-clicked", lambda _b: self.stack.set_visible_child_name("backup"))

        toolbar = Adw.ToolbarView(content=self.stack)
        toolbar.add_top_bar(header)
        toolbar.add_top_bar(self.banner)
        self.split.set_content(toolbar)
        overlay = Gtk.Overlay(child=self.split)
        self._setup_drop(overlay)
        self.stack.connect("notify::visible-child-name", self._page_changed)
        GLib.idle_add(self._refresh_verify_all)
        GLib.timeout_add_seconds(3600, self._refresh_verify_all_again)  # fenêtre laissée ouverte des jours
        self.toasts = Adw.ToastOverlay(child=overlay)
        self.set_content(self.toasts)

        # Fenêtre étroite (téléphone, écran partagé) : le menu devient un tiroir
        breakpoint_ = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 760sp"))
        breakpoint_.add_setter(self.split, "collapsed", True)
        breakpoint_.add_setter(self.split, "show-sidebar", False)
        self.add_breakpoint(breakpoint_)

        open_action = Gio.SimpleAction.new("open-folder", GLib.VariantType.new("s"))
        open_action.connect("activate", self._open_folder)
        self.add_action(open_action)
        self.connect("close-request", self._on_close)

    # ------------------------------------------------------------ Navigation latérale

    STYLE_CSS = """
    .navigation-sidebar row { border-radius: 10px; margin: 1px 6px; padding: 4px 8px; }
    .nav-footer { margin: 10px 16px 12px 16px; }
    """

    def _install_style(self) -> None:
        provider = Gtk.CssProvider()
        if hasattr(provider, "load_from_string"):
            provider.load_from_string(self.STYLE_CSS)
        else:  # GTK < 4.12
            provider.load_from_data(self.STYLE_CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _nav_selected(self, _listbox, row) -> None:
        if row is None:
            return
        self.stack.set_visible_child_name(row.nav_name)
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)

    def _nav_sync(self, *_args) -> None:
        """Garde le menu latéral et le titre alignés sur la page affichée."""
        name = self.stack.get_visible_child_name()
        row = self.nav_rows.get(name)
        if row is not None and self.nav_list.get_selected_row() is not row:
            self.nav_list.select_row(row)
        child = self.stack.get_visible_child()
        if child is not None:
            self.nav_title.set_title(self.stack.get_page(child).get_title())

    # ------------------------------------------------------------ Glisser-déposer

    DROP_CSS = """
    .drop-hint {
        background-color: alpha(@window_bg_color, 0.94);
        border: 3px dashed @accent_bg_color;
        border-radius: 18px;
        margin: 12px;
    }
    .drop-zone {
        border: 2px dashed alpha(@accent_bg_color, 0.55);
        border-radius: 12px;
        padding: 16px;
    }
    """

    def _setup_drop(self, overlay: Gtk.Overlay) -> None:
        """Rend la fenêtre sensible au glisser-déposer d'une archive .gpg ou d'un dossier."""
        provider = Gtk.CssProvider()
        if hasattr(provider, "load_from_string"):
            provider.load_from_string(self.DROP_CSS)
        else:  # GTK < 4.12
            provider.load_from_data(self.DROP_CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        # Cadre pointillé affiché pendant le survol, qui annonce ce qui va se passer
        self.drop_title = Gtk.Label(css_classes=["title-2"], wrap=True, justify=Gtk.Justification.CENTER)
        self.drop_text = Gtk.Label(css_classes=["dim-label"], wrap=True, justify=Gtk.Justification.CENTER,
                                   max_width_chars=48)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER,
                          valign=Gtk.Align.CENTER, can_target=False)
        content.append(Gtk.Image(icon_name="document-open-symbolic", pixel_size=64))
        content.append(self.drop_title)
        content.append(self.drop_text)
        self.drop_hint = Gtk.Box(css_classes=["drop-hint"], can_target=False, visible=False)
        self.drop_hint.append(content)
        content.set_hexpand(True)
        overlay.add_overlay(self.drop_hint)

        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        target.set_preload(True)  # le contenu est connu dès le survol : on peut l'annoncer
        target.connect("notify::current-drop", self._drop_hover_changed)
        target.connect("notify::value", self._drop_hover_changed)
        target.connect("drop", self._on_drop)
        overlay.add_controller(target)

    @staticmethod
    def _drop_items(value) -> list[Path | None]:
        """Chemins locaux des éléments déposés ; None pour un emplacement qui n'est pas sur le disque."""
        if value is None or not hasattr(value, "get_files"):
            return []
        return [Path(f.get_path()) if f.get_path() else None for f in value.get_files()]

    def _drop_hover_changed(self, target: Gtk.DropTarget, _pspec) -> None:
        if target.get_current_drop() is None:
            self.drop_hint.set_visible(False)
            return
        value = target.get_value()
        if value is None:  # contenu pas encore connu
            title, text = "Déposez ici", "Une archive chiffrée (.gpg) ou un dossier de sauvegarde."
        else:
            title, text = self._describe_drop(self._drop_items(value))
        self.drop_title.set_label(title)
        self.drop_text.set_label(text)
        self.drop_hint.set_visible(True)

    def _drop_blocker(self) -> tuple[str, str] | None:
        """Raison pour laquelle un dépôt est refusé pour l'instant, s'il y en a une."""
        if self.busy:
            return "Opération en cours", "Attendez qu'elle soit terminée avant de déposer quoi que ce soit."
        if self._current_page() == "wizard" and self.w_stack.get_visible_child_name() != "source":
            return ("Assistant en cours",
                    "Terminez l'assistant avant de changer de sauvegarde.")
        return None

    def _current_page(self) -> str:
        return self.stack.get_visible_child_name() or ""

    def _drop_side(self) -> str:
        """Page « Comparer » : le premier emplacement libre, sinon on remplace la sauvegarde récente."""
        return "old" if self.c_src["old"] is None else "new"

    def _describe_drop(self, items: list[Path | None]) -> tuple[str, str]:
        """Annonce, pendant le survol, ce que ferait le dépôt (même logique que _drop_one)."""
        blocker = self._drop_blocker()
        if blocker:
            return blocker
        if len(items) != 1:
            return "Un seul élément à la fois", "Déposez une archive .gpg ou un dossier, pas plusieurs."
        path = items[0]
        if path is None:
            return "Emplacement non pris en charge", "Copiez d'abord l'élément sur cet ordinateur."
        kind = backend.classify_drop(path)
        page = self._current_page()
        if kind == backend.DROP_OTHER:
            return "Élément non pris en charge", "Seuls une archive chiffrée (.gpg) et un dossier sont acceptés."
        if page == "mine" and kind in (backend.DROP_FOLDER, backend.DROP_ARCHIVES):
            return "Afficher ce dossier", f"Les sauvegardes de « {path.name} » seront listées sur cette page."
        if page == "compare":
            if kind == backend.DROP_FOLDER:
                return "Ce dossier n'est pas une sauvegarde", f"Rien à comparer dans « {path.name} »."
            side = self.SIDE_TITLES[self._drop_side()]
            return f"Comparer : {side}", f"« {path.name} » sera utilisée comme {side.lower()}."
        if kind == backend.DROP_ARCHIVE:
            return "Ouvrir cette archive", \
                f"« {path.name} » sera vérifiée, puis déchiffrée avec votre mot de passe."
        if kind == backend.DROP_BACKUP:
            return "Ouvrir cette sauvegarde", \
                f"« {path.name} » sera contrôlée, puis proposée à la restauration."
        if kind == backend.DROP_ARCHIVES:
            return "Ce dossier contient des archives", \
                "Vous choisirez de les ouvrir ou d'utiliser le dossier comme emplacement."
        if page == "auto":
            return "Destination automatique", f"Les sauvegardes automatiques iront dans « {path.name} »."
        return "Emplacement de la sauvegarde", f"Les sauvegardes seront créées dans « {path.name} »."

    def _toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, use_markup=False))

    def _on_drop(self, _target: Gtk.DropTarget, value, _x: float, _y: float) -> bool:
        items = self._drop_items(value)
        if not items:
            return False
        # On répond au glisser tout de suite ; l'action (boîtes de dialogue…) vient après
        GLib.idle_add(self._handle_drop, items)
        return True

    def _handle_drop(self, items: list[Path | None]) -> bool:
        blocker = self._drop_blocker()
        if blocker:
            self._toast(f"{blocker[0]} : {blocker[1]}")
        elif len(items) != 1:
            self._toast("Déposez un seul élément à la fois.")
        elif items[0] is None:
            self._toast("Cet emplacement n'est pas sur l'ordinateur : copiez-le d'abord sur le disque.")
        else:
            self._drop_one(items[0])
        return GLib.SOURCE_REMOVE

    def open_from_outside(self, items: list[Path | None]) -> bool:
        """Archive ou dossier transmis par le gestionnaire de fichiers ou la ligne de commande.

        Même traitement qu'un glisser-déposer, mais l'archive s'ouvre toujours dans « Restaurer » :
        celui qui double-clique ne regarde peut-être pas la page « Comparer »."""
        if self._current_page() == "compare" and not self.busy:
            self.stack.set_visible_child_name("restore")
        return self._handle_drop(items)

    def _drop_one(self, path: Path) -> None:
        """Choisit l'action selon ce qui est déposé et la page affichée."""
        kind = backend.classify_drop(path)
        page = self._current_page()
        if kind == backend.DROP_OTHER:
            self._toast(f"« {path.name} » n'est ni une archive chiffrée (.gpg) ni un dossier.")
            return
        if page == "mine" and kind in (backend.DROP_FOLDER, backend.DROP_ARCHIVES):
            self._mine_set_folder(path)  # un dossier : on en affiche les sauvegardes
            return
        if page == "compare":
            self._drop_compare(path, kind)
            return
        if kind == backend.DROP_ARCHIVE:
            self._ask_password(path, lambda archive, pw: (self._show_restore(), self._open_archive(archive, pw)))
        elif kind == backend.DROP_BACKUP:
            self._show_restore()
            row = self.w_src_row if page == "wizard" else self.r_src_row
            self._verify_then(path, row, lambda: (
                self._drop_restore_tmp(), self._set_restore_src(path)))
        elif kind == backend.DROP_ARCHIVES:
            self._offer_archives(
                path, backend.archives_in_folder(path),
                lambda archive: self._ask_password(
                    archive, lambda arc, pw: (self._show_restore(), self._open_archive(arc, pw))),
                on_folder=lambda: self._drop_location(path))
        else:
            self._drop_location(path)

    def _show_restore(self) -> None:
        """Montre où la sauvegarde s'ouvre ; l'assistant « Nouveau PC » partage la même source."""
        if self._current_page() != "wizard":
            self.stack.set_visible_child_name("restore")

    def _drop_location(self, path: Path) -> None:
        """Un dossier ordinaire : c'est l'emplacement des sauvegardes (manuelles ou automatiques)."""
        if not os.access(path, os.W_OK | os.X_OK):
            self._toast(f"Impossible d'écrire dans « {path.name} » : choisissez un autre dossier.")
            return
        if self._current_page() == "auto":
            self.auto_dest = path
            self.a_dest_row.set_subtitle(str(path))
            self._toast(f"Destination automatique : « {path.name} ». "
                        "Cliquez sur « Enregistrer » pour l'appliquer.")
        else:
            self.backup_parent = path
            self.m_folder = None
            self.b_dest_row.set_subtitle(str(path))
            self.stack.set_visible_child_name("backup")
            self._toast(f"Les sauvegardes seront créées dans « {path.name} ».")

    def _drop_compare(self, path: Path, kind: str) -> None:
        side = self._drop_side()

        def open_archive(archive: Path) -> None:
            self._ask_password(archive, lambda arc, pw: self._open_compare_archive(side, arc, pw))

        if kind == backend.DROP_ARCHIVE:
            open_archive(path)
        elif kind == backend.DROP_BACKUP:
            self._verify_then(path, self.c_rows[side], lambda: self._set_compare_src(side, path, None))
        elif kind == backend.DROP_ARCHIVES:
            self._offer_archives(path, backend.archives_in_folder(path), open_archive)
        else:
            self._toast(f"« {path.name} » ne contient aucune sauvegarde à comparer.")

    # ---------------------------------------------------------------- Sauvegarde

    def _build_backup_page(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title="Que sauvegarder ?",
                                     description="Décochez ce que vous ne voulez pas inclure.")
        self.b_profile_items = backend.detect_profile_items()
        self.b_profile_pick = {i.key for i in self.b_profile_items} & backend.default_profile_keys()
        profile_apps = ", ".join(dict.fromkeys(i.app for i in self.b_profile_items))
        self.b_rows = {
            "apt": switch_row("Applications", "Paquets installés avec apt"),
            "snap": switch_row("Snaps", "Applications installées avec snap", available=backend.has("snap")),
            "flatpak": switch_row("Flatpaks", "Applications installées avec Flatpak",
                                  available=backend.has("flatpak")),
            "repos": switch_row("Dépôts tiers", "PPA et clés de signature (hors dépôts Ubuntu)"),
            "extensions": switch_row("Extensions GNOME", "Liste des extensions et état d'activation"),
            "dconf": switch_row("Réglages du bureau",
                                "Dock, thème, raccourcis, fond d'écran, réglages des extensions",
                                available=backend.has("dconf"),
                                unavailable_text="Le paquet dconf-cli est requis"),
            "profiles": switch_row("Navigateurs et courrier",
                                   f"{profile_apps} : profils, signets, extensions, comptes",
                                   active=False, available=bool(self.b_profile_items),
                                   unavailable_text="Aucun navigateur ni client mail détecté"),
            "home": switch_row("Configurations personnelles",
                               "~/.config, ~/.ssh, données des applications… peut être volumineux",
                               active=False, available=backend.has("rsync"),
                               unavailable_text="Le paquet rsync est requis"),
        }
        for row in self.b_rows.values():
            group.add(row)
        self.b_profile_button = Gtk.Button(label="Choisir…", valign=Gtk.Align.CENTER,
                                           tooltip_text="Choisir précisément les profils à sauvegarder")
        self.b_profile_button.connect("clicked", self._pick_profiles)
        self.b_rows["profiles"].add_suffix(self.b_profile_button)
        self.b_rows["profiles"].bind_property("active", self.b_profile_button, "sensitive",
                                              GObject.BindingFlags.SYNC_CREATE)

        dest_group = Adw.PreferencesGroup(
            title="Destination",
            description="Un dossier « sauvegarde-ubuntu-AAAA-MM-JJ » sera créé à cet endroit. "
                        "Choisissez de préférence un disque externe.",
        )
        self.b_dest_row = Adw.ActionRow(title="Emplacement", subtitle=str(self.backup_parent),
                                        use_markup=False)
        self.b_dest_row.add_suffix(choose_button(self._choose_backup_parent))
        dest_group.add(self.b_dest_row)

        enc_group = Adw.PreferencesGroup(
            title="Protection",
            description="Sans le mot de passe, une sauvegarde chiffrée est définitivement "
                        "illisible. Notez-le dans un gestionnaire de mots de passe.",
        )
        self.b_encrypt = switch_row(
            "Chiffrer la sauvegarde",
            "Crée une archive unique protégée par mot de passe (AES-256). "
            "Recommandé avec les configurations personnelles.",
            active=False, available=backend.has("gpg"),
            unavailable_text="Le paquet gnupg est requis",
        )
        self.b_pw = Adw.PasswordEntryRow(title="Mot de passe (8 caractères minimum)")
        self.b_pw2 = Adw.PasswordEntryRow(title="Confirmer le mot de passe")
        enc_group.add(self.b_encrypt)
        for row in (self.b_pw, self.b_pw2):
            self.b_encrypt.bind_property("active", row, "visible", GObject.BindingFlags.SYNC_CREATE)
            enc_group.add(row)
        # Les configurations personnelles contiennent des secrets : on propose le chiffrement
        for secret_row in ("home", "profiles"):  # mots de passe, cookies, courriers
            self.b_rows[secret_row].connect(
                "notify::active",
                lambda row, _p: row.get_active() and self.b_encrypt.get_sensitive()
                and self.b_encrypt.set_active(True),
            )

        self.b_cloud = CloudGroup(
            can_encrypt=self.b_encrypt.get_sensitive(), unavailable_encrypt="Le paquet gnupg est requis",
            on_enable=lambda: self.b_encrypt.set_active(True))  # l'envoi n'a lieu que chiffré

        self.b_button = Gtk.Button(label="Lancer la sauvegarde", halign=Gtk.Align.CENTER,
                                   css_classes=["suggested-action", "pill"])
        self.b_button.connect("clicked", self._start_backup)
        self.b_task = TaskView()
        return page(group, dest_group, enc_group, self.b_cloud.group, self.b_button, self.b_task)

    def _choose_backup_parent(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog(title="Choisir l'emplacement de la sauvegarde",
                                initial_folder=Gio.File.new_for_path(str(self.backup_parent)))

        def done(dlg: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return  # annulé
            self.backup_parent = Path(folder.get_path())
            self.m_folder = None
            self.b_dest_row.set_subtitle(str(self.backup_parent))

        dialog.select_folder(self, None, done)

    def _pick_profiles(self, _button: Gtk.Button) -> None:
        shown = {i.key: i.label + (" · volumineux" if i.heavy else "")
                 for i in self.b_profile_items}
        names = list(shown.values())

        def done(chosen: set[str]) -> None:
            self.b_profile_pick = {k for k, label in shown.items() if label in chosen}
            self.b_profile_button.set_label(f"{len(chosen)} sur {len(names)}")

        pick_dialog(self, "Navigateurs et courrier à sauvegarder", names,
                    {shown[k] for k in self.b_profile_pick}, done)

    def _start_backup(self, _button: Gtk.Button) -> None:
        selected = {name for name, row in self.b_rows.items() if row.get_active()}
        profile_keys = None
        if "profiles" in selected:
            profile_keys = set(self.b_profile_pick)
            if not profile_keys:
                selected.discard("profiles")
                self.toasts.add_toast(Adw.Toast(title="Aucun profil choisi : navigateurs et courrier ignorés."))
            else:
                running = backend.running_apps(profile_keys)
                if running:
                    self.toasts.add_toast(Adw.Toast(
                        title=f"Fermez d'abord {', '.join(running)} pour copier son profil sans risque."))
                    return
        if not selected:
            self.toasts.add_toast(Adw.Toast(title="Sélectionnez au moins un élément."))
            return
        passphrase = None
        if self.b_encrypt.get_active():
            passphrase = self.b_pw.get_text()
            if len(passphrase) < 8:
                self.toasts.add_toast(Adw.Toast(title="Le mot de passe doit faire au moins 8 caractères."))
                self.b_pw.grab_focus()
                return
            if passphrase != self.b_pw2.get_text():
                self.toasts.add_toast(Adw.Toast(title="Les deux mots de passe sont différents."))
                self.b_pw2.grab_focus()
                return
        cloud = None
        if self.b_cloud.active:
            if not passphrase:
                self.toasts.add_toast(Adw.Toast(title="L'envoi vers le cloud exige de chiffrer la sauvegarde."))
                return
            try:
                cloud = self.b_cloud.target()
            except ValueError as exc:
                self.toasts.add_toast(Adw.Toast(title=f"Envoi cloud : {exc}."))
                return
        name = f"sauvegarde-ubuntu-{datetime.now():%Y-%m-%d}"
        dest = self.backup_parent / name
        if dest.exists() or backend.archive_path(dest).exists():
            dest = self.backup_parent / f"{name}-{datetime.now():%H%M%S}"
        # Avec chiffrement, seul le fichier .gpg est créé : on ouvre le dossier parent
        open_path = self.backup_parent if passphrase else dest

        def launch(space: backend.SpaceCheck | None) -> None:
            # Les mots de passe ne sont effacés qu'ici : si l'utilisateur annule à cause du manque
            # de place, il n'a pas à les retaper.
            self.b_pw.set_text("")
            self.b_pw2.set_text("")

            def job(log, prog):
                if space is not None:
                    log(space.summary())
                errors = backend.backup(dest, selected, log, prog, passphrase, profile_keys, cloud,
                                        skip_space_check=True)  # déjà vérifié juste avant
                # backup() a relu et contrôlé le résultat (il lève une erreur sinon) : on s'en souvient
                made = backend.archive_path(dest) if passphrase else dest
                verification.record_checks([(made, backend.OK, None)])
                return errors

            self._run(self.b_task, job, "Sauvegarde terminée", open_path,
                      backend.count_steps(selected, cloud is not None),
                      on_done=lambda _errors, _failure: self._refresh_verify_all())

        self._check_space_then(dest, selected, passphrase is not None, profile_keys, launch)

    def _check_space_then(self, dest: Path, selected: set[str], encrypted: bool,
                          profile_keys: set[str] | None, proceed) -> None:
        """Estime la place nécessaire (hors du fil de l'interface) avant de sauvegarder.

        Si elle suffit, ou si l'estimation est impossible, `proceed(contrôle)` est appelé tout de
        suite. Sinon une alerte laisse le choix d'annuler ou de continuer malgré tout.
        """
        if self.busy:
            return
        self.busy = True
        self._sync_buttons()
        self.b_task.reset()
        self.b_task.set_progress(0, "Estimation de la taille de la sauvegarde…")

        def worker() -> None:
            try:
                space = backend.check_space(dest, selected, encrypted, profile_keys)
            except Exception:  # noqa: BLE001 - une estimation ratée ne doit jamais empêcher de sauvegarder
                space = None
            GLib.idle_add(done, space)

        def done(space: backend.SpaceCheck | None) -> bool:
            self.busy = False
            self._sync_buttons()
            if space is None or space.ok:
                proceed(space)
                return GLib.SOURCE_REMOVE
            body = f"Taille estimée de la sauvegarde : {backend.fmt_size(space.size)}.\n\n{space.message()}"
            if space.encrypted:
                body += ("\n\nL'estimation est prudente : l'archive est compressée et pourrait occuper "
                         "moins de place. Sans assez de place, la sauvegarde risque pourtant d'échouer "
                         "en cours de route.")
            else:
                body += "\n\nSans assez de place, la sauvegarde échouera en cours de route."
            body += "\n\nLibérez de la place, choisissez un autre emplacement ou décochez un élément volumineux."
            dialog = Adw.AlertDialog(heading="Espace disque insuffisant", body=body)
            dialog.add_response("cancel", "Annuler")
            dialog.add_response("go", "Sauvegarder quand même")
            dialog.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")

            def answered(_dlg, response: str) -> None:
                if response == "go":
                    proceed(space)
                else:
                    self.b_task.set_visible(False)

            dialog.connect("response", answered)
            dialog.present(self)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=worker, daemon=True).start()

    # --------------------------------------------------------------- Restauration

    # ------------------------------------------------------- Mes sauvegardes

    MINE_MAX_ROWS = 100   # sauvegardes listées ; au-delà, une ligne dit combien restent cachées

    def _build_mine_page(self) -> Gtk.Widget:
        folder_group = Adw.PreferencesGroup(
            title="Mes sauvegardes",
            description="Tout le contenu de votre dossier de sauvegarde, avec la date, la taille et le "
                        "résultat de la dernière vérification. Chaque sauvegarde peut être ouverte, "
                        "vérifiée ou supprimée d'ici.",
        )
        self.m_folder_row = Adw.ActionRow(title="Dossier", subtitle="Recherche…", use_markup=False)
        self.m_folder_row.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
        self.m_folder_row.add_suffix(choose_button(
            self._mine_choose_folder, "Changer…", "Afficher les sauvegardes d'un autre dossier"))
        show_button = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER,
                                 css_classes=["flat"], tooltip_text="Ouvrir le dossier dans le gestionnaire de fichiers")
        show_button.update_property([Gtk.AccessibleProperty.LABEL], ["Ouvrir le dossier dans le gestionnaire de fichiers"])
        show_button.connect("clicked", self._mine_show_folder)
        self.m_folder_row.add_suffix(show_button)
        folder_group.add(self.m_folder_row)

        # Zone de dépôt : la fenêtre entière accepte le glisser-déposer, celle-ci l'annonce
        zone = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["drop-zone"])
        zone.append(Gtk.Image(icon_name="document-open-symbolic", pixel_size=32))
        zone.append(Gtk.Label(
            label="Glissez ici un dossier pour afficher ses sauvegardes, ou une archive (.gpg) "
                  "ou un dossier de sauvegarde pour l'ouvrir dans « Restaurer ».",
            wrap=True, justify=Gtk.Justification.CENTER, css_classes=["dim-label"]))

        self.m_list_group = Adw.PreferencesGroup(title="Sauvegardes")
        self.m_spinner = Gtk.Spinner(valign=Gtk.Align.CENTER, visible=False)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER,
                             css_classes=["flat"], tooltip_text="Actualiser la liste")
        refresh.update_property([Gtk.AccessibleProperty.LABEL], ["Actualiser la liste"])
        refresh.connect("clicked", lambda _b: self._refresh_mine())
        self.m_list_group.set_header_suffix(self._header_box(self.m_spinner, refresh))

        rotation_group = Adw.PreferencesGroup(
            title="Rotation",
            description="Pour ne pas remplir le disque, les plus anciennes sauvegardes automatiques sont "
                        "supprimées au fil du temps. Vos sauvegardes manuelles ne sont jamais touchées.",
        )
        self.m_rotate_row = Adw.ActionRow(title="Appliquer la rotation maintenant", use_markup=False,
                                          subtitle="Recherche des sauvegardes…")
        self.m_rotate_row.add_prefix(Gtk.Image(icon_name="edit-clear-all-symbolic"))
        settings = Gtk.Button(label="Régler…", valign=Gtk.Align.CENTER, css_classes=["flat"],
                              tooltip_text="Choisir le nombre de sauvegardes à conserver (onglet « Automatique »)")
        settings.connect("clicked", lambda _b: self.stack.set_visible_child_name("auto"))
        self.m_rotate_button = Gtk.Button(label="Appliquer", valign=Gtk.Align.CENTER)
        self.m_rotate_button.connect("clicked", self._mine_rotate_clicked)
        self.m_rotate_row.add_suffix(settings)
        self.m_rotate_row.add_suffix(self.m_rotate_button)
        rotation_group.add(self.m_rotate_row)
        self.m_actions = [self.m_rotate_button]

        return page(folder_group, zone, self.m_list_group, self._build_verify_group(), rotation_group)

    def _mine_guard(self) -> bool:
        if self.busy:
            self._toast("Une opération est en cours : attendez qu'elle soit terminée.")
            return False
        return True

    def _refresh_mine(self) -> bool:
        """Relit le dossier hors de la boucle graphique (un disque réseau peut être lent), puis refait la liste."""
        folder = self._backup_start_folder()
        self.m_gen += 1
        gen = self.m_gen
        self.m_spinner.set_visible(True)
        self.m_spinner.start()

        def worker() -> None:
            try:
                entries = []
                if folder is not None:
                    for path in backend.backups_in_folder(folder):
                        try:
                            when = verification.backup_time(path)
                        except OSError:
                            when = datetime.min
                        try:
                            size = backend.backup_size(path)
                        except OSError:
                            size = None
                        entries.append((when, path, size, verification.date_text(path)))
                    entries.sort(key=lambda e: (e[0], e[1].name), reverse=True)
                states = verification.item_states(folder)
                try:
                    keep = auto.load_config()["keep"]
                except (ValueError, TypeError):
                    keep = auto.DEFAULTS["keep"]
                n_auto = len(auto.automatic_backups(folder)) if folder is not None else 0
                n_victims = len(auto.rotation_victims(folder, keep)) if folder is not None else 0
            except Exception as exc:  # noqa: BLE001 - le sablier ne doit jamais rester affiché
                GLib.idle_add(failed, str(exc))
                return
            GLib.idle_add(show, entries, states, keep, n_auto, n_victims)

        def failed(message: str) -> bool:
            if gen == self.m_gen:
                self.m_spinner.stop()
                self.m_spinner.set_visible(False)
                self._toast(f"Impossible de lire le dossier : {message}")
            return GLib.SOURCE_REMOVE

        def show(entries: list, states: dict, keep: int, n_auto: int, n_victims: int) -> bool:
            if gen != self.m_gen:  # une lecture plus récente est en cours
                return GLib.SOURCE_REMOVE
            self.m_spinner.stop()
            self.m_spinner.set_visible(False)
            self.m_folder_row.set_subtitle(
                str(folder) if folder else "Aucun dossier de sauvegarde connu : choisissez-en un avec « Changer… »")
            for row in self.m_rows:
                self.m_list_group.remove(row)
            self.m_rows.clear()
            self.m_actions = [self.m_rotate_button]

            total = sum(e[2] for e in entries if e[2] is not None)
            self.m_list_group.set_description(
                f"{backend.plural(len(entries), 'sauvegarde', 'sauvegardes')} · "
                f"{backend.fmt_size(total)} au total" if entries else "")
            now = datetime.now()
            if not entries:
                self.m_rows.append(info_row("Aucune sauvegarde dans ce dossier" if folder else "Aucun dossier choisi"))
            for index, (_when, path, size, date) in enumerate(entries[:self.MINE_MAX_ROWS]):
                note = ""
                if index == 0:
                    note = ("C'est votre seule sauvegarde dans ce dossier." if len(entries) == 1
                            else "C'est votre sauvegarde la plus récente.")
                self.m_rows.append(self._mine_row(path, date, size, states.get(path.name), now, note))
            if len(entries) > self.MINE_MAX_ROWS:
                self.m_rows.append(info_row(
                    f"{len(entries) - self.MINE_MAX_ROWS} sauvegardes plus anciennes ne sont pas affichées"))
            for row in self.m_rows:
                self.m_list_group.add(row)

            if folder is None:
                self.m_rotate_row.set_subtitle("Choisissez d'abord un dossier de sauvegarde.")
            elif not n_auto:
                self.m_rotate_row.set_subtitle("Aucune sauvegarde automatique dans ce dossier.")
            elif n_victims:
                self.m_rotate_row.set_subtitle(
                    f"Garde les {keep} plus récentes : {backend.plural(n_victims, 'sauvegarde automatique serait supprimée', 'sauvegardes automatiques seraient supprimées')}.")
            else:
                self.m_rotate_row.set_subtitle(
                    f"Rien à supprimer : {backend.plural(n_auto, 'sauvegarde automatique', 'sauvegardes automatiques')} "
                    f"pour {keep} conservées.")
            self._sync_buttons()
            return GLib.SOURCE_REMOVE

        threading.Thread(target=worker, daemon=True).start()
        return GLib.SOURCE_REMOVE

    def _mine_row(self, path: Path, date: str, size: int | None, info: dict | None,
                  now: datetime, delete_note: str) -> Adw.ActionRow:
        state, level = verification.describe_item(info, now)
        kind = "dossier non chiffré" if path.is_dir() else "archive chiffrée"
        origin = "automatique" if auto.AUTO_RE.match(path.name) else "manuelle"
        size_text = backend.fmt_size(size) if size is not None else "taille inconnue"
        row = Adw.ActionRow(title=path.name, use_markup=False, tooltip_text=str(path),
                            subtitle=f"{date} · {size_text} · {kind} · {origin}\n{state}")
        icon, css = self.STATUS_ICONS[level]
        image = Gtk.Image(icon_name=icon)
        if css:
            image.add_css_class(css)
        row.add_prefix(image)
        buttons = Gtk.Box(spacing=2, valign=Gtk.Align.CENTER)
        for icon_name, tip, callback, extra in (
                ("emblem-ok-symbolic", "Vérifier cette sauvegarde", self._mine_verify, None),
                ("edit-find-symbolic", "Retrouver un fichier ou un dossier…", self._mine_single, None),
                *((("media-playback-start-symbolic", "Répétition de restauration à blanc (facultatif)",
                    self._rehearse_ask, None),) if path.is_file() else ()),
                ("document-open-symbolic", "Ouvrir dans « Restaurer »", self._mine_open, None),
                ("user-trash-symbolic", "Supprimer cette sauvegarde",
                 lambda p, n=delete_note: self._mine_delete(p, n), "error")):
            button = Gtk.Button(icon_name=icon_name, valign=Gtk.Align.CENTER,
                                css_classes=["flat"] + ([extra] if extra else []), tooltip_text=tip,
                                sensitive=not self.busy)
            button.update_property([Gtk.AccessibleProperty.LABEL], [f"{tip} : {path.name}"])
            button.connect("clicked", lambda _b, cb=callback, p=path: cb(p))
            buttons.append(button)
            self.m_actions.append(button)
        row.add_suffix(buttons)
        return row

    def _mine_choose_folder(self, _button: Gtk.Button) -> None:
        dialog = self._file_dialog(title="Choisir le dossier qui contient vos sauvegardes")

        def done(dlg: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return
            self._mine_set_folder(Path(folder.get_path()))

        dialog.select_folder(self, None, done)

    def _mine_set_folder(self, path: Path) -> None:
        self.m_folder = path
        self.m_folder_row.set_subtitle(str(path))
        self._refresh_verify_all()  # remet aussi à jour l'état du contrôle et la liste

    def _mine_show_folder(self, _button: Gtk.Button) -> None:
        folder = self._backup_start_folder()
        if folder is not None:
            self._open_folder(None, GLib.Variant.new_string(str(folder)))

    def _mine_verify(self, path: Path) -> None:
        if self._mine_guard():
            self._verify_now(path)

    def _mine_single(self, path: Path) -> None:
        if self._mine_guard():
            self._show_restore()
            self._single_start(path)

    def _mine_open(self, path: Path) -> None:
        """Même chemin qu'un dépôt sur la fenêtre : contrôle, puis la sauvegarde s'ouvre dans « Restaurer »."""
        if not self._mine_guard():
            return
        if path.is_file():
            self._ask_password(path, lambda archive, pw: (self._show_restore(), self._open_archive(archive, pw)))
        else:
            self._show_restore()
            self._verify_then(path, self.r_src_row, lambda: (
                self._drop_restore_tmp(), self._set_restore_src(path)))

    def _mine_delete(self, path: Path, note: str = "") -> None:
        if not self._mine_guard():
            return
        dialog = Adw.AlertDialog(
            heading="Supprimer cette sauvegarde ?",
            body=f"« {path.name} » sera supprimée définitivement de ce disque, avec sa somme de contrôle. "
                 f"Une copie envoyée dans le cloud n'est pas touchée.{(chr(10) * 2 + note) if note else ''}")
        dialog.add_response("cancel", "Annuler")
        dialog.add_response("delete", "Supprimer")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, r: self._mine_remove([path]) if r == "delete" else None)
        dialog.present(self)

    def _mine_rotate_clicked(self, _button: Gtk.Button) -> None:
        if not self._mine_guard():
            return
        folder = self._backup_start_folder()
        if folder is None:
            self._toast("Choisissez d'abord un dossier de sauvegarde.")
            return

        def worker() -> None:
            try:
                keep = auto.load_config()["keep"]
            except (ValueError, TypeError):
                keep = auto.DEFAULTS["keep"]
            GLib.idle_add(confirm, keep, auto.rotation_victims(folder, keep))

        def confirm(keep: int, victims: list[Path]) -> bool:
            if not victims:
                self._toast(f"Rien à supprimer : vos sauvegardes automatiques tiennent dans les {keep} conservées.")
                return GLib.SOURCE_REMOVE
            names = "\n".join(f"• {p.name}" for p in victims[:8])
            if len(victims) > 8:
                names += f"\n… et {len(victims) - 8} autres"
            dialog = Adw.AlertDialog(
                heading=f"Supprimer {backend.plural(len(victims), 'ancienne sauvegarde', 'anciennes sauvegardes')} ?",
                body=f"Pour n'en garder que les {keep} plus récentes, ces sauvegardes automatiques seront "
                     f"supprimées définitivement :\n\n{names}\n\nVos sauvegardes manuelles ne sont pas touchées.")
            dialog.add_response("cancel", "Annuler")
            dialog.add_response("delete", "Supprimer")
            dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")
            dialog.connect("response", lambda _d, r: self._mine_remove(victims) if r == "delete" else None)
            dialog.present(self)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=worker, daemon=True).start()

    def _mine_remove(self, paths: list[Path]) -> None:
        """Supprime des sauvegardes (dossier ou archive et somme de contrôle), hors de la boucle graphique."""
        if not self._mine_guard():
            return
        self.busy = True
        self._sync_buttons()

        def worker() -> None:
            removed, failed = [], []
            for path in paths:
                try:
                    backend.remove_backup(path)
                    removed.append(path)
                except OSError as exc:
                    if not path.exists():
                        removed.append(path)  # la sauvegarde a disparu ; seul un fichier annexe a résisté
                    failed.append(f"{path.name} ({exc.strerror or exc})")
            verification.forget(removed)
            GLib.idle_add(done, removed, failed)

        def done(removed: list[Path], failed: list[str]) -> bool:
            self.busy = False
            # Une sauvegarde supprimée ne peut plus servir de source à une restauration ni à une comparaison
            if self.restore_src in removed and not self.restore_tmp:
                self._reset_restore()
            for side in ("old", "new"):
                if self.c_src[side] in removed:
                    self.c_src[side] = None
                    self.c_label[side] = None
                    self._refresh_compare_row(side)
                    self._clear_compare()
            self._sync_buttons()
            if failed:
                dialog = Adw.AlertDialog(
                    heading="Suppression incomplète" if removed else "Suppression impossible",
                    body=("Impossible de supprimer :\n" + "\n".join(f"• {f}" for f in failed[:8])
                          + "\n\nVérifiez que le dossier n'est pas protégé en écriture ni utilisé par un autre programme."))
                dialog.add_response("ok", "Fermer")
                dialog.present(self)
            if removed:
                self._toast(f"Sauvegarde supprimée : « {removed[0].name} »" if len(removed) == 1
                            else f"{len(removed)} sauvegardes supprimées")
            self._refresh_verify_all()
            return GLib.SOURCE_REMOVE

        threading.Thread(target=worker, daemon=True).start()

    def _build_restore_page(self) -> Gtk.Widget:
        src_group = Adw.PreferencesGroup(
            title="Sauvegarde à restaurer",
            description="Choisissez l'archive chiffrée (.gpg), une archive à télécharger depuis le cloud, "
                        "ou un dossier « sauvegarde-ubuntu-… » déjà déchiffré. "
                        "Vous pouvez aussi glisser l'archive ou le dossier sur la fenêtre.",
        )
        self.r_src_row = Adw.ActionRow(title="Sauvegarde",
                                       subtitle="Aucune sauvegarde sélectionnée", use_markup=False)
        self.r_src_row.add_suffix(self._open_menu(self._choose_restore_src, self._choose_restore_archive))
        self.r_info_row = Adw.ActionRow(title="Informations", visible=False, use_markup=False)
        self.r_info_row.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
        src_group.add(self.r_src_row)
        src_group.add(self.r_info_row)

        single_group = Adw.PreferencesGroup(
            title="Restaurer un seul fichier ou dossier",
            description="Vous avez supprimé ou abîmé un fichier ? Parcourez ou cherchez dans une sauvegarde et "
                        "ne remettez que celui-là, sans tout restaurer. Seuls les fichiers personnels de la "
                        "sauvegarde sont concernés (configurations, profils de navigateurs et de courrier) : "
                        "vos documents (Documents, Images…) ne sont pas sauvegardés par l'application.",
        )
        self.s_row = Adw.ActionRow(title="Retrouver un fichier ou un dossier", use_markup=False,
                                   subtitle=self.SINGLE_IDLE)
        self.s_row.add_prefix(Gtk.Image(icon_name="edit-find-symbolic"))
        self.s_spinner = Gtk.Spinner(valign=Gtk.Align.CENTER, visible=False)
        self.s_stop = Gtk.Button(label="Arrêter", valign=Gtk.Align.CENTER, visible=False,
                                 css_classes=["destructive-action"])
        self.s_stop.connect("clicked", self._single_stop_clicked)
        self.s_menu = self._open_menu(self._single_choose_folder, self._single_choose_archive, cloud=False)
        for widget in (self.s_spinner, self.s_stop, self.s_menu):
            self.s_row.add_suffix(widget)
        self.s_cancel: threading.Event | None = None
        single_group.add(self.s_row)

        group = Adw.PreferencesGroup(
            title="Que restaurer ?",
            description="Votre mot de passe administrateur sera demandé pour les dépôts, "
                        "les paquets et les snaps.",
        )
        self.r_subtitles = {
            "repos": "PPA et clés de signature",
            "apt": "Réinstalle les paquets apt (ceux déjà présents sont ignorés)",
            "snap": "Réinstalle les snaps",
            "flatpak": "Réinstalle les flatpaks depuis Flathub",
            "extensions": "Réinstalle les extensions depuis extensions.gnome.org",
            "dconf": "Applique dock, thème, raccourcis, fond d'écran et extensions activées",
            "profiles": "Profils Firefox, Thunderbird, Chrome… (fichiers remplacés gardés en .avant-restauration)",
            "home": "Les fichiers existants sont conservés avec l'extension .avant-restauration",
        }
        titles = {k: backend.LABELS[k] for k in self.r_subtitles}
        self.r_rows = {}
        for name in ("repos", "apt", "snap", "flatpak", "extensions", "dconf", "profiles", "home"):
            row = Adw.SwitchRow(title=titles[name], subtitle="Choisissez d'abord une sauvegarde",
                                sensitive=False, use_markup=False)
            self.r_rows[name] = row
            if name in backend.PICKABLE:
                button = Gtk.Button(label="Choisir…", valign=Gtk.Align.CENTER, sensitive=False,
                                    tooltip_text="Choisir précisément ce qui sera installé")
                button.connect("clicked", lambda _b, n=name: self._pick(n))
                row.add_suffix(button)
                self.r_pick_buttons[name] = button
            group.add(row)

        self.p_group = Adw.PreferencesGroup(
            title="Aperçu avant restauration",
            description="Contenu de la sauvegarde comparé à cette machine, pour les éléments "
                        "cochés ci-dessus. Rien n'est modifié avant que vous lanciez la restauration.",
            visible=False,
        )
        self.p_spinner = Gtk.Spinner(valign=Gtk.Align.CENTER)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER,
                             css_classes=["flat"], tooltip_text="Actualiser l'aperçu")
        refresh.connect("clicked", lambda _b: self.restore_src and self._start_preview(self.restore_src))
        self.p_group.set_header_suffix(self._header_box(self.p_spinner, refresh))

        self.r_button = Gtk.Button(label="Lancer la restauration", halign=Gtk.Align.CENTER,
                                   css_classes=["suggested-action", "pill"], sensitive=False)
        self.r_button.connect("clicked", self._confirm_restore)
        self.r_task = TaskView()
        return page(src_group, single_group, group, self.p_group, self.r_button, self.r_task)

    def _build_verify_group(self) -> Adw.PreferencesGroup:
        """Contrôles d'intégrité (dernier contrôle, toutes les sauvegardes, une seule, copies cloud)."""
        verify_group = Adw.PreferencesGroup(
            title="Vérifier l'intégrité",
            description="Contrôle que vos sauvegardes ne sont pas abîmées, sans rien restaurer. "
                        "Le mot de passe n'est nécessaire que si vous choisissez de le tester.",
        )
        self.v_status_row = Adw.ActionRow(title="Dernière vérification", use_markup=False,
                                          subtitle="Recherche des sauvegardes…")
        self.v_status_icon = Gtk.Image(icon_name="emblem-ok-symbolic")
        self.v_status_row.add_prefix(self.v_status_icon)
        verify_group.add(self.v_status_row)
        self.v_all_row = Adw.ActionRow(title="Vérifier toutes mes sauvegardes", use_markup=False,
                                       subtitle="Recherche des sauvegardes…")
        self.v_all_row.add_prefix(Gtk.Image(icon_name="view-list-symbolic"))
        self.v_all_spinner = Gtk.Spinner(valign=Gtk.Align.CENTER, visible=False)
        self.v_all_other = Gtk.Button(label="Autre dossier…", valign=Gtk.Align.CENTER,
                                      css_classes=["flat"], tooltip_text="Contrôler les sauvegardes d'un autre dossier")
        self.v_all_other.connect("clicked", self._verify_all_choose)
        self.v_all_button = Gtk.Button(label="Tout vérifier", valign=Gtk.Align.CENTER,
                                       css_classes=["suggested-action"])
        self.v_all_button.connect("clicked", self._verify_all_clicked)
        for widget in (self.v_all_spinner, self.v_all_other, self.v_all_button):
            self.v_all_row.add_suffix(widget)
        self.v_all_cancel: threading.Event | None = None
        verify_group.add(self.v_all_row)
        self.v_all_pw = Adw.SwitchRow(
            title="Tester aussi le mot de passe", use_markup=False,
            subtitle="Facultatif. Il est demandé une fois, puis essayé sur chaque archive chiffrée, "
                     "sans rien restaurer ni écrire sur le disque.")
        self.v_all_pw.add_prefix(Gtk.Image(icon_name="dialog-password-symbolic"))
        verify_group.add(self.v_all_pw)
        self.v_reh_cancel: threading.Event | None = None
        self.v_reh_row = Adw.ActionRow(
            title="Répétition de restauration à blanc", use_markup=False,
            subtitle="Facultatif. Déchiffre toute l'archive dans un dossier temporaire, l'extrait et contrôle "
                     "ses sommes de contrôle internes, puis supprime tout. Demande de la place disque et du temps.")
        self.v_reh_row.add_prefix(Gtk.Image(icon_name="media-playback-start-symbolic"))
        self.v_reh_spinner = Gtk.Spinner(valign=Gtk.Align.CENTER, visible=False)
        self.v_reh_button = Gtk.Button(label="Choisir une archive…", valign=Gtk.Align.CENTER)
        self.v_reh_button.connect("clicked", self._rehearse_clicked)
        self.v_reh_row.add_suffix(self.v_reh_spinner)
        self.v_reh_row.add_suffix(self.v_reh_button)
        verify_group.add(self.v_reh_row)
        self.v_row = Adw.ActionRow(title="Vérifier une seule sauvegarde",
                                   subtitle="Choisissez la sauvegarde à contrôler", use_markup=False)
        self.v_row.add_prefix(Gtk.Image(icon_name="emblem-ok-symbolic"))
        self.v_row.add_suffix(self._open_menu(self._verify_choose_dir, self._verify_choose_archive,
                                              cloud=False))
        verify_group.add(self.v_row)
        self.v_cloud_row = Adw.ActionRow(title="Vérifier les copies cloud", use_markup=False,
                                         subtitle="Compare les empreintes chez le service, sans rien télécharger.")
        self.v_cloud_row.add_prefix(Gtk.Image(icon_name="folder-remote-symbolic"))
        self.v_cloud_spinner = Gtk.Spinner(valign=Gtk.Align.CENTER, visible=False)
        self.v_cloud_button = Gtk.Button(label="Vérifier", valign=Gtk.Align.CENTER)
        self.v_cloud_button.connect("clicked", self._verify_cloud_clicked)
        self.v_cloud_row.add_suffix(self.v_cloud_spinner)
        self.v_cloud_row.add_suffix(self.v_cloud_button)
        verify_group.add(self.v_cloud_row)
        return verify_group

    @staticmethod
    def _header_box(*widgets: Gtk.Widget) -> Gtk.Box:
        box = Gtk.Box(spacing=6)
        for w in widgets:
            box.append(w)
        return box

    # -- Aperçu : comparaison de la sauvegarde avec la machine

    def _pick(self, name: str) -> None:
        pv = self.previews.get(name)
        if pv is None:
            return
        names = backend.installable(pv, name)
        titles = {"apt": "Applications à installer", "snap": "Snaps à installer",
                  "flatpak": "Flatpaks à installer", "profiles": "Profils à restaurer"}

        def done(chosen: set[str]) -> None:
            self.r_picks[name] = None if len(chosen) == len(names) else chosen
            self._update_pick_ui(name)

        pick_dialog(self, titles[name], names, self.r_picks.get(name), done)

    def _update_pick_ui(self, name: str) -> None:
        chosen = self.r_picks.get(name)
        total = len(backend.installable(self.previews[name], name)) if name in self.previews else 0
        self.r_pick_buttons[name].set_label("Choisir…" if chosen is None
                                            else f"{len(chosen)} sur {total}")
        expander = self.p_expanders.get(name)
        if expander is not None and name in self.previews:
            text = backend.summarize(name, self.previews[name])
            if chosen is not None:
                text += f" · {len(chosen)} choisis"
            expander.set_subtitle(text)

    def _clear_preview(self) -> None:
        """Annule l'analyse en cours et vide l'aperçu."""
        self.preview_gen += 1
        self.previews = {}
        self.p_expanders = {}
        self.r_picks = {}
        for name, button in self.r_pick_buttons.items():
            button.set_sensitive(False)
            button.set_label("Choisir…")
        for row in self.p_rows:
            self.p_group.remove(row)
        self.p_rows.clear()
        self.p_spinner.stop()
        self.p_group.set_visible(False)

    def _start_preview(self, src: Path) -> None:
        self._clear_preview()
        gen = self.preview_gen
        names = [n for n in backend.COMPONENTS if self.restore_found.get(n)]
        placeholder = info_row("Analyse de la sauvegarde en cours…")
        self.p_group.add(placeholder)
        self.p_rows.append(placeholder)
        self.p_group.set_visible(True)
        self.p_spinner.start()

        def worker() -> None:
            result = backend.preview_all(src, names)
            GLib.idle_add(self._preview_done, gen, result)

        threading.Thread(target=worker, daemon=True).start()

    def _preview_done(self, gen: int, result: dict[str, backend.Preview]) -> bool:
        if gen != self.preview_gen:
            return GLib.SOURCE_REMOVE  # une autre sauvegarde a été choisie entre-temps
        self.previews = result
        self.p_spinner.stop()
        for row in self.p_rows:
            self.p_group.remove(row)
        self.p_rows.clear()
        for name in backend.COMPONENTS:
            pv = result.get(name)
            if pv is None:
                continue
            expander = Adw.ExpanderRow(title=backend.LABELS[name], use_markup=False,
                                       subtitle=backend.summarize(name, pv))
            fill_expander(expander, name, pv)
            # L'aperçu suit la sélection : décocher un élément le retire de la liste
            self.r_rows[name].bind_property("active", expander, "visible",
                                            GObject.BindingFlags.SYNC_CREATE)
            self.p_group.add(expander)
            self.p_rows.append(expander)
            self.p_expanders[name] = expander
        for name, button in self.r_pick_buttons.items():
            pv = result.get(name)
            button.set_sensitive(bool(pv and not pv.error and backend.installable(pv, name)))
        self._wizard_refresh_subtitles()
        return GLib.SOURCE_REMOVE

    def _choose_restore_src(self, _button: Gtk.Button, row: Adw.ActionRow | None = None) -> None:
        row = row or self.r_src_row
        dialog = self._file_dialog(title="Choisir le dossier de sauvegarde")

        def done(dlg: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return
            path = Path(folder.get_path())
            archives = backend.archives_in_folder(path)
            if archives:
                self._offer_archives(path, archives, self._ask_password)
                return
            self._verify_then(path, row, lambda: (
                self._drop_restore_tmp(), self._set_restore_src(path)))

        dialog.select_folder(self, None, done)

    def _backup_start_folder(self) -> Path | None:
        """Dossier où l'on a le plus de chances de retrouver ses sauvegardes."""
        if self.m_folder is not None and self.m_folder.is_dir():  # choisi sur « Mes sauvegardes »
            return self.m_folder
        candidates = [self.backup_parent, getattr(self, "auto_dest", None)]
        existing = [p for p in candidates if p and p.is_dir()]
        for folder in existing:
            try:
                if any(e.name.endswith(".tar.gz.gpg") or e.name.startswith("sauvegarde-ubuntu")
                       for e in folder.iterdir()):
                    return folder
            except OSError:
                continue
        return existing[0] if existing else None

    def _file_dialog(self, **kwargs) -> Gtk.FileDialog:
        """Sélecteur de fichier qui s'ouvre directement dans le dossier des sauvegardes."""
        dialog = Gtk.FileDialog(**kwargs)
        folder = self._backup_start_folder()
        if folder:
            dialog.set_initial_folder(Gio.File.new_for_path(str(folder)))
        return dialog

    def _offer_archives(self, folder: Path, archives: list[Path], on_pick, on_folder=None) -> None:
        """Le dossier choisi contient des archives chiffrées : propose de les ouvrir directement.

        `on_folder`, s'il est donné, ajoute le choix d'utiliser plutôt le dossier comme emplacement."""
        count = len(archives)
        newest = archives[0]
        if count == 1:
            body = (f"« {folder.name} » ne contient pas de sauvegarde non chiffrée, mais l'archive "
                    f"chiffrée « {newest.name} ».")
        else:
            body = (f"« {folder.name} » ne contient pas de sauvegarde non chiffrée, mais {count} archives "
                    f"chiffrées. La plus récente est « {newest.name} ».")
        dialog = Adw.AlertDialog(heading="Ce dossier contient des archives chiffrées", body=body)
        dialog.add_response("cancel", "Annuler")
        if count > 1:
            dialog.add_response("choose", "Choisir une autre…")
        if on_folder:
            dialog.add_response("folder", "Choisir ce dossier comme emplacement")
        dialog.add_response("open", "Ouvrir cette archive" if count == 1 else "Ouvrir la plus récente")
        dialog.set_response_appearance("open", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("open")
        dialog.set_close_response("cancel")

        def answered(_d, response: str) -> None:
            if response == "open":
                on_pick(newest)
            elif response == "choose":
                self._pick_archive_in(folder, on_pick)
            elif response == "folder":
                on_folder()

        dialog.connect("response", answered)
        dialog.present(self)

    def _pick_archive_in(self, folder: Path, on_pick) -> None:
        archives = Gtk.FileFilter(name="Archives chiffrées (*.gpg)")
        archives.add_pattern("*.gpg")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(archives)
        dialog = Gtk.FileDialog(title="Choisir l'archive chiffrée", filters=filters,
                                default_filter=archives,
                                initial_folder=Gio.File.new_for_path(str(folder)))

        def done(dlg: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                file = dlg.open_finish(result)
            except GLib.Error:
                return
            on_pick(Path(file.get_path()))

        dialog.open(self, None, done)

    def _choose_restore_archive(self, _button: Gtk.Button) -> None:
        archives = Gtk.FileFilter(name="Archives chiffrées (*.gpg)")
        archives.add_pattern("*.gpg")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(archives)
        dialog = self._file_dialog(title="Choisir l'archive chiffrée",
                                filters=filters, default_filter=archives)

        def done(dlg: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                file = dlg.open_finish(result)
            except GLib.Error:
                return
            self._ask_password(Path(file.get_path()))

        dialog.open(self, None, done)

    def _ask_password(self, archive: Path, on_ok=None, on_cancel=None, heading: str = "Archive chiffrée",
                      body: str | None = None, ok_label: str = "Déchiffrer") -> None:
        entry = Gtk.PasswordEntry(show_peek_icon=True, activates_default=True)
        dialog = Adw.AlertDialog(
            heading=heading,
            body=body or f"Saisissez le mot de passe de « {archive.name} ».",
        )
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Annuler")
        dialog.add_response("ok", ok_label)
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")
        dialog.set_close_response("cancel")

        def answered(_d, response: str) -> None:
            if response == "ok":
                (on_ok or self._open_archive)(archive, entry.get_text())
            elif on_cancel:
                on_cancel()

        dialog.connect("response", answered)
        dialog.present(self)

    def _open_archive(self, archive: Path, passphrase: str) -> None:
        if self.busy:
            return
        self.busy = True
        self._sync_buttons()
        self.r_src_row.set_subtitle("Vérification et déchiffrement en cours…")
        self.w_src_row.set_subtitle("Vérification et déchiffrement en cours…")

        def worker() -> None:
            try:
                status = backend.check_archive(archive)
                tmp, folder = backend.decrypt_archive(archive, passphrase)
                GLib.idle_add(self._archive_opened, archive, tmp, folder, None, status)
            except backend.IntegrityError as exc:
                GLib.idle_add(self._archive_opened, archive, None, None, str(exc), backend.BAD)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._archive_opened, archive, None, None, str(exc), None)

        threading.Thread(target=worker, daemon=True).start()

    def _archive_opened(self, archive: Path, tmp: Path | None, folder: Path | None,
                        error: str | None, status: str | None = None) -> bool:
        self.busy = False
        if error:
            self.r_src_row.set_subtitle("Aucune sauvegarde sélectionnée")
            self._wizard_source_changed(reset=False)
            self._sync_buttons()
            if status == backend.BAD:
                self._integrity_alert(archive, error)
            else:
                self.toasts.add_toast(Adw.Toast(title=f"Impossible d'ouvrir l'archive : {error}"))
        else:
            self._drop_restore_tmp()
            self.restore_tmp = tmp
            self._set_restore_src(folder, label=f"{archive} (déchiffrée)")
            self._integrity_toast(status)
        return GLib.SOURCE_REMOVE

    # -- Restauration depuis le cloud (rclone)

    def _open_menu(self, choose_folder, choose_archive, cloud: bool = True) -> Gtk.MenuButton:
        """Le bouton « Ouvrir… » : archive chiffrée (cas habituel), dossier déchiffré, cloud."""
        entries = [
            ("Archive chiffrée (.gpg)…",
             "Le fichier « sauvegarde-ubuntu-….tar.gz.gpg » créé par l'application",
             choose_archive, True, None),
            ("Dossier déjà déchiffré…",
             "Un dossier « sauvegarde-ubuntu-… » non chiffré, déchiffré à la main ou ancien",
             choose_folder, True, None),
        ]
        if cloud:
            have = backend.has("rclone")
            entries.append(("Depuis le cloud…",
                            "Liste les archives de votre service cloud (rclone), en télécharge "
                            "une puis l'ouvre",
                            self._choose_cloud_backup, have,
                            None if have else "Le paquet rclone est requis"))
        return open_menu(entries)

    def _set_src_subtitles(self, text: str) -> bool:
        self.r_src_row.set_subtitle(text)
        self.w_src_row.set_subtitle(text)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _cloud_date(modified: str) -> str:
        try:
            return datetime.fromisoformat(modified.replace("Z", "+00:00")).astimezone() \
                .strftime("%d/%m/%Y à %H:%M")
        except ValueError:
            return "date inconnue"

    def _choose_cloud_backup(self, _button: Gtk.Button) -> None:
        """Liste les archives du dossier distant et laisse choisir celle à restaurer."""
        if self.busy:
            return
        remotes = backend.rclone_remotes()
        if not remotes:
            self.toasts.add_toast(Adw.Toast(
                title="Aucun service rclone configuré : lancez « rclone config » dans un terminal."))
            return
        cfg = auto.load_config()  # service et dossier déjà utilisés pour l'envoi
        names = list(remotes)
        combo = Adw.ComboRow(title="Service", use_markup=False, model=Gtk.StringList.new(names))
        if cfg["cloud_remote"] in names:
            combo.set_selected(names.index(cfg["cloud_remote"]))
        path_row = Adw.EntryRow(title="Dossier distant",
                                text=cfg["cloud_path"] or backend.DEFAULT_CLOUD_PATH)
        search = Gtk.Button(label="Rechercher", valign=Gtk.Align.CENTER,
                            css_classes=["suggested-action"])
        path_row.add_suffix(search)
        spinner = Gtk.Spinner(valign=Gtk.Align.CENTER)
        service_group = Adw.PreferencesGroup(
            title="Service cloud",
            description="Les sauvegardes sont cherchées dans le dossier où l'envoi les dépose.")
        service_group.set_header_suffix(spinner)
        warning = Adw.ActionRow(title="À savoir", use_markup=False, subtitle_lines=0, visible=False)
        warning.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
        for row in (combo, path_row, warning):
            service_group.add(row)

        results = Adw.PreferencesGroup(
            title="Sauvegardes disponibles",
            description="Les plus récentes d'abord. L'archive est téléchargée, son intégrité "
                        "contrôlée, puis le mot de passe vous est demandé.")
        result_rows: list[Gtk.Widget] = []
        generation = [0]  # une recherche périmée (nouvelle recherche, fenêtre fermée) est ignorée

        def selected() -> str:
            item = combo.get_selected_item()
            return item.get_string() if item else ""

        def show_warning(*_args) -> None:
            text = backend.CLOUD_WARNINGS.get(remotes.get(selected(), ""), "")
            warning.set_subtitle(text)
            warning.set_visible(bool(text))

        def set_rows(rows: list[Gtk.Widget]) -> None:
            for row in result_rows:
                results.remove(row)
            result_rows.clear()
            for row in rows:
                results.add(row)
                result_rows.append(row)

        dialog = Adw.Dialog(title="Ouvrir une sauvegarde du cloud", content_width=520,
                            content_height=640)

        def choose(remote: str, path: str, item: backend.CloudBackup) -> None:
            dialog.close()
            self._download_cloud_backup(remote, path, item)

        def listed(gen: int, remote: str, path: str, found: list[backend.CloudBackup],
                   error: str | None) -> bool:
            if gen != generation[0]:
                return GLib.SOURCE_REMOVE
            spinner.stop()
            search.set_sensitive(True)
            if error:
                set_rows([info_row("Recherche impossible", error)])
            elif not found:
                set_rows([info_row("Aucune archive chiffrée trouvée",
                                   f"Le dossier « {path} » de {remote} n'en contient pas.")])
            else:
                rows: list[Gtk.Widget] = []
                for item in found[:MAX_ROWS]:
                    detail = f"{self._cloud_date(item.modified)} · {backend._fmt_size(item.size)}"
                    if not item.has_checksum:
                        detail += " · sans somme de contrôle"
                    row = Adw.ActionRow(title=item.name, subtitle=detail, use_markup=False,
                                        activatable=True)
                    row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
                    row.connect("activated", lambda _r, i=item: choose(remote, path, i))
                    rows.append(row)
                if len(found) > MAX_ROWS:
                    rows.append(info_row(f"… et {len(found) - MAX_ROWS} archives plus anciennes "
                                         "non affichées"))
                set_rows(rows)
            return GLib.SOURCE_REMOVE

        def search_now(*_args) -> None:
            try:
                remote, path = backend.cloud_target(selected(), path_row.get_text())
            except ValueError as exc:
                set_rows([info_row("Service ou dossier invalide", str(exc))])
                return
            generation[0] += 1
            gen = generation[0]
            set_rows([info_row("Recherche en cours…")])
            spinner.start()
            search.set_sensitive(False)

            def worker() -> None:
                try:
                    found, error = backend.list_cloud_backups(remote, path), None
                except Exception as exc:  # noqa: BLE001
                    found, error = [], str(exc)
                GLib.idle_add(listed, gen, remote, path, found, error)

            threading.Thread(target=worker, daemon=True).start()

        search.connect("clicked", search_now)
        path_row.connect("entry-activated", search_now)
        combo.connect("notify::selected", show_warning)
        dialog.connect("closed", lambda _d: generation.__setitem__(0, generation[0] + 1))
        show_warning()
        set_rows([info_row("Cliquez sur « Rechercher » pour lister le dossier distant.")])

        toolbar = Adw.ToolbarView(content=page(service_group, results))
        toolbar.add_top_bar(Adw.HeaderBar())
        dialog.set_child(toolbar)
        dialog.present(self)
        if cfg["cloud_remote"] in names:
            search_now()  # service connu : on lance directement la recherche

    def _download_cloud_backup(self, remote: str, path: str, item: backend.CloudBackup) -> None:
        """Télécharge l'archive choisie, contrôle son intégrité, puis demande le mot de passe."""
        if self.busy:
            return
        self.busy = True
        self._sync_buttons()
        previous = self.r_src_row.get_subtitle()
        self._set_src_subtitles(f"Téléchargement de « {item.name} »…")

        def log(line: str) -> None:
            # rclone affiche sa progression sur une ligne : « 1,2 Mo / 10 Mo, 12%, … ETA 9s »
            if "%" in line and "ETA" in line:
                GLib.idle_add(self._set_src_subtitles,
                              "Téléchargement… " + line.split("INFO  :")[-1].strip())

        def worker() -> None:
            try:
                tmp, archive, status = backend.download_from_cloud(remote, path, item.name, log)
            except backend.IntegrityError as exc:
                GLib.idle_add(done, None, None, None, str(exc), True)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(done, None, None, None, str(exc), False)
            else:
                GLib.idle_add(done, tmp, archive, status, None, False)

        def done(tmp: Path | None, archive: Path | None, status: str | None,
                 error: str | None, damaged: bool) -> bool:
            self.busy = False
            self.r_src_row.set_subtitle(previous)
            self._wizard_source_changed(reset=False)
            self._sync_buttons()
            if error:
                if damaged:
                    self._integrity_alert(Path(item.name), error)
                else:
                    self.toasts.add_toast(Adw.Toast(title=f"Téléchargement impossible : {error}"))
            else:
                label = f"{remote}:{path}/{item.name} (déchiffrée)"
                self._ask_password(
                    archive,
                    on_ok=lambda a, pw: self._open_cloud_archive(tmp, a, pw, label, status),
                    on_cancel=lambda: backend.remove_tree(tmp))
            return GLib.SOURCE_REMOVE

        threading.Thread(target=worker, daemon=True).start()

    def _open_cloud_archive(self, download: Path, archive: Path, passphrase: str,
                            label: str, status: str | None) -> None:
        """Déchiffre l'archive téléchargée. En cas d'erreur de mot de passe, on le redemande
        sans retélécharger ; la copie téléchargée est supprimée dans tous les autres cas."""
        if self.busy:
            backend.remove_tree(download)
            return
        self.busy = True
        self._sync_buttons()
        previous = self.r_src_row.get_subtitle()
        self._set_src_subtitles("Déchiffrement en cours…")

        def worker() -> None:
            try:
                tmp, folder = backend.decrypt_archive(archive, passphrase)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(done, None, None, str(exc))
            else:
                GLib.idle_add(done, tmp, folder, None)

        def done(tmp: Path | None, folder: Path | None, error: str | None) -> bool:
            self.busy = False
            self.r_src_row.set_subtitle(previous)
            self._wizard_source_changed(reset=False)
            self._sync_buttons()
            if error:
                self.toasts.add_toast(Adw.Toast(title=f"Impossible d'ouvrir l'archive : {error}"))
                self._ask_password(
                    archive,
                    on_ok=lambda a, pw: self._open_cloud_archive(download, a, pw, label, status),
                    on_cancel=lambda: backend.remove_tree(download))
            else:
                backend.remove_tree(download)  # l'archive déchiffrée suffit désormais
                self._drop_restore_tmp()
                self.restore_tmp = tmp
                self._set_restore_src(folder, label=label)
                self._integrity_toast(status)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=worker, daemon=True).start()

    def _verify_choose_dir(self, _button: Gtk.Button) -> None:
        dialog = self._file_dialog(title="Vérifier : choisir le dossier de sauvegarde")

        def done(dlg: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return
            path = Path(folder.get_path())
            archives = backend.archives_in_folder(path)
            if archives:
                self._offer_archives(path, archives, self._verify_now)
                return
            self._verify_now(path)

        dialog.select_folder(self, None, done)

    def _verify_choose_archive(self, _button: Gtk.Button) -> None:
        archives = Gtk.FileFilter(name="Archives chiffrées (*.gpg)")
        archives.add_pattern("*.gpg")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(archives)
        dialog = self._file_dialog(title="Vérifier : choisir l'archive chiffrée",
                                filters=filters, default_filter=archives)

        def done(dlg: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                file = dlg.open_finish(result)
            except GLib.Error:
                return
            self._verify_now(Path(file.get_path()))

        dialog.open(self, None, done)

    def _page_changed(self, *_args) -> None:
        if self._current_page() in ("restore", "backup", "auto", "mine"):
            self._refresh_verify_all()

    STATUS_ICONS = {"ok": ("emblem-ok-symbolic", "success"),
                    "warn": ("dialog-warning-symbolic", "warning"),
                    "bad": ("dialog-error-symbolic", "error"),
                    "none": ("dialog-information-symbolic", None)}

    def _refresh_verify_all_again(self) -> bool:
        self._refresh_verify_all()
        return GLib.SOURCE_CONTINUE

    def _known_folders(self) -> list[Path]:
        """Dossiers où l'on range ses sauvegardes : manuelles et automatiques."""
        folders = []
        for folder in (self.m_folder, self.backup_parent, getattr(self, "auto_dest", None)):
            if folder and folder not in folders:
                folders.append(folder)
        return folders

    def _cloud_target(self) -> tuple[str, str] | None:
        """(service, dossier) des copies cloud : le choix en cours dans « Sauvegarder », à défaut celui
        des sauvegardes automatiques. None si aucun service n'est choisi."""
        try:
            if self.b_cloud.active:
                return self.b_cloud.target()
            cfg = auto.load_config()
            if cfg["cloud_remote"]:
                return backend.cloud_target(cfg["cloud_remote"], cfg["cloud_path"])
        except ValueError:
            pass
        return None

    def _show_cloud_status(self) -> None:
        target = self._cloud_target()
        if not backend.has("rclone"):
            self.v_cloud_row.set_subtitle("Le paquet rclone est requis.")
        elif target is None:
            self.v_cloud_row.set_subtitle("Aucun service cloud choisi : réglez l'envoi dans « Sauvegarder » "
                                          "ou « Automatique ».")
        else:
            text, _level = verification.cloud_status(*target)
            self.v_cloud_row.set_subtitle(f"{target[0]}:{target[1]} · {text}")
        self.v_cloud_button.set_sensitive(backend.has("rclone") and target is not None and not self.busy)

    def _refresh_verify_all(self) -> bool:
        """Met à jour l'état du dernier contrôle, l'alerte d'ancienneté et le sous-titre
        du bouton « Tout vérifier » (quel dossier, combien de sauvegardes)."""
        folder = self._backup_start_folder()
        search = self._known_folders()
        alert_days = auto.load_config()["alert_days"]

        def worker() -> None:  # un disque réseau peut être lent : on ne bloque pas la fenêtre
            count = len(backend.backups_in_folder(folder)) if folder else 0
            status = verification.evaluate(folder, search, alert_days)
            GLib.idle_add(show, count, status)

        def show(count: int, status: verification.Status) -> bool:
            icon, css = self.STATUS_ICONS[status.level]
            self.v_status_icon.set_from_icon_name(icon)
            for name in ("success", "warning", "error"):
                self.v_status_icon.remove_css_class(name)
            if css:
                self.v_status_icon.add_css_class(css)
            self.v_status_row.set_subtitle(status.text)
            self._show_cloud_status()
            self.banner.set_title(status.alert)
            self.banner.set_revealed(bool(status.alert))
            if self._current_page() == "mine":
                self._refresh_mine()
            if self.v_all_cancel is None:  # sinon le contrôle en cours écrit lui-même le sous-titre
                if folder is None:
                    self.v_all_row.set_subtitle(
                        "Aucun dossier de sauvegarde connu : choisissez-en un avec « Autre dossier… »")
                elif count:
                    self.v_all_row.set_subtitle(f"{count} sauvegarde{'s' if count > 1 else ''} dans {folder}")
                else:
                    self.v_all_row.set_subtitle(f"Aucune sauvegarde dans {folder}")
            return GLib.SOURCE_REMOVE

        threading.Thread(target=worker, daemon=True).start()
        return GLib.SOURCE_REMOVE

    def _verify_all_clicked(self, _button: Gtk.Button) -> None:
        if self.v_all_cancel is not None:  # le bouton sert d'« Arrêter » pendant le contrôle
            self.v_all_cancel.set()
            self.v_all_button.set_sensitive(False)
            self.v_all_button.set_label("Arrêt…")
            return
        folder = self._backup_start_folder()
        if folder is None:
            self._verify_all_choose(None)
        else:
            self._verify_all_start(folder)

    def _verify_all_choose(self, _button: Gtk.Button | None) -> None:
        dialog = self._file_dialog(title="Vérifier : choisir le dossier qui contient les sauvegardes")

        def done(dlg: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return
            self._verify_all_start(Path(folder.get_path()))

        dialog.select_folder(self, None, done)

    def _verify_all_start(self, folder: Path) -> None:
        """Contrôle d'un coup toutes les sauvegardes du dossier (archives et dossiers non chiffrés)."""
        if self.busy:
            return
        paths = backend.backups_in_folder(folder)
        if not paths and backend.is_backup_folder(folder):
            paths = [folder]  # on a choisi une sauvegarde elle-même : on la contrôle seule
        if not paths:
            alert = Adw.AlertDialog(
                heading="Aucune sauvegarde trouvée",
                body=f"« {folder} » ne contient ni archive chiffrée (.tar.gz.gpg) ni dossier "
                     "« sauvegarde-ubuntu-… ».\n\nChoisissez le dossier où vos sauvegardes sont rangées.")
            alert.add_response("ok", "Fermer")
            alert.add_response("choose", "Choisir un autre dossier…")
            alert.connect("response", lambda _d, r: self._verify_all_choose(None) if r == "choose" else None)
            alert.present(self)
            return

        archives = [p for p in paths if p.is_file()]
        if self.v_all_pw.get_active() and archives:
            n = len(archives)
            self._ask_password(
                archives[0],
                lambda _a, passphrase: self._verify_all_run(folder, paths, passphrase),
                heading="Tester le mot de passe",
                body=f"Saisissez le mot de passe de vos archives. Il sera essayé sur {n} archive"
                     f"{'s' if n > 1 else ''} chiffrée{'s' if n > 1 else ''} ; rien n'est restauré "
                     "ni écrit sur le disque.",
                ok_label="Vérifier")
            return
        self._verify_all_run(folder, paths, None)

    def _verify_all_run(self, folder: Path, paths: list[Path], passphrase: str | None) -> None:
        if self.busy:
            return
        if passphrase is not None and not passphrase:
            self._toast("Saisissez un mot de passe, ou désactivez le test du mot de passe.")
            return
        cancel = threading.Event()
        self.v_all_cancel = cancel
        self.busy = True
        self._sync_buttons()
        self.v_all_other.set_sensitive(False)
        self.v_all_pw.set_sensitive(False)
        self.v_row.set_sensitive(False)
        self.v_all_button.set_label("Arrêter")
        self.v_all_button.remove_css_class("suggested-action")
        self.v_all_button.add_css_class("destructive-action")
        self.v_all_spinner.set_visible(True)
        self.v_all_spinner.start()
        self.v_all_row.set_subtitle(f"Vérification de {len(paths)} sauvegarde{'s' if len(paths) > 1 else ''}…")

        def progress(_fraction: float, text: str) -> None:
            if text:
                GLib.idle_add(self.v_all_row.set_subtitle, f"Vérification {text}…")

        def worker() -> None:
            results = backend.verify_many(paths, progress, cancel.is_set, passphrase)
            verification.record_checks((r.path, r.status, r.password) for r in results)
            GLib.idle_add(done, results)

        def done(results: list[backend.BackupCheck]) -> bool:
            self.v_all_cancel = None
            self.busy = False
            self._sync_buttons()
            self.v_all_other.set_sensitive(True)
            self.v_all_pw.set_sensitive(True)
            self.v_row.set_sensitive(True)
            self.v_all_button.set_sensitive(True)
            self.v_all_button.set_label("Tout vérifier")
            self.v_all_button.remove_css_class("destructive-action")
            self.v_all_button.add_css_class("suggested-action")
            self.v_all_spinner.stop()
            self.v_all_spinner.set_visible(False)
            self._refresh_verify_all()
            self._show_verify_all_report(folder, results, len(paths), passphrase is not None)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=worker, daemon=True).start()

    def _show_verify_all_report(self, folder: Path, results: list[backend.BackupCheck], total: int,
                                password_tested: bool = False) -> None:
        ok = [r for r in results if r.status == backend.OK and r.password is not False]
        missing = [r for r in results if r.status == backend.MISSING and r.password is not False]
        bad = [r for r in results if r.status == backend.BAD]
        refused = [r for r in results if r.password is False]
        accepted = [r for r in results if r.password is True]
        stopped = len(results) < total
        plural = lambda n: "s" if n > 1 else ""  # noqa: E731

        if not results:
            heading, body = "Contrôle interrompu", "Aucune sauvegarde n'a eu le temps d'être contrôlée."
        elif bad:
            heading = f"{len(bad)} sauvegarde{plural(len(bad))} abîmée{plural(len(bad))}"
            body = ("Ne comptez pas sur elle%s : refaites une sauvegarde dès que possible. "
                    "Rien n'a été supprimé ni modifié." % plural(len(bad)))
        elif refused:
            if len(refused) == len([r for r in results if r.password is not None]):
                heading = "Mot de passe refusé"
                body = ("Aucune des archives testées ne s'ouvre avec ce mot de passe. Vérifiez la saisie "
                        "(majuscules, clavier) et recommencez. Les fichiers eux-mêmes ne sont pas abîmés.")
            else:
                heading = f"Mot de passe refusé par {len(refused)} archive{plural(len(refused))}"
                body = ("Les autres s'ouvrent avec ce mot de passe. Ces archives ont sans doute été "
                        "chiffrées avec un autre mot de passe : retrouvez-le avant d'en avoir besoin.")
        elif missing:
            heading = "Aucune sauvegarde abîmée"
            if len(missing) == 1:
                body = ("Mais une sauvegarde n'a pas de somme de contrôle (ancienne version de "
                        "l'application, ou fichier .sha256 déplacé) : on ne peut pas confirmer qu'elle est intacte.")
            else:
                body = (f"Mais {len(missing)} sauvegardes n'ont pas de somme de contrôle (ancienne version de "
                        "l'application, ou fichiers .sha256 déplacés) : on ne peut pas confirmer qu'elles sont intactes.")
        else:
            heading = "Toutes vos sauvegardes sont intactes"
            body = f"{len(ok)} sauvegarde{plural(len(ok))} contrôlée{plural(len(ok))} dans « {folder} »."
        if stopped and results:
            heading = "Contrôle interrompu"
            body = f"{len(results)} sauvegarde{plural(len(results))} sur {total} contrôlée{plural(len(results))}. " + body
        if password_tested:
            if accepted and not refused:
                body += (f"\n\nMot de passe correct pour {len(accepted)} archive{plural(len(accepted))} "
                         "(le début a été déchiffré, rien n'a été restauré).")
        elif ok and any(r.path.is_file() for r in ok):
            body += ("\n\nLe mot de passe des archives n'est pas testé : activez « Tester aussi le "
                     "mot de passe » pour le vérifier sans rien restaurer.")

        alert = Adw.AlertDialog(heading=heading, body=body)
        if results:
            listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
            order = {backend.BAD: 0, backend.MISSING: 2, backend.OK: 3}
            icons = {backend.BAD: "dialog-warning-symbolic", backend.MISSING: "dialog-question-symbolic",
                     backend.OK: "emblem-ok-symbolic"}

            def rank(r: backend.BackupCheck) -> tuple[int, str]:
                return (1 if r.password is False and r.status != backend.BAD else order[r.status], r.path.name)

            for check in sorted(results, key=rank):
                subtitle = check.message or "Intacte"
                icon = icons[check.status]
                if check.password is False and check.status != backend.BAD:
                    subtitle, icon = "Mot de passe refusé : " + check.password_message, "dialog-password-symbolic"
                elif check.password is True:
                    subtitle += " · mot de passe correct"
                row = Adw.ActionRow(title=check.path.name, use_markup=False, subtitle=subtitle)
                row.add_prefix(Gtk.Image(icon_name=icon))
                listbox.append(row)
            scroller = Gtk.ScrolledWindow(child=listbox, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                          propagate_natural_height=True, max_content_height=300)
            alert.set_extra_child(scroller)
        alert.add_response("ok", "Fermer")
        alert.present(self)

    def _verify_now(self, path: Path) -> None:
        """Contrôle une sauvegarde à la demande et affiche le verdict."""
        if self.busy:
            return
        self.busy = True
        self._sync_buttons()
        self.v_row.set_subtitle(f"Vérification de « {path.name} »…")

        def worker() -> None:
            status, msg = backend.verify_backup(path)
            verification.record_checks([(path, status, None)])
            GLib.idle_add(done, status, msg)

        def done(status: str, msg: str) -> bool:
            self.busy = False
            self._sync_buttons()
            self.v_row.set_subtitle("Choisissez la sauvegarde à contrôler")
            self._refresh_verify_all()
            if status == backend.OK:
                heading, body = "Sauvegarde intacte", f"« {path.name} » correspond à sa somme de contrôle."
                if path.is_file():
                    body += ("\n\nLe fichier n'a pas été modifié ni abîmé. Le mot de passe, lui, "
                             "n'est pas encore testé : le bouton « Tester le mot de passe… » le vérifie "
                             "sans rien restaurer.")
            elif status == backend.MISSING:
                heading = "Vérification impossible"
                if path.is_dir():
                    body = (f"« {path.name} » ne contient pas le fichier {backend.FOLDER_SUMS}, qui sert "
                            "à contrôler que rien n'est abîmé.\n\nCe n'est pas forcément un problème : "
                            "ce dossier vient peut-être d'une ancienne version de l'application, ou ce "
                            "n'est pas un dossier de sauvegarde. Si votre sauvegarde est une archive "
                            "chiffrée (.gpg), utilisez « Archive chiffrée… » à la place.")
                else:
                    body = (f"Le fichier « {backend.checksum_path(path).name} », qui doit se trouver à "
                            f"côté de « {path.name} », est introuvable : il a peut-être été déplacé ou "
                            "supprimé, ou l'archive vient d'une ancienne version de l'application.\n\n"
                            "L'archive n'est pas forcément abîmée, mais on ne peut pas le confirmer. "
                            "Refaites une sauvegarde pour être protégé.")
            else:
                heading = "Sauvegarde abîmée"
                body = (f"« {path.name} » : {msg}.\n\n"
                        "Ne comptez pas sur elle : refaites une sauvegarde dès que possible.")
            alert = Adw.AlertDialog(heading=heading, body=body)
            alert.add_response("ok", "Fermer")
            if path.is_file() and status in (backend.OK, backend.MISSING):
                alert.add_response("password", "Tester le mot de passe…")
                alert.add_response("rehearsal", "Répétition de restauration…")
                alert.connect("response", lambda _d, r: self._test_password_ask(path) if r == "password"
                              else self._rehearse_ask(path) if r == "rehearsal" else None)
            alert.present(self)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=worker, daemon=True).start()

    def _verify_cloud_clicked(self, _button: Gtk.Button) -> None:
        """Compare les copies du cloud à vos copies locales par empreinte, sans les télécharger."""
        target = self._cloud_target()
        if self.busy or target is None:
            return
        remote, path = target
        folders = [f for f in self._known_folders() if f.is_dir()]
        self.busy = True
        self._sync_buttons()
        self.v_cloud_spinner.set_visible(True)
        self.v_cloud_spinner.start()
        self.v_cloud_row.set_subtitle(f"Lecture de {remote}:{path}…")

        def progress(_fraction: float, text: str) -> None:
            if text:
                GLib.idle_add(self.v_cloud_row.set_subtitle, f"Vérification {text}…")

        def worker() -> None:
            try:
                checks, error = backend.verify_cloud(remote, path, folders, progress), None
                verification.record_cloud(remote, path, [(c.name, c.status) for c in checks])
            except Exception as exc:  # noqa: BLE001
                checks, error = [], str(exc)
            GLib.idle_add(done, checks, error)

        def done(checks: list[backend.CloudCheck], error: str | None) -> bool:
            self.busy = False
            self.v_cloud_spinner.stop()
            self.v_cloud_spinner.set_visible(False)
            self._sync_buttons()
            self._show_cloud_status()
            self._show_cloud_report(remote, path, checks, error)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=worker, daemon=True).start()

    def _show_cloud_report(self, remote: str, path: str, checks: list[backend.CloudCheck],
                           error: str | None) -> None:
        plural = lambda n: "s" if n > 1 else ""  # noqa: E731
        if error:
            alert = Adw.AlertDialog(heading="Contrôle impossible",
                                    body=f"{remote}:{path} : {error}.\n\nRien n'a été téléchargé ni modifié.")
            alert.add_response("ok", "Fermer")
            alert.present(self)
            return
        bad = [c for c in checks if c.status == backend.CLOUD_BAD]
        unsure = [c for c in checks if c.status in (backend.CLOUD_WEAK, backend.CLOUD_NOREF)]
        good = [c for c in checks if c.status == backend.CLOUD_OK]
        if not checks:
            heading, body = "Aucune archive sur le cloud", f"Le dossier « {path} » de {remote} ne contient pas d'archive chiffrée."
        elif bad:
            heading = f"{len(bad)} copie{plural(len(bad))} cloud abîmée{plural(len(bad))}"
            body = ("Ne comptez pas sur elle%s : refaites une sauvegarde avec envoi cloud. Rien n'a été "
                    "supprimé ni modifié." % plural(len(bad)))
        elif unsure:
            heading = "Aucune copie abîmée"
            body = (f"Mais {len(unsure)} copie{plural(len(unsure))} n'{'ont' if len(unsure) > 1 else 'a'} pas pu être "
                    "confirmée{0} : seule la taille a pu être comparée (pas d'empreinte commune avec ce "
                    "service, ou pas de copie locale).").format(plural(len(unsure)))
        else:
            heading = "Toutes les copies cloud sont identiques"
            body = f"{len(good)} archive{plural(len(good))} de {remote}:{path} comparée{plural(len(good))} à vos copies locales."
        body += "\n\nComparaison faite chez le service : rien n'a été téléchargé."
        alert = Adw.AlertDialog(heading=heading, body=body)
        if checks:
            listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
            icons = {backend.CLOUD_BAD: "dialog-warning-symbolic", backend.CLOUD_WEAK: "dialog-question-symbolic",
                     backend.CLOUD_NOREF: "dialog-question-symbolic", backend.CLOUD_OK: "emblem-ok-symbolic"}
            order = {backend.CLOUD_BAD: 0, backend.CLOUD_NOREF: 1, backend.CLOUD_WEAK: 1, backend.CLOUD_OK: 2}
            for c in sorted(checks, key=lambda c: (order[c.status], c.name)):
                row = Adw.ActionRow(title=c.name, subtitle=c.message, use_markup=False, subtitle_lines=0)
                row.add_prefix(Gtk.Image(icon_name=icons[c.status]))
                listbox.append(row)
            alert.set_extra_child(Gtk.ScrolledWindow(
                child=listbox, hscrollbar_policy=Gtk.PolicyType.NEVER,
                propagate_natural_height=True, max_content_height=300))
        alert.add_response("ok", "Fermer")
        alert.present(self)

    def _test_password_ask(self, archive: Path) -> None:
        self._ask_password(
            archive, self._test_password_now, heading="Tester le mot de passe",
            body=f"Saisissez le mot de passe de « {archive.name} ». Rien n'est restauré ni écrit sur le disque.",
            ok_label="Tester")

    def _test_password_now(self, archive: Path, passphrase: str) -> None:
        """Teste le mot de passe d'une archive sans la restaurer, puis affiche le verdict."""
        if self.busy:
            return
        if not passphrase:
            self._toast("Saisissez un mot de passe pour le tester.")
            self._test_password_ask(archive)
            return
        self.busy = True
        self._sync_buttons()
        self.v_row.set_subtitle(f"Test du mot de passe de « {archive.name} »…")

        def worker() -> None:
            accepted, msg = backend.check_passphrase(archive, passphrase)
            GLib.idle_add(done, accepted, msg)

        def done(accepted: bool, msg: str) -> bool:
            self.busy = False
            self._sync_buttons()
            self.v_row.set_subtitle("Choisissez la sauvegarde à contrôler")
            if accepted:
                alert = Adw.AlertDialog(
                    heading="Mot de passe correct",
                    body=f"« {archive.name} » s'ouvre avec ce mot de passe.\n\nSeul le début de l'archive a "
                         "été déchiffré : rien n'a été restauré ni écrit sur le disque. Le reste du fichier n'est "
                         "pas lu : la « Répétition de restauration à blanc » le fait, de bout en bout.")
                alert.add_response("ok", "Fermer")
                alert.add_response("rehearsal", "Répétition de restauration…")
                alert.connect("response", lambda _d, r: self._rehearse_ask(archive) if r == "rehearsal" else None)
            else:
                alert = Adw.AlertDialog(
                    heading="Mot de passe refusé",
                    body=f"Ce mot de passe ne déchiffre pas « {archive.name} » ({msg}).\n\n"
                         "Vérifiez la saisie (majuscules, clavier). Si vous l'avez oublié, cette archive ne "
                         "pourra plus être ouverte : refaites une sauvegarde tant que vous le pouvez.")
                alert.add_response("ok", "Fermer")
                alert.add_response("retry", "Réessayer")
                alert.set_default_response("retry")
                alert.connect("response", lambda _d, r: self._test_password_ask(archive) if r == "retry" else None)
            alert.present(self)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=worker, daemon=True).start()

    # -- Restaurer un seul fichier ou dossier

    SINGLE_IDLE = ("Choisissez une sauvegarde (archive chiffrée ou dossier déchiffré), puis le fichier "
                   "ou le dossier à remettre.")

    def _single_choose_archive(self, _button: Gtk.Button) -> None:
        archives = Gtk.FileFilter(name="Archives chiffrées (*.gpg)")
        archives.add_pattern("*.gpg")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(archives)
        dialog = self._file_dialog(title="Choisir l'archive chiffrée qui contient le fichier",
                                   filters=filters, default_filter=archives)

        def done(dlg: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                file = dlg.open_finish(result)
            except GLib.Error:
                return
            self._single_start(Path(file.get_path()))

        dialog.open(self, None, done)

    def _single_choose_folder(self, _button: Gtk.Button) -> None:
        dialog = self._file_dialog(title="Choisir le dossier de sauvegarde qui contient le fichier")

        def done(dlg: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return
            path = Path(folder.get_path())
            archives = backend.archives_in_folder(path)
            if archives:
                self._offer_archives(path, archives, self._single_start)
                return
            self._single_start(path)

        dialog.select_folder(self, None, done)

    def _single_start(self, path: Path) -> None:
        """Ouvre une sauvegarde pour y retrouver un fichier. Une archive demande d'abord son mot de passe."""
        if self.busy:
            return
        if path.is_file():
            self._ask_password(
                path, self._single_list, heading="Retrouver un fichier",
                body=(f"Saisissez le mot de passe de « {path.name} ».\n\nL'archive sera lue une fois en entier "
                      "pour afficher son contenu : cela peut prendre quelques minutes. Rien n'est écrit sur "
                      "le disque."),
                ok_label="Ouvrir")
        else:
            self._single_list(path, None)

    def _single_stop_clicked(self, _button: Gtk.Button) -> None:
        if self.s_cancel is not None:
            self.s_cancel.set()
            self.s_stop.set_sensitive(False)
            self.s_stop.set_label("Arrêt…")

    def _single_list(self, path: Path, passphrase: str | None) -> None:
        """Lit le contenu de la sauvegarde (hors de la fenêtre), puis ouvre l'explorateur."""
        if self.busy:
            return
        if path.is_file() and not passphrase:
            self._toast("Saisissez un mot de passe pour ouvrir l'archive.")
            self._single_start(path)
            return
        cancel = threading.Event()
        self.s_cancel = cancel
        self.busy = True
        self._sync_buttons()
        self.s_stop.set_visible(True)
        self.s_stop.set_sensitive(True)
        self.s_stop.set_label("Arrêter")
        self.s_spinner.set_visible(True)
        self.s_spinner.start()
        self.s_row.set_subtitle(f"Lecture de « {path.name} »…")

        def progress(_fraction: float, text: str) -> None:
            if text:
                GLib.idle_add(self.s_row.set_subtitle, f"Lecture de « {path.name} » : {text}…")

        def worker() -> None:
            try:
                if path.is_file():
                    status = backend.check_archive(path)
                    verification.record_checks([(path, status, None)])
                    index = backend.list_archive_files(path, passphrase or "", cancel.is_set, progress)
                else:
                    index = backend.list_folder_files(path, cancel.is_set)
                GLib.idle_add(done, index, None)
            except Exception as exc:  # noqa: BLE001 - l'erreur est montrée à l'utilisateur
                GLib.idle_add(done, None, exc)

        def done(index: backend.BackupIndex | None, error: Exception | None) -> bool:
            self.s_cancel = None
            self.busy = False
            self._sync_buttons()
            self.s_stop.set_visible(False)
            self.s_spinner.stop()
            self.s_spinner.set_visible(False)
            self.s_row.set_subtitle(self.SINGLE_IDLE)
            if isinstance(error, backend.OperationCancelled):
                self._toast("Lecture interrompue.")
            elif isinstance(error, backend.IntegrityError):
                self._integrity_alert(path, str(error))
            elif error is not None:
                alert = Adw.AlertDialog(heading="Impossible de lire la sauvegarde",
                                        body=f"« {path.name} » : {error}.")
                alert.add_response("ok", "Fermer")
                if "mot de passe" in str(error):
                    alert.add_response("retry", "Réessayer")
                    alert.set_default_response("retry")
                    alert.connect("response", lambda _d, r: self._single_start(path) if r == "retry" else None)
                alert.present(self)
            elif index is not None and len(index) == 0:
                alert = Adw.AlertDialog(
                    heading="Aucun fichier personnel",
                    body=f"« {path.name} » ne contient pas de configurations personnelles ni de profils : elle "
                         "a sans doute été faite sans ces éléments. Il n'y a donc pas de fichier à y retrouver.")
                alert.add_response("ok", "Fermer")
                alert.present(self)
            elif index is not None:
                single_restore.SingleRestoreDialog(self, index, passphrase).present(self)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=worker, daemon=True).start()

    # -- Répétition de restauration à blanc

    REHEARSAL_IDLE = ("Facultatif. Déchiffre toute l'archive dans un dossier temporaire, l'extrait et contrôle "
                      "ses sommes de contrôle internes, puis supprime tout. Demande de la place disque et du temps.")

    def _rehearse_clicked(self, _button: Gtk.Button) -> None:
        if self.v_reh_cancel is not None:  # le bouton sert d'« Arrêter » pendant la répétition
            self.v_reh_cancel.set()
            self.v_reh_button.set_sensitive(False)
            self.v_reh_button.set_label("Arrêt…")
            return
        archives = Gtk.FileFilter(name="Archives chiffrées (*.gpg)")
        archives.add_pattern("*.gpg")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(archives)
        dialog = self._file_dialog(title="Répétition de restauration : choisir l'archive chiffrée",
                                   filters=filters, default_filter=archives)

        def done(dlg: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                file = dlg.open_finish(result)
            except GLib.Error:
                return
            self._rehearse_ask(Path(file.get_path()))

        dialog.open(self, None, done)

    def _rehearse_ask(self, archive: Path) -> None:
        """Explique ce que coûte la répétition (place, temps), puis demande le mot de passe."""
        if self.busy:
            return
        needed, free = backend.rehearsal_space(archive)
        space = f"Prévoyez environ {backend.fmt_size(needed)} de place temporaire"
        space += f" (il reste {backend.fmt_size(free)})." if free is not None else "."
        self._ask_password(
            archive, self._rehearse_now, heading="Répétition de restauration à blanc",
            body=(f"« {archive.name} » sera déchiffrée en entier dans un dossier temporaire privé, extraite et "
                  "contrôlée, puis tout sera supprimé. Rien n'est restauré sur votre ordinateur.\n\n"
                  f"{space} Cela peut durer plusieurs minutes selon la taille de l'archive.\n\n"
                  f"Saisissez son mot de passe."),
            ok_label="Lancer la répétition")

    def _rehearse_now(self, archive: Path, passphrase: str) -> None:
        if self.busy:
            return
        if not passphrase:
            self._toast("Saisissez un mot de passe pour lancer la répétition.")
            self._rehearse_ask(archive)
            return
        cancel = threading.Event()
        self.v_reh_cancel = cancel
        self.busy = True
        self._sync_buttons()
        self.v_all_other.set_sensitive(False)
        self.v_all_button.set_sensitive(False)
        self.v_row.set_sensitive(False)
        self.v_reh_button.set_label("Arrêter")
        self.v_reh_button.set_sensitive(True)
        self.v_reh_button.add_css_class("destructive-action")
        self.v_reh_spinner.set_visible(True)
        self.v_reh_spinner.start()
        self.v_reh_row.set_subtitle(f"Répétition sur « {archive.name} »…")

        def progress(_fraction: float, text: str) -> None:
            if text:
                GLib.idle_add(self.v_reh_row.set_subtitle, f"« {archive.name} » : {text}…")

        def worker() -> None:
            result = backend.rehearse_restore(archive, passphrase, progress, cancel.is_set)
            verification.record_rehearsal(archive, result.verdict)
            GLib.idle_add(done, result)

        def done(result: backend.RehearsalResult) -> bool:
            self.v_reh_cancel = None
            self.busy = False
            self._sync_buttons()
            self.v_all_other.set_sensitive(True)
            self.v_all_button.set_sensitive(True)
            self.v_row.set_sensitive(True)
            self.v_reh_button.set_label("Choisir une archive…")
            self.v_reh_button.set_sensitive(True)
            self.v_reh_button.remove_css_class("destructive-action")
            self.v_reh_spinner.stop()
            self.v_reh_spinner.set_visible(False)
            self.v_reh_row.set_subtitle(self.REHEARSAL_IDLE)
            self._refresh_verify_all()
            self._show_rehearsal_report(archive, result)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=worker, daemon=True).start()

    def _show_rehearsal_report(self, archive: Path, result: backend.RehearsalResult) -> None:
        name = f"« {archive.name} »"
        retry = False
        if result.verdict == backend.REH_OK:
            heading = "Sauvegarde récupérable"
            body = (f"{name} a été déchiffrée en entier, extraite, et {result.files} fichier"
                    f"{'s' if result.files > 1 else ''} ({backend.fmt_size(result.size)}) "
                    "correspondent à leurs sommes de contrôle internes. C'est la preuve que cette sauvegarde "
                    "peut être restaurée. Tout ce qui avait été extrait a été supprimé.")
        elif result.verdict == backend.REH_PARTIAL:
            heading = "Récupérable, contenu non comparé"
            body = f"{name} : {result.message}. Refaites une sauvegarde avec cette version pour en avoir une."
        elif result.verdict == backend.REH_PASSWORD:
            heading = "Mot de passe refusé"
            body = (f"Ce mot de passe ne déchiffre pas {name} ({result.message}). La répétition n'a pas "
                    "commencé : rien n'a été écrit sur le disque.")
            retry = True
        elif result.verdict == backend.REH_SPACE:
            heading = "Pas assez de place"
            body = (f"La répétition n'a pas pu aller au bout : {result.message}. Libérez de la place ou "
                    "changez de dossier de travail (XDG_CACHE_HOME), puis recommencez. Rien n'a été laissé "
                    "sur le disque et l'archive n'est pas mise en cause.")
        elif result.verdict == backend.REH_CANCELLED:
            heading = "Répétition interrompue"
            body = "Elle a été arrêtée avant la fin : aucune conclusion sur l'archive. Rien n'a été laissé sur le disque."
        else:
            heading = "Sauvegarde non récupérable"
            body = (f"{name} : {result.message}.\n\nNe comptez pas sur cette archive : refaites une "
                    "sauvegarde dès que possible.")
        if not result.cleaned:
            body += ("\n\nAttention : le dossier temporaire n'a pas pu être supprimé en entier. Il contient des "
                     "données en clair : effacez le contenu de ~/.cache/sauvegarde-bureau.")
        alert = Adw.AlertDialog(heading=heading, body=body)
        alert.add_response("ok", "Fermer")
        if retry:
            alert.add_response("retry", "Réessayer")
            alert.set_default_response("retry")
            alert.connect("response", lambda _d, r: self._rehearse_ask(archive) if r == "retry" else None)
        alert.present(self)

    def _integrity_alert(self, path: Path, message: str) -> None:
        dialog = Adw.AlertDialog(
            heading="Sauvegarde abîmée",
            body=f"« {path.name} » ne peut pas être utilisée : {message}.\n\n"
                 "Utilisez une autre sauvegarde, ou refaites-en une dès que possible.",
        )
        dialog.add_response("ok", "Compris")
        dialog.present(self)

    def _integrity_toast(self, status: str | None) -> None:
        if status == backend.OK:
            self.toasts.add_toast(Adw.Toast(title="Intégrité vérifiée : la sauvegarde est intacte."))
        elif status == backend.MISSING:
            self.toasts.add_toast(Adw.Toast(
                title="Intégrité non vérifiable : cette sauvegarde n'a pas de somme de contrôle (ancienne version ?)."))

    def _verify_then(self, path: Path, row: Adw.ActionRow, proceed) -> None:
        """Contrôle l'intégrité d'un dossier de sauvegarde (hors fenêtre), puis poursuit."""
        if self.busy:
            return
        self.busy = True
        self._sync_buttons()
        previous = row.get_subtitle()
        row.set_subtitle("Vérification de l'intégrité…")

        def worker() -> None:
            status, msg = backend.verify_backup(path)
            verification.record_checks([(path, status, None)])
            GLib.idle_add(done, status, msg)

        def done(status: str, msg: str) -> bool:
            self.busy = False
            row.set_subtitle(previous)
            if status == backend.BAD:
                self._sync_buttons()
                self._integrity_alert(path, msg)
            else:
                proceed()
                self._integrity_toast(status)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=worker, daemon=True).start()

    def _drop_restore_tmp(self) -> None:
        """Supprime la copie déchiffrée temporaire, s'il y en a une."""
        if self.restore_tmp:
            backend.remove_tree(self.restore_tmp)
            self.restore_tmp = None

    def _reset_restore(self) -> None:
        self.restore_src = None
        self.restore_ready = False
        self.restore_found = {}
        self._clear_preview()
        self.r_src_row.set_subtitle("Aucune sauvegarde sélectionnée")
        self.r_info_row.set_visible(False)
        for row in self.r_rows.values():
            row.set_sensitive(False)
            row.set_active(False)
            row.set_subtitle("Choisissez d'abord une sauvegarde")
        self._wizard_source_changed()
        self._sync_buttons()

    def _set_restore_src(self, src: Path, label: str | None = None) -> None:
        info = backend.detect(src)
        found = info["components"]
        self.restore_src = src
        self.restore_found = found
        self.r_src_row.set_subtitle(label or str(src))
        for name, row in self.r_rows.items():
            present = found[name]
            row.set_sensitive(present)
            row.set_active(present and name not in ("home", "profiles"))
            row.set_subtitle(self.r_subtitles[name] if present else "Absent de cette sauvegarde")
        manifest = info["manifest"]
        if manifest:
            try:
                date = datetime.fromisoformat(manifest["date"]).strftime("%d/%m/%Y à %H:%M")
            except (KeyError, ValueError):
                date = "date inconnue"
            self.r_info_row.set_subtitle(
                f"Créée le {date} sur « {manifest.get('machine', '?')} » "
                f"({manifest.get('systeme', 'système inconnu')})"
            )
            self.r_info_row.set_visible(True)
        else:
            self.r_info_row.set_visible(False)
        self.restore_ready = any(found.values())
        if self.restore_ready:
            self._start_preview(src)
        else:
            self._clear_preview()
            self.toasts.add_toast(Adw.Toast(title="Ce dossier ne ressemble pas à une sauvegarde."))
        self._wizard_source_changed(label)
        self._sync_buttons()

    def _confirm_restore(self, _button: Gtk.Button) -> None:
        selected = {name for name, row in self.r_rows.items()
                    if row.get_sensitive() and row.get_active()}
        # Un composant où l'on a tout décoché n'a rien à restaurer : on l'écarte
        only = {n: set(c) for n, c in self.r_picks.items() if c is not None and n in selected}
        emptied = {n for n, c in only.items() if not c}
        selected -= emptied
        for n in emptied:
            only.pop(n)
        if not selected or self.restore_src is None:
            self.toasts.add_toast(Adw.Toast(title="Sélectionnez au moins un élément."))
            return
        if emptied:
            names = ", ".join(backend.LABELS[n] for n in sorted(emptied))
            self.toasts.add_toast(Adw.Toast(title=f"Aucun élément choisi, ignoré : {names}"))
        if "profiles" in selected and self.restore_src is not None:
            keys = backend.profile_keys_in(self.restore_src, only.get("profiles"))
            running = backend.running_apps(keys)
            if running:
                self.toasts.add_toast(Adw.Toast(
                    title=f"Fermez d'abord {', '.join(running)} : son profil serait corrompu."))
                return
        body = ("Des paquets seront installés et les réglages du bureau remplacés. "
                "Cette opération peut durer plusieurs minutes.")
        overview = backend.restore_overview(self.previews, selected, only)
        if overview:
            body = f"Ce qui va changer sur cette machine :\n{overview}\n\n{body}"
        dialog = Adw.AlertDialog(heading="Lancer la restauration ?", body=body)
        dialog.add_response("cancel", "Annuler")
        dialog.add_response("go", "Restaurer")
        dialog.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        src = self.restore_src
        dialog.connect("response", lambda _d, r: r == "go" and self._run(
            self.r_task, lambda log, prog: backend.restore(src, selected, log, prog, only),
            "Restauration terminée", None, backend.count_steps(selected)))
        dialog.present(self)

    # ---------------------------------------------------------------- Nouvelle machine

    WIZ_ICONS = {"todo": "view-more-symbolic", "current": "media-playback-start-symbolic",
                 "ok": "emblem-ok-symbolic", "warn": "dialog-warning-symbolic",
                 "skip": "list-remove-symbolic"}
    WIZ_WORDS = {"todo": "À faire", "current": "En cours", "ok": "Terminée",
                 "warn": "Terminée avec des erreurs", "skip": "Passée"}

    def _build_wizard_page(self) -> Gtk.Widget:
        self.w_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT,
                                 transition_duration=200)
        self.w_stack.add_named(self._build_wizard_source(), "source")
        self.w_stack.add_named(self._build_wizard_step(), "step")
        self.w_stack.add_named(self._build_wizard_done(), "done")
        return self.w_stack

    @staticmethod
    def _wizard_header(title: str, text: str) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(Gtk.Label(label=title, xalign=0, wrap=True, css_classes=["title-2"]))
        box.append(Gtk.Label(label=text, xalign=0, wrap=True, css_classes=["dim-label"]))
        return box

    def _build_wizard_source(self) -> Gtk.Widget:
        header = self._wizard_header(
            "Configurer une nouvelle machine",
            "Cet assistant restaure votre sauvegarde pas à pas, dans le bon ordre : dépôts, "
            "applications, extensions, puis réglages. Vous validez chaque étape, et il vous "
            "propose de rouvrir votre session à la fin.")
        group = Adw.PreferencesGroup(
            title="Sauvegarde à restaurer",
            description="Choisissez l'archive chiffrée (.gpg), une archive du cloud, "
                        "ou un dossier « sauvegarde-ubuntu-… » déjà déchiffré (ou glissez-le sur la fenêtre). "
                        "Prévoyez une connexion Internet et "
                        "le secteur : l'installation des applications peut durer longtemps.")
        self.w_src_row = Adw.ActionRow(title="Sauvegarde", subtitle="Aucune sauvegarde sélectionnée",
                                       use_markup=False)
        self.w_src_row.add_suffix(self._open_menu(
            lambda b: self._choose_restore_src(b, self.w_src_row), self._choose_restore_archive))
        self.w_info_row = Adw.ActionRow(title="Informations", visible=False, use_markup=False)
        self.w_info_row.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
        group.add(self.w_src_row)
        group.add(self.w_info_row)
        self.w_start = Gtk.Button(label="Commencer", halign=Gtk.Align.CENTER,
                                  css_classes=["suggested-action", "pill"], sensitive=False)
        self.w_start.connect("clicked", self._wizard_start)
        return page(header, group, self.w_start)

    def _build_wizard_step(self) -> Gtk.Widget:
        self.w_kicker = Gtk.Label(xalign=0, css_classes=["dim-label"])
        self.w_title = Gtk.Label(xalign=0, wrap=True, css_classes=["title-2"])
        self.w_intro = Gtk.Label(xalign=0, wrap=True)
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for widget in (self.w_kicker, self.w_title, self.w_intro):
            header.append(widget)

        self.w_parts = Adw.PreferencesGroup(title="Contenu de l'étape")
        for name in backend.COMPONENTS:
            row = Adw.SwitchRow(title=backend.LABELS[name], use_markup=False)
            self.w_part_rows[name] = row
            self.w_parts.add(row)

        self.w_skip = Gtk.Button(label="Passer cette étape", css_classes=["pill"])
        self.w_skip.connect("clicked", self._wizard_skip)
        self.w_go = Gtk.Button(label="Lancer cette étape", css_classes=["suggested-action", "pill"])
        self.w_go.connect("clicked", self._wizard_go)
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        buttons.append(self.w_skip)
        buttons.append(self.w_go)

        self.w_task = TaskView()
        self.w_progress_group = Adw.PreferencesGroup(title="Progression")
        self.w_quit = Gtk.Button(label="Quitter l'assistant", halign=Gtk.Align.CENTER,
                                 css_classes=["flat"])
        self.w_quit.connect("clicked", self._wizard_quit)
        return page(header, self.w_parts, buttons, self.w_task, self.w_progress_group, self.w_quit)

    def _build_wizard_done(self) -> Gtk.Widget:
        self.w_done = Adw.StatusPage(icon_name="emblem-ok-symbolic", title="Terminé")
        self.w_logout = Gtk.Button(label="Fermer la session maintenant", halign=Gtk.Align.CENTER,
                                   css_classes=["suggested-action", "pill"])
        self.w_logout.connect("clicked", self._wizard_logout)
        later = Gtk.Button(label="Plus tard", halign=Gtk.Align.CENTER, css_classes=["pill"])
        later.connect("clicked", self._wizard_later)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(self.w_logout)
        box.append(later)
        self.w_done.set_child(box)
        return self.w_done

    # -- Source et navigation

    def _wizard_source_changed(self, label: str | None = None, reset: bool = True) -> None:
        """Répercute le choix (ou l'abandon) de la sauvegarde sur la page de l'assistant."""
        if self.restore_src is None:
            self.w_src_row.set_subtitle("Aucune sauvegarde sélectionnée")
            self.w_info_row.set_visible(False)
        else:
            self.w_src_row.set_subtitle(label or str(self.restore_src))
            self.w_info_row.set_subtitle(self.r_info_row.get_subtitle() or "")
            self.w_info_row.set_visible(self.r_info_row.get_visible())
        # Autre sauvegarde choisie en cours de route : on repart du début
        if reset and self.w_stack.get_visible_child_name() == "step":
            self._wizard_reset()

    def _wizard_reset(self) -> None:
        self.w_steps, self.w_status, self.w_index, self.w_phase = [], [], 0, "ready"
        self.w_stack.set_visible_child_name("source")

    def _wizard_start(self, _button: Gtk.Button) -> None:
        if self.busy or self.restore_src is None:
            return
        self.w_steps = backend.wizard_steps(self.restore_found)
        if not self.w_steps:
            self.toasts.add_toast(Adw.Toast(title="Cette sauvegarde ne contient rien à restaurer."))
            return
        self.w_status = ["todo"] * len(self.w_steps)
        self.w_index = 0
        for row, _image in self.w_progress:
            self.w_progress_group.remove(row)
        self.w_progress.clear()
        for step in self.w_steps:
            image = Gtk.Image(icon_name=self.WIZ_ICONS["todo"])
            row = Adw.ActionRow(title=step.title, subtitle=self.WIZ_WORDS["todo"], use_markup=False)
            row.add_prefix(image)
            self.w_progress_group.add(row)
            self.w_progress.append((row, image))
        self._wizard_show_step()

    def _wizard_show_step(self) -> None:
        step = self.w_steps[self.w_index]
        self.w_kicker.set_label(f"Étape {self.w_index + 1} sur {len(self.w_steps)}")
        self.w_title.set_label(step.title)
        self.w_intro.set_label(step.intro)
        for name, row in self.w_part_rows.items():
            row.set_visible(name in step.parts)
            if name in step.parts:
                present = bool(self.restore_found.get(name))
                row.set_sensitive(present)
                row.set_active(present and name not in backend.OPT_IN)
        self.w_task.set_visible(False)
        self.w_phase = "ready"
        self._wizard_refresh_subtitles()
        self._wizard_mark(self.w_index, "current")
        self.w_stack.set_visible_child_name("step")
        self._wizard_sync()

    def _wizard_refresh_subtitles(self) -> None:
        """Résumé de chaque élément de l'étape (aperçu de la sauvegarde face à cette machine)."""
        for name, row in self.w_part_rows.items():
            if not row.get_visible():
                continue
            if not self.restore_found.get(name):
                row.set_subtitle("Absent de cette sauvegarde")
            elif name in self.previews:
                row.set_subtitle(backend.summarize(name, self.previews[name]))
            else:
                row.set_subtitle(self.r_subtitles[name])

    def _wizard_mark(self, index: int, state: str) -> None:
        """Met à jour l'icône et le texte d'une étape dans la liste de progression."""
        row, image = self.w_progress[index]
        image.set_from_icon_name(self.WIZ_ICONS[state])
        row.set_subtitle(self.WIZ_WORDS[state])

    def _wizard_sync(self) -> None:
        """Libellés et disponibilité des boutons de l'assistant selon l'état de l'étape."""
        last = self.w_index == len(self.w_steps) - 1
        labels = {
            "ready": ("Lancer cette étape", "Passer cette étape"),
            "ok": ("Terminer" if last else "Étape suivante", ""),
            "warn": ("Réessayer", "Continuer quand même"),
        }
        go, skip = labels[self.w_phase]
        self.w_go.set_label(go)
        self.w_skip.set_label(skip or "Passer cette étape")
        self.w_skip.set_visible(bool(skip))
        self.w_go.set_sensitive(not self.busy)
        self.w_skip.set_sensitive(not self.busy)
        self.w_quit.set_sensitive(not self.busy)

    # -- Exécution d'une étape

    def _wizard_go(self, _button: Gtk.Button) -> None:
        if self.busy or self.restore_src is None:
            return
        if self.w_phase == "ok":
            self._wizard_advance()
            return
        step = self.w_steps[self.w_index]
        selected = {n for n in step.parts
                    if self.w_part_rows[n].get_sensitive() and self.w_part_rows[n].get_active()}
        if not selected:
            self.toasts.add_toast(Adw.Toast(title="Activez au moins un élément, ou passez l'étape."))
            return
        if "profiles" in selected:
            running = backend.running_apps(backend.profile_keys_in(self.restore_src, None))
            if running:
                self.toasts.add_toast(Adw.Toast(
                    title=f"Fermez d'abord {', '.join(running)} : son profil serait corrompu."))
                return
        src, index, total = self.restore_src, self.w_index, backend.count_steps(selected)
        self._wizard_mark(index, "current")
        self._run(
            self.w_task,
            lambda log, prog: backend.restore(src, selected, log, prog, None, notice=False),
            f"Restauration ({step.title.lower()}) terminée", None, total,
            on_done=lambda errors, failure: self._wizard_step_done(index, errors, failure),
        )

    def _wizard_step_done(self, index: int, errors: list[str], failure: str | None) -> None:
        bad = bool(errors or failure)
        self.w_status[index] = "warn" if bad else "ok"
        self.w_phase = "warn" if bad else "ok"
        self._wizard_mark(index, self.w_status[index])
        self._wizard_sync()

    def _wizard_skip(self, _button: Gtk.Button) -> None:
        if self.busy:
            return
        if self.w_phase == "ready":  # étape passée sans être lancée
            self.w_status[self.w_index] = "skip"
            self._wizard_mark(self.w_index, "skip")
        self._wizard_advance()

    def _wizard_advance(self) -> None:
        self.w_index += 1
        if self.w_index < len(self.w_steps):
            self._wizard_show_step()
        else:
            self._wizard_finish()

    def _wizard_quit(self, _button: Gtk.Button) -> None:
        if self.busy:
            return
        applied = any(st in ("ok", "warn") for st in self.w_status)
        self._wizard_reset()
        if applied:
            self.toasts.add_toast(Adw.Toast(
                title="Assistant interrompu. Rouvrez votre session pour appliquer ce qui a été restauré."))

    # -- Fin : bilan et redémarrage de la session

    def _wizard_finish(self) -> None:
        names = {"ok": "terminée", "warn": "terminée avec des erreurs", "skip": "passée"}
        lines = [f"• {step.title} : {names[state]}"
                 for step, state in zip(self.w_steps, self.w_status)]
        problems = "warn" in self.w_status
        self.w_done.set_icon_name("dialog-warning-symbolic" if problems else "emblem-ok-symbolic")
        self.w_done.set_title("Presque terminé" if problems else "Votre nouvelle machine est prête")
        text = "\n".join(lines)
        if problems:
            text += "\n\nCertaines étapes ont signalé des erreurs : vous pouvez les relancer " \
                    "depuis la page « Restaurer »."
        text += "\n\nExtensions, réglages et nouvelles applications ne sont pleinement pris en " \
                "compte qu'à la prochaine ouverture de session. Enregistrez vos documents " \
                "ouverts avant de fermer la session."
        self.w_done.set_description(text)
        self.w_stack.set_visible_child_name("done")
        # Plus de copie déchiffrée en clair une fois la restauration finie
        if self.restore_tmp:
            self._drop_restore_tmp()
            self._reset_restore()
        elif self.restore_src is not None:
            self._start_preview(self.restore_src)

    def _wizard_logout(self, _button: Gtk.Button) -> None:
        self.w_logout.set_sensitive(False)

        def worker() -> None:
            ok = backend.restart_session(lambda _line: None)
            GLib.idle_add(self._wizard_logout_done, ok)

        threading.Thread(target=worker, daemon=True).start()

    def _wizard_logout_done(self, ok: bool) -> bool:
        self.w_logout.set_sensitive(True)
        if not ok:
            self.toasts.add_toast(Adw.Toast(
                title="Impossible de fermer la session automatiquement : "
                      "utilisez le menu système (en haut à droite)."))
        return GLib.SOURCE_REMOVE

    def _wizard_later(self, _button: Gtk.Button) -> None:
        self._wizard_reset()
        self.toasts.add_toast(Adw.Toast(
            title="Pensez à fermer votre session pour appliquer extensions et réglages."))

    # ---------------------------------------------------------------- Comparaison

    SIDE_TITLES = {"old": "Ancienne sauvegarde", "new": "Sauvegarde récente"}

    def _build_compare_page(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup(
            title="Sauvegardes à comparer",
            description="On voit ce qui a été ajouté ou retiré dans la sauvegarde récente par "
                        "rapport à l'ancienne. Aucune des deux n'est modifiée. "
                        "Glissez une archive ou un dossier sur la fenêtre pour remplir le premier emplacement libre.",
        )
        swap = Gtk.Button(icon_name="media-playlist-shuffle-symbolic", valign=Gtk.Align.CENTER,
                          css_classes=["flat"], tooltip_text="Inverser ancienne et récente")
        swap.connect("clicked", self._swap_compare)
        group.set_header_suffix(swap)
        for side in ("old", "new"):
            row = Adw.ActionRow(title=self.SIDE_TITLES[side], subtitle="Aucune sauvegarde sélectionnée",
                                use_markup=False)
            row.add_suffix(self._open_menu(lambda _b, sd=side: self._choose_compare_dir(sd),
                                           lambda _b, sd=side: self._choose_compare_archive(sd),
                                           cloud=False))
            self.c_rows[side] = row
            group.add(row)

        self.c_group = Adw.PreferencesGroup(
            title="Changements", visible=False,
            description="Dépôts tiers, applications (apt, snap, flatpak) et extensions GNOME.")
        self.c_spinner = Gtk.Spinner(valign=Gtk.Align.CENTER)
        self.c_group.set_header_suffix(self.c_spinner)
        return page(group, self.c_group)

    @staticmethod
    def _backup_caption(src: Path, label: str | None = None) -> str:
        manifest = backend.detect(src)["manifest"]
        try:
            date = datetime.fromisoformat(manifest["date"]).strftime("%d/%m/%Y à %H:%M")
            return f"{label or src} · créée le {date}"
        except (KeyError, ValueError, TypeError):
            return str(label or src)

    def _choose_compare_dir(self, side: str) -> None:
        dialog = self._file_dialog(title=f"{self.SIDE_TITLES[side]} : choisir le dossier")

        def done(dlg: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return
            path = Path(folder.get_path())
            archives = backend.archives_in_folder(path)
            if archives:
                self._offer_archives(path, archives, lambda archive: self._ask_password(
                    archive, lambda arc, pw: self._open_compare_archive(side, arc, pw)))
                return
            self._verify_then(path, self.c_rows[side],
                              lambda: self._set_compare_src(side, path, None))

        dialog.select_folder(self, None, done)

    def _choose_compare_archive(self, side: str) -> None:
        archives = Gtk.FileFilter(name="Archives chiffrées (*.gpg)")
        archives.add_pattern("*.gpg")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(archives)
        dialog = self._file_dialog(title=f"{self.SIDE_TITLES[side]} : choisir l'archive chiffrée",
                                filters=filters, default_filter=archives)

        def done(dlg: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                file = dlg.open_finish(result)
            except GLib.Error:
                return
            self._ask_password(Path(file.get_path()),
                               lambda archive, pw: self._open_compare_archive(side, archive, pw))

        dialog.open(self, None, done)

    def _open_compare_archive(self, side: str, archive: Path, passphrase: str) -> None:
        if self.busy:
            return
        self.busy = True
        self._sync_buttons()
        self.c_rows[side].set_subtitle("Déchiffrement en cours…")

        def worker() -> None:
            try:
                status = backend.check_archive(archive)
                tmp, folder = backend.decrypt_archive(archive, passphrase)
                GLib.idle_add(self._compare_archive_opened, side, archive, tmp, folder, None, status)
            except backend.IntegrityError as exc:
                GLib.idle_add(self._compare_archive_opened, side, archive, None, None, str(exc), backend.BAD)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._compare_archive_opened, side, archive, None, None, str(exc), None)

        threading.Thread(target=worker, daemon=True).start()

    def _compare_archive_opened(self, side: str, archive: Path, tmp: Path | None,
                                folder: Path | None, error: str | None,
                                status: str | None = None) -> bool:
        self.busy = False
        self._sync_buttons()
        if error:
            self._refresh_compare_row(side)
            if status == backend.BAD:
                self._integrity_alert(archive, error)
            else:
                self.toasts.add_toast(Adw.Toast(title=f"Impossible d'ouvrir l'archive : {error}"))
        else:
            self._set_compare_src(side, folder, f"{archive} (déchiffrée)", tmp)
            self._integrity_toast(status)
        return GLib.SOURCE_REMOVE

    def _drop_compare_tmp(self, side: str) -> None:
        tmp = self.c_tmp.get(side)
        if tmp:
            backend.remove_tree(tmp)
            self.c_tmp[side] = None

    def _refresh_compare_row(self, side: str) -> None:
        src = self.c_src[side]
        self.c_rows[side].set_subtitle(
            self._backup_caption(src, self.c_label.get(side)) if src else "Aucune sauvegarde sélectionnée")

    def _set_compare_src(self, side: str, src: Path, label: str | None, tmp: Path | None = None) -> None:
        if not any(backend.detect(src)["components"].get(n) for n in backend.COMPARABLE):
            if tmp:
                backend.remove_tree(tmp)
            self.toasts.add_toast(Adw.Toast(title="Ce dossier ne contient ni dépôts, ni applications, ni extensions."))
            self._refresh_compare_row(side)
            return
        self._drop_compare_tmp(side)
        self.c_tmp[side] = tmp
        self.c_src[side] = src
        self.c_label[side] = label
        self._refresh_compare_row(side)
        self._start_compare()

    def _swap_compare(self, _button: Gtk.Button) -> None:
        for d in (self.c_src, self.c_tmp, self.c_label):
            d["old"], d["new"] = d.get("new"), d.get("old")
        for side in ("old", "new"):
            self._refresh_compare_row(side)
        self._start_compare()

    def _clear_compare(self) -> None:
        self.compare_gen += 1
        for row in self.c_result_rows:
            self.c_group.remove(row)
        self.c_result_rows.clear()
        self.c_spinner.stop()
        self.c_group.set_visible(False)

    def _start_compare(self) -> None:
        self._clear_compare()
        old, new = self.c_src["old"], self.c_src["new"]
        if old is None or new is None:
            return
        gen = self.compare_gen
        placeholder = info_row("Comparaison en cours…")
        self.c_group.add(placeholder)
        self.c_result_rows.append(placeholder)
        self.c_group.set_visible(True)
        self.c_spinner.start()

        def worker() -> None:
            GLib.idle_add(self._compare_done, gen, backend.compare_backups(old, new))

        threading.Thread(target=worker, daemon=True).start()

    def _compare_done(self, gen: int, result: dict[str, backend.Diff]) -> bool:
        if gen != self.compare_gen:
            return GLib.SOURCE_REMOVE  # d'autres sauvegardes ont été choisies entre-temps
        self.c_spinner.stop()
        for row in self.c_result_rows:
            self.c_group.remove(row)
        self.c_result_rows.clear()
        if not result:
            row = info_row("Rien à comparer", "Ces sauvegardes ne contiennent ni dépôts, ni applications, ni extensions.")
            self.c_group.add(row)
            self.c_result_rows.append(row)
        for name in backend.COMPARABLE:
            diff = result.get(name)
            if diff is None:
                continue
            expander = Adw.ExpanderRow(title=backend.LABELS[name], use_markup=False,
                                       subtitle=backend.diff_summary(diff))
            rows: list[tuple[str, str, str, str]] = (
                [("Ajouté", "success", n, diff.details.get(n, "")) for n in diff.added]
                + [("Retiré", "error", n, diff.details.get(n, "")) for n in diff.removed]
                + [("Modifié", "warning", n, d) for n, d in diff.changed])
            for label, css, title, detail in rows[:COMPARE_ROWS]:
                row = Adw.ActionRow(title=title, subtitle=detail, use_markup=False)
                row.add_suffix(Gtk.Label(label=label, valign=Gtk.Align.CENTER, css_classes=[css]))
                expander.add_row(row)
            if len(rows) > COMPARE_ROWS:
                expander.add_row(info_row(f"… et {len(rows) - COMPARE_ROWS} autres changements non affichés"))
            if diff.error:
                expander.add_row(info_row(diff.error))
            elif diff.empty:
                expander.add_row(info_row("Aucun changement"))
            self.c_group.add(expander)
            self.c_result_rows.append(expander)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------ Automatique

    def _build_auto_page(self) -> Gtk.Widget:
        cfg = auto.load_config()
        self.auto_dest = Path(cfg["destination"])

        status_group = Adw.PreferencesGroup(title="État")
        self.a_next_row = Adw.ActionRow(title="Prochaine sauvegarde", subtitle="—", use_markup=False)
        self.a_last_row = Adw.ActionRow(title="Dernière sauvegarde automatique",
                                        subtitle="Aucune pour le moment", use_markup=False)
        status_group.add(self.a_next_row)
        status_group.add(self.a_last_row)

        plan_group = Adw.PreferencesGroup(
            title="Planification",
            description="La sauvegarde s'exécute en arrière-plan quand votre session est ouverte. "
                        "Si l'ordinateur était éteint à l'heure prévue, elle est rattrapée "
                        "à l'ouverture de la session suivante.",
        )
        self.a_enable = Adw.SwitchRow(title="Activer les sauvegardes automatiques",
                                      active=cfg["enabled"])
        self.a_freq = Adw.ComboRow(title="Fréquence",
                                   subtitle="Chaque semaine : le lundi. Chaque mois : le 1er.",
                                   use_markup=False,
                                   model=Gtk.StringList.new(list(auto.FREQUENCY_LABELS)),
                                   selected=auto.FREQUENCIES.index(cfg["frequency"]))
        time_row = Adw.ActionRow(title="Heure", use_markup=False)
        self.a_hour = Gtk.SpinButton.new_with_range(0, 23, 1)
        self.a_minute = Gtk.SpinButton.new_with_range(0, 59, 1)
        for spin, value in ((self.a_hour, cfg["hour"]), (self.a_minute, cfg["minute"])):
            spin.set_value(value)
            spin.set_wrap(True)
            spin.set_valign(Gtk.Align.CENTER)
            spin.set_width_chars(2)
            spin.connect("output", lambda sp: (sp.set_text(f"{int(sp.get_value()):02d}"), True)[1])
        time_row.add_suffix(self.a_hour)
        time_row.add_suffix(Gtk.Label(label=":"))
        time_row.add_suffix(self.a_minute)
        self.a_dest_row = Adw.ActionRow(title="Destination", subtitle=str(self.auto_dest),
                                        use_markup=False)
        self.a_dest_row.add_suffix(choose_button(self._choose_auto_dest))
        self.a_keep = Adw.SpinRow(
            title="Sauvegardes à conserver",
            subtitle="Les plus anciennes sauvegardes automatiques sont supprimées. "
                     "Vos sauvegardes manuelles ne sont jamais touchées.",
            adjustment=Gtk.Adjustment(value=cfg["keep"], lower=1, upper=50,
                                      step_increment=1, page_increment=5),
            use_markup=False,
        )
        self.a_verify = Adw.SwitchRow(
            title="Vérifier après chaque sauvegarde", active=cfg["verify"], use_markup=False,
            subtitle="Recontrôle les sauvegardes automatiques conservées. Avec le chiffrement, le mot de "
                     "passe du trousseau est aussi testé, sans rien demander. Les copies cloud sont "
                     "comparées par empreinte, sans téléchargement. Une notification vous prévient "
                     "en cas de problème.")
        for row in (self.a_enable, self.a_freq, time_row, self.a_dest_row, self.a_keep, self.a_verify):
            plan_group.add(row)

        alert_group = Adw.PreferencesGroup(
            title="Alerte",
            description="Une bannière s'affiche en haut de la fenêtre quand votre dernière sauvegarde "
                        "(manuelle ou automatique) est plus vieille que ce délai. Elle fonctionne aussi "
                        "sans sauvegardes automatiques.",
        )
        self.a_alert = Adw.SpinRow(
            title="Alerter après (jours)",
            subtitle="Une sauvegarde par semaine laisse quelques jours de marge avec 10 jours.",
            adjustment=Gtk.Adjustment(value=cfg["alert_days"], lower=1, upper=365,
                                      step_increment=1, page_increment=7),
            use_markup=False,
        )
        alert_group.add(self.a_alert)

        what_group = Adw.PreferencesGroup(title="Que sauvegarder ?")
        chosen = set(cfg["components"])
        self.a_rows = {
            "apt": switch_row("Applications", "Paquets installés avec apt", active="apt" in chosen),
            "snap": switch_row("Snaps", "Applications installées avec snap",
                               active="snap" in chosen, available=backend.has("snap")),
            "flatpak": switch_row("Flatpaks", "Applications installées avec Flatpak",
                                  active="flatpak" in chosen, available=backend.has("flatpak")),
            "repos": switch_row("Dépôts tiers", "PPA et clés de signature", active="repos" in chosen),
            "extensions": switch_row("Extensions GNOME", "Liste des extensions et état d'activation",
                                     active="extensions" in chosen),
            "dconf": switch_row("Réglages du bureau", "Dock, thème, raccourcis, fond d'écran",
                                active="dconf" in chosen, available=backend.has("dconf"),
                                unavailable_text="Le paquet dconf-cli est requis"),
            "home": switch_row("Configurations personnelles",
                               "Exige le chiffrement (contient clés SSH et données d'applications)",
                               active="home" in chosen, available=backend.has("rsync"),
                               unavailable_text="Le paquet rsync est requis"),
        }
        for row in self.a_rows.values():
            what_group.add(row)

        prot_group = Adw.PreferencesGroup(
            title="Protection",
            description="Le mot de passe est conservé dans le trousseau GNOME pour pouvoir chiffrer "
                        "sans vous le demander. Si vous le changez, les anciennes sauvegardes "
                        "restent protégées par l'ancien : conservez-le. Si le mot de passe est "
                        "introuvable, la sauvegarde est annulée, jamais faite en clair.",
        )
        can_encrypt = backend.has("gpg") and backend.has("secret-tool")
        self.a_encrypt = switch_row(
            "Chiffrer les sauvegardes", "Archive unique protégée par mot de passe (AES-256)",
            active=cfg["encrypt"], available=can_encrypt,
            unavailable_text="Les paquets gnupg et libsecret-tools sont requis",
        )
        self.a_pw = Adw.PasswordEntryRow(title="Mot de passe (8 caractères minimum)")
        self.a_pw2 = Adw.PasswordEntryRow(title="Confirmer le mot de passe")
        prot_group.add(self.a_encrypt)
        for row in (self.a_pw, self.a_pw2):
            self.a_encrypt.bind_property("active", row, "visible", GObject.BindingFlags.SYNC_CREATE)
            prot_group.add(row)
        self.a_rows["home"].connect(
            "notify::active",
            lambda row, _p: row.get_active() and self.a_encrypt.get_sensitive()
            and self.a_encrypt.set_active(True),
        )

        self.a_cloud = CloudGroup(
            can_encrypt=can_encrypt,
            unavailable_encrypt="Les paquets gnupg et libsecret-tools sont requis",
            enabled=cfg["cloud"], remote=cfg["cloud_remote"], path=cfg["cloud_path"],
            on_enable=lambda: self.a_encrypt.set_active(True))

        self.a_save = Gtk.Button(label="Enregistrer", css_classes=["suggested-action", "pill"])
        self.a_save.connect("clicked", self._save_auto)
        self.a_now = Gtk.Button(label="Lancer maintenant", css_classes=["pill"])
        self.a_now.connect("clicked", self._run_auto_now)
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        buttons.append(self.a_now)
        buttons.append(self.a_save)
        self._refresh_auto_status()
        return page(status_group, plan_group, alert_group, what_group, prot_group, self.a_cloud.group, buttons)

    def _choose_auto_dest(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog(title="Choisir la destination des sauvegardes automatiques",
                                initial_folder=Gio.File.new_for_path(str(self.auto_dest)))

        def done(dlg: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return
            self.auto_dest = Path(folder.get_path())
            self.a_dest_row.set_subtitle(str(self.auto_dest))

        dialog.select_folder(self, None, done)

    def _refresh_auto_status(self) -> None:
        status = auto.timer_status()
        if status["active"]:
            self.a_next_row.set_subtitle(status["next"] or "Planifiée")
        else:
            self.a_next_row.set_subtitle("Désactivées")
        last = auto.last_run()
        if last:
            try:
                when = datetime.fromisoformat(last["date"]).strftime("%d/%m/%Y à %H:%M")
            except (KeyError, ValueError):
                when = "date inconnue"
            mark = "Réussie" if last.get("ok") else "Échec"
            self.a_last_row.set_subtitle(f"{mark} le {when} : {last.get('message', '')}")
        self.a_now.set_sensitive(status["installed"])

    def _save_auto(self, _button: Gtk.Button) -> None:
        toast = lambda text: self.toasts.add_toast(Adw.Toast(title=text))  # noqa: E731
        selected = [n for n, r in self.a_rows.items() if r.get_active()]
        cfg = {
            "enabled": self.a_enable.get_active(),
            "frequency": auto.FREQUENCIES[self.a_freq.get_selected()],
            "hour": self.a_hour.get_value_as_int(),
            "minute": self.a_minute.get_value_as_int(),
            "destination": str(self.auto_dest),
            "components": selected,
            "encrypt": self.a_encrypt.get_active(),
            "keep": int(self.a_keep.get_value()),
            "cloud": self.a_cloud.active,
            "cloud_remote": self.a_cloud.selected(),
            "cloud_path": self.a_cloud.path_row.get_text().strip(),
            "verify": self.a_verify.get_active(),
            "alert_days": int(self.a_alert.get_value()),
        }
        if cfg["enabled"] and cfg["cloud"]:
            if not cfg["encrypt"]:
                return toast("L'envoi vers le cloud exige le chiffrement.")
            try:
                self.a_cloud.target()
            except ValueError as exc:
                return toast(f"Envoi cloud : {exc}.")
        if cfg["enabled"]:
            if not selected:
                return toast("Sélectionnez au moins un élément à sauvegarder.")
            if not self.auto_dest.is_dir():
                return toast("Le dossier de destination n'existe pas.")
            if "home" in selected and not cfg["encrypt"]:
                return toast("Les configurations personnelles exigent le chiffrement.")
        if cfg["encrypt"]:
            passphrase = self.a_pw.get_text()
            if passphrase or not auto.password_exists():
                if len(passphrase) < 8:
                    self.a_pw.grab_focus()
                    return toast("Saisissez un mot de passe d'au moins 8 caractères.")
                if passphrase != self.a_pw2.get_text():
                    self.a_pw2.grab_focus()
                    return toast("Les deux mots de passe sont différents.")
                if not auto.password_store(passphrase):
                    return toast("Impossible d'enregistrer le mot de passe dans le trousseau.")
            self.a_pw.set_text("")
            self.a_pw2.set_text("")
        else:
            auto.password_clear()

        auto.save_config(cfg)
        if cfg["enabled"]:
            ok, message = auto.install_timer(cfg)
            if not ok:
                return toast(f"Planification impossible : {message}")
            toast("Sauvegardes automatiques activées.")
        else:
            auto.remove_timer()
            toast("Sauvegardes automatiques désactivées.")
        self._refresh_auto_status()
        self._refresh_verify_all()  # le délai d'alerte a pu changer

    def _run_auto_now(self, _button: Gtk.Button) -> None:
        ok, message = auto.run_now()
        text = ("Sauvegarde lancée en arrière-plan : vous serez notifié à la fin."
                if ok else f"Impossible de lancer : {message}")
        self.toasts.add_toast(Adw.Toast(title=text))

    # ------------------------------------------------------------------- Commun

    def _sync_buttons(self) -> None:
        self.b_button.set_sensitive(not self.busy)
        self.r_button.set_sensitive(not self.busy and self.restore_ready)
        self.w_start.set_sensitive(not self.busy and self.restore_ready)
        for widget in self.m_actions:
            widget.set_sensitive(not self.busy)
        if hasattr(self, "s_menu"):
            self.s_menu.set_sensitive(not self.busy)
        if hasattr(self, "v_reh_button") and self.v_reh_cancel is None:
            self.v_reh_button.set_sensitive(not self.busy)
        if hasattr(self, "v_cloud_button"):
            self.v_cloud_button.set_sensitive(not self.busy and backend.has("rclone")
                                              and self._cloud_target() is not None)
        self._wizard_sync()

    def _run(self, task: TaskView, job, done_text: str, open_path: Path | None,
             total: int = 0, on_done=None) -> None:
        if self.busy:
            return
        self.busy = True
        self._sync_buttons()
        task.reset()
        task.expander.set_expanded(True)

        def log(line: str) -> None:
            GLib.idle_add(task.add_line, line)

        def progress(fraction: float, text: str) -> None:
            GLib.idle_add(task.set_progress, fraction, text)

        def worker() -> None:
            errors, failure = [], None
            try:
                errors = job(log, progress)
            except Exception as exc:  # noqa: BLE001
                failure = str(exc)
            GLib.idle_add(self._finished, task, errors, failure, done_text, open_path, total, on_done)

        threading.Thread(target=worker, daemon=True).start()

    def _finished(self, task: TaskView, errors: list[str], failure: str | None,
                  done_text: str, open_path: Path | None, total: int = 0, on_done=None) -> bool:
        self.busy = False
        self._sync_buttons()
        if task is self.r_task and self.restore_tmp:
            # Ne pas laisser de copie en clair : il faudra rouvrir l'archive pour recommencer
            self._drop_restore_tmp()
            self._reset_restore()
        elif task is self.r_task and self.restore_src is not None:
            self._start_preview(self.restore_src)  # montre ce qui reste à faire après coup
        bilan = backend.report(total, errors) if total else ""
        if failure:
            task.set_progress(task.bar.get_fraction(), "Échec")
            toast = Adw.Toast(title=f"Erreur : {failure}")
            self._notify(f"{done_text.split()[0]} : échec", failure, True)
        elif errors:
            task.set_progress(1.0, f"Terminé avec des erreurs — {bilan}")
            toast = Adw.Toast(title=f"Terminé avec des erreurs : {bilan}")
            self._notify(f"{done_text} avec des erreurs", bilan, True)
        else:
            task.set_progress(1.0, f"{done_text} — {bilan}" if bilan else done_text)
            toast = Adw.Toast(title=f"{done_text} : {bilan}" if bilan else done_text)
            self._notify(done_text, bilan or "Tout s'est bien passé.", False)
            if open_path:
                toast.set_button_label("Ouvrir le dossier")
                toast.set_action_name("win.open-folder")
                toast.set_action_target_value(GLib.Variant.new_string(str(open_path)))
        toast.set_timeout(0 if open_path and not (failure or errors) else 5)
        self.toasts.add_toast(toast)
        if on_done:
            on_done(errors, failure)
        return GLib.SOURCE_REMOVE

    def _notify(self, title: str, body: str, failure: bool) -> None:
        """Notification du bureau : utile quand la fenêtre est passée au second plan."""
        notification = Gio.Notification.new(title)
        notification.set_body(body)
        notification.set_icon(Gio.ThemedIcon.new(ICON))
        if failure:
            notification.set_priority(Gio.NotificationPriority.URGENT)
        self.get_application().send_notification(None, notification)

    def _open_folder(self, _action: Gio.SimpleAction, param: GLib.Variant) -> None:
        uri = Gio.File.new_for_path(param.get_string()).get_uri()
        Gio.AppInfo.launch_default_for_uri(uri, None)

    def _on_close(self, _window: Gtk.Window) -> bool:
        if not self.busy:
            self._drop_restore_tmp()
            for side in self.c_tmp:
                self._drop_compare_tmp(side)
            return False
        dialog = Adw.AlertDialog(
            heading="Opération en cours",
            body="Attendez la fin de l'opération avant de fermer l'application.",
        )
        dialog.add_response("ok", "D'accord")
        dialog.present(self)
        return True


class App(Adw.Application):
    def __init__(self) -> None:
        # HANDLES_OPEN : « sauvegarde-bureau archive.tar.gz.gpg » et le double-clic dans le
        # gestionnaire de fichiers ouvrent l'archive, dans la fenêtre déjà ouverte s'il y en a une
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self._show_about)
        self.add_action(about)

    def do_activate(self) -> None:
        Gtk.Window.set_default_icon_name(ICON)
        window = self.props.active_window or Window(self)
        window.present()

    def do_open(self, files: list[Gio.File], _n_files: int, _hint: str) -> None:
        window = self.props.active_window or Window(self)
        window.present()
        paths = [Path(f.get_path()) if f.get_path() else None for f in files]
        GLib.idle_add(window.open_from_outside, paths)

    def _show_about(self, *_args) -> None:
        mois = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
                "septembre", "octobre", "novembre", "décembre"]
        annee, m, jour = (int(x) for x in __date__.split("-"))
        date_fr = f"{jour}{'er' if jour == 1 else ''} {mois[m - 1]} {annee}"
        Adw.AboutDialog(
            application_name="Sauvegarde du bureau",
            application_icon=ICON,
            developer_name=__author__,
            developers=[__author__],
            copyright=f"© {annee} {__author__}",
            version=__version__,
            comments="Sauvegarde et restaure vos applications, extensions GNOME "
                     "et réglages du bureau.\n\n"
                     f"Dernière mise à jour : {date_fr}",
            license_type=Gtk.License.MIT_X11,
            website="https://www.iactualite.info/",
        ).present(self.props.active_window)


def main() -> int:
    return App().run(sys.argv)
