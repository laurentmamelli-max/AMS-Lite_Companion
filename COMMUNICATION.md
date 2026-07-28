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
> La bibliothèque conserve chaque bobine indépendamment des voies A1–A4 : nom,
> matière, couleur, poids, emplacement, historique et alertes. On choisit une
> bobine dans la liste puis on l’affecte à la voie AMS utilisée.
>
> Si vous avez une A1 mini + AMS Lite, ce serait sympa de venir jeter un œil et
> de me faire un retour sur votre utilisation, surtout en multicolore ou avec
> une version de firmware différente.
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
l’impression à `FINISH`, puis met à jour les bobines utilisées. Sa bibliothèque
garde la fiche complète de chaque bobine, indépendamment de sa position : nom,
matière, couleur, marque, poids restant, rangement physique, seuil d’alerte et
historique. Depuis cette liste, on place une bobine sur A1, A2, A3 ou A4 ; une
voie déjà occupée est libérée proprement et l’ancienne bobine reste dans la
bibliothèque. Les filtres, la recherche et les actions par lots restent utiles
même avec un stock important.

Les données restent exclusivement sur le Mac. Le poids affiché est une
estimation du trancheur, donc une pesée occasionnelle reste utile.

Si vous utilisez une A1 mini + AMS Lite, venez jeter un œil et dites-moi ce qui
vous plaît ou ce qui mériterait d’être amélioré. Un retour avec macOS, Bambu
Studio, firmware, type d’impression et résultat observé aide énormément.

Projet et téléchargement :
https://github.com/laurentmamelli-max/AMS-Lite_Companion/releases/latest

## English post for Reddit, Discord, or maker communities

### Open-source local filament tracker for Bambu A1 mini + AMS Lite on macOS

I built **AMS Lite Companion**, a small macOS companion app for tracking
estimated filament remaining on a Bambu A1 mini with AMS Lite.

It does not replace Bambu Studio and never starts prints. It reads the local
`.gcode.3mf` created by Bambu Studio, watches the printer’s local status, and
updates the spools only after the print actually finishes. Its spool library
keeps a complete card for every roll (material, colour, remaining weight, shelf
location, alerts, and history) independently from the A1–A4 slots. You simply
pick a roll from the library and assign it to an AMS slot; moving it later
preserves its record and history. Search, filters, bulk actions, low-stock
alerts, and multi-colour print tracking are included too.

Everything stays on the Mac: no cloud account and no telemetry. Spool weights
remain slicer estimates, so occasional weighing is still recommended.

If you use an A1 mini + AMS Lite, I’d love for you to take a look and share
your feedback, especially for multi-colour prints or different firmware
versions.

Download and source:
https://github.com/laurentmamelli-max/AMS-Lite_Companion/releases/latest

Bug reports / feedback:
https://github.com/laurentmamelli-max/AMS-Lite_Companion/issues

## Publications très courtes

**Français**

> AMS Lite Companion est disponible : une bibliothèque locale de bobines et le
> suivi des voies A1–A4 pour A1 mini + AMS Lite sur macOS, sans cloud. Venez
> jeter un œil et me dire ce que vous en pensez :
> https://github.com/laurentmamelli-max/AMS-Lite_Companion/releases/latest

**English**

> AMS Lite Companion is available: a local spool library and A1–A4 tracking for
> Bambu A1 mini + AMS Lite on macOS, no cloud required. Feedback welcome:
> https://github.com/laurentmamelli-max/AMS-Lite_Companion/releases/latest

## Ordre de publication conseillé

1. Ajoutez les sujets GitHub : `bambu-lab`, `bambu-studio`, `ams-lite`,
   `3d-printing`, `filament-management`, `macos`, `open-source`.
2. Utilisez `assets/dashboard-demo.jpg` comme aperçu social du dépôt.
3. Publiez le message détaillé dans une communauté Bambu ou d’impression 3D.
4. Publiez ensuite la version courte dans un ou deux groupes ciblés, sans
   copier-coller massif.
5. Répondez aux premiers retours, puis regroupez les problèmes dans GitHub.
