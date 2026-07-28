# Kit de lancement

Les visuels de `assets/` utilisent des données fictives. Avant de publier,
remplacez le lien de release par la dernière version et adaptez le ton à la
communauté. Ne publiez jamais de code LAN, de `state.json` ou de journal non
relus.

## Message court en français

> Je publie **AMS Lite Companion**, une app macOS open source pour suivre le
> filament restant sur une Bambu A1 mini avec AMS Lite.
>
> Elle laisse Bambu Studio gérer les impressions, récupère localement le fichier
> tranché, suit l’état de l’imprimante sur le réseau local et décompte les
> bobines à la fin. Pas de compte, pas de cloud : les données restent sur le
> Mac.
>
> Le poids est une estimation du trancheur et l’app est encore jeune : je
> cherche des testeurs A1 mini + AMS Lite, surtout pour le multicolore et les
> différents firmwares.
>
> Téléchargement : https://github.com/laurentmamelli-max/AMS-Lite_Companion/releases/latest
>
> Retours et problèmes : https://github.com/laurentmamelli-max/AMS-Lite_Companion/issues

## Post communautaire détaillé en français

### Un compagnon local pour l’AMS Lite sur macOS

J’ai créé **AMS Lite Companion**, un petit outil open source pour les personnes
qui utilisent une Bambu A1 mini avec AMS Lite et souhaitent garder une trace
simple du filament restant.

Companion ne remplace pas Bambu Studio : il l’accompagne. À l’envoi d’un
plateau, il récupère le `.gcode.3mf` local, observe le passage réel de
l’impression à `FINISH`, puis met à jour les bobines utilisées. Il gère le
catalogue, les positions A1–A4, les échanges de bobines, l’historique et les
impressions multicolores.

Les données restent exclusivement sur le Mac. Le poids affiché est une
estimation du trancheur, donc une pesée occasionnelle reste utile.

Je cherche des testeurs sur A1 mini + AMS Lite, Mac Intel ou Apple Silicon. Les
retours les plus utiles indiquent macOS, Bambu Studio, firmware, type
d’impression et résultat observé.

Projet et téléchargement :
https://github.com/laurentmamelli-max/AMS-Lite_Companion/releases/latest

## English post for Reddit, Discord, or maker communities

### Open-source local filament tracker for Bambu A1 mini + AMS Lite on macOS

I built **AMS Lite Companion**, a small macOS companion app for tracking
estimated filament remaining on a Bambu A1 mini with AMS Lite.

It does not replace Bambu Studio and never starts prints. It reads the local
`.gcode.3mf` created by Bambu Studio, watches the printer’s local status, and
updates the spools only after the print actually finishes. It also includes a
spool catalogue, A1–A4 assignments, history, low-stock alerts, and
multi-colour print tracking.

Everything stays on the Mac: no cloud account and no telemetry. Spool weights
remain slicer estimates, so occasional weighing is still recommended.

I’m looking for A1 mini + AMS Lite testers on both Intel and Apple Silicon
Macs, especially with different firmware versions and multi-colour prints.

Download and source:
https://github.com/laurentmamelli-max/AMS-Lite_Companion/releases/latest

Bug reports / feedback:
https://github.com/laurentmamelli-max/AMS-Lite_Companion/issues

## Publications très courtes

**Français**

> AMS Lite Companion est disponible : suivi local des bobines A1–A4 pour A1
> mini + AMS Lite sur macOS, sans cloud. Je cherche des testeurs :
> https://github.com/laurentmamelli-max/AMS-Lite_Companion/releases/latest

**English**

> AMS Lite Companion is available: local spool tracking for Bambu A1 mini +
> AMS Lite on macOS, no cloud required. Looking for testers:
> https://github.com/laurentmamelli-max/AMS-Lite_Companion/releases/latest

## Ordre de publication conseillé

1. Ajoutez les sujets GitHub : `bambu-lab`, `bambu-studio`, `ams-lite`,
   `3d-printing`, `filament-management`, `macos`, `open-source`.
2. Utilisez `assets/dashboard-demo.jpg` comme aperçu social du dépôt.
3. Publiez le message détaillé dans une communauté Bambu ou d’impression 3D.
4. Publiez ensuite la version courte dans un ou deux groupes ciblés, sans
   copier-coller massif.
5. Répondez aux premiers retours, puis regroupez les problèmes dans GitHub.
