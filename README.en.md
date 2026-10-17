# Sauvegarde du bureau (Desktop Backup)

[🇫🇷 Français](README.md) · 🇬🇧 English

A GNOME application (GTK4 / libadwaita) to **back up and restore your Ubuntu desktop**: applications, third-party repositories, GNOME extensions and settings.

> **Note:** the interface is available in English and French, following your system language. The code's source language is French.

![Backup page](docs/screenshots/01-sauvegarder.png)

## What it backs up

- Applications installed with **apt**, **snap** and **flatpak**
- Third-party repositories (PPAs) and their signing keys
- GNOME extensions and whether they are enabled
- Desktop settings (dconf): dock, theme, keyboard shortcuts, wallpaper…
- Browser and mail profiles, personal configuration files (optional)

## Features

- Backups protected by a **SHA-256** checksum, verified when created and before any restore
- Archive encryption (GPG) and upload to the cloud with **rclone**
- Full restore, or restore of a single file or folder
- A **"New PC"** wizard to set everything up again, step by step
- Comparison of two backups
- Scheduled **automatic** backups, with rotation and a warning if the latest backup is too old
- Interface in **English and French**, following the system language

| Restore | New PC | Automatic |
|---|---|---|
| ![](docs/screenshots/02-restaurer.png) | ![](docs/screenshots/04-nouveau-pc.png) | ![](docs/screenshots/06-automatique.png) |

## Installation

Download the `.deb` file from the [Releases](../../releases) page, then:

```bash
sudo apt install ./sauvegarde-bureau_*_all.deb
```

Requirements: Ubuntu with GNOME and libadwaita 1.5 or newer (Ubuntu 24.04 and later). Developed and tested on Ubuntu 26.04 LTS.

## Build the package yourself

```bash
git clone https://github.com/BreizhMann/sauvegarde-bureau.git
cd sauvegarde-bureau
make deb
```

The package is created in `dist/`. Building needs the `gettext` package (`sudo apt install gettext`), which compiles the translations.

## Translating

The texts live in `po/`. To add a language, copy `po/sauvegarde-bureau.pot` to `po/xx.po` (for example `po/de.po`), translate the texts, then run `make deb`. To refresh the template after changing the code, run `make pot`. A language without a translation is shown in English; French is the code's source language.

## Feedback

Found a bug or have an idea? Please open an [issue](../../issues).

## License

[MIT](LICENSE) © 2026 BreizhMann — <https://www.iactualite.info/>
