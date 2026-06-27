# Publier une version (bundle offline Windows)

Le bundle offline Windows est **buildé et publié automatiquement** par GitHub
Actions sur **push d'un tag `vX.Y.Z`**. Plus de build manuel ni d'upload à la main.

## Étapes

1. Mets à jour `CHANGELOG.md`.
2. Commit + push sur `main`.
3. Tag la version et pousse le tag :

   ```bash
   git tag v0.3.3
   git push origin v0.3.3
   ```

4. Le workflow **« Build Windows offline bundle »** se déclenche
   (`.github/workflows/build-windows-bundle.yml`) :
   - build le bundle sur un runner **Windows** (`scripts/release/build_bundle.ps1`),
   - vérifie qu'il est complet,
   - zippe le projet + `bundle/` en **`jarvis-offline-windows-v0.3.3.zip`**,
   - crée la **release `v0.3.3`** et y attache le zip.

   Durée : ~15-25 min (téléchargement Python + deps + modèles + livekit).

5. Vérifie la release sur GitHub : l'asset `jarvis-offline-windows-v0.3.3.zip`
   (~700 MB) doit être présent.

## Important

- **Le bundle est un SNAPSHOT FIGÉ du code au tag.** Pousser du code sur `main`
  **après** le tag ne met **pas** à jour le bundle des utilisateurs — il faut un
  **nouveau tag** pour reconstruire et republier.
- Le **build manuel reste possible** et inchangé :
  `scripts/release/build_bundle.ps1` (Windows) ou `build_bundle.sh` (Linux/macOS)
  → produit `bundle/`. Le workflow ne remplace pas le script, il l'automatise et
  ajoute le zip + la release.

## Tester le workflow sans polluer les vraies releases

Pousse un tag jetable, puis supprime la release + le tag après vérification :

```bash
git tag v0.0.0-test && git push origin v0.0.0-test
# ... vérifier que le workflow build et attache le zip ...
gh release delete v0.0.0-test --yes
git push origin :refs/tags/v0.0.0-test   # supprime le tag distant
git tag -d v0.0.0-test
```
