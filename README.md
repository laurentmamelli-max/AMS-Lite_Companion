# AMS Lite Companion

Compteur local de filament pour **Bambu Lab A1 mini + AMS Lite**, conçu pour
fonctionner à côté de Bambu Studio officiel sur macOS.

L’AMS Lite ne fournit pas le poids réel restant pour les bobines génériques.
Companion extrait donc la consommation estimée `used_g` d’un fichier tranché
`.gcode.3mf`, surveille localement l’état de l’imprimante et débite la bobine
uniquement après une transition `RUNNING → FINISH`.

## Fonctionnalités

- suivi indépendant des emplacements A1 à A4 ;
- extraction multifilament depuis `Metadata/slice_info.config` ;
- secours par lecture des en-têtes G-code ;
- connexion MQTT TLS directe à l’imprimante sur le réseau local ;
- aucune dépendance Python externe ;
- persistance des niveaux, du travail actif et de l’historique ;
- protection contre les doubles déductions ;
- aucune déduction après annulation ou échec ;
- bouton d’arrêt propre dans l’interface ;
- lancement simultané avec Bambu Studio officiel.

## Prérequis

- macOS avec Python 3 ;
- A1 mini et Mac sur le même réseau ;
- IP, numéro de série et code d’accès LAN de l’imprimante ;
- fichier **tranché** `.gcode.3mf` exporté depuis Bambu Studio.

Sur certains firmwares récents, l’accès MQTT local peut nécessiter l’activation
du mode développeur dans les paramètres réseau de l’imprimante.

## Installation

Téléchargez l’archive de la dernière release, décompressez-la, puis :

```bash
chmod +x *.command ams_companion.py
./Lancer_BambuStudio_et_Companion.command
```

L’interface s’ouvre sur <http://127.0.0.1:8765>.

## Utilisation

1. Configurez l’IP, le numéro de série et le code LAN.
2. Enregistrez le poids actuel des bobines A1–A4.
3. Tranchez le plateau dans Bambu Studio officiel.
4. Exportez le plateau au format `.gcode.3mf`.
5. Importez ce fichier dans Companion.
6. Associez chaque filament à son emplacement réel.
7. Cliquez sur **Armer ce travail**.
8. Lancez l’impression avec Bambu Studio officiel.
9. Après `FINISH`, vérifiez la déduction dans l’historique.

Le bouton **Arrêter Companion** ferme proprement le service sans laisser de
processus en arrière-plan. Bambu Studio reste ouvert.

## Données locales et confidentialité

Companion écoute uniquement sur `127.0.0.1`. Son état est conservé dans :

```text
~/Library/Application Support/AMS Lite Companion/state.json
```

Le fichier est créé avec des droits `0600`, mais contient le code d’accès LAN
en clair afin de permettre la reconnexion. Ne le publiez jamais et ne joignez
pas son contenu à une issue GitHub.

Journal de diagnostic :

```text
~/Library/Application Support/AMS Lite Companion/companion.log
```

## Tests

```bash
python3 -m unittest -v test_companion.py
```

Les tests couvrent l’extraction multifilament, la fin réussie, l’échec, la
limite à zéro, le redémarrage, l’idempotence et l’arrêt depuis l’interface.

## Limites

- Le poids est une estimation du trancheur, pas une mesure physique.
- Le fichier tranché doit être importé et armé avant chaque travail.
- Les impressions partielles annulées ne sont pas débitées automatiquement.
- Une pesée occasionnelle reste recommandée pour corriger la dérive.

## Avertissement

Projet communautaire non officiel, sans affiliation avec Bambu Lab. Bambu
Studio, Bambu Lab, A1 mini et AMS Lite sont des marques de leurs propriétaires.

Licence : MIT.
