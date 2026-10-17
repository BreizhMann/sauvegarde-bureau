VERSION := $(shell sed -n "s/^Version: //p" pkg/DEBIAN/control)
DEB := dist/sauvegarde-bureau_$(VERSION)_all.deb
PKGDIR := pkg/usr/lib/python3/dist-packages/sauvegarde_bureau
POT := po/sauvegarde-bureau.pot
LANGS := $(patsubst po/%.po,%,$(wildcard po/*.po))
MOS := $(foreach l,$(LANGS),pkg/usr/share/locale/$(l)/LC_MESSAGES/sauvegarde-bureau.mo)

.PHONY: deb mo pot clean

# Construit le paquet (il faut le paquet « gettext » pour compiler les traductions).
deb: mo
	mkdir -p dist
	find pkg -name __pycache__ -exec rm -rf {} +
	dpkg-deb --root-owner-group -b pkg $(DEB)
	@echo "Paquet créé : $(DEB)"

# Compile les traductions (po/*.po -> .mo, installés dans pkg/usr/share/locale).
mo: $(MOS)

pkg/usr/share/locale/%/LC_MESSAGES/sauvegarde-bureau.mo: po/%.po
	mkdir -p $(dir $@)
	msgfmt --check -o $@ $<

# Met à jour le modèle de traduction à partir du code source.
pot:
	cd pkg/usr/lib/python3/dist-packages && xgettext --language=Python --from-code=UTF-8 \
		--keyword=_ --keyword=ngettext:1,2 --no-location --sort-output \
		--package-name=sauvegarde-bureau -o ../../../../../$(POT) \
		$(patsubst $(dir $(PKGDIR))%,%,$(wildcard $(PKGDIR)/*.py))

clean:
	rm -rf dist pkg/usr/share/locale
