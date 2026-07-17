# Sécurité

## Signaler un problème

Ouvrez une issue sans joindre de données sensibles. Ne publiez jamais :

- le code d’accès LAN ;
- `state.json` ;
- un jeton Bambu ou MakerWorld ;
- une adresse IP publique ;
- des journaux non relus contenant des identifiants.

## Modèle de sécurité

- l’interface HTTP écoute exclusivement sur `127.0.0.1` ;
- la connexion à l’imprimante utilise MQTT sur TLS ;
- le certificat local de l’imprimante n’est pas vérifié, car les imprimantes
  utilisent couramment un certificat qui ne correspond pas à leur adresse IP ;
- le code LAN est stocké localement dans `state.json`, protégé par le mode
  de fichier `0600` ;
- Companion demande uniquement un état complet (`pushall`) et ne lance pas
  d’impression.

Utilisez Companion uniquement sur un réseau local de confiance.
