# AMS Lite Companion pour macOS

Ce compagnon laisse **Bambu Studio officiel** envoyer les impressions. Il suit
localement l’état de l’A1 mini et déduit la consommation estimée du fichier
tranché lorsque l’imprimante passe réellement de `RUNNING` à `FINISH`.

## Prérequis

- Mac et imprimante sur le même réseau local ;
- Python 3 (`python3 --version`) ;
- adresse IP, numéro de série et code d’accès LAN de l’imprimante ;
- un fichier **tranché** `.gcode.3mf` exporté depuis Bambu Studio.

Le compagnon n’utilise ni le cloud Bambu ni son module propriétaire. Aucune
dépendance Python n’est nécessaire.

## Démarrage

Dans Terminal :

```bash
cd ~/Downloads/AMS_Lite_Companion
chmod +x Lancer_AMS_Lite_Companion.command ams_companion.py
./Lancer_AMS_Lite_Companion.command
```

L’interface s’ouvre sur <http://127.0.0.1:8765>.

Pour démarrer simultanément Bambu Studio officiel et le compagnon :

```bash
chmod +x Lancer_BambuStudio_et_Companion.command
./Lancer_BambuStudio_et_Companion.command
```

Le lanceur combiné garde une fenêtre Terminal ouverte. Appuyez sur `Ctrl+C`
dans cette fenêtre pour arrêter proprement Companion ; aucun processus ne reste
ensuite en arrière-plan.

## Première configuration

1. Sur l’A1 mini, affichez le code d’accès LAN dans les paramètres réseau.
2. Saisissez l’IP, le numéro de série et ce code dans le compagnon.
3. Renseignez les poids actuels des bobines A1 à A4.
4. Dans Bambu Studio officiel, tranchez le plateau puis exportez le fichier
   tranché `.gcode.3mf`.
5. Importez ce fichier dans le compagnon, choisissez le plateau et associez
   chaque filament à son emplacement A1–A4.
6. Cliquez **Armer ce travail**, puis lancez normalement l’impression depuis
   Bambu Studio officiel.

La déduction est effectuée une fois seulement à la réception de `FINISH`.
Une impression annulée ou échouée n’est pas débitée.

## Données et dépannage

Les données sont enregistrées avec des droits privés dans :

```text
~/Library/Application Support/AMS Lite Companion/state.json
```

Journal lisible :

```text
~/Library/Application Support/AMS Lite Companion/companion.log
```

Pour vérifier un fichier sans lancer l’interface :

```bash
python3 ams_companion.py --parse /chemin/vers/plateau.gcode.3mf
```

La mesure est une estimation du trancheur, car l’AMS Lite ne publie pas de
poids réel. Le poids peut être corrigé manuellement à tout moment.
