# Sécurité

## Signaler un problème

Ouvrez une issue sans joindre de données sensibles. Ne publiez jamais :

- le code d’accès LAN ;
- `state.json` ;
- un jeton Bambu ou MakerWorld ;
- une adresse IP publique ;
- des journaux non relus contenant des identifiants.

## Modèle de sécurité

- l’interface HTTP écoute exclusivement sur `127.0.0.1` ; ses API exigent un
  jeton aléatoire par session, un pair TCP loopback, un hôte local valide et
  une origine locale ; les réponses désactivent aussi le reniflage MIME, les
  référents et l’encadrement par une autre page ;
- la connexion à l’imprimante utilise MQTT sur TLS ;
- le certificat local de l’imprimante est épinglé à sa première connexion. Les
  imprimantes utilisent souvent un certificat auto-signé, mais un changement
  ultérieur est refusé et doit être vérifié sur le réseau local ;
- le code LAN est stocké localement dans `state.json`, protégé par le mode
  de fichier `0600` ;
- Companion demande uniquement un état complet (`pushall`) et ne lance pas
  d’impression.

Utilisez Companion uniquement sur un réseau local de confiance, surtout lors
de la première connexion qui établit l’empreinte du certificat.

## Diagnostic local

Les erreurs de connexion restent résumées dans `companion.log` pour ne pas
faire grossir inutilement le journal lors d’une imprimante éteinte. Pour une
analyse ponctuelle, lancez le moteur avec `AMS_COMPANION_DEBUG=1` : les erreurs
MQTT et API incluront alors leur traceback. Le journal et l’empreinte SHA-256
du premier certificat MQTT sont accessibles uniquement à l’utilisateur macOS
du compte courant.
