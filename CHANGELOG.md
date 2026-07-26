# Changelog

## 1.4.4 — 2026-07-26

- Stabilisation MQTT : fermeture systématique des sockets, reconnexion propre et
  isolation d’un événement imprévu sans perdre toute la connexion.
- Les confirmations JavaScript de l’interface sont prises en charge nativement
  dans la fenêtre macOS ; l’état du moteur est vérifié avec son jeton réel.
- La vue A1–A4 est synchronisée et enregistrée dès le démarrage.
- Supprimer une bobine retire aussi ses lignes de l’historique général des
  impressions, y compris dans une impression multibobine.

## 1.4.3 — 2026-07-26

- Suppression définitive d’une bobine et de tout son historique, confirmée par
  un second clic fiable dans la fenêtre macOS.
- Import unique dans le catalogue de l’historique des impressions antérieur à
  la migration du 26 juillet, en conservant les dates d’origine.
- Les libellés RFID techniques (par exemple `A01-W2`) sont remplacés par des
  noms lisibles tels que « PLA blanc », sans écraser un nom personnalisé.
- Le lanceur conserve son jeton de session entre relances, signale une autre
  instance incompatible et écrit ses erreurs moteur dans `launcher.log`.

## 1.4.2 — 2026-07-26

- Les déplacements de bobines sont désormais atomiques et explicites : échange
  des deux voies, remplacement avec sortie hors AMS, ou retrait idempotent.
- Le clic sur une ligne du catalogue ouvre son historique ; les champs restent
  éditables sans déclencher la frise.
- Ajout de l’archivage sécurisé d’une bobine, qui libère sa voie tout en
  conservant l’audit, avec protection pendant une impression active.
- Nom descriptif proposé automatiquement à partir de la matière et de la
  couleur (par exemple « PLA bleu ») et date d’ajout rétrodatable dans la
  première entrée de l’historique.

## 1.4.1 — 2026-07-26

- Décompte multi-bobines rendu atomique et idempotent dans SQLite : un arrêt
  entre le débit et la sauvegarde ne peut plus débiter une même impression deux fois.
- Sauvegarde automatique de `state.json` corrompu avant récupération ; données,
  journal et répertoire applicatif protégés avec des droits réservés à l’utilisateur.
- API locale protégée par un jeton aléatoire de session, contrôle strict de
  l’hôte/origine et validation des types de requêtes.
- Limites ajoutées aux imports 3MF/ZIP pour refuser les archives anormalement
  volumineuses ou fortement compressées.
- Les fichiers Bambu Studio récupérés avec la correspondance enregistrée
  nécessitent désormais une confirmation explicite ; la correspondance reçue
  dans une commande Bambu récente reste armée automatiquement.
- Certificat MQTT local épinglé lors de la première connexion et refusé s’il change.
- Archive macOS sans métadonnées Finder, artefact CI corrigé et construction
  prête pour une signature Developer ID et une notarisation optionnelles.

## 1.4.0 — 2026-07-26

- Ajout du catalogue local SQLite de toutes les bobines.
- Les voies A1–A4 deviennent des emplacements temporaires : retirer puis remettre une bobine conserve son poids estimé.
- Migration automatique des quatre bobines existantes depuis `state.json` au premier démarrage.
- Le débit est lié à l’identité de la bobine présente à `RUNNING`, même après un échange ultérieur.
- Ajout des contrôles de création, placement et retrait depuis le tableau de bord complet.
- Synchronisation RFID automatique des bobines Bambu reconnues par l’AMS Lite,
  avec réassociation de la même fiche et de son poids lors d’un retour dans l’AMS.
- Catalogue déplacé dans une fenêtre macOS indépendante, sous forme de tableau éditable.

## 1.3.0 — 2026-07-19

- Panneau macOS natif lié à Bambu Studio officiel, sans modifier sa signature.
- Récupération automatique du paquet d’impression `.gcode.3mf` sous `Metadata`.
- Connexion MQTT locale stable sur le canal `report` des A1 mini et AMS Lite.
- Décompte monochrome et multicolore avec correspondance A1–A4 enregistrée.
- Déduction unique après la transition réelle `RUNNING → FINISH`.
- Aucune déduction après annulation, échec ou remplacement d’un ancien travail.
- Protection contre les sauvegardes de projet, réarmements et doubles déductions.
- Validation sur plusieurs impressions réelles, dont une impression bicolore.

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
