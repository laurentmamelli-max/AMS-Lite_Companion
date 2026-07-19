# Changelog

## 1.3.0-beta.3 — 2026-07-19

- Analyse du journal réel d’une impression complète avec la bêta 2.
- Surveillance limitée aux paquets d’impression situés dans `Metadata`.
- Exclusion des sauvegardes de projet `.3mf` créées à la racine par Bambu Studio.
- Consommation définitive de l’import automatique après `FINISH`, annulation ou échec.
- Suppression au démarrage des anciens armements automatiques devenus périmés.
- Protection testée contre le réarmement et une future déduction parasite.

## 1.3.0-beta.2 — 2026-07-19

- Correction des déconnexions MQTT répétées sur A1 mini et AMS Lite.
- Abonnement limité au canal `report` accepté par le firmware ; le canal `request` reste réservé à l’envoi de `pushall`.
- Détection d’un nouvel identifiant de tâche après une coupure réseau.
- Abandon de l’ancien travail bloqué sans aucune déduction avant d’armer le nouveau.
- Correspondance A1–A4 enregistrée explicitement utilisée par la passerelle automatique.

## 1.3.0-beta.1 — 2026-07-19

- Ajout d’un panneau macOS natif intégré à côté de Bambu Studio officiel.
- Affichage du tableau Companion dans WebKit, sans ouverture obligatoire du navigateur.
- Suivi automatique de la position de la fenêtre Bambu Studio, désactivable depuis le menu.
- Accès séparé au tableau complet dans le navigateur pour les fonctions de secours.
- Navigation du panneau limitée au serveur local Companion.
- Conservation de la signature et de toutes les fonctions d’impression de Bambu Studio officiel.

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
