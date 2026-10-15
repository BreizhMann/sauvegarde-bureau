VERSION := $(shell sed -n "s/^Version: //p" pkg/DEBIAN/control)
DEB := dist/sauvegarde-bureau_$(VERSION)_all.deb

.PHONY: deb clean

deb:
	mkdir -p dist
	find pkg -name __pycache__ -exec rm -rf {} +
	dpkg-deb --root-owner-group -b pkg $(DEB)
	@echo "Paquet créé : $(DEB)"

clean:
	rm -rf dist
