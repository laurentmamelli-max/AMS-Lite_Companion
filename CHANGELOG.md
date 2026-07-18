# Changelog

## 1.2.0 — 2026-07-18

- Ajout de la passerelle automatique avec Bambu Studio officiel.
- Récupération du `.gcode.3mf` temporaire créé lors de l’envoi de l’impression.
- Détection de la correspondance AMS A1–A4 depuis la commande locale lorsque disponible.
- Correspondance enregistrée configurable en solution de repli.
- Attente d’un fichier ZIP stable et priorité stricte au projet le plus récent.
- Conservation de l’import manuel comme solution de secours.

## 1.1.0 — 2026-07-18

- Ajout d’une véritable application dans la barre des menus macOS.
- Lancement automatique de Bambu Studio officiel.
- Affichage direct des niveaux A1–A4 dans le menu macOS.
- Ouverture du tableau, du journal et redémarrage du moteur depuis l’icône.
- Arrêt automatique de Companion lorsque Bambu Studio est fermé.
- Construction locale et signature ad hoc automatisées.

## 1.0.0 — 2026-07-17

- Première version publique.
- Suivi persistant des quatre emplacements AMS Lite.
- Extraction de la consommation depuis les fichiers `.gcode.3mf`.
- Surveillance MQTT locale de `RUNNING → FINISH`.
- Protection contre les doubles déductions et les valeurs négatives.
- Conservation du travail actif après redémarrage.
- Interface web locale et bouton d’arrêt propre.
- Lanceurs macOS séparé et combiné avec Bambu Studio officiel.
